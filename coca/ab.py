#!/usr/bin/env python3
"""COCO (Claude Orchestrating Cheaper Agents): Claude's dispatcher for cheaper worker agents.

Claude stays the planner/reviewer. Workers do the token-heavy work:
  codex     (ChatGPT plan)          research, code, review
  agy       (Antigravity, Google)   Gemini 3.8 Flash + Opus 4.6 Thinking on Google's quota
  opencode  (free cloud models)     browse, simple, bigread
  ollama    (local GPU)              private/offline + simple jobs

Communication:
  ~/.agentbus/bus.jsonl     timeline of every task start/attempt/fail/finish, with real token usage
  ~/.agentbus/BOARD.md      shared blackboard; every worker sees it, findings are appended
  ~/.agentbus/tasks/<id>/   prompt.md, live.log (streamed), out.md, raw.log, meta.json
  ~/.agentbus/models.json   which model each agent uses per role (`ab.py models`)

Claude only reads the short SUMMARY printed to stdout; details stay on disk.
"""
import argparse, concurrent.futures as cf, datetime as dt, json, os, re, shutil
import subprocess, sys, threading, time, urllib.request, uuid
from pathlib import Path

HOME = Path(os.environ.get("AGENTBUS_HOME", Path.home() / ".agentbus"))
TASKS, BUS, BOARD, CONF = HOME / "tasks", HOME / "bus.jsonl", HOME / "BOARD.md", HOME / "models.json"
LOCK = threading.Lock()
NO_WINDOW, NEW_GROUP, DETACHED = 0x08000000, 0x00000200, 0x00000008

# Best agent first, then fallbacks (used when one fails / hits quota).
ROUTES = {
    "research": ["codex", "agy", "opencode"],     # multi-source web research
    "browse":   ["opencode", "codex", "agy"],     # fetch specific URLs, extract facts
    "code":     ["codex", "agy", "opencode"],     # edits files in -C dir
    "review":   ["codex", "agy", "opencode"],     # read-only review (codex was more precise than agy/Opus 4.6 in tests)
    "bigread":  ["agy", "opencode", "codex"],     # summarise big files/logs/repos (huge context)
    "simple":   ["ollama", "opencode", "agy"],    # rewrite, classify, extract, format
    "local":    ["ollama"],                       # private: never leaves this machine
}
# Model per agent per role. "default" applies to roles not listed. Edit with `ab.py models set`.
DEFAULT_MODELS = {
    "codex":    {"default": "", "effort": "medium", "effort_code": "high", "effort_review": "high"},
    "agy":      {"default": "gemini-3.8-flash-medium", "research": "gemini-3.8-flash-high",
                 "bigread": "gemini-3.8-flash-high", "simple": "gemini-3.8-flash-low",
                 "review": "claude-opus-4-6-thinking", "code": "claude-opus-4-6-thinking"},
    "opencode": {"default": "opencode/deepseek-v4.1-flash-free"},
    "ollama":   {"default": "auto"},   # auto = best installed (prefers the OmniCoder/Qwen coder, then gemma)
}
OLLAMA_PREF = ["omnicoder", "qwen", "deepseek", "llama", "gemma", "mistral"]
ROLE_HINT = {
    "research": "You are a web research agent. Use your web search and URL-reading tools (never terminal commands). Cite URLs. Prefer primary sources and recent dates.",
    "browse":   "You are a browsing agent. Read the given pages with your URL tools (never terminal commands) and extract exactly what is asked. Quote and cite URLs.",
    "code":     "You are a coding agent working in the current directory. Make the change, run relevant tests/linters if cheap, and report files changed.",
    "review":   "You are a code reviewer. Do NOT modify any files. Report concrete bugs with file:line, most severe first.",
    "bigread":  "You are a reading agent. Read the referenced files/dirs and summarise precisely what is asked. Do not modify files.",
    "simple":   "Do the task directly and concisely.",
    "local":    "Do the task directly and concisely.",
}
WRITE_ROLES = {"code"}
QUOTA_RE = re.compile(r"quota|rate.?limit|\b429\b|resource.?exhausted|usage limit|too many requests|unauthori[sz]ed|login required", re.I)
DASH = os.environ.get("AB_DASH", "1") != "0"    # auto-open + keep alive the terminal dashboard
TABS = os.environ.get("AB_TABS", "0") == "1"    # also open a raw output tab per worker
DASH_LOCK, OLLAMA_LOCK = threading.Lock(), threading.Lock()
TAB_COLOR = {"codex": "#19c37d", "opencode": "#ffb020", "agy": "#4c8dff", "ollama": "#b06cff"}
CTX = threading.local()


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def emit(ev):
    HOME.mkdir(parents=True, exist_ok=True)
    with LOCK, open(BUS, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": now(), **ev}, ensure_ascii=False) + "\n")


def which(name):
    return shutil.which(name + ".cmd") or shutil.which(name + ".exe") or shutil.which(name)


# ───────────────────────── models config ─────────────────────────
def load_conf():
    conf = json.loads(json.dumps(DEFAULT_MODELS))
    if CONF.exists():
        try:
            for k, v in json.loads(CONF.read_text(encoding="utf-8")).items():
                conf.setdefault(k, {}).update(v)
        except ValueError:
            pass
    return conf


def model_for(agent, role, override=None):
    if override: return override
    c = load_conf().get(agent, {})
    m = c.get(role, c.get("default", ""))
    if agent == "ollama" and m in ("", "auto"):
        tags = ollama_tags() or []
        for pref in OLLAMA_PREF:
            hit = [t for t in tags if pref in t.lower()]
            if hit: return hit[0]
        return tags[0] if tags else ""
    return m


# ───────────────────────── ollama autostart ─────────────────────────
def user_env(name):
    if os.environ.get(name): return os.environ[name]
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return None
    return None


def ollama_tags(timeout=1.5):
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=timeout) as r:
            return [m["name"] for m in json.loads(r.read())["models"]]
    except Exception:
        return None


