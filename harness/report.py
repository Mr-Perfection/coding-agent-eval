"""Render a run's metrics.jsonl as a readable, self-contained HTML page.

    python -m harness.report --run-id base_full            # single run
    python -m harness.report --baseline base_v1 --candidate idx_v1   # A/B

Writes runs/<id>/report.html (single) or runs/compare_<a>_<b>.html (A/B). The file
is fully self-contained (inline CSS, no network), theme-aware, and safe to open with
`open runs/<id>/report.html`.
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import datetime
from pathlib import Path

from .compare import load_metrics, load_resolved, apply_grades
from .metrics import aggregate

# ---------------------------------------------------------------- shared helpers


def _subset_meta() -> dict[str, dict]:
    """instance_id -> {repo, bucket, target_file} from the curated subset, if present."""
    out: dict[str, dict] = {}
    p = Path("tasks/indexing_subset.json")
    if p.exists():
        for t in json.loads(p.read_text()).get("tasks", []):
            out[t["instance_id"]] = t
    return out


def _load(run_id: str) -> tuple[list[dict], dict]:
    recs = load_metrics(run_id)
    apply_grades(recs, load_resolved(run_id))
    return recs, aggregate(recs)


def _fmt(v, spec="{}", dash="—"):
    return dash if v is None else spec.format(v)


def _resolved_cell(r: dict) -> str:
    v = r.get("resolved")
    if v is True:
        return '<td class="c ok">✅ pass</td>'
    if v is False:
        return '<td class="c bad">❌ fail</td>'
    return '<td class="c muted">— n/a</td>'


def _f1_cell(loc: dict) -> str:
    f1 = loc.get("f1", 0.0)
    cls = "ok" if f1 >= 0.99 else ("bad" if f1 == 0 else "warn")
    return f'<td class="c {cls}">{f1:.2f}</td>'


def _tools(tc: dict) -> str:
    if not tc:
        return '<span class="muted">—</span>'
    order = ["file_search", "file_read", "file_edit", "file_write", "shell"]
    keys = [k for k in order if k in tc] + [k for k in tc if k not in order]
    short = {"file_search": "search", "file_read": "read", "file_edit": "edit",
             "file_write": "write", "shell": "shell"}
    return " ".join(
        f'<span class="pill">{html.escape(short.get(k, k))} {tc[k]}</span>' for k in keys
    )


def _files(r: dict, meta: dict) -> str:
    loc = r["localization"]
    edited = loc.get("edited_files", [])
    gold = loc.get("gold_files", [])
    hit = set(edited) & set(gold)
    parts = []
    for f in edited:
        cls = "ok" if f in hit else "bad"
        parts.append(f'<span class="file {cls}">{html.escape(f)}</span>')
    edited_html = "<br>".join(parts) or '<span class="muted">(no edits)</span>'
    gold_html = "<br>".join(f'<span class="file muted">{html.escape(f)}</span>'
                            for f in gold)
    return f'<div class="files"><div>{edited_html}</div>' \
           f'<div class="goldwrap"><span class="lbl">gold:</span> {gold_html}</div></div>'


# ---------------------------------------------------------------------- styling

_CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--muted:#6b7280;--line:#e5e7eb;--card:#f9fafb;
  --ok:#059669;--okbg:#ecfdf5;--bad:#dc2626;--badbg:#fef2f2;--warn:#b45309;--warnbg:#fffbeb;
  --pill:#eef2ff;--pillfg:#3730a3;--accent:#4f46e5;}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e5e7eb;--muted:#9ca3af;
  --line:#232733;--card:#161923;--ok:#34d399;--okbg:#052e22;--bad:#f87171;--badbg:#2a0f12;
  --warn:#fbbf24;--warnbg:#2a1e05;--pill:#1e2233;--pillfg:#a5b4fc;--accent:#818cf8;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:28px}
h1{font-size:20px;margin:0 0 2px} .sub{color:var(--muted);margin-bottom:20px;font-size:13px}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:120px}
.card .k{color:var(--muted);font-size:12px} .card .v{font-size:20px;font-weight:600;margin-top:2px}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;min-width:820px}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{background:var(--card);font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted);position:sticky;top:0}
td.c{text-align:center;font-variant-numeric:tabular-nums;white-space:nowrap}
tr:last-child td{border-bottom:none}
.ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)} .muted{color:var(--muted)}
td.ok{background:var(--okbg)} td.bad{background:var(--badbg)} td.warn{background:var(--warnbg)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;background:var(--pill);color:var(--pillfg)}
.pill{display:inline-block;padding:1px 6px;margin:1px;border-radius:6px;font-size:11px;background:var(--pill);color:var(--pillfg);white-space:nowrap}
.file{display:inline;font-family:ui-monospace,monospace;font-size:12px}
.file.ok{color:var(--ok)} .file.bad{color:var(--bad)}
.files .goldwrap{margin-top:3px;font-size:11px} .lbl{color:var(--muted)}
.delta-good{color:var(--ok);font-weight:600} .delta-bad{color:var(--bad);font-weight:600}
.legend{color:var(--muted);font-size:12px;margin-top:16px}
.tid{font-weight:600} .repo{color:var(--muted);font-size:12px}
"""


