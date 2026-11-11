"""COCO smart selection: which model (per agent) and which agent order fits a task.

  tier_for(role, task)        light | standard | heavy | max, from the role and what the task asks for
  pick_model(agent, role, t)  the agent's model for that tier (user overrides in models.json always win)
  smart_order(chain, home)    reorders a fallback chain using real signals only:
                                - 5-hour quota left (agents that report it)
                                - last health ping (agent failed its warm-up recently)
                                - recent success rate from the event bus
Nothing here is guessed: quota comes from the agents' own logs, health and success from COCO's own records.
"""
import json, os, re, time
from pathlib import Path

LOW_QUOTA = float(os.environ.get("COCO_LOW_QUOTA", "25"))    # % of the 5-hour window left -> demote the agent
SAVE_QUOTA = float(os.environ.get("COCO_SAVE_QUOTA", "45"))  # below this, use one tier lighter on that agent
TIER_NAMES = ["light", "standard", "heavy", "max"]

# model per tier. codex entries are "model@reasoning-effort". "<tier>_<role>" overrides a tier for one role.
TIERS = {
    "codex": {"light": "gpt-6-luna@low", "standard": "gpt-6.1-sol@medium", "heavy": "gpt-6.1-sol@high",
              "max": "gpt-6-astra@high"},
    "agy": {"light": "gemini-3.8-flash-low", "standard": "gemini-3.8-flash-medium", "heavy": "gemini-3.8-flash-high",
            "heavy_code": "claude-opus-4-6-thinking", "heavy_review": "claude-opus-4-6-thinking",
            "max": "claude-opus-4-6-thinking"},
    "opencode": {"light": "opencode/deepseek-v4.1-flash-free", "standard": "opencode/deepseek-v4.1-flash-free",
                 "heavy": "opencode/deepseek-v4.1-flash-free", "max": "opencode/deepseek-v4.1-flash-free"},
    "ollama": {"light": "auto-small", "standard": "auto", "heavy": "auto", "max": "auto"},
}
# a second model to try on the same agent when the first one fails for a non-quota reason
ALT_MODEL = {"opencode": "opencode/glm-5.3-flash-free", "codex": "gpt-6-sol@medium"}

ROLE_BASE = {"simple": 0, "local": 0, "browse": 0, "research": 1, "bigread": 1, "code": 2, "review": 2}
HARD_RE = re.compile(r"\b(architect\w*|refactor\w*|concurren\w*|race condition|deadlock|security|vulnerab\w*|root cause|"
                     r"debug\w*|migrat\w*|optimi[sz]\w*|performance|algorithm\w*|prove|proof|design|trade-?offs?|"
                     r"compare|evaluate|in depth|thorough\w*|end[- ]to[- ]end|multi-?step|whole (repo|codebase))\b", re.I)
EASY_RE = re.compile(r"\b(rename|typo|format|reformat|list|extract|classify|translate|rewrite|shorten|summari[sz]e briefly|"
                     r"one[- ]line|tagline|title|bullet)\b", re.I)


def tier_for(role, task):
    s = ROLE_BASE.get(role, 1)
    hard = len(set(m.group(0).lower() for m in HARD_RE.finditer(task)))
    if hard: s += 1
    if hard >= 3 or len(task) > 2500: s += 1
    if len(task) < 200 and EASY_RE.search(task) and not hard: s -= 1
    cap = 3 if role in ("code", "review") else 2          # the frontier model is only for hard code/review work
    return TIER_NAMES[max(0, min(cap, s))]


def pick_model(agent, role, tier, user_conf=None, left=None):
    """Returns (model, tier actually used). A model the user set with `coco models set` always wins."""
    u = (user_conf or {}).get(agent, {})
    if u.get(role): return u[role], "user"
    if u.get("default") and agent != "ollama": return u["default"], "user"
    i = TIER_NAMES.index(tier)
    if left is not None and left < SAVE_QUOTA and i > 0: i -= 1   # running low: spend the cheaper model
    t = TIERS.get(agent, {})
    used = TIER_NAMES[i]
    return t.get(f"{used}_{role}", t.get(used, "")), used


# ───────────────────────── health + history ─────────────────────────
def health(home: Path):
    try: return json.loads((home / "health.json").read_text(encoding="utf-8"))
    except Exception: return {}


def set_health(home: Path, agent, ok, note=""):
    h = health(home)
    h[agent] = {"ok": bool(ok), "t": time.time(), "note": note[:200]}
    (home / "health.json").write_text(json.dumps(h, indent=1), encoding="utf-8")


LAST_FAIL = {}


def success_rates(home: Path, window=48 * 3600, last=12):
    """agent -> (successes, attempts) over its most recent attempts. Warm-up pings are not counted."""
    import datetime as dt
    per = {}
    try: lines = (home / "bus.jsonl").read_text(encoding="utf-8").splitlines()[-3000:]
    except Exception: return {}
    for line in lines:
        try: e = json.loads(line)
        except ValueError: continue
        if e.get("ev") not in ("done", "fail") or not e.get("agent") or str(e.get("id", "")).startswith(("warm-", "doctor-")):
            continue
        try: ts = dt.datetime.fromisoformat(e["t"]).timestamp()
        except Exception: continue
        if time.time() - ts > window: continue
        per.setdefault(e["agent"], []).append(e["ev"] == "done")
        if e["ev"] == "fail": LAST_FAIL[e["agent"]] = ts
    return {a: (sum(v[-last:]), len(v[-last:])) for a, v in per.items()}


def smart_order(chain, home: Path, left=None):
    """Returns (new chain, {agent: reason}) with weak agents moved to the end (never removed)."""
    left = left or {}
    why = {}
    h, rates = health(home), success_rates(home)
    for a in chain:
        if left.get(a, 100) < LOW_QUOTA:
            why[a] = f"{left[a]:.0f}% of 5h quota left"
        elif a in h and not h[a]["ok"] and time.time() - h[a]["t"] < 1800:
            why[a] = "failed its last health check"
        elif a in h and h[a]["ok"] and h[a]["t"] > LAST_FAIL.get(a, 0):
            pass                                   # it has passed a health check since its last failure: trust it again
        elif a in rates and rates[a][1] >= 4 and rates[a][0] / rates[a][1] < 0.5:
            why[a] = f"only {rates[a][0]}/{rates[a][1]} recent tasks succeeded"
    if not why or len(why) == len(chain): return list(chain), {}
    return [a for a in chain if a not in why] + [a for a in chain if a in why], why
