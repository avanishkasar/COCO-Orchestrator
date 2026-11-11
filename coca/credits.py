"""Real Claude usage vs. work offloaded to workers.

CLAUDE BURN  = every assistant message in ~/.claude/projects/**/*.jsonl (Claude Code's own
               transcripts, including subagents), deduplicated by message id, using the exact
               token counts the API returned, priced at Anthropic's API list prices.
WORKER WORK  = the token counts each worker CLI reported for tasks run through agentbus
               (bus.jsonl "done"/"fail" events), priced as if Claude's main model had
               processed those same tokens. That second number is a counterfactual: it is
               what the offloaded tokens would have cost on Claude, not money that was charged.

Subscription plans (Pro/Max) don't bill per token, so dollar figures are API-equivalent values;
the token counts themselves are exact.
"""
import datetime as dt, json, re, threading
from pathlib import Path

# $ per million tokens: (input, output, cache_read). Cache writes: 1.25x input (5 min), 2x input (1 h).
PRICES = {
    "claude-fable-5-1": (10, 50, .25), "claude-fable-5": (10, 50, .25), "claude-mythos-5-1": (10, 50, .25),
    "claude-opus-5-5": (4, 20, .20), "claude-opus-5": (5, 25, .50), "claude-opus-4-8": (5, 25, .50),
    "claude-opus-4-7": (5, 25, .50), "claude-opus-4-6": (5, 25, .50), "claude-opus-4-5": (5, 25, .50),
    "claude-sonnet-5-5": (2, 10, .20), "claude-sonnet-5": (2, 10, .20), "claude-sonnet-4-6": (3, 15, .30),
    "claude-sonnet-4-5": (3, 15, .30), "claude-haiku-5-5": (.10, .50, .01), "claude-haiku-4-5": (1, 5, .10),
}
FALLBACK = "claude-opus-5-5"
_lock = threading.Lock()


def price_of(model):
    m = re.sub(r"-\d{8}$", "", (model or "").lower().replace("[1m]", "").strip())
    if m in PRICES: return PRICES[m], True
    for k in sorted(PRICES, key=len, reverse=True):
        if m.startswith(k): return PRICES[k], True
    return PRICES[FALLBACK], False


def cost(model, inp, out, cw5, cw1h, cr):
    (pi, po, pcr), _ = price_of(model)
    return (inp * pi + out * po + cw5 * pi * 1.25 + cw1h * pi * 2 + cr * pcr) / 1e6


def _scan_claude(home: Path):
    """Incrementally parse Claude Code transcripts; cache offsets + per-message usage."""
    cache_f = home / "claude_usage.json"
    try: cache = json.loads(cache_f.read_text(encoding="utf-8"))
    except Exception: cache = {"files": {}, "msgs": {}}
    root = Path.home() / ".claude" / "projects"
    for f in root.rglob("*.jsonl"):
        key, size = str(f), f.stat().st_size
        off = cache["files"].get(key, 0)
        if size < off: off = 0
        if size == off: continue
        with open(f, "rb") as fh:
            fh.seek(off); chunk = fh.read()
        cut = chunk.rfind(b"\n") + 1
        cache["files"][key] = off + cut
        for raw in chunk[:cut].split(b"\n"):
            if b'"usage"' not in raw or b'"assistant"' not in raw: continue
            try: e = json.loads(raw)
            except ValueError: continue
            msg = e.get("message") or {}
            u, mid, model = msg.get("usage"), msg.get("id"), msg.get("model", "")
            if not u or not mid or not model.startswith("claude"): continue  # skip <synthetic> and local models
            cc = u.get("cache_creation") or {}
            cw1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
            cw5 = cc.get("ephemeral_5m_input_tokens", (u.get("cache_creation_input_tokens", 0) or 0) - cw1h) or 0
            day = (e.get("timestamp") or "")[:10]
            if day:
                try:
                    day = dt.datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).astimezone().date().isoformat()
                except ValueError:
                    pass
            prev = cache["msgs"].get(mid)
            row = [day, model, u.get("input_tokens", 0) or 0, u.get("output_tokens", 0) or 0, cw5, cw1h,
                   u.get("cache_read_input_tokens", 0) or 0, Path(key).parent.name]
            if not prev or row[3] >= prev[3]:
                cache["msgs"][mid] = row
    tmp = cache_f.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    tmp.replace(cache_f)
    return cache["msgs"]


def _scan_workers(home: Path):
    rows = []
    bus = home / "bus.jsonl"
    if not bus.exists(): return rows
    for line in bus.read_text(encoding="utf-8").splitlines():
        try: e = json.loads(line)
        except ValueError: continue
        if str(e.get("id", "")).startswith(("warm-", "doctor-")): continue   # health pings are not "work saved"
        if e.get("ev") in ("done", "fail") and isinstance(e.get("usage"), dict) and isinstance(e.get("t"), str) and e.get("agent"):
            u = e["usage"]
            rows.append({"day": e["t"][:10], "agent": e["agent"], "ok": e["ev"] == "done", "in": u.get("in", 0),
                         "out": u.get("out", 0), "cr": u.get("cache_read", 0), "cw": u.get("cache_write", 0),
                         "returned": e.get("returned_chars", 0)})
    return rows


