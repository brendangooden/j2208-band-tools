"""Render a day of band heart-rate history as a self-contained HTML chart.

Reads captures/history_hr.csv (and history_sleep.csv, if present, to shade the
sleep period). Writes one HTML file with no external requests.

    python tools/hr_chart.py --date 2026-07-27 --out captures/hr_today.html
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

W, H = 920, 360
M = {"top": 18, "right": 18, "bottom": 34, "left": 46}
PW = W - M["left"] - M["right"]
PH = H - M["top"] - M["bottom"]


def load_hr(path: Path, day: str) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            dt = r.get("device_time") or ""
            if dt.startswith(day) and r.get("value"):
                t = datetime.fromisoformat(dt)
                rows.append({"min": t.hour * 60 + t.minute + t.second / 60,
                             "hhmm": t.strftime("%H:%M"),
                             "bpm": int(r["value"])})
    rows.sort(key=lambda r: r["min"])
    return rows


def load_sleep(path: Path, day: str) -> list[tuple[float, float]]:
    """Sleep blocks that fall on `day`, as (start_min, end_min)."""
    if not path.exists():
        return []
    spans = []
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            dt = r.get("device_time") or ""
            if not dt.startswith(day) or not r.get("count"):
                continue
            t = datetime.fromisoformat(dt)
            start = t.hour * 60 + t.minute
            spans.append((start, start + int(r["count"])))
    if not spans:
        return []
    spans.sort()
    merged = [spans[0]]
    for s, e in spans[1:]:
        if s - merged[-1][1] <= 20:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _median(xs: list[int]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def flag_artifacts(rows: list[dict], dev: int = 30, half: int = 2) -> int:
    """Mark readings that sit far from their local median as suspect.

    Contact dropouts on this band come in bursts of one or two samples - a
    spike immediately followed by a crash - so comparing a point against its
    two immediate neighbours misses them: each is the other's neighbour.
    Comparing against the median of the surrounding window is robust to that,
    and it does not fire on genuine exertion, because a real climb drags the
    local median up with it.

    Flagged points stay on the chart and in the table; they are only excluded
    from the summary figures.
    """
    n = 0
    for i, r in enumerate(rows):
        window = [rows[j]["bpm"] for j in range(max(0, i - half),
                                                min(len(rows), i + half + 1))
                  if j != i]
        r["suspect"] = bool(window) and abs(r["bpm"] - _median(window)) > dev
        n += r["suspect"]
    return n


def build(rows: list[dict], sleep: list[tuple[float, float]], day: str) -> str:
    n_suspect = flag_artifacts(rows)
    clean = [r for r in rows if not r["suspect"]] or rows
    bpms = [r["bpm"] for r in rows]
    lo = (min(bpms) // 10) * 10
    hi = -(-max(bpms) // 10) * 10
    x_max = min(1440, (int(rows[-1]["min"]) // 60 + 1) * 60)

    def sx(m: float) -> float:
        return M["left"] + (m / x_max) * PW

    def sy(v: float) -> float:
        return M["top"] + PH - ((v - lo) / (hi - lo)) * PH

    pts = [(sx(r["min"]), sy(r["bpm"])) for r in rows]
    line = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = (f"M{pts[0][0]:.1f},{M['top']+PH:.1f} L"
            + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            + f" L{pts[-1][0]:.1f},{M['top']+PH:.1f} Z")

    y_ticks = list(range(int(lo), int(hi) + 1, 20 if hi - lo > 60 else 10))
    grid = "".join(
        f'<line class="grid" x1="{M["left"]}" x2="{M["left"]+PW}" '
        f'y1="{sy(t):.1f}" y2="{sy(t):.1f}"/>'
        f'<text class="tick" x="{M["left"]-8}" y="{sy(t)+4:.1f}" '
        f'text-anchor="end">{t}</text>'
        for t in y_ticks
    )
    step = 180 if x_max > 720 else 120
    xg = "".join(
        f'<text class="tick" x="{sx(m):.1f}" y="{M["top"]+PH+20}" '
        f'text-anchor="middle">{m//60:02d}:00</text>'
        for m in range(0, x_max + 1, step)
    )
    shade = "".join(
        f'<rect class="sleep" x="{sx(s):.1f}" y="{M["top"]}" '
        f'width="{max(0, sx(min(e, x_max))-sx(s)):.1f}" height="{PH}"/>'
        for s, e in sleep if s < x_max
    )
    sleep_label = ""
    if sleep:
        s, e = sleep[0]
        mid = sx(s) + (sx(min(e, x_max)) - sx(s)) / 2
        sleep_label = (f'<text class="tick" x="{mid:.1f}" y="{M["top"]+13}" '
                       f'text-anchor="middle">asleep</text>')

    lo_r = min(clean, key=lambda r: r["bpm"])
    hi_r = max(clean, key=lambda r: r["bpm"])
    marks = ""
    for r in (hi_r, lo_r):
        cx, cy = sx(r["min"]), sy(r["bpm"])
        # Keep the label inside the plot: below the point near the top edge,
        # above it everywhere else.
        dy = 18 if cy - M["top"] < 24 else -11
        anchor = "middle"
        if cx < M["left"] + 60:
            anchor = "start"
        elif cx > M["left"] + PW - 60:
            anchor = "end"
        marks += (f'<circle class="pt" cx="{cx:.1f}" cy="{cy:.1f}" r="4"/>'
                  f'<text class="peak" x="{cx:.1f}" y="{cy+dy:.1f}" '
                  f'text-anchor="{anchor}">{r["bpm"]} at {r["hhmm"]}</text>')
    for r in (x for x in rows if x["suspect"]):
        marks += (f'<circle class="susp" cx="{sx(r["min"]):.1f}" '
                  f'cy="{sy(r["bpm"]):.1f}" r="4"/>')

    mean = round(sum(r["bpm"] for r in clean) / len(clean))
    asleep = sorted(r["bpm"] for r in clean
                    if any(s <= r["min"] < e for s, e in sleep))
    if asleep:
        k = max(1, len(asleep) // 4)
        rest_val, rest_note = round(sum(asleep[:k]) / k), "lowest quartile asleep"
    else:
        rest_val, rest_note = lo_r["bpm"], f'at {lo_r["hhmm"]}'

    tiles = [
        ("Latest", f'{rows[-1]["bpm"]}', f'at {rows[-1]["hhmm"]}'),
        ("Average", f"{mean}", f"across {len(clean)} readings"),
        ("Resting", f"{rest_val}", rest_note),
        ("Peak", f'{hi_r["bpm"]}', f'at {hi_r["hhmm"]}'),
    ]
    tile_html = "".join(
        f'<div class="tile"><div class="tl">{a}</div>'
        f'<div class="tv">{b}<span class="tu">bpm</span></div>'
        f'<div class="ts">{c}</div></div>' for a, b, c in tiles
    )
    trows = "".join(
        f"<tr><td>{r['hhmm']}</td><td>{r['bpm']}</td>"
        f"<td>{'suspect' if r['suspect'] else ''}</td></tr>" for r in rows)
    try:
        # %-d is not portable to Windows; strip the leading zero by hand.
        nice = datetime.fromisoformat(day).strftime("%A %d %B %Y")
        nice = nice.replace(" 0", " ", 1)
    except ValueError:
        nice = day

    data_js = json.dumps([{"x": round(sx(r["min"]), 1), "y": round(sy(r["bpm"]), 1),
                           "t": r["hhmm"], "v": r["bpm"]} for r in rows])

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Heart rate &mdash; {nice}</title>
</head><body style="margin:0;background:#f9f9f7">
<style>
@media (prefers-color-scheme: dark) {{ body {{ background: #0d0d0d !important; }} }}
.viz-root {{
  color-scheme: light;
  --surface-1: #fcfcfb; --plane: #f9f9f7;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --series-1: #2a78d6;
  --border: rgba(11,11,11,0.10); --sleep: rgba(42,120,214,0.07);
  background: var(--plane); color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  padding: 20px; border-radius: 12px;
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) .viz-root {{
    color-scheme: dark;
    --surface-1: #1a1a19; --plane: #0d0d0d;
    --text-primary: #fff; --text-secondary: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --series-1: #3987e5;
    --border: rgba(255,255,255,0.10); --sleep: rgba(57,135,229,0.10);
  }}
}}
:root[data-theme="dark"] .viz-root {{
  color-scheme: dark;
  --surface-1: #1a1a19; --plane: #0d0d0d;
  --text-primary: #fff; --text-secondary: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --series-1: #3987e5;
  --border: rgba(255,255,255,0.10); --sleep: rgba(57,135,229,0.10);
}}
.viz-root h1 {{ font-size: 1.15rem; margin: 0 0 2px; font-weight: 600; }}
.viz-root .sub {{ color: var(--text-secondary); font-size: .82rem; margin-bottom: 16px; }}
.tiles {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; }}
.tile {{ flex: 1 1 130px; background: var(--surface-1); border: 1px solid var(--border);
        border-radius: 10px; padding: 10px 12px; }}
.tl {{ font-size: .72rem; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }}
.tv {{ font-size: 1.7rem; font-weight: 600; line-height: 1.15; }}
.tu {{ font-size: .8rem; font-weight: 400; color: var(--text-secondary); margin-left: 4px; }}
.ts {{ font-size: .74rem; color: var(--text-secondary); }}
.chartbox {{ background: var(--surface-1); border: 1px solid var(--border);
             border-radius: 10px; padding: 8px 6px 2px; overflow-x: auto; }}
svg {{ display: block; width: 100%; height: auto; min-width: 560px; }}
.grid {{ stroke: var(--grid); stroke-width: 1; }}
.axis {{ stroke: var(--axis); stroke-width: 1; }}
.tick {{ fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }}
.sleep {{ fill: var(--sleep); }}
.ln {{ fill: none; stroke: var(--series-1); stroke-width: 2;
       stroke-linejoin: round; stroke-linecap: round; }}
.ar {{ fill: var(--series-1); opacity: .10; }}
.pt {{ fill: var(--series-1); stroke: var(--surface-1); stroke-width: 2; }}
.susp {{ fill: var(--surface-1); stroke: var(--muted); stroke-width: 2; }}
.peak {{ fill: var(--text-secondary); font-size: 11px; font-weight: 500; }}
.cross {{ stroke: var(--axis); stroke-width: 1; stroke-dasharray: 3 3; opacity: 0; }}
.hov {{ fill: var(--series-1); stroke: var(--surface-1); stroke-width: 2; opacity: 0; }}
.tip {{ position: absolute; pointer-events: none; opacity: 0; transform: translate(-50%,-130%);
        background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
        padding: 5px 9px; font-size: .78rem; white-space: nowrap;
        box-shadow: 0 2px 10px rgba(0,0,0,.14); color: var(--text-primary); }}
.wrap {{ position: relative; }}
.note {{ margin-top: 10px; font-size: .76rem; color: var(--text-secondary); }}
details {{ margin-top: 12px; }}
summary {{ cursor: pointer; color: var(--text-secondary); font-size: .82rem; }}
table {{ border-collapse: collapse; margin-top: 8px; font-size: .8rem;
         font-variant-numeric: tabular-nums; }}
th, td {{ border-bottom: 1px solid var(--border); padding: 3px 14px 3px 0; text-align: left; }}
</style>

<div class="viz-root">
  <h1>Heart rate &mdash; {nice}</h1>
  <div class="sub">{len(rows)} readings from the band, {rows[0]['hhmm']}&ndash;{rows[-1]['hhmm']}.
    Logged by the band itself; shaded band is the recorded sleep period.</div>

  <div class="tiles">{tile_html}</div>

  <div class="chartbox"><div class="wrap">
    <svg viewBox="0 0 {W} {H}" role="img"
         aria-label="Heart rate over {nice}, ranging {min(bpms)} to {max(bpms)} bpm">
      {shade}{grid}{sleep_label}
      <line class="axis" x1="{M['left']}" x2="{M['left']+PW}"
            y1="{M['top']+PH}" y2="{M['top']+PH}"/>
      <path class="ar" d="{area}"/>
      <path class="ln" d="{line}"/>
      {marks}{xg}
      <line class="cross" id="cx" y1="{M['top']}" y2="{M['top']+PH}"/>
      <circle class="hov" id="hv" r="5"/>
      <rect x="{M['left']}" y="{M['top']}" width="{PW}" height="{PH}"
            fill="transparent" id="hit"/>
    </svg>
    <div class="tip" id="tip"></div>
  </div></div>

  {f'<div class="note">Hollow marker = {n_suspect} isolated reading'
    f'{"s" if n_suspect != 1 else ""} far from both neighbours, almost '
    f'certainly a contact dropout. Plotted, but excluded from the figures '
    f'above.</div>' if n_suspect else ''}

  <details>
    <summary>View as table</summary>
    <table><thead><tr><th>Time</th><th>bpm</th><th></th></tr></thead><tbody>{trows}</tbody></table>
  </details>
</div>

<script>
(function() {{
  const D = {data_js};
  const svg = document.currentScript.parentElement.querySelector('svg');
  const hit = svg.querySelector('#hit'), cx = svg.querySelector('#cx');
  const hv = svg.querySelector('#hv'), tip = document.getElementById('tip');
  function move(ev) {{
    const r = svg.getBoundingClientRect(), vb = svg.viewBox.baseVal;
    const px = (ev.clientX - r.left) / r.width * vb.width;
    let best = D[0], bd = Infinity;
    for (const d of D) {{ const dd = Math.abs(d.x - px); if (dd < bd) {{ bd = dd; best = d; }} }}
    cx.setAttribute('x1', best.x); cx.setAttribute('x2', best.x);
    cx.style.opacity = 1;
    hv.setAttribute('cx', best.x); hv.setAttribute('cy', best.y); hv.style.opacity = 1;
    tip.style.opacity = 1;
    tip.style.left = (best.x / vb.width * r.width) + 'px';
    tip.style.top = (best.y / vb.height * r.height) + 'px';
    tip.innerHTML = '<b>' + best.v + ' bpm</b> &middot; ' + best.t;
  }}
  function out() {{ cx.style.opacity = 0; hv.style.opacity = 0; tip.style.opacity = 0; }}
  hit.addEventListener('mousemove', move);
  hit.addEventListener('mouseleave', out);
  hit.addEventListener('touchmove', e => {{ move(e.touches[0]); e.preventDefault(); }},
                       {{passive: false}});
}})();
</script>
</body></html>
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--hr", default="captures/history_hr.csv")
    p.add_argument("--sleep", default="captures/history_sleep.csv")
    p.add_argument("--out", default=None)
    a = p.parse_args()

    rows = load_hr(Path(a.hr), a.date)
    if not rows:
        print(f"no heart-rate records for {a.date} in {a.hr}")
        return 1
    sleep = load_sleep(Path(a.sleep), a.date)
    out = Path(a.out or f"captures/hr_{a.date}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(rows, sleep, a.date), encoding="utf-8")
    print(f"{len(rows)} readings, {len(sleep)} sleep span(s) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
