"""Static dashboard (SPEC 6.2): one self-contained ``site/index.html``.

Charts are SVG rendered here, with a little inline script for the hover
crosshair. No external files, fonts or scripts.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import display
from .metrics import HOUR_MS, WINDOWS, Calculator, MetricRow, Value

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "site" / "index.html"

CHART_HOURS = 14 * 24  # charts show the last 14 days

# Chart geometry, in SVG user units.
W, H = 600, 240
ML, MR, MT, MB = 56, 72, 14, 28


@dataclass
class Context:
    """Everything the page shows, gathered once."""

    calc: Calculator
    latest: dict[str, MetricRow | None]
    now: datetime
    last_success: str | None
    summary_block: int | None
    summary_ts: int | None
    stats_24h: dict[str, int]
    stats_all: dict[str, int]
    threshold_pct: Decimal | None


def gather(store, calc: Calculator, config: Mapping[str, Any], now: datetime | None = None) -> Context:
    now = now or datetime.now(timezone.utc)
    latest = store.latest_summary()
    threshold = config.get("alerts", {}).get("liveRatioThresholdPctPerDay")
    return Context(
        calc=calc,
        latest={g: calc.latest(g) for g in WINDOWS},
        now=now,
        last_success=store.last_success(),
        summary_block=latest["source_block"] if latest else None,
        summary_ts=latest["source_ts"] if latest else None,
        stats_24h=store.attempt_stats((now - timedelta(hours=24)).isoformat()),
        stats_all=store.attempt_stats(),
        threshold_pct=Decimal(str(threshold)) if threshold is not None else None,
    )


# Charts -------------------------------------------------------------------

def nice_ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    """Round tick values covering [lo, hi]."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / count
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks = [round(start, 12)]
    while ticks[-1] < hi:  # the top tick must reach the maximum
        ticks.append(round(start + len(ticks) * step, 12))
    return ticks


def _tick_label(v: float, kind: str) -> str:
    """Comma-grouped, no scientific notation, no trailing zeros: 10,000 / 2.5 / 0.0025."""
    places = 0 if v == 0 else max(0, min(8, 3 - math.floor(math.log10(abs(v)))))
    text = f"{abs(v):,.{places}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    sign = "-" if v < 0 else ""
    if kind == "pct":
        return f"{sign}{text}%"
    if kind in ("usd", "usd_small"):
        return f"{sign}${text}"
    return sign + text


def _time_ticks(t0: int, t1: int) -> list[int]:
    """Midnight UTC ticks, or every 6 h when the span is under two days."""
    step = 24 * HOUR_MS if t1 - t0 >= 48 * HOUR_MS else 6 * HOUR_MS
    first = (t0 + step - 1) // step * step
    return list(range(first, t1 + 1, step))


