"""COCO: animated full-screen terminal dashboard  (`coco dash`).

Shows the agent network with packets flying between Claude, the workers and the shared
blackboard, a live activity log of exactly what each agent is doing, task progress, and the
board. Data comes from ~/.agentbus (bus.jsonl, BOARD.md, tasks/*/live.log).
"""
import datetime as dt, json, math, os, random, re, shutil, signal, threading, time, urllib.request
from collections import deque
from pathlib import Path

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich import box

FPS = 14          # while agents work
IDLE_FPS = 3      # nothing running: keep the CPU quiet
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ORB = "◐◓◑◒"
COL = {"claude": "#ff8a4c", "codex": "#19c37d", "opencode": "#ffb020", "agy": "#4c8dff",
       "ollama": "#b06cff", "board": "#5ee6ff", "err": "#ff5470"}
NAME = {"claude": "CLAUDE", "codex": "CODEX", "opencode": "OPENCODE", "agy": "ANTIGRAVITY",
        "ollama": "OLLAMA", "board": "BOARD"}
ROLE_OF = {"claude": "planner · reviewer", "codex": "research · code · review", "opencode": "browse · simple · free",
           "agy": "gemini · opus 4.6", "ollama": "local · private", "board": "shared memory"}
POS = {"claude": (.50, .46), "codex": (.17, .16), "opencode": (.83, .16), "agy": (.17, .80),
       "ollama": (.83, .80), "board": (.50, .86)}
WORKERS = ["codex", "opencode", "agy", "ollama"]
ICON = {"search": "⌕", "fetch": "⇣", "cmd": "❯", "edit": "✎", "read": "≡", "think": "∴", "text": "›",
        "sys": "·", "error": "✖", "flow": "⇄", "board": "◆"}
KSTYLE = {"search": "#7fd4ff", "fetch": "#7fd4ff", "cmd": "#e6e6e6", "edit": "#ffd36e", "read": "#b9c2d8",
          "think": "italic #8f9bb8", "text": "#d6dcea", "sys": "italic #6b7590", "error": "bold #ff5470",
          "flow": "bold #ffffff", "board": "#5ee6ff"}