def _doc(title: str, body: str) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
            f"<body>{body}</body></html>")


def _card(k: str, v: str) -> str:
    return f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div></div>'


# ------------------------------------------------------------------- single run


def render_single(run_id: str) -> str:
    recs, a = _load(run_id)
    meta = _subset_meta()
    fork = recs[0]["fork"] if recs else "?"

    cards = [
        _card("tasks", str(a.get("n", 0))),
        _card("resolved", f'{a.get("n_resolved",0)}/{a.get("n_graded",0)}'
              if a.get("resolution_rate") is not None else "—"),
        _card("resolution", _fmt(a.get("resolution_rate"), "{:.0%}")),
        _card("mean steps", _fmt(a.get("mean_turns"), "{:.1f}")),
        _card("mean search", _fmt(a.get("mean_search_calls"), "{:.1f}")),
        _card("total cost", "$" + _fmt(a.get("total_cost_usd"), "{:.2f}", "0")),
        _card("cost / solve", "$" + _fmt(a.get("cost_per_solve_usd"), "{:.3f}")
              if a.get("cost_per_solve_usd") is not None else "—"),
        _card("mean loc F1", _fmt(a.get("mean_localization_f1"), "{:.2f}")),
    ]

    rows = []
    for r in recs:
        m = meta.get(r["instance_id"], {})
        bucket = m.get("bucket", "")
        badge = f'<span class="badge">{html.escape(bucket)}</span>' if bucket else ""
        rows.append(
            "<tr>"
            f'<td><div class="tid">{html.escape(r["instance_id"])}</div>'
            f'<div class="repo">{html.escape(m.get("repo",""))}</div></td>'
            f"<td>{badge}</td>"
            f"{_resolved_cell(r)}"
            f"{_f1_cell(r['localization'])}"
            f'<td class="c mono">{_fmt(r.get("turns"))}</td>'
            f'<td class="c mono">{_fmt(r.get("search_calls"))}</td>'
            f"<td>{_tools(r.get('tool_calls', {}))}</td>"
            f'<td class="c mono">{"$"+_fmt(r.get("cost_usd"),"{:.4f}")}</td>'
            f'<td class="c mono">{_fmt(r.get("wall_clock_s"),"{:.0f}s")}</td>'
            f"<td>{_files(r, m)}</td>"
            "</tr>"
        )

    table = (
        '<div class="wrap"><table><thead><tr>'
        "<th>Task</th><th>Bucket</th><th>Resolved</th><th>Loc F1</th><th>Steps</th>"
        "<th>Search</th><th>Tools</th><th>Cost</th><th>Time</th>"
        "<th>Edited files (green=matches gold)</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )
    legend = (
        '<div class="legend">'
        "<b>Resolved</b> = official SWE-bench result — the repo's real test suite "
        "passes in Docker after applying the patch (no model involved). "
        "<b>Loc&nbsp;F1</b> <i>(our proxy, not a SWE-bench metric)</i> = file-level "
        "overlap of the agent's edits vs the gold patch: F1 of {edited files} against "
        "{gold files}. Green = edited the gold file, red = missed it. It captures "
        "whether the agent <i>found</i> the right file — the sub-skill indexing "
        "targets — but is only a proxy: an alternate valid fix can still score&nbsp;0. "
        "<b>Steps</b> = agent iterations. <b>Search</b> = file_search + file_read "
        "calls (retrieval effort indexing should reduce)."
        "</div>")
    body = (f"<h1>Eval report — <span class='mono'>{html.escape(run_id)}</span></h1>"
            f'<div class="sub">fork <b>{html.escape(fork)}</b> · '
            f'{datetime.now():%Y-%m-%d %H:%M}</div>'
            f'<div class="cards">{"".join(cards)}</div>{table}{legend}')
    return _doc(f"Eval report — {run_id}", body)


# ---------------------------------------------------------------------- A/B view


def _delta(base, cand, higher_better):
    if base is None or cand is None:
        return ""
    d = cand - base
    if d == 0:
        return '<span class="muted">±0</span>'
    good = (d > 0) == higher_better
    cls = "delta-good" if good else "delta-bad"
    return f'<span class="{cls}">{d:+.3f}</span>'


def render_ab(base_id: str, cand_id: str) -> str:
    _, b = _load(base_id)
    _, c = _load(cand_id)
    rows_spec = [
        ("Resolution rate", "resolution_rate", "{:.0%}", True),
        ("Mean steps", "mean_turns", "{:.1f}", False),
        ("Mean search calls", "mean_search_calls", "{:.1f}", False),
        ("Mean wall-clock (s)", "mean_wall_clock_s", "{:.0f}", False),
        ("Mean index build (s)", "mean_index_build_s", "{:.1f}", None),
        ("Mean cost ($)", "mean_cost_usd", "{:.4f}", False),
        ("Cost per solve ($)", "cost_per_solve_usd", "{:.4f}", False),
        ("Mean localization F1", "mean_localization_f1", "{:.3f}", True),
    ]
    trs = []
    for label, key, spec, hb in rows_spec:
        bv, cv = b.get(key), c.get(key)
        dcell = "" if hb is None else _delta(bv, cv, hb)
        trs.append(
            f"<tr><td>{label}</td>"
            f'<td class="c mono">{_fmt(bv, spec)}</td>'
            f'<td class="c mono">{_fmt(cv, spec)}</td>'
            f'<td class="c">{dcell}</td></tr>'
        )
    table = ('<div class="wrap"><table><thead><tr><th>Metric</th>'
             f'<th>{html.escape(base_id)}</th><th>{html.escape(cand_id)}</th>'
             '<th>Δ (cand−base)</th></tr></thead><tbody>'
             + "".join(trs) + "</tbody></table></div>")
    note = ('<div class="legend">Green Δ = candidate better (index helps); red = worse. '
            'The index-build row covers out-of-band builds only; the ultra-index fork '
            'indexes passively, so its one-time-per-repo build folds into wall-clock.</div>')
    body = (f"<h1>A/B — <span class='mono'>{html.escape(base_id)}</span> vs "
            f"<span class='mono'>{html.escape(cand_id)}</span></h1>"
            f'<div class="sub">baseline vs candidate · {datetime.now():%Y-%m-%d %H:%M}</div>'
            f"{table}{note}")
    return _doc(f"A/B — {base_id} vs {cand_id}", body)


# --------------------------------------------------------------------------- CLI


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id")
    ap.add_argument("--baseline")
    ap.add_argument("--candidate")
    ap.add_argument("--open", action="store_true", help="open the file after writing")
    args = ap.parse_args()

    if args.baseline and args.candidate:
        out = Path("runs") / f"compare_{args.baseline}_{args.candidate}.html"
        out.write_text(render_ab(args.baseline, args.candidate))
    elif args.run_id:
        out = Path("runs") / args.run_id / "report.html"
        out.write_text(render_single(args.run_id))
    else:
        raise SystemExit("Pass --run-id ID, or --baseline A --candidate B")

    print(f"Wrote {out}")
    if args.open:
        import subprocess
        subprocess.run(["open", str(out)], check=False)


if __name__ == "__main__":
    main()