def line_chart(
    chart_id: str,
    title: str,
    subtitle: str,
    points: Sequence[tuple[int, Value]],
    kind: str,
    metric: str,
    reference: tuple[float, str] | None = None,
) -> str:
    """A single-series line chart card, or an empty-state card if nothing is plottable."""
    head = f'<h3>{escape(title)}</h3><p class="sub">{escape(subtitle)}</p>'
    vals = [(ts, float(v.value)) for ts, v in points if v.ok]
    if not vals:
        note = next((v.note for _, v in reversed(points) if v.note), "n/a")
        return f'<div class="card chart-card">{head}<div class="empty">{escape(note)}</div></div>'

    t0, t1 = points[0][0], points[-1][0]
    ys = [y for _, y in vals] + ([reference[0]] if reference else [])
    ticks = nice_ticks(min(0.0, min(ys)), max(ys))
    y_lo, y_hi = ticks[0], ticks[-1]

    def sx(ts: int) -> float:
        return ML + (W - ML - MR) * ((ts - t0) / (t1 - t0) if t1 > t0 else 0.5)

    def sy(y: float) -> float:
        return MT + (H - MT - MB) * (1 - (y - y_lo) / (y_hi - y_lo))

    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{escape(title)}" tabindex="0">']
    for t in ticks:
        y = sy(t)
        parts.append(f'<line class="grid" x1="{ML}" x2="{W - MR}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{ML - 8}" y="{y + 4:.1f}" text-anchor="end">{escape(_tick_label(t, kind))}</text>')
    base = sy(max(y_lo, min(0.0, y_hi)))
    parts.append(f'<line class="axis" x1="{ML}" x2="{W - MR}" y1="{base:.1f}" y2="{base:.1f}"/>')
    for t in _time_ticks(t0, t1):
        x = sx(t)
        dt = datetime.fromtimestamp(t / 1000, timezone.utc)
        label = f"{dt:%b} {dt.day}" if t % (24 * HOUR_MS) == 0 else f"{dt:%H:%M}"
        parts.append(f'<text class="tick" x="{x:.1f}" y="{H - 8}" text-anchor="middle">{escape(label)}</text>')
    if reference:
        ry = sy(reference[0])
        parts.append(f'<line class="ref" x1="{ML}" x2="{W - MR}" y1="{ry:.1f}" y2="{ry:.1f}"/>')
        # Label at the left end, above the line, clear of the end-value label on the right.
        parts.append(f'<text class="tick" x="{ML + 4}" y="{ry - 4:.1f}">{escape(reference[1])}</text>')

    # Split the line wherever a value is missing.
    segments: list[list[tuple[float, float]]] = [[]]
    for ts, v in points:
        if v.ok:
            segments[-1].append((sx(ts), sy(float(v.value))))
        elif segments[-1]:
            segments.append([])
    for seg in filter(None, segments):
        line = " ".join(f"{x:.1f},{y:.1f}" for x, y in seg)
        area = f"M{seg[0][0]:.1f},{base:.1f} L" + " L".join(f"{x:.1f},{y:.1f}" for x, y in seg) + f" L{seg[-1][0]:.1f},{base:.1f} Z"
        parts.append(f'<path class="area" d="{area}"/>')
        parts.append(f'<polyline class="line" points="{line}"/>')
    end_ts, end_y = vals[-1]
    ex, ey = sx(end_ts), sy(end_y)
    end_text = display.fmt_number(Decimal(str(end_y)), kind)
    parts.append(f'<circle class="dot" cx="{ex:.1f}" cy="{ey:.1f}" r="4"/>')
    parts.append(f'<text class="end" x="{ex + 8:.1f}" y="{ey + 4:.1f}">{escape(end_text)}</text>')
    parts.append(f'<line class="cross" x1="0" x2="0" y1="{MT}" y2="{H - MB}" visibility="hidden"/>')
    parts.append(f'<circle class="hover-dot" r="4" visibility="hidden"/>')
    parts.append("</svg>")

    data = [
        [round(sx(ts), 1), round(sy(float(v.value)), 1) if v.ok else None,
         display.fmt(metric, v), display.utc(ts)]
        for ts, v in points
    ]
    rows = "".join(
        f"<tr><td>{escape(display.utc(ts))}</td><td>{escape(display.fmt(metric, v))}</td></tr>"
        for ts, v in reversed(points)
    )
    return (
        f'<div class="card chart-card">{head}'
        f'<div class="chart" id="{chart_id}" data-points="{escape(json.dumps(data))}">{"".join(parts)}'
        f'<div class="tip" hidden><strong></strong><span></span></div></div>'
        f'<details><summary>Show data</summary><div class="scroll"><table class="data">'
        f"<thead><tr><th>Hour starting</th><th>{escape(title)}</th></tr></thead><tbody>{rows}</tbody></table></div></details>"
        f"</div>"
    )


def _series(ctx: Context, metric: str, granularity: str) -> list[tuple[int, Value]]:
    """The metric for each closed hour in the last ``CHART_HOURS``."""
    calc = ctx.calc
    closed = [i for i in range(len(calc.ts)) if not calc.partial[i]][-CHART_HOURS:]
    return [(calc.ts[i], calc.row(i, granularity).values[metric]) for i in closed]


# Page ---------------------------------------------------------------------

def _tile(label: str, metric: str, row: MetricRow | None, extra: str = "", hero: bool = False) -> str:
    v = row.values[metric] if row else Value(None, "n/a — no data")
    cls = "tile hero" if hero else "tile"
    val_cls = "value" if v.ok else "value na"
    extra_html = f'<div class="extra">{escape(extra)}</div>' if extra else ""
    return (
        f'<div class="{cls}"><div class="label">{escape(label)}</div>'
        f'<div class="{val_cls}">{escape(display.fmt(metric, v))}</div>{extra_html}</div>'
    )


def _table(ctx: Context) -> str:
    head = "".join(f"<th>{escape(display.WINDOW_LABELS[g])}</th>" for g in WINDOWS)
    body = []
    for section, metrics_ in (("Headline", display.HEADLINE), ("Diagnostics", display.DIAGNOSTICS)):
        body.append(f'<tr class="section"><th colspan="{len(WINDOWS) + 1}">{section}</th></tr>')
        for m in metrics_:
            cells = []
            for g in WINDOWS:
                row = ctx.latest[g]
                v = row.values[m] if row else Value(None, "n/a — no data")
                cells.append(f'<td class="{"" if v.ok else "na"}">{escape(display.fmt(m, v))}</td>')
            body.append(f"<tr><th>{escape(display.LABELS[m][0])}</th>{''.join(cells)}</tr>")
    return (
        f'<div class="scroll"><table class="diag"><thead><tr><th>Metric</th>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div><p class="sub">{escape(display.WINDOW_NOTE)}</p>'
    )


