---
name: coco
description: COCO (Claude Orchestrating Cheaper Agents). Use whenever the user says "use coco" or mentions COCO, and proactively to Offload token-heavy work (web research, browsing URLs, bulk/boilerplate coding, code review, summarising large files, simple rewrites) to cheaper worker agents (codex, Antigravity agy, opencode free models, local ollama) via the agentbus dispatcher, so Claude spends fewer credits. Use proactively whenever a subtask is self-contained and doesn't need Claude-level judgment.
---

# COCO: delegate to worker agents

Claude = planner + reviewer. Workers do the heavy lifting. Only the worker's short SUMMARY comes back into context.

## Commands
```bash
python {AB} run <role> "<task>" [-C <dir>] [-a <agent> [-m <model>]] [-t secs] [-n max_chars]
python {AB} run <role> @task.md          # long task text from a file
python {AB} par jobs.json -j 4           # parallel: [{"role","task","cwd"?,"agent"?,"model"?,"label"?}]
python {AB} show <id>                    # full output (only if the summary is not enough)
python {AB} board | log | credits        # shared findings · bus timeline · Claude $ vs saved
python {AB} models [list|set <agent> <role|default> <model>]
python {AB} up | doctor                  # start everything · real smoke test of every agent
```

## Roles → best agent first, auto-fallback on failure/quota
| role | use for | chain (model) |
|---|---|---|
| research | multi-source web research | codex → agy (gemini-3.8-flash-high) → opencode |
| browse | read specific URLs, extract facts | opencode (free) → codex → agy |
| code | implement/boilerplate/refactor/tests in `-C <repo>` | codex (sandboxed, effort high) → agy (claude-opus-4-6-thinking) → opencode |
| review | read-only review of a diff/dir | codex (effort high) → agy (claude-opus-4-6-thinking) → opencode |
| bigread | summarise big files, logs, unfamiliar repos | agy (gemini, huge context) → opencode → codex |
| simple | rewrite, classify, extract, format, translate | ollama (local OmniCoder 9B) → opencode → agy |
| local | private data — never leaves the machine | ollama only |

agy is slow (1–3 min); in tests codex gave the most precise reviews and research. Pin an agent/model with `-a <agent> -m <model>` when needed.

## Rules
- Delegate when the subtask is self-contained: searching, reading many pages/files, generating boilerplate, first-pass reviews, repetitive edits. Keep for Claude: final decisions, tricky debugging, security-sensitive or public-facing text, anything the user must approve.
- Write a precise task: goal, exact inputs (paths/URLs), constraints, and what "done" looks like. Workers have no conversation context.
- Run independent subtasks with `par`. Use `run_in_background` for long jobs (agy, big code tasks).
- Verify: for `code`, check `git diff --stat` and run the tests yourself; spot-check research claims that matter.
- Don't `show` full output unless the summary is insufficient; that defeats the purpose.
- Workers must never push, publish, post, or touch credentials. Say so in the task if relevant.
- The dashboard (Windows Terminal window "agentbus") auto-opens, auto-reopens while tasks run, and ollama auto-starts with the D:\DevCache model folder.