# ───────────────────────── event source ─────────────────────────
class Tail:
    MARK = re.compile(r"━━━━ (\w+) (starting|exited)(.*?)━━━━")

    def __init__(self, home: Path):
        self.TASKS, self.BUS, self.BOARD = home / "tasks", home / "bus.jsonl", home / "BOARD.md"
        self.bus_off = self.BUS.stat().st_size if self.BUS.exists() else 0
        self.board_off = self.BOARD.stat().st_size if self.BOARD.exists() else 0
        self.offs, self.states, self.tid_agent, self.first = {}, {}, {}, True
        if self.BUS.exists():
            for e in self._lines(self.BUS.read_text(encoding="utf-8")):
                self._track(e)

    @staticmethod
    def _lines(data):
        for line in data.splitlines():
            try: yield json.loads(line)
            except ValueError: pass

    def _track(self, e):
        if e.get("ev") == "start": self.tid_agent[e["id"]] = e["chain"][0]
        elif e.get("ev") == "done": self.tid_agent[e["id"]] = e["agent"]

    @staticmethod
    def classify(s, mode):
        l = s.lower()
        if re.search(r"\b(error|failed|denied|exception|traceback|timeout|refused)\b", l): return "error"
        if re.search(r"web[ _.-]?search|searching|search_query|\bquery:", l): return "search"
        if re.search(r"https?://|webfetch|web_fetch|\bfetch", l): return "fetch"
        if re.search(r"apply_patch|file update|\bwrote\b|\bedit(ed|ing)?\b|\bcreated?\b|^\+\+\+|^diff ", l): return "edit"
        if mode == "exec" or re.search(r"^\s*(\$|exec\b|bash|pwsh|powershell|cmd\b|python|npm|git |succeeded in|exited \d)", l): return "cmd"
        if re.search(r"\b(read|glob|grep|list|ls)\b", l) and len(s) < 200: return "read"
        if mode == "thinking": return "think"
        if re.search(r"^tokens used|^\d[\d,]*$", l): return "sys"
        return "text"

    def _line(self, tid, raw, st, out):
        s = raw.rstrip()
        m = self.MARK.search(s)
        if m:
            st.update(agent=m.group(1), pre=False, skip=False, mode=None)
            verb = "booted" if m.group(2) == "starting" else "exited" + m.group(3).rstrip()
            out.append({"type": "log", "tid": tid, "agent": st["agent"], "kind": "sys", "text": f"{st['agent']} {verb}"})
            return
        w = s.strip()
        if st["pre"] or not w: return
        if w == "user": st["skip"] = True; return          # codex echoes the prompt: hide
        if w in ("thinking", "codex", "exec"): st.update(skip=False, mode=w); return
        if st["skip"] or w.startswith(("--------", "OpenAI Codex v")): return
        kind = "sys" if (st["mode"] is None and re.match(r"^(workdir|model|provider|approval|sandbox|reasoning|session id):", w)) \
            else self.classify(w, st["mode"])
        out.append({"type": "log", "tid": tid, "agent": st["agent"], "kind": kind, "text": w[:300]})

    def poll(self):
        out, hist = [], self.first
        if self.BUS.exists() and self.BUS.stat().st_size > self.bus_off:
            with open(self.BUS, encoding="utf-8") as f:
                f.seek(self.bus_off); data = f.read(); self.bus_off = f.tell()
            for e in self._lines(data):
                self._track(e); out.append({"type": "bus", **e})
        if self.BOARD.exists() and self.BOARD.stat().st_size > self.board_off:
            with open(self.BOARD, encoding="utf-8") as f:
                f.seek(self.board_off); data = f.read(); self.board_off = f.tell()
            for line in data.splitlines():
                m = re.match(r"- \[([^\]]+)\]\s*(.*)", line)
                if m: out.append({"type": "board", "tid": m.group(1), "agent": self.tid_agent.get(m.group(1), "claude"), "text": m.group(2)})
        if self.TASKS.exists():
            for d in self.TASKS.iterdir():
                lf = d / "live.log"
                if not lf.exists(): continue
                stt = lf.stat()
                if d.name not in self.offs:
                    if time.time() - stt.st_mtime > 900:
                        self.offs[d.name] = stt.st_size; continue
                    self.offs[d.name] = 0
                    self.states[d.name] = {"agent": self.tid_agent.get(d.name, "claude"), "pre": True, "skip": False, "mode": None}
                if stt.st_size > self.offs[d.name]:
                    with open(lf, "rb") as f:
                        f.seek(self.offs[d.name]); chunk = f.read()
                    cut = chunk.rfind(b"\n") + 1
                    if not cut: continue
                    self.offs[d.name] += cut
                    for raw in chunk[:cut].decode("utf-8", "replace").splitlines():
                        self._line(d.name, raw, self.states[d.name], out)
        self.first = False
        for e in out: e["hist"] = hist; e.setdefault("ts", time.time())
        return out

    def tasks(self):
        tasks = {}
        if self.BUS.exists():
            for e in self._lines(self.BUS.read_text(encoding="utf-8")):
                if "id" not in e or "ev" not in e: continue
                t = tasks.setdefault(e["id"], {"id": e["id"], "state": "running", "fails": [], "agent": "?"})
                ts = dt.datetime.fromisoformat(e["t"]).timestamp()
                if e["ev"] == "start":
                    t.update(role=e["role"], task=e["task"], chain=e["chain"], agent=e["chain"][0], start=ts)
                elif e["ev"] == "fail":
                    t["fails"].append(e["agent"])
                    nxt = [a for a in t.get("chain", []) if a not in t["fails"]]
                    if nxt: t["agent"] = nxt[0]
                    else: t.update(state="failed", end=ts)
                elif e["ev"] == "done":
                    u = e.get("usage") or {}
                    t.update(state="done", agent=e["agent"], sec=e["sec"], model=e.get("model", ""),
                             tokens=u.get("in", 0) + u.get("out", 0) + u.get("cache_read", 0))
                elif e["ev"] == "attempt":
                    t["model"] = e.get("model", "")
        for t in tasks.values():
            if t["state"] == "running" and time.time() - t.get("start", 0) > 3600: t["state"] = "stale"
        return list(tasks.values())

    def tokens(self, tid, cache={}):
        if tid in cache: return cache[tid]
        d = self.TASKS / tid
        size = lambda p: p.stat().st_size if p.exists() else 0
        out = (d / "out.md").read_text(encoding="utf-8", errors="replace") if (d / "out.md").exists() else ""
        m = re.search(r"##\s*SUMMARY\s*(.*?)(?:\n##\s|\Z)", out, re.S | re.I)
        summ = min(len(m.group(1) if m else out), 1500)
        cache[tid] = max(0, (size(d / "raw.log") + size(d / "prompt.md") - summ) // 4)
        return cache[tid]

    def board_lines(self):
        if not self.BOARD.exists(): return []
        res = []
        for line in self.BOARD.read_text(encoding="utf-8").splitlines()[-40:]:
            m = re.match(r"- \[([^\]]+)\]\s*(.*)", line)
            if m: res.append({"tid": m.group(1), "agent": self.tid_agent.get(m.group(1), "claude"), "text": m.group(2), "ts": 0})
        return res


def agents_online():
    v = {n: bool(shutil.which(n + ".cmd") or shutil.which(n)) for n in ("codex", "opencode")}
    v["agy"] = bool(shutil.which("agy") or shutil.which("agy.exe"))
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=0.8) as r:
            v["ollama"] = bool(json.loads(r.read())["models"])
    except Exception:
        v["ollama"] = False
    return v