def _rate(stats: dict[str, int]) -> str:
    total = stats["total"]
    if not total:
        return "no attempts"
    errors = stats["server_errors"]
    return (
        f"{total:,} attempts, {errors:,} returned 5xx ({errors / total:.1%}), "
        f"{stats['no_response']:,} without a response"
    )


def render(ctx: Context) -> str:
    row24 = ctx.latest["24h"]
    latest_ts = row24.ts if row24 else None
    last_success_age = (
        (ctx.now - datetime.fromisoformat(ctx.last_success)).total_seconds() if ctx.last_success else None
    )
    flags = sorted({f for r in ctx.latest.values() if r for f in r.flags})

    tiles = "".join([
        _tile("Live ratio, last 24 h", "live_ratio_pct_per_day", row24,
              f"Dilution {display.fmt('dilution_pct_per_day', row24.values['dilution_pct_per_day']) if row24 else 'n/a'}",
              hero=True),
        _tile("Payback", "payback_days", row24),
        _tile("Cost per PAPER", "cost_per_paper", row24,
              "measured" if row24 and row24.values["cost_measured"].ok else "marginal (formula)"),
        _tile("Reward per staked PAPER per day", "reward_per_staked_per_day", row24),
    ])
    ref = None
    if ctx.threshold_pct is not None:
        ref = (float(ctx.threshold_pct), f"alert {ctx.threshold_pct}%")
    charts = "".join([
        line_chart("c-rewards", "Rewards flow", "Staking rewards per hour", _series(ctx, "rewards", "1h"), "usd", "rewards"),
        line_chart("c-minted", "PAPER minted", "Per hour", _series(ctx, "minted", "1h"), "amount", "minted"),
        line_chart("c-dilution", "Dilution rate", "%/day, rolling 24 h", _series(ctx, "dilution_pct_per_day", "24h"), "pct", "dilution_pct_per_day"),
        line_chart("c-ratio", "Live ratio", "%/day, rolling 24 h", _series(ctx, "live_ratio_pct_per_day", "24h"), "pct", "live_ratio_pct_per_day", ref),
    ])
    flag_html = (
        '<section><h2>Assumptions in these numbers</h2><ul class="flags">'
        + "".join(f"<li>{escape(f)}</li>" for f in flags)
        + "</ul></section>"
    ) if flags else ""
    closed = f"{display.utc(latest_ts)} to {display.utc(latest_ts + HOUR_MS)[11:]}" if latest_ts else "none yet"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PAPER Tracker</title>
<style>{CSS}</style>
</head>
<body>
<main>
<header>
  <h1>Is minting PAPER paying for itself?</h1>
  <p class="sub">Live ratio = staker rewards per staked PAPER per day ÷ cost to mint one PAPER. Latest closed hour: {escape(closed)}.</p>
</header>
<section class="tiles">{tiles}</section>
<p class="sub">Read the live ratio next to the dilution rate: a ratio below dilution means your share shrinks faster than it pays.</p>
{flag_html}
<section><h2>Last 14 days</h2><div class="charts">{charts}</div></section>
<section><h2>Headline and diagnostics</h2>{_table(ctx)}</section>
<footer>
  <p>Last successful poll: {escape(display.age(last_success_age) + (f" ({ctx.last_success})" if ctx.last_success else ""))}.
  Latest summary: block {escape(str(ctx.summary_block or "n/a"))} at {escape(display.utc(ctx.summary_ts))}.</p>
  <p>HTTP, last 24 h: {escape(_rate(ctx.stats_24h))}. All time: {escape(_rate(ctx.stats_all))}.</p>
  <p>Generated {escape(ctx.now.strftime("%Y-%m-%d %H:%M UTC"))}. Data: Papertrade public dashboard endpoints.</p>