def compute(home: Path):
    with _lock:
        msgs = _scan_claude(home)
    today = dt.date.today().isoformat()
    first_bus = None
    bus = home / "bus.jsonl"
    if bus.exists():
        with open(bus, encoding="utf-8") as f:
            line = f.readline()
            try: first_bus = json.loads(line)["t"][:10]
            except Exception: pass

    def agg(filter_fn):
        r = {"in": 0, "out": 0, "cw": 0, "cr": 0, "usd": 0.0, "msgs": 0, "by_model": {}, "unpriced": set()}
        for mid, (day, model, i, o, c5, c1, cr, proj) in msgs.items():
            if not filter_fn(day): continue
            usd = cost(model, i, o, c5, c1, cr)
            r["in"] += i; r["out"] += o; r["cw"] += c5 + c1; r["cr"] += cr; r["usd"] += usd; r["msgs"] += 1
            r["by_model"][model] = r["by_model"].get(model, 0) + usd
            if not price_of(model)[1]: r["unpriced"].add(model)
        return r

    claude_today = agg(lambda d: d == today)
    claude_since = agg(lambda d: first_bus is not None and d >= first_bus)
    claude_all = agg(lambda d: True)
    main_model = max(claude_since["by_model"] or claude_all["by_model"] or {FALLBACK: 1}, key=lambda k: (claude_since["by_model"] or claude_all["by_model"] or {FALLBACK: 1})[k])

    workers = _scan_workers(home)
    (pi, po, pcr), _ = price_of(main_model)

    def wagg(filter_fn):
        r = {"in": 0, "out": 0, "cr": 0, "tasks": 0, "usd_equiv": 0.0, "by_agent": {}, "returned_tok": 0}
        for w in workers:
            if not filter_fn(w["day"]): continue
            # failed attempts did burn worker tokens but saved Claude nothing
            usd = (w["in"] * pi + w["out"] * po + w["cr"] * pcr + w["cw"] * pi * 1.25) / 1e6 if w["ok"] else 0.0
            r["in"] += w["in"]; r["out"] += w["out"]; r["cr"] += w["cr"]; r["usd_equiv"] += usd
            r["tasks"] += 1 if w["ok"] else 0
            r["returned_tok"] += w["returned"] // 4
            a = r["by_agent"].setdefault(w["agent"], {"tok": 0, "usd_equiv": 0.0, "tasks": 0})
            a["tok"] += w["in"] + w["out"] + w["cr"] + w["cw"]; a["usd_equiv"] += usd; a["tasks"] += 1 if w["ok"] else 0
        # Claude still has to read each returned summary: subtract that from the saving
        r["net_saved"] = max(0.0, r["usd_equiv"] - r["returned_tok"] * pi / 1e6)
        return r

    return {
        "today": today, "since": first_bus, "main_model": main_model,
        "claude_today": claude_today, "claude_since": claude_since, "claude_all": claude_all,
        "workers_today": wagg(lambda d: d == today), "workers_since": wagg(lambda d: True),
    }


def fmt_tok(n):
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(int(n))


def report_text(home: Path):
    c = compute(home)
    ct, cs, ca, wt, ws = c["claude_today"], c["claude_since"], c["claude_all"], c["workers_today"], c["workers_since"]
    tot = lambda r: r["in"] + r["out"] + r["cw"] + r["cr"]
    pct = lambda s, b: f"{100 * s / (s + b):.0f}%" if s + b > 0 else "—"
    lines = [
        "CLAUDE (exact tokens from Claude Code transcripts, API list-price $)",
        f"  today        {fmt_tok(tot(ct)):>8} tokens  ${ct['usd']:.2f}   ({ct['msgs']} responses)",
        f"  since agentbus ({c['since']}) {fmt_tok(tot(cs)):>8} tokens  ${cs['usd']:.2f}",
        f"  all history  {fmt_tok(tot(ca)):>8} tokens  ${ca['usd']:.2f}",
        f"  main model   {c['main_model']}",
        "WORKERS (exact tokens reported by each CLI; $ = same tokens at Claude prices)",
        f"  today        {fmt_tok(wt['in'] + wt['out'] + wt['cr']):>8} tokens  ${wt['usd_equiv']:.2f} equiv  net saved ${wt['net_saved']:.2f}  ({wt['tasks']} tasks)",
        f"  since start  {fmt_tok(ws['in'] + ws['out'] + ws['cr']):>8} tokens  ${ws['usd_equiv']:.2f} equiv  net saved ${ws['net_saved']:.2f}  ({ws['tasks']} tasks)",
        "  by agent     " + "  ".join(f"{a}: {fmt_tok(v['tok'])} (${v['usd_equiv']:.2f})" for a, v in ws["by_agent"].items()),
        f"OFFLOAD SHARE  today {pct(wt['net_saved'], ct['usd'])}   since start {pct(ws['net_saved'], cs['usd'])}",
    ]
    if ca["unpriced"]:
        lines.append(f"  (models priced at {FALLBACK} rates because not in table: {', '.join(sorted(ca['unpriced']))})")
    return "\n".join(lines)
