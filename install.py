#!/usr/bin/env python3
"""Install C.O.C.A into Claude Code.

  python install.py            # skill + CLAUDE.md rule + settings (allow rule, session-start hook)
  python install.py --dry-run  # show what would change
  python install.py --no-settings

Everything is idempotent and every file it touches is backed up first (*.coca-bak).
"""
import argparse, json, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AB = (ROOT / "coca" / "ab.py").as_posix()
CLAUDE = Path.home() / ".claude"


def backup(p: Path, dry):
    if p.exists() and not dry:
        shutil.copy2(p, p.with_name(p.name + ".coca-bak"))


def write(p: Path, text: str, dry):
    print(("would write " if dry else "wrote ") + str(p))
    if not dry:
        p.parent.mkdir(parents=True, exist_ok=True)
        backup(p, dry)
        p.write_text(text, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-settings", action="store_true")
    a = ap.parse_args()

    try:
        import rich  # noqa: F401
    except ImportError:
        print("installing rich …")
        if not a.dry_run:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])

    # 1. delegate skill
    skill = (ROOT / "claude/skills/coco/SKILL.md").read_text(encoding="utf-8").replace("{AB}", AB)
    for d in (CLAUDE / "skills/coco", Path.home() / ".codex/skills/coco", Path.home() / ".agents/skills/coco"):
        write(d / "SKILL.md", skill, a.dry_run)   # Claude Code, Codex, and other agents that read ~/.agents/skills

    # 2. global CLAUDE.md rule (replaced in place on re-install)
    snippet = (ROOT / "claude/CLAUDE.md").read_text(encoding="utf-8").replace("{AB}", AB).strip()
    md = CLAUDE / "CLAUDE.md"
    cur = md.read_text(encoding="utf-8") if md.exists() else ""
    if "<!-- C.O.C.A -->" in cur:
        start, end = cur.index("<!-- C.O.C.A -->"), cur.index("<!-- /C.O.C.A -->") + len("<!-- /C.O.C.A -->")
        new = cur[:start] + snippet + cur[end:]
    else:
        new = (cur.rstrip() + "\n\n" if cur.strip() else "") + snippet + "\n"
    write(md, new, a.dry_run)

    # 3. settings.json: allow the dispatcher + start everything when a session starts
    if not a.no_settings:
        sp = CLAUDE / "settings.json"
        s = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
        allow = s.setdefault("permissions", {}).setdefault("allow", [])
        for tool in ("Bash", "PowerShell"):
            for rule in (f"{tool}(python {AB} *)", f"{tool}(coco *)"):
                if rule not in allow: allow.append(rule)
        hooks = s.setdefault("hooks", {}).setdefault("SessionStart", [])
        cmd = f"python {AB} up"
        if not any(h.get("command") == cmd for g in hooks for h in g.get("hooks", [])):
            hooks.append({"hooks": [{"type": "command", "command": cmd, "async": True, "timeout": 90}]})
        if "statusLine" not in s:   # feeds Claude's real 5h/weekly limits to the dashboard
            s["statusLine"] = {"type": "command", "command": f"python {AB} statusline"}
        write(sp, json.dumps(s, indent=2) + "\n", a.dry_run)

    # 4. `coco` command in ~/.local/bin (add it to PATH if it isn't)
    b = Path.home() / ".local/bin"
    write(b / "coco.cmd", f'@echo off\npython "{AB}" %*\n', a.dry_run)
    write(b / "coco", f'#!/bin/sh\nexec python "{AB}" "$@"\n', a.dry_run)

    print("\nDone. Say \"use coco\" in Claude Code or Codex, or run: coco doctor")


if __name__ == "__main__":
    main()