</footer>
</main>
<script>{JS}</script>
</body>
</html>
"""


def build(store, calc: Calculator, config: Mapping[str, Any], out: Path = DEFAULT_OUT, now: datetime | None = None) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(gather(store, calc, config, now)), encoding="utf-8")
    return out


CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10); --series: #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10); --series: #3987e5;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10); --series: #3987e5;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1120px; margin: 0 auto; padding: 24px 16px 40px; }
h1 { font-size: 24px; margin: 0 0 4px; font-weight: 600; }
h2 { font-size: 17px; margin: 32px 0 12px; font-weight: 600; }
h3 { font-size: 15px; margin: 0; font-weight: 600; }
.sub { color: var(--ink-2); margin: 4px 0 0; font-size: 13px; }
.card, .tile { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-top: 20px; }
.tile { padding: 16px; }
.tile .label { color: var(--ink-2); font-size: 13px; }
.tile .value { font-size: 26px; font-weight: 600; margin-top: 6px; overflow-wrap: anywhere; }
.tile.hero .value { font-size: 48px; line-height: 1.1; }
.tile .value.na { font-size: 17px; font-weight: 500; color: var(--muted); }
.tile.hero .value.na { font-size: 22px; }
.tile .extra { color: var(--ink-2); font-size: 13px; margin-top: 6px; }
.flags { margin: 0; padding-left: 20px; color: var(--ink-2); font-size: 13px; }
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 460px), 1fr)); gap: 12px; }
.chart-card { padding: 16px; min-width: 0; }
.chart { position: relative; margin-top: 8px; }
.chart svg { display: block; width: 100%; height: auto; outline: none; touch-action: pan-y; }
.chart svg:focus-visible { outline: 2px solid var(--series); outline-offset: 2px; border-radius: 4px; }
.empty { color: var(--muted); padding: 48px 0; text-align: center; }
.grid { stroke: var(--grid); stroke-width: 1; }
.axis { stroke: var(--axis); stroke-width: 1; }
.ref { stroke: var(--muted); stroke-width: 1; }
.tick { fill: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.line { fill: none; stroke: var(--series); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.area { fill: var(--series); opacity: 0.1; }
.dot, .hover-dot { fill: var(--series); stroke: var(--surface); stroke-width: 2; }
.end { fill: var(--ink); font-size: 13px; font-weight: 600; }
.cross { stroke: var(--muted); stroke-width: 1; }
.tip { position: absolute; top: 0; transform: translateX(-50%); pointer-events: none; white-space: nowrap;
  background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 4px 8px;
  font-size: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.12); }
.tip strong { display: block; color: var(--ink); }
.tip span { color: var(--ink-2); }
details { margin-top: 8px; font-size: 13px; color: var(--ink-2); }
summary { cursor: pointer; }
.scroll { overflow-x: auto; }
details .scroll { max-height: 240px; overflow-y: auto; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--grid); }
table.diag { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; font-size: 14px; }
table.diag td { text-align: right; white-space: nowrap; }
table.diag thead th:not(:first-child) { text-align: right; }
table.diag tbody th { font-weight: 400; }
tr.section th { font-weight: 600; padding-top: 14px; color: var(--ink-2); }
td.na { color: var(--muted); }
footer { margin-top: 32px; color: var(--ink-2); font-size: 13px; }
footer p { margin: 4px 0; }
"""

JS = """
document.querySelectorAll('.chart').forEach(function (chart) {
  var pts = JSON.parse(chart.dataset.points);
  var svg = chart.querySelector('svg');
  var cross = svg.querySelector('.cross');
  var dot = svg.querySelector('.hover-dot');
  var tip = chart.querySelector('.tip');
  var current = pts.length - 1;
  function show(i) {
    current = Math.max(0, Math.min(pts.length - 1, i));
    var p = pts[current];
    cross.setAttribute('x1', p[0]); cross.setAttribute('x2', p[0]);
    cross.setAttribute('visibility', 'visible');
    if (p[1] === null) { dot.setAttribute('visibility', 'hidden'); }
    else { dot.setAttribute('cx', p[0]); dot.setAttribute('cy', p[1]); dot.setAttribute('visibility', 'visible'); }
    tip.querySelector('strong').textContent = p[2];
    tip.querySelector('span').textContent = p[3];
    tip.hidden = false;
    var w = svg.viewBox.baseVal.width;
    var left = Math.max(15, Math.min(85, p[0] / w * 100));
    tip.style.left = left + '%';
  }
  function hide() {
    cross.setAttribute('visibility', 'hidden'); dot.setAttribute('visibility', 'hidden'); tip.hidden = true;
  }
  function nearest(evt) {
    var pt = svg.createSVGPoint(); pt.x = evt.clientX; pt.y = evt.clientY;
    var x = pt.matrixTransform(svg.getScreenCTM().inverse()).x;
    var best = 0;
    for (var i = 1; i < pts.length; i++) {
      if (Math.abs(pts[i][0] - x) < Math.abs(pts[best][0] - x)) best = i;
    }
    return best;
  }
  svg.addEventListener('pointermove', function (e) { show(nearest(e)); });
  svg.addEventListener('pointerleave', hide);
  svg.addEventListener('focus', function () { show(current); });
  svg.addEventListener('blur', hide);
  svg.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowLeft') { show(current - 1); e.preventDefault(); }
    if (e.key === 'ArrowRight') { show(current + 1); e.preventDefault(); }
  });
});
"""
