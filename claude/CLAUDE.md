<!-- C.O.C.A -->
# COCO: Claude Orchestrating Cheaper Agents

Goal: minimise Claude usage. When the user says **"use coco"** (or mentions COCO), route the work through COCO; even when they don't, do so for token-heavy work (web research, reading many pages or files, boilerplate code, first-pass reviews, bulk rewrites). Use the `coco` skill:

    coco run <research|browse|code|review|bigread|simple|local> "<precise task>" [-C dir]
    (same as: python {AB} run ...)

Run independent subtasks in parallel with `coco par jobs.json`. You plan, verify and decide; workers execute. Read only the returned SUMMARY; open full output (`coco show <id>`) only when needed. Shared findings live in `~/.agentbus/BOARD.md`. The live dashboard is `coco dash`.

Exception: if your prompt says you are a COCO worker agent, never delegate. Do the task yourself.
<!-- /C.O.C.A -->