def ensure_ollama(quiet=True):
    """Start Ollama in the background if it's down, or restart it if it can't see the model folder."""
    with OLLAMA_LOCK:
        tags = ollama_tags()
        mdir = user_env("OLLAMA_MODELS")
        on_disk = bool(mdir) and (Path(mdir) / "manifests").exists() and any((Path(mdir) / "manifests").rglob("*"))
        if tags is not None and (tags or not on_disk):
            return tags
        exe = which("ollama")
        if not exe:
            return None
        if tags is not None:  # running but blind to the model folder -> restart with the right env
            if os.name == "nt":
                for im in ("ollama app.exe", "ollama.exe"):
                    subprocess.run(["taskkill", "/F", "/IM", im], capture_output=True)
            else:
                subprocess.run(["pkill", "-f", "ollama serve"], capture_output=True)
            time.sleep(1.5)
        env = os.environ.copy()
        if mdir: env["OLLAMA_MODELS"] = mdir
        log = open(HOME / "ollama.log", "a")
        subprocess.Popen([exe, "serve"], env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         creationflags=(NO_WINDOW | NEW_GROUP) if os.name == "nt" else 0, close_fds=True)
        for _ in range(40):
            time.sleep(0.5)
            tags = ollama_tags()
            if tags is not None: break
        emit({"ev": "service", "name": "ollama", "state": "started", "models": tags or []})
        return tags