# ───────────────────────── state ─────────────────────────
class State:
    limits = None

    def __init__(self):
        self.feed = deque(maxlen=400)
        self.packets, self.sparks = [], []
        self.hit, self.act = {}, {}            # node -> until ; node -> (text, t0)
        self.tasks, self.demo_tasks, self.board = [], [], []
        self.online = {w: True for w in WORKERS}
        self.model = {}                         # agent -> model currently/last used
        self.credits = None                     # credits.compute() result
        self.notice = ("", 0)                   # (text, until)
        self.frame = 0

    def all_tasks(self):
        return self.tasks + self.demo_tasks

    def log(self, agent, kind, text, tid="", hist=False):
        self.feed.append({"ts": time.time(), "agent": agent, "kind": kind, "text": text, "tid": tid,
                          "fresh": 0 if hist else time.time() + 1.6})

    def packet(self, a, b, color, label="", dur=1.4):
        self.packets.append({"a": a, "b": b, "color": color, "label": label, "t0": time.time(), "dur": dur})

    def spark(self, node, n=3):
        for _ in range(n):
            self.sparks.append({"node": node, "ang": random.random() * math.tau, "t0": time.time(), "dur": .5 + random.random() * .5,
                                "ch": random.choice("✦*·+✧")})

    def set_act(self, node, text):
        self.act[node] = (text, time.time())

    def handle(self, e):
        hist = e.get("hist")
        if e["type"] == "log":
            a = e["agent"] if e["agent"] in COL else "claude"
            self.log(a, e["kind"], e["text"], e.get("tid", ""), hist)
            if not hist and e["kind"] != "sys":
                self.spark(a, 1 if e["kind"] in ("text", "think") else 3)
                self.set_act(a, f"{ICON.get(e['kind'], '›')} {e['text']}")
        elif e["type"] == "bus" and e.get("ev") == "attempt":
            self.model[e["agent"]] = e.get("model", "")
            if not hist:
                self.log(e["agent"], "sys", f"model → {e.get('model')}", e["id"])
        elif e["type"] == "bus" and e.get("ev") == "service":
            if not hist:
                self.log("claude", "sys", f"{e['name']} {e['state']}" + (f" · {len(e.get('models', []))} models" if e.get("models") is not None else ""))
        elif e["type"] == "bus" and not hist:
            if e["ev"] == "start":
                a = e["chain"][0]
                self.packet("claude", a, COL.get(a, "#fff"), e["role"])
                self.log("claude", "flow", f"dispatch → {NAME.get(a, a)} [{e['role']}] {e['task']}", e["id"])
                self.set_act("claude", f"→ {NAME.get(a, a)}: {e['task']}")
            elif e["ev"] == "fail":
                t = next((x for x in self.all_tasks() if x["id"] == e["id"]), {})
                ch = t.get("chain", [])
                nx = ch[ch.index(e["agent"]) + 1] if e["agent"] in ch and ch.index(e["agent"]) + 1 < len(ch) else None
                self.log(e["agent"], "error", f"failed rc={e.get('rc')} {e.get('err', '')[:120]}", e["id"])
                self.hit[e["agent"]] = (time.time() + .6, COL["err"])
                if nx:
                    self.packet(e["agent"], nx, COL["err"], "fallback")
                    self.log("claude", "flow", f"fallback {NAME.get(e['agent'])} ✖ → {NAME.get(nx, nx)}", e["id"])
            elif e["ev"] == "done":
                self.packet(e["agent"], "claude", COL.get(e["agent"], "#fff"), "summary", 1.6)
                self.log(e["agent"], "flow", f"✔ summary → Claude ({e['sec']}s)", e["id"])
                self.set_act(e["agent"], f"✔ done in {e['sec']}s")
        elif e["type"] == "board":
            self.board.append({**e, "ts": time.time()})
            if not hist:
                self.packet(e["agent"], "board", COL.get(e["agent"], "#fff"), "finding", 1.3)
                self.log(e["agent"], "board", f"board: {e['text']}", e.get("tid", ""))


# ───────────────────────── canvas ─────────────────────────
def bres(x0, y0, x1, y1):
    pts, dx, dy = [], abs(x1 - x0), -abs(y1 - y0)
    sx, sy, err = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1), abs(x1 - x0) - abs(y1 - y0)
    while True:
        pts.append((x0, y0))
        if x0 == x1 and y0 == y1: return pts
        e2 = 2 * err
        if e2 >= dy: err += dy; x0 += sx
        if e2 <= dx: err += dx; y0 += sy


