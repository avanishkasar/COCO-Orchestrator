<!-- C.O.C.A -->
# Credit saving: delegate to worker agents

Goal: minimise Claude usage. Before doing token-heavy work yourself (web research, reading many pages or files, boilerplate code, first-pass reviews, bulk rewrites), hand it to a worker with the `delegate` skill:

    python {AB} run <research|browse|code|review|bigread|simple|local> "<precise task>" [-C dir]

Run independent subtasks in parallel with `ab.py par`. You plan, verify, and decide; workers execute. Read only the returned SUMMARY; open full output (`ab.py show <id>`) only when needed. Shared findings live in `~/.agentbus/BOARD.md`.
<!-- /C.O.C.A -->