# ───────────────────────── dashboard ─────────────────────────
def open_tab(title, color, command):
    """Windows Terminal tab. On macOS/Linux run `python ab.py dash` in any terminal instead."""
    wt = shutil.which("wt.exe") or shutil.which("wt")
    if os.name != "nt" or not wt: return
    shell = shutil.which("pwsh.exe") or "powershell.exe"
    try:
        env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "COCO_WORKER")}
        subprocess.Popen([wt, "-w", "coco", "new-tab", "--title", title, "--tabColor", color,
                          "--suppressApplicationTitle", shell, "-NoLogo", "-NoExit", "-Command", command],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    except Exception:
        pass


def ensure_dashboard():
    hb = HOME / "dash.heartbeat"
    with DASH_LOCK:
        if hb.exists() and time.time() - hb.stat().st_mtime < 5:
            return False
        hb.write_text(str(time.time()))
        open_tab("COCO", "#ff8a4c", f"python '{Path(__file__).resolve()}' dash")
        time.sleep(1.5)
        return True


class Watchdog:
    """While any task runs, reopen the dashboard if it gets closed."""
    active = 0
    lock = threading.Lock()

    def __enter__(self):
        with Watchdog.lock:
            Watchdog.active += 1
            if Watchdog.active == 1 and DASH:
                threading.Thread(target=self._loop, daemon=True).start()
        return self

    def __exit__(self, *a):
        with Watchdog.lock:
            Watchdog.active -= 1

    @staticmethod
    def _loop():
        time.sleep(6)
        while Watchdog.active > 0:
            if ensure_dashboard():
                emit({"ev": "service", "name": "dashboard", "state": "reopened"})
            time.sleep(3)


# ───────────────────────── guard: dashboard lives as long as a Claude session ─────────────────────────
GUARD_PID, GUARD_OFF = HOME / "guard.pid", HOME / "guard.off"


def host_sessions():
    """Interactive Claude Code / Codex CLI sessions (not the desktop apps, not COCO's own workers)."""
    try:
        import psutil
    except ImportError:
        return 1
    n = 0
    for p in psutil.process_iter(["name", "exe", "cmdline"]):
        try:
            name = (p.info["name"] or "").lower()
            exe = (p.info["exe"] or "").lower()
            cmd = " ".join(p.info["cmdline"] or []).lower()
            if name in ("claude.exe", "claude") and "windowsapps" not in exe and "--type=" not in cmd:
                n += 1
            elif name in ("codex.exe", "codex") and not any(k in cmd for k in (" exec", "app-server", "mcp")):
                n += 1
        except Exception:
            continue
    return n


def guard_alive():
    try:
        import psutil
        pid = int(GUARD_PID.read_text())
        return psutil.pid_exists(pid) and "python" in psutil.Process(pid).name().lower()
    except Exception:
        return False


def start_guard():
    if guard_alive() or not DASH: return False
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    exe = str(pyw if os.name == "nt" and pyw.exists() else py)
    kw = {"creationflags": NO_WINDOW | DETACHED | NEW_GROUP} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen([exe, str(Path(__file__).resolve()), "guard", "run"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    return True


def guard_loop():
    """Keep the dashboard open while any Claude Code session runs; exit 30 s after the last one ends."""
    GUARD_PID.write_text(str(os.getpid()))
    idle_since = None
    try:
        while True:
            if host_sessions() > 0:
                idle_since = None
                if not GUARD_OFF.exists() and ensure_dashboard():
                    emit({"ev": "service", "name": "dashboard", "state": "reopened"})
            else:
                idle_since = idle_since or time.time()
                if time.time() - idle_since > 30: break
            time.sleep(4)
    finally:
        try:
            if GUARD_PID.read_text() == str(os.getpid()): GUARD_PID.unlink()
        except Exception:
            pass


def cmd_statusline():
    """Claude Code status line: save the session JSON (incl. real rate limits) for the dashboard, print a short line."""
    raw = sys.stdin.read()
    try:
        d = json.loads(raw)
        (HOME / "claude_status.json").write_text(raw, encoding="utf-8")
    except Exception:
        return print("COCO")
    parts = [f"COCO · {d.get('model', {}).get('display_name', 'Claude')}"]
    for key, lab in (("five_hour", "5h"), ("seven_day", "wk")):
        w = (d.get("rate_limits") or {}).get(key) or {}
        if w.get("used_percentage") is not None: parts.append(f"{lab} {100 - float(w['used_percentage']):.0f}% left")
    print(" · ".join(parts))


def cmd_guard(a):
    if a.action == "run": return guard_loop()
    if a.action == "off":
        GUARD_OFF.write_text("1"); return print("guard: paused (dashboard may be closed). `coco guard on` to resume.")
    if a.action == "on":
        GUARD_OFF.unlink(missing_ok=True); start_guard(); return print("guard: on")
    print(f"guard: {'running' if guard_alive() else 'stopped'}{' (paused)' if GUARD_OFF.exists() else ''}, "
          f"claude/codex sessions: {host_sessions()}")


# ───────────────────────── prompts / board ─────────────────────────
def wrap_prompt(role, task, tid):
    HOME.mkdir(parents=True, exist_ok=True)
    BOARD.touch(exist_ok=True)
    board = "\n".join(BOARD.read_text(encoding="utf-8").splitlines()[-40:]) or "(empty)"
    return f"""You are a COCO worker agent. Do this task yourself; never call coco or ab.py to delegate it.
{ROLE_HINT[role]}

SHARED BLACKBOARD (recent findings from other agents on this team; use if relevant):
{board}

OUTPUT FORMAT (strict, do not write to the blackboard file yourself):
## SUMMARY
(at most 10 short bullet lines: the answer, key facts, files changed, URLs)
## BOARD
(0-5 one-line durable findings worth sharing with other agents)
## DETAILS
(everything else: evidence, code, longer notes)

TASK:
{task}
"""


def post_board(tid, text):
    m = re.search(r"##\s*BOARD\s*(.*?)(?:\n##\s|\Z)", text, re.S | re.I)
    lines = [l.strip().lstrip("-* ").strip() for l in (m.group(1) if m else "").splitlines()]
    lines = [l for l in lines if l and not l.startswith("(")][:5]
    if lines:
        with LOCK, open(BOARD, "a", encoding="utf-8") as f:
            f.write("".join(f"- [{tid}] {l}\n" for l in lines))


# ───────────────────────── process runner ─────────────────────────
def live_log(d):
    return d / "live.log"


def short(v, n=140):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "…"


def tool_line(name, params):
    """Render a tool call as a readable live-log line the dashboard can classify."""
    n = (name or "tool").lower()
    p = params or {}
    get = lambda *ks: next((p[k] for k in ks if isinstance(p, dict) and p.get(k)), None)
    url, q = get("url", "Url", "URL"), get("query", "Query", "q")
    path = get("path", "Path", "filePath", "file_path", "AbsolutePath", "TargetFile")
    cmd = get("command", "CommandLine", "cmd")
    if "search" in n and q: return f"web_search {short(q)}"
    if url: return f"fetch {short(url)}"
    if cmd: return f"$ {short(cmd)}"
    if any(w in n for w in ("write", "edit", "patch", "replace", "create")): return f"edit {short(path or p)}"
    if any(w in n for w in ("read", "view", "list", "glob", "grep", "find")): return f"read {short(path or q or p)}"
    return f"{n} {short(p)}"


def run_cli(cmd, prompt, cwd, timeout, stdin=True, transform=None, model=""):
    """Run a worker CLI, stream readable lines to live.log, collect final text + token usage."""
    d, agent = CTX.d, CTX.agent
    lf = open(live_log(d), "a", encoding="utf-8", buffering=1)
    lf.write(f"\n━━━━ {agent} starting in {cwd} ━━━━\n")
    if model: lf.write(f"model: {model}\n")
    st = {"final": [], "usage": CTX.usage, "buf": ""}
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace", cwd=cwd, env={**os.environ, "COCO_WORKER": "1"})
    bufs = {"out": [], "err": []}

    def pump(stream, key):
        prev = ""
        for line in stream:
            bufs[key].append(line)
            if key == "out" and transform:
                for disp in transform(line, st) or []:
                    lf.write(disp.rstrip("\n") + "\n")
            elif key == "err" and re.match(r"^[IWE]\d{4} ", line):
                continue  # glog noise (agy)
            else:
                lf.write(line)
                if prev.strip() == "tokens used":       # codex prints the total on the next line
                    try: st["usage"]["total"] = st["usage"].get("total", 0) + int(line.strip().replace(",", ""))
                    except ValueError: pass
            prev = line
    ts = [threading.Thread(target=pump, args=(p.stdout, "out"), daemon=True),
          threading.Thread(target=pump, args=(p.stderr, "err"), daemon=True)]
    for t in ts: t.start()
    if stdin:
        try: p.stdin.write(prompt); p.stdin.close()
        except OSError: pass
    try:
        rc = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt": subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        else: p.kill()
        rc = 124
        bufs["err"].append(f"\nTIMEOUT after {timeout}s\n")
    for t in ts: t.join(timeout=5)
    if st["buf"].strip(): lf.write(st["buf"].strip() + "\n")
    lf.write(f"━━━━ {agent} exited rc={rc} ━━━━\n")
    lf.close()
    out = "".join(st["final"]) if transform else "".join(bufs["out"])
    return rc, out, "".join(bufs["err"])


def add_usage(u, inp=0, out=0, cache_read=0, cache_write=0, think=0):
    u["in"] = u.get("in", 0) + int(inp or 0)
    u["out"] = u.get("out", 0) + int(out or 0) + int(think or 0)
    u["cache_read"] = u.get("cache_read", 0) + int(cache_read or 0)
    u["cache_write"] = u.get("cache_write", 0) + int(cache_write or 0)


# ───────────────────────── agents ─────────────────────────
def agent_codex(prompt, role, cwd, timeout, d, model):
    exe = which("codex")
    if not exe: raise FileNotFoundError("codex")
    c = load_conf()["codex"]
    last = d / "codex_last.txt"
    sandbox = "workspace-write" if role in WRITE_ROLES else "read-only"
    cmd = [exe, "exec", "--skip-git-repo-check", "-s", sandbox, "-o", str(last),
           "-c", f'model_reasoning_effort="{c.get("effort_" + role, c.get("effort", "medium"))}"']
    if role in ("research", "browse"): cmd += ["-c", "tools.web_search=true"]
    if model: cmd += ["-m", model]
    cmd += ["-"]
    rc, out, err = run_cli(cmd, prompt, cwd, timeout, model=model or "codex default")
    if last.exists() and last.read_text(encoding="utf-8").strip():
        out = last.read_text(encoding="utf-8")
    u = CTX.usage
    if u.get("total"):  # codex reports one total; split off the answer as output tokens
        o = max(1, len(out) // 4)
        add_usage(u, inp=max(0, u.pop("total") - o), out=o)
    return rc, out, err


def agy_transform(line, st):
    try: e = json.loads(line)
    except ValueError: return [line] if line.strip() else []
    disp = []
    if e.get("event") == "init":
        disp.append(f"model: {e.get('init', {}).get('model', '?')}")
    s = e.get("step_update") or {}
    if s:
        if s.get("step_type") == "tool" and s.get("state") == "ACTIVE":
            ti = s.get("tool_info") or {}
            disp.append(tool_line(s.get("tool_name") or ti.get("name"), ti.get("parameters")))
        if s.get("text_delta"):
            st["buf"] += s["text_delta"]
            while "\n" in st["buf"]:
                ln, st["buf"] = st["buf"].split("\n", 1)
                if ln.strip(): disp.append(ln)
        if s.get("state") == "DONE" and s.get("usage"):
            u = s["usage"]
            add_usage(st["usage"], u.get("input_tokens"), u.get("output_tokens"), u.get("cache_read_tokens"),
                      think=u.get("thinking_tokens"))
        if s.get("state") == "DONE" and s.get("step_type") == "tool" and s.get("error"):
            disp.append(f"error {short(s.get('error'))}")
    r = e.get("result")
    if r:
        st["final"] = [r.get("response", "")]
        if r.get("status") != "SUCCESS": disp.append(f"error status={r.get('status')}")
        for a in r.get("denied_actions") or []: disp.append(f"denied {a.get('display_name', a)}")
    return disp


def agent_agy(prompt, role, cwd, timeout, d, model):
    exe = shutil.which("agy") or shutil.which("agy.exe")
    if not exe: raise FileNotFoundError("agy")
    # read-only roles run inside the task folder so any scratch files stay out of your projects;
    # shell commands are never auto-approved in headless mode
    wd = cwd if role in WRITE_ROLES else str(d)
    (d / "task.md").write_text(prompt, encoding="utf-8")
    msg = (f"Do the task in task.md exactly (it is in your working directory). The user's project directory is "
           f"{cwd} — any file or folder the task mentions lives there; open it with your file tools using full paths. "
           "Never run terminal commands (they are blocked and will end your turn)." if wd == str(d) else
           f"Do the task described in {d / 'task.md'} exactly. Never run terminal commands (they are blocked).")
    cmd = [exe, "-p", msg, "--mode", "accept-edits", "--output-format", "stream-json",
           "--print-timeout", f"{timeout}s", "--add-dir", str(d)]
    if role != "code": cmd += ["--add-dir", cwd]
    if model: cmd += ["--model", model]
    if role in ("code", "review") and "gemini" in (model or ""): cmd += ["--effort", "high"]  # Opus "thinking" rejects --effort
    return run_cli(cmd, None, wd, timeout + 30, stdin=False, transform=agy_transform, model=model)


def opencode_transform(line, st):
    try: e = json.loads(line)
    except ValueError: return [line] if line.strip() else []
    t, p = e.get("type"), e.get("part") or {}
    if t == "tool_use":
        stt = p.get("state") or {}
        return [tool_line(p.get("tool"), stt.get("input") or p.get("input"))]
    if t == "text" and p.get("text"):
        st["final"].append(p["text"])
        return [l for l in p["text"].splitlines() if l.strip()][:12]
    if t == "step_finish":
        tk = p.get("tokens") or {}
        add_usage(st["usage"], tk.get("input"), tk.get("output"), (tk.get("cache") or {}).get("read"),
                  (tk.get("cache") or {}).get("write"), tk.get("reasoning"))
    if t == "error":
        return [f"error {short(e.get('error') or p)}"]
    return []


def agent_opencode(prompt, role, cwd, timeout, d, model):
    exe = which("opencode")
    if not exe: raise FileNotFoundError("opencode")
    cmd = [exe, "run", "--format", "json", "-f", str(d / "prompt.md")]
    if model: cmd += ["-m", model]
    cmd += ["Do the task in the attached prompt.md exactly."]
    return run_cli(cmd, None, cwd, timeout, stdin=False, transform=opencode_transform, model=model)


def agent_ollama(prompt, role, cwd, timeout, d, model):
    if ensure_ollama() is None: raise RuntimeError("ollama is not installed or failed to start")
    if not model: raise RuntimeError("no ollama model installed")
    body = json.dumps({"model": model, "prompt": prompt, "stream": True, "options": {"num_ctx": 16384}}).encode()
    req = urllib.request.Request("http://127.0.0.1:11434/api/generate", body, {"Content-Type": "application/json"})
    parts, line_buf = [], ""
    with open(live_log(d), "a", encoding="utf-8", buffering=1) as lf:
        lf.write(f"\n━━━━ ollama starting in {cwd} ━━━━\nmodel: {model}\n")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                ch = json.loads(raw)
                tok = ch.get("response", "")
                parts.append(tok); line_buf += tok
                while "\n" in line_buf:
                    ln, line_buf = line_buf.split("\n", 1)
                    if ln.strip(): lf.write(ln + "\n")
                if ch.get("done"):
                    add_usage(CTX.usage, ch.get("prompt_eval_count"), ch.get("eval_count"))
        if line_buf.strip(): lf.write(line_buf + "\n")
        lf.write("━━━━ ollama exited rc=0 ━━━━\n")
    out = re.sub(r"<think>.*?</think>", "", "".join(parts), flags=re.S)
    return 0, out, ""


AGENTS = {"codex": agent_codex, "agy": agent_agy, "opencode": agent_opencode, "ollama": agent_ollama}


def summary_of(text, limit):
    m = re.search(r"##\s*SUMMARY\s*(.*?)(?:\n##\s|\Z)", text, re.S | re.I)
    s = (m.group(1) if m else text).strip()
    return s if len(s) <= limit else s[:limit] + " …[truncated]"


def run_task(task, role="research", agent=None, cwd=None, timeout=900, limit=1500, label=None, model=None):
    tid = (label or role)[:12] + "-" + uuid.uuid4().hex[:6]
    d = TASKS / tid
    d.mkdir(parents=True, exist_ok=True)
    prompt = wrap_prompt(role, task, tid)
    (d / "prompt.md").write_text(prompt, encoding="utf-8")
    chain = [agent] if agent else ROUTES[role]
    cwd = str(Path(cwd or os.getcwd()).resolve())
    emit({"ev": "start", "id": tid, "role": role, "chain": chain, "cwd": cwd, "task": task[:200]})
    live_log(d).write_text(f"TASK {tid}  role={role}\n{task}\n", encoding="utf-8")
    CTX.d = d
    if DASH: ensure_dashboard()
    if "ollama" in chain: ensure_ollama()
    tried = []
    with Watchdog():
        for a in chain:
            CTX.agent, CTX.usage = a, {}
            m = model_for(a, role, model if agent else None)
            emit({"ev": "attempt", "id": tid, "agent": a, "model": m or "default"})
            if TABS:
                open_tab(f"{a} · {tid}", TAB_COLOR.get(a, "#888888"),
                         f"Get-Content -Wait -Tail 200 -Encoding utf8 '{live_log(d)}'")
            t0 = time.time()
            try:
                rc, out, err = AGENTS[a](prompt, role, cwd, timeout, d, m)
            except Exception as e:
                rc, out, err = 1, "", f"{type(e).__name__}: {e}"
            dur = round(time.time() - t0, 1)
            usage = {k: v for k, v in CTX.usage.items() if k != "total"}
            with open(d / "raw.log", "a", encoding="utf-8") as f:
                f.write(f"===== {a} model={m} rc={rc} {dur}s usage={usage}\n--- stdout\n{out}\n--- stderr\n{err}\n")
            ok = rc == 0 and len(out.strip()) > 1 and not (QUOTA_RE.search(err[-2000:]) and len(out.strip()) < 200)
            tried.append({"agent": a, "model": m, "rc": rc, "sec": dur, "ok": ok, "usage": usage})
            if ok:
                (d / "out.md").write_text(out, encoding="utf-8")
                post_board(tid, out)
                (d / "meta.json").write_text(json.dumps({"id": tid, "role": role, "agent": a, "model": m, "tried": tried,
                                                         "cwd": cwd, "task": task}, indent=2), encoding="utf-8")
                summ = summary_of(out, limit)
                emit({"ev": "done", "id": tid, "agent": a, "model": m, "sec": dur, "usage": usage,
                      "returned_chars": len(summ)})
                return {"id": tid, "agent": a, "ok": True, "summary": summ, "out": str(d / "out.md")}
            emit({"ev": "fail", "id": tid, "agent": a, "model": m, "rc": rc, "usage": usage,
                  "err": (err.strip() or out.strip())[-300:]})
    (d / "meta.json").write_text(json.dumps({"id": tid, "role": role, "tried": tried, "task": task}, indent=2), encoding="utf-8")
    return {"id": tid, "agent": None, "ok": False, "summary": "ALL AGENTS FAILED: " + json.dumps(tried), "out": str(d / "raw.log")}


def fmt(r):
    return f"[{r['id']}] agent={r['agent']} ok={r['ok']} full={r['out']}\n{r['summary']}"


# ───────────────────────── commands ─────────────────────────
def cmd_models(a):
    conf = load_conf()
    if a.action == "set":
        cur = json.loads(CONF.read_text(encoding="utf-8")) if CONF.exists() else {}
        cur.setdefault(a.agent, {})[a.role] = a.model
        CONF.write_text(json.dumps(cur, indent=2), encoding="utf-8")
        print(f"{a.agent}.{a.role} = {a.model}")
        return
    print("CURRENT (agent: role -> model)")
    for ag, c in conf.items():
        print(f"  {ag:9} " + "  ".join(f"{k}={v or 'cli-default'}" for k, v in c.items()))
    print("  ollama    auto ->", model_for("ollama", "simple"))
    if a.action == "list":
        print("\nAVAILABLE")
        tags = ensure_ollama() or []
        print("  ollama   ", ", ".join(tags) or "none")
        for ag, args in (("agy", ["models"]), ("opencode", ["models"])):
            exe = which(ag)
            if not exe: continue
            try:
                outp = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", timeout=60).stdout
                names = [l.split()[0] for l in outp.splitlines() if l.strip() and not l.startswith("Fetching")]
                print(f"  {ag:9}", ", ".join(names))
            except Exception as e:
                print(f"  {ag:9} error {e}")
        print("  codex     uses your ChatGPT plan's default model (override: ab.py models set codex default <model>)")


def cmd_up(a):
    print("dashboard :", "opened" if ensure_dashboard() else "already open")
    print("guard     :", "started" if start_guard() else ("running" if guard_alive() else "off (AB_DASH=0)"))
    tags = ensure_ollama()
    print("ollama    :", "DOWN" if tags is None else f"up, models={tags}")
    for ag in ("codex", "agy", "opencode"):
        print(f"{ag:10}:", which(ag) or "MISSING")


def cmd_doctor(a):
    """Run a tiny real task through every agent and report what works."""
    jobs = [("simple", ag, "Reply with exactly: pong") for ag in ("ollama", "opencode", "codex", "agy")]
    with cf.ThreadPoolExecutor(4) as ex:
        futs = {ag: ex.submit(run_task, t, role, ag, None, 300, 200, f"doctor-{ag}") for role, ag, t in jobs}
        for ag, f in futs.items():
            r = f.result()
            print(f"{ag:9} {'OK ' if r['ok'] else 'FAIL'} {r['summary'][:160]!r}")


def main():
    ap = argparse.ArgumentParser(prog="coco", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run", help="run one task")
    r.add_argument("role", choices=ROUTES)
    r.add_argument("task", help="task text, or @file to read it from a file")
    r.add_argument("-a", "--agent", choices=AGENTS)
    r.add_argument("-m", "--model", help="model override (needs -a)")
    r.add_argument("-C", "--cwd")
    r.add_argument("-t", "--timeout", type=int, default=900)
    r.add_argument("-n", "--limit", type=int, default=1500, help="max summary chars printed")
    p = sp.add_parser("par", help="parallel tasks from JSON: [{role, task, agent?, model?, cwd?, label?}]")
    p.add_argument("file"); p.add_argument("-j", "--jobs", type=int, default=4)
    p.add_argument("-n", "--limit", type=int, default=800)
    s = sp.add_parser("show", help="print a task's full output"); s.add_argument("id")
    sp.add_parser("board", help="print the shared blackboard")
    l = sp.add_parser("log", help="recent bus events"); l.add_argument("-k", type=int, default=20)
    m = sp.add_parser("models", help="show / list / set models per agent and role")
    m.add_argument("action", nargs="?", default="show", choices=["show", "list", "set"])
    m.add_argument("agent", nargs="?"); m.add_argument("role", nargs="?"); m.add_argument("model", nargs="?")
    sp.add_parser("up", help="start everything: dashboard + ollama, check CLIs")
    sp.add_parser("doctor", help="send a tiny real task to every agent")
    sp.add_parser("credits", help="Claude usage ($) vs work done by workers")
    sp.add_parser("dash", help="animated live terminal dashboard")
    sp.add_parser("statusline", help="Claude Code status line hook (captures real rate limits)")
    g = sp.add_parser("guard", help="keep the dashboard open while Claude sessions run: on | off | status")
    g.add_argument("action", nargs="?", default="status", choices=["on", "off", "status", "run"])
    a = ap.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    HOME.mkdir(parents=True, exist_ok=True)

    if a.cmd in ("run", "par") and os.environ.get("COCO_WORKER"):
        sys.exit("COCO: you are a worker agent. Do the task yourself instead of delegating.")
    if a.cmd == "guard":
        return cmd_guard(a)
    if a.cmd == "statusline":
        return cmd_statusline()
    if a.cmd == "dash":
        import tui
        return tui.run(HOME)
    if a.cmd == "credits":
        import credits
        return print(credits.report_text(HOME))
    if a.cmd == "run":
        task = Path(a.task[1:]).read_text(encoding="utf-8") if a.task.startswith("@") else a.task
        print(fmt(run_task(task, a.role, a.agent, a.cwd, a.timeout, a.limit, model=a.model)))
    elif a.cmd == "par":
        jobs = json.loads(Path(a.file).read_text(encoding="utf-8"))
        with cf.ThreadPoolExecutor(a.jobs) as ex:
            futs = [ex.submit(run_task, j["task"], j.get("role", "research"), j.get("agent"), j.get("cwd"),
                              j.get("timeout", 900), a.limit, j.get("label"), j.get("model")) for j in jobs]
            for f in futs: print(fmt(f.result()), end="\n\n")
    elif a.cmd == "show":
        d = TASKS / a.id
        print((d / "out.md").read_text(encoding="utf-8") if (d / "out.md").exists() else (d / "raw.log").read_text(encoding="utf-8"))
    elif a.cmd == "board":
        print(BOARD.read_text(encoding="utf-8") if BOARD.exists() else "(empty)")
    elif a.cmd == "log":
        lines = BUS.read_text(encoding="utf-8").splitlines() if BUS.exists() else []
        print("\n".join(lines[-a.k:]))
    elif a.cmd == "models":
        if a.action == "set" and not (a.agent and a.role and a.model):
            sys.exit("usage: ab.py models set <agent> <role|default> <model>")
        cmd_models(a)
    elif a.cmd == "up":
        cmd_up(a)
    elif a.cmd == "doctor":
        cmd_doctor(a)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

