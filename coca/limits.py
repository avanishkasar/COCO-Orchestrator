"""Live usage limits, only from real sources. Anything without a real source is not shown.

  codex   rate_limits from the newest Codex session rollout (~/.codex/sessions), written by every codex run
  claude  rate_limits Claude Code passes to the status line (captured by `coco statusline`)
  ollama  models loaded right now (/api/ps), local so no quota
"""
import json, time, urllib.request
from pathlib import Path


def _newest(root: Path, pattern: str, max_files=400):
    try:
        files = sorted(root.rglob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        return files[:max_files]
    except Exception:
        return []


def codex():
    root = Path.home() / ".codex" / "sessions"
    for f in _newest(root, "*.jsonl", 8):
        try:
            with open(f, "rb") as fh:
                fh.seek(0, 2); size = fh.tell(); fh.seek(max(0, size - 400_000))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except Exception:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line: continue
            try:
                rl = json.loads(line)["payload"]["rate_limits"]
            except Exception:
                continue
            if not rl or not rl.get("primary"): continue
            return {"asof": f.stat().st_mtime, "plan": rl.get("plan_type"),
                    "windows": [(w["window_minutes"], w["used_percent"], w.get("resets_at"))
                                for w in (rl.get("primary"), rl.get("secondary")) if w]}
    return None


def claude(home: Path):
    f = home / "claude_status.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        rl = d.get("rate_limits") or {}
    except Exception:
        return None
    wins = []
    for key, mins in (("five_hour", 300), ("seven_day", 10080)):
        w = rl.get(key)
        if isinstance(w, dict) and w.get("used_percentage") is not None:
            wins.append((mins, float(w["used_percentage"]), w.get("resets_at")))
    return {"asof": f.stat().st_mtime, "windows": wins} if wins else None


def ollama():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=1.5) as r:
            ms = json.load(r).get("models", [])
        return {"asof": time.time(), "loaded": [(m["name"].split("/")[-1][:28], m.get("size_vram", 0)) for m in ms]}
    except Exception:
        return None


def compute(home: Path):
    return {"codex": codex(), "claude": claude(home), "ollama": ollama()}