def fmt_s(s):
    s = max(0, s)
    return f"{int(s)}s" if s < 60 else f"{int(s // 60)}m{int(s % 60):02d}s"


class Network:
    BW, BH = 24, 5

    def __init__(self, st: State): self.st = st

    def __rich_console__(self, console, options):
        w, h = options.max_width, max(10, (options.height or 22))
        st, now, f = self.st, time.time(), self.st.frame
        ch = [[" "] * w for _ in range(h)]
        sty = [[None] * w for _ in range(h)]

        def put(x, y, c, s):
            if 0 <= x < w and 0 <= y < h: ch[y][x] = c; sty[y][x] = s

        bw = min(self.BW, max(16, w // 4))
        C = {k: (int(px * (w - 1)), int(py * (h - 1))) for k, (px, py) in POS.items()}
        running = [t for t in st.all_tasks() if t["state"] == "running"]
        busy = {t["agent"] for t in running}

        # edges
        paths = {}
        for wk in WORKERS:
            paths[("claude", wk)] = bres(*C["claude"], *C[wk])
            paths[(wk, "board")] = bres(*C[wk], *C["board"])
        paths[("claude", "board")] = bres(*C["claude"], *C["board"])
        for (a, b), pts in paths.items():
            wk = b if a == "claude" else a
            live = wk in busy and a == "claude"
            off = wk in WORKERS and not st.online.get(wk, True)
            for i, (x, y) in enumerate(pts):
                if live:
                    on = (i - f) % 4 == 0
                    put(x, y, "•" if on else "·", f"bold {COL[wk]}" if on else COL[wk])
                else:
                    put(x, y, "·", "#2a3147" if off else "#3b4563")

        # packets
        keep = []
        for p in st.packets:
            q = (now - p["t0"]) / p["dur"]
            if q >= 1:
                st.hit[p["b"]] = (now + .35, p["color"]); continue
            keep.append(p)
            pts = paths.get((p["a"], p["b"])) or (paths[(p["b"], p["a"])][::-1] if (p["b"], p["a"]) in paths
                                                   else bres(*C[p["a"]], *C[p["b"]]))
            if p["a"] in WORKERS and p["b"] in WORKERS:  # fallback hop worker→worker
                for x, y in pts: put(x, y, "·", COL["err"])
            e = q * q * (3 - 2 * q)
            i = int(e * (len(pts) - 1))
            for k, c in enumerate("●•∙·"):
                j = i - k * 2 if k else i
                if 0 <= j < len(pts): put(*pts[j], c, f"bold {p['color']}" if k == 0 else p["color"])
            x, y = pts[i]
            for k, c in enumerate(p["label"][:12]):
                put(x + 2 + k, y - 1, c, f"bold {p['color']}")
        st.packets = keep

        # sparks
        keep = []
        for s in st.sparks:
            q = (now - s["t0"]) / s["dur"]
            if q >= 1: continue
            keep.append(s)
            cx, cy = C[s["node"]]
            r = bw // 2 + 1 + q * 4
            put(int(cx + math.cos(s["ang"]) * r), int(cy + math.sin(s["ang"]) * r * .5), s["ch"],
                COL[s["node"]] if q < .6 else "#4a5470")
        st.sparks = keep

        # nodes
        for k, (cx, cy) in C.items():
            nbw = bw if k != "board" else max(18, bw - 2)
            bh = self.BH if k != "board" else 3
            x0, y0 = max(0, min(w - nbw, cx - nbw // 2)), max(0, min(h - bh, cy - bh // 2))
            col = COL[k]
            off = k in WORKERS and not st.online.get(k, True)
            is_busy = (k in busy) or (k == "claude" and running)
            hit = st.hit.get(k)
            flash = hit and hit[0] > now
            base = "#3a4258" if off else (f"bold {col}" if is_busy else col)
            if flash: base = f"bold {hit[1]} reverse"
            perim = [(x0 + i, y0) for i in range(nbw)] + [(x0 + nbw - 1, y0 + j) for j in range(1, bh)] + \
                    [(x0 + nbw - 1 - i, y0 + bh - 1) for i in range(1, nbw)] + [(x0, y0 + bh - 1 - j) for j in range(1, bh - 1)]
            for idx, (x, y) in enumerate(perim):
                c = "─" if y in (y0, y0 + bh - 1) else "│"
                if (x, y) == (x0, y0): c = "╭"
                elif (x, y) == (x0 + nbw - 1, y0): c = "╮"
                elif (x, y) == (x0, y0 + bh - 1): c = "╰"
                elif (x, y) == (x0 + nbw - 1, y0 + bh - 1): c = "╯"
                s = base
                if is_busy and not flash and (idx - f * 2) % len(perim) < 5: s = "bold #ffffff"
                put(x, y, c, s)
            for yy in range(y0 + 1, y0 + bh - 1):
                for xx in range(x0 + 1, x0 + nbw - 1): put(xx, yy, " ", None)
            inner = nbw - 4
            spin = SPIN[f % len(SPIN)] if is_busy else ("○" if off else "●")
            title = f"{spin} {NAME[k]}"
            for i, c in enumerate(title[:inner]): put(x0 + 2 + i, y0 + 1, c, f"bold {col}" if not off else "#5a6378")
            if k == "board":
                txt = f"{len(st.board)} shared"
                if len(title) + 1 + len(txt) <= inner:
                    for i, c in enumerate(txt): put(x0 + nbw - 2 - len(txt) + i, y0 + 1, c, "#8fa0c0")
                continue
            mine = [t for t in running if t["agent"] == k]
            if k == "claude":
                status = f"waiting on {len(running)} worker{'s' * (len(running) != 1)}" if running else "idle · saving credits"
            elif off: status = "offline"
            elif mine: status = f"▶ {mine[0].get('role', '')} {fmt_s(now - mine[0].get('start', now))}" + (f" +{len(mine) - 1}" if len(mine) > 1 else "")
            else: status = (st.model.get(k) or ROLE_OF[k]).split("/")[-1].split(":")[0]
            for i, c in enumerate(status[:inner]): put(x0 + 2 + i, y0 + 2, c, "#ffffff" if mine or (k == "claude" and running) else "#7d88a3")
            act = st.act.get(k)
            if act and not off:
                shown = act[0][: min(inner, int((now - act[1]) * 70))]
                for i, c in enumerate(shown): put(x0 + 2 + i, y0 + 3, c, "#c9d1e6")

        lines = []
        for y in range(h):
            t, run, cur = Text(), "", sty[y][0]
            for x in range(w):
                if sty[y][x] != cur:
                    t.append(run, style=cur); run, cur = "", sty[y][x]
                run += ch[y][x]
            t.append(run, style=cur)
            lines.append(t)
        yield Text("\n").join(lines)


class Feed:
    def __init__(self, st): self.st = st

    def __rich_console__(self, console, options):
        h, w = options.height or 20, options.max_width
        now = time.time()
        rows = []
        for e in list(self.st.feed)[-h:]:
            t = Text(no_wrap=True, overflow="ellipsis")
            fresh = e["fresh"] > now
            bg = " on #1b2233" if fresh else ""
            t.append(time.strftime("%H:%M:%S ", time.localtime(e["ts"])), style="#566079" + bg)
            t.append(f"{NAME.get(e['agent'], e['agent'])[:8]:<9}", style=f"bold {COL.get(e['agent'], '#fff')}{bg}")
            t.append(f"{ICON.get(e['kind'], '›')} ", style=KSTYLE.get(e["kind"], "") + bg)
            t.append(e["text"].replace("\n", " "), style=KSTYLE.get(e["kind"], "") + bg)
            t.truncate(w, overflow="ellipsis")
            rows.append(t)
        if not rows:
            rows = [Text("Waiting for Claude to dispatch work…", style="italic #566079"),
                    Text("Tasks appear here the moment Claude delegates.", style="#3e475e")]
        yield Group(*rows)


class Tasks:
    def __init__(self, st): self.st = st

    def __rich_console__(self, console, options):
        h, w = options.height or 8, options.max_width
        now, f = time.time(), self.st.frame
        items = sorted(self.st.all_tasks(), key=lambda t: t.get("start", 0), reverse=True)
        items = sorted(items, key=lambda t: t["state"] != "running")[:h]
        rows = []
        for t in items:
            c = COL.get(t["agent"], "#888")
            r = Text(no_wrap=True, overflow="ellipsis")
            if t["state"] == "running":
                r.append(SPIN[f % len(SPIN)] + " ", style=f"bold {c}")
                bar = "".join("▰" if (i - f // 2) % 10 in (0, 1, 2) else "▱" for i in range(10))
                r.append(bar + " ", style=c)
                r.append(f"{fmt_s(now - t.get('start', now)):>6} ", style="#ffffff")
            elif t["state"] == "done":
                r.append("✔ ", style="bold #19c37d"); r.append("▰" * 10 + " ", style="#1e5c43")
                r.append(f"{fmt_s(t.get('sec', 0)):>6} ", style="#7d88a3")
            else:
                r.append("✖ ", style="bold #ff5470"); r.append("▱" * 10 + " ", style="#5c1e2a"); r.append(f"{t['state']:>6} ", style="#ff5470")
            for fl in t.get("fails", []): r.append(f"{fl}✖→", style="#ff5470")
            r.append(f"{t['agent']:<8} ", style=f"bold {c}")
            r.append(f"{t.get('role', ''):<8} ", style="#8fa0c0")
            if t.get("tokens"): r.append(f"{t['tokens'] / 1000:>6.1f}k tok ", style="#5ee6ff")
            r.append(t.get("task", "").replace("\n", " "), style="#d6dcea")
            r.truncate(w, overflow="ellipsis")
            rows.append(r)
        yield Group(*rows) if rows else Text("No tasks yet.", style="italic #566079")


class Board:
    def __init__(self, st): self.st = st

    def __rich_console__(self, console, options):
        h, w, now = options.height or 8, options.max_width, time.time()
        rows = []
        for b in list(reversed(self.st.board))[:h]:
            fresh = now - b["ts"] < 2.5
            r = Text(no_wrap=True, overflow="ellipsis")
            r.append("◆ ", style=f"bold {COL.get(b['agent'], '#5ee6ff')}")
            r.append(f"{NAME.get(b['agent'], b['agent'])[:8]:<9}", style=COL.get(b["agent"], "#5ee6ff"))
            r.append(b["text"], style=("bold #ffffff on #123540" if fresh else "#c4cde0"))
            r.truncate(w, overflow="ellipsis")
            rows.append(r)
        yield Group(*rows) if rows else Text("Nothing shared yet.", style="italic #566079")


def header(st, wide=True):
    f = st.frame
    real = st.tasks
    run = [t for t in st.all_tasks() if t["state"] == "running"]
    done = [t for t in real if t["state"] == "done"]
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(f" {ORB[(f // 3) % 4]} ", style="bold #ff8a4c")
    grad = ["#ff8a4c", "#ffb020", "#19c37d", "#5ee6ff", "#4c8dff", "#b06cff"]
    for i, c in enumerate("C O C O"):
        t.append(c, style=f"bold {grad[(i // 2 + f // 4) % len(grad)]}")
    t.append("  ")
    if wide: t.append("Claude orchestrating cheaper agents  ", style="#566079")
    t.append(" ● LIVE " if f % 14 < 10 else "   LIVE ", style="bold #19c37d")
    cr = st.credits
    stats = [("running", len(run), "#ffffff"), ("done", len(done), "#19c37d"),
             ("failed", sum(1 for x in real if x["state"] == "failed"), "#ff5470")]
    if cr:
        stats += [("Claude today", f"${cr['claude_today']['usd']:.2f}", "#ff8a4c"),
                  ("saved today", f"${cr['workers_today']['net_saved']:.2f}", "#19c37d")]
    for name, v, c in stats:
        t.append("  │  ", style="#2c3449"); t.append(str(v), style=f"bold {c}"); t.append(f" {name}", style="#6b7590")
    note, until = st.notice
    if note and until > time.time():
        t.append(f"   {note}", style="bold #ffb020 reverse" if f % 6 < 3 else "bold #ffb020")
    else:
        t.append(f"   {time.strftime('%H:%M:%S')}", style="#566079")
    return t


class Credits:
    """Real numbers: Claude tokens from Claude Code transcripts; worker tokens reported by each CLI."""
    def __init__(self, st): self.st = st

    def __rich_console__(self, console, options):
        c = self.st.credits
        if not c:
            yield Text("reading Claude Code transcripts…", style="italic #566079"); return
        import credits as cm
        ct, cs, wt, ws = c["claude_today"], c["claude_since"], c["workers_today"], c["workers_since"]
        tot = lambda r: r["in"] + r["out"] + r.get("cw", 0) + r["cr"]
        def row(label, a, b, sa="#d6dcea", sb="#d6dcea"):
            r = Text(no_wrap=True, overflow="ellipsis")
            r.append(f"{label:<14}", style="#8a93ab"); r.append(f"{a:>{cw}}", style=sa); r.append(f"{b:>{cw}}", style=sb)
            return r
        cw = max(9, min(17, (options.max_width - 14) // 2))
        hdr = Text(no_wrap=True); hdr.append(f"{'':<14}{'TODAY':>{cw}}{('SINCE START' if cw > 11 else 'TOTAL'):>{cw}}", style="bold #6b7590")
        share = lambda s, b: f"{100 * s / (s + b):.0f}%" if s + b > 0 else "—"
        rows = [
            hdr,
            row("Claude burn", f"${ct['usd']:.2f}", f"${cs['usd']:.2f}", "bold #ff8a4c", "bold #ff8a4c"),
            row("  tokens", cm.fmt_tok(tot(ct)), cm.fmt_tok(tot(cs)), "#c08060", "#c08060"),
            row("Worker tokens", cm.fmt_tok(tot(wt)), cm.fmt_tok(tot(ws)), "#5ee6ff", "#5ee6ff"),
            row("Saved (net)", f"${wt['net_saved']:.2f}", f"${ws['net_saved']:.2f}", "bold #19c37d", "bold #19c37d"),
            row("Offload share", share(wt["net_saved"], ct["usd"]), share(ws["net_saved"], cs["usd"]), "bold #ffffff", "bold #ffffff"),
        ]
        ba = Text(no_wrap=True, overflow="ellipsis")
        for a, v in ws["by_agent"].items():
            ba.append("■ ", style=COL.get(a, "#888")); ba.append(f"{a} {cm.fmt_tok(v['tok'])}  ", style="#8fa0c0")
        if ws["by_agent"]: rows.append(ba)
        yield Group(*rows)


class Limits:
    def __init__(self, st): self.st = st

    def __rich_console__(self, console, options):
        rows = limit_rows(self.st.limits)
        yield Group(*rows) if rows else Text("no live limit data yet", style="italic #566079")


def _bar(pct, n=8):
    full = int(round(min(100, max(0, pct)) / 100 * n))
    col = "#19c37d" if pct < 60 else "#ffb020" if pct < 85 else "#ff5470"
    return Text("█" * full, style=col) + Text("░" * (n - full), style="#2c3449")


def _ago(t):
    s = time.time() - t
    return "now" if s < 90 else f"{s / 60:.0f}m ago" if s < 5400 else f"{s / 3600:.0f}h ago"


def _reset(ts):
    if not ts: return ""
    try: ts = float(ts)
    except (TypeError, ValueError):
        try: ts = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        except ValueError: return ""
    s = ts - time.time()
    return "" if s <= 0 else f"↻{s / 60:.0f}m" if s < 5400 else f"↻{s / 3600:.0f}h" if s < 172800 else f"↻{s / 86400:.0f}d"


def limit_rows(L):
    """Usage left per agent, only where a real live source exists."""
    out = []
    lab = {300: "5h", 10080: "wk"}
    for agent, name in (("claude", "Claude"), ("codex", "Codex")):
        d = (L or {}).get(agent)
        if not d: continue
        r = Text(no_wrap=True, overflow="ellipsis")
        r.append(f"{name:<7}", style=f"bold {COL[agent]}")
        r.append(f"{_ago(d['asof'])} ", style="italic #4a5470") if time.time() - d["asof"] > 90 else None
        for mins, pct, rs in d["windows"]:
            r.append(f"{lab.get(mins, f'{mins}m')} ")
            try: passed = rs and float(rs) < time.time()
            except (TypeError, ValueError): passed = False
            if passed:
                r.append("reset ✓  ", style="#19c37d"); continue
            r.append_text(_bar(pct, 6))
            r.append(f" {100 - pct:.0f}% left {_reset(rs)}  ", style="#c4cde0")
        out.append(r)
    o = (L or {}).get("ollama")
    if o is not None:
        r = Text(no_wrap=True, overflow="ellipsis")
        r.append(f"{'Ollama':<7}", style=f"bold {COL['ollama']}")
        r.append("local, no quota · ", style="#8fa0c0")
        r.append(", ".join(f"{n} {v / 2**30:.1f}GB VRAM" for n, v in o["loaded"]) or "no model loaded", style="#c4cde0")
        out.append(r)
    return out


def build_layout(w, h):
    """Pick a layout for the current terminal size. Returns (layout, visible panel names)."""
    lay = Layout()
    if w >= 110 and h >= 28:                          # full
        foot = 11 if h >= 34 else 9
        lay.split_column(Layout(name="head", size=3), Layout(name="body", ratio=1), Layout(name="foot", size=foot))
        lay["body"].split_row(Layout(name="net", ratio=3), Layout(name="right", ratio=2))
        lay["right"].split_column(Layout(name="feed", ratio=1), Layout(name="limits", size=5))
        lay["foot"].split_row(Layout(name="tasks", ratio=5), Layout(name="credits", minimum_size=44, ratio=3), Layout(name="board", ratio=4))
        return lay, {"head", "net", "feed", "limits", "tasks", "credits", "board"}
    if w >= 76 and h >= 22:                           # medium: no blackboard
        body = Layout(name="body", ratio=1) if w >= 96 else Layout(name="feed", ratio=1)
        lay.split_column(Layout(name="head", size=3), body, Layout(name="limits", size=5), Layout(name="foot", size=9))
        if w >= 96:
            lay["body"].split_row(Layout(name="net", ratio=1), Layout(name="feed", ratio=1)); names = {"net", "feed"}
        else:
            names = {"feed"}
        lay["foot"].split_row(Layout(name="tasks", ratio=1), Layout(name="credits", size=42))
        return lay, names | {"head", "limits", "tasks", "credits"}
    # narrow / short: stack what fits
    parts, names = [Layout(name="head", size=3)], {"head"}
    if h >= 34:
        parts.append(Layout(name="net", ratio=2)); names.add("net")
    parts.append(Layout(name="feed", ratio=3, minimum_size=4)); names.add("feed")
    if h >= 18:
        parts.append(Layout(name="tasks", size=7)); names.add("tasks")
    if h >= 26:
        parts.append(Layout(name="credits", size=9)); names.add("credits")
    if h >= 31:
        parts.append(Layout(name="limits", size=5)); names.add("limits")
    lay.split_column(*parts)
    return lay, names


def panel(r, title, color="#3b4563"):
    return Panel(r, title=f"[bold #8a93ab]{title}[/]", title_align="left", border_style=color, box=box.ROUNDED, padding=(0, 1))


# ───────────────────────── main loop ─────────────────────────
def run(home: Path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import credits as cm
    os.environ.pop("NO_COLOR", None)
    console = Console(force_terminal=True, color_system="truecolor")
    tail, st = Tail(home), State()
    st.board = tail.board_lines()
    hb = home / "dash.heartbeat"
    size, lay, show = None, None, set()
    net, feed, tasks, board, cred, lim = Network(st), Feed(st), Tasks(st), Board(st), Credits(st), Limits(st)
    last_poll = last_tasks = last_online = 0
    last_ctrl_c = [0.0]

    def on_sigint(*_):
        running = [t for t in st.tasks if t["state"] == "running"]
        if running:
            st.notice = (f"⚠ {len(running)} worker(s) running — dashboard stays open", time.time() + 3)
            return
        if time.time() - last_ctrl_c[0] < 2:
            raise KeyboardInterrupt
        last_ctrl_c[0] = time.time()
        st.notice = ("press Ctrl+C again to close the dashboard", time.time() + 2)
    signal.signal(signal.SIGINT, on_sigint)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: None)

    def credits_loop():
        while True:
            try: st.credits = cm.compute(home)
            except Exception as ex: st.notice = (f"credits error: {ex}", time.time() + 5)
            time.sleep(15)

    def limits_loop():
        import limits as lm
        while True:
            try: st.limits = lm.compute(home)
            except Exception: pass
            time.sleep(20)

    def online_loop():
        while True:
            st.online = agents_online(); time.sleep(20)
    threading.Thread(target=credits_loop, daemon=True).start()
    threading.Thread(target=online_loop, daemon=True).start()
    threading.Thread(target=limits_loop, daemon=True).start()
    try:
        console.set_window_title("COCO — Claude + workers")
    except Exception:
        pass
    try:
        size = (console.size.width, console.size.height)
        lay, show = build_layout(*size)
        with Live(lay, console=console, screen=True, auto_refresh=False, transient=True) as live:
            while True:
                now = time.time()
                st.frame += 1
                if now - last_poll > .25:
                    for e in tail.poll(): st.handle(e)
                    last_poll = now
                    try: hb.write_text(str(now))
                    except OSError: pass
                if now - last_tasks > 1:
                    st.tasks = tail.tasks()[-60:]
                    last_tasks = now
                cur = (console.size.width, console.size.height)
                if cur != size:                       # terminal resized: re-flow the panels
                    size = cur
                    lay, show = build_layout(*size)
                    live.update(lay)
                    console.clear()
                ups = {
                    "head": lambda: Panel(header(st, size[0] >= 120), box=box.HEAVY, border_style="#2c3449", padding=(0, 0)),
                    "net": lambda: panel(net, "AGENT NETWORK  " + "  ".join(
                        f"[{COL[w]}]●[/] {w}" if st.online.get(w) else f"[#3a4258]○ {w}[/]" for w in WORKERS)),
                    "feed": lambda: panel(feed, f"LIVE ACTIVITY  [#566079]{len(st.feed)} events[/]"),
                    "tasks": lambda: panel(tasks, "TASKS"),
                    "credits": lambda: panel(cred, "CREDITS  [#566079]real tokens · $ at API list price[/]", "#5a4a2a"),
                    "limits": lambda: panel(lim, "USAGE LEFT  [#566079]live, real sources only[/]", "#2f4a3a"),
                    "board": lambda: panel(board, f"SHARED BLACKBOARD  [#566079]{len(st.board)}[/]"),
                }
                for name in show:
                    lay[name].update(ups[name]())
                live.refresh()
                busy = st.packets or any(t["state"] == "running" for t in st.tasks)
                time.sleep(1 / FPS if busy else 1 / IDLE_FPS)
    except KeyboardInterrupt:
        pass
