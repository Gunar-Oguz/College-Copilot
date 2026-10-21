"""Server-rendered /admin page: usage, response times, thumbs-down answers, common questions."""

from html import escape

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>CollegeCopilot Admin</title>
<style>
  :root {{ --bg: #f5f6fb; --surface: #fff; --border: #e3e6ef; --text: #111827; --muted: #616a7d;
          --brand: #3b3fd8; --brand-dark: #1e1b6b; --down: #be123c; --up: #047857; }}
  * {{ box-sizing: border-box; margin: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg);
         color: var(--text); }}
  header {{ background: linear-gradient(120deg, var(--brand-dark), var(--brand)); color: #fff; padding: 16px; }}
  header div, main {{ max-width: 1100px; margin: 0 auto; }}
  header h1 {{ font-size: 1.15rem; }}
  header p {{ font-size: 0.8rem; opacity: 0.8; margin-top: 2px; }}
  main {{ padding: 20px 16px 48px; display: flex; flex-direction: column; gap: 20px; }}
  .tiles {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; }}
  .tile {{ background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 14px 16px; }}
  .tile .label {{ font-size: 0.78rem; color: var(--muted); }}
  .tile .value {{ font-size: 1.6rem; font-weight: 700; letter-spacing: -0.02em; margin-top: 2px;
                 font-variant-numeric: tabular-nums; }}
  .tile .sub {{ font-size: 0.75rem; color: var(--muted); margin-top: 2px; }}
  .up {{ color: var(--up); }} .down {{ color: var(--down); }}
  section {{ background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 16px; }}
  h2 {{ font-size: 0.95rem; margin-bottom: 10px; }}
  .wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }}
  th {{ font-size: 0.75rem; color: var(--muted); font-weight: 600; white-space: nowrap; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  td.when {{ white-space: nowrap; color: var(--muted); }}
  tr:last-child td {{ border-bottom: none; }}
  details summary {{ cursor: pointer; color: var(--brand); font-size: 0.8rem; }}
  pre {{ white-space: pre-wrap; font-size: 0.75rem; background: #f4f5fa; padding: 8px; border-radius: 8px;
        margin-top: 6px; max-height: 240px; overflow: auto; }}
  .empty {{ color: var(--muted); font-size: 0.85rem; }}
  .kind {{ font-size: 0.72rem; padding: 1px 7px; border-radius: 999px; background: #eceeff; white-space: nowrap; }}
  .kind.error, .kind.cannot_answer, .kind.no_results {{ background: #fff1f2; color: var(--down); }}
</style>
</head>
<body>
<header><div><h1>CollegeCopilot Admin</h1><p>Usage and answer quality. Times are database server time (UTC on Render).</p></div></header>
<main>
  <div class="tiles">{tiles}</div>
  <section><h2>Recent 👎 answers to review</h2>{thumbs_down}</section>
  <section><h2>Most common questions</h2>{common}</section>
  <section><h2>Latest questions</h2>{recent}</section>
</main>
</body>
</html>"""


def _tile(label: str, value, sub: str = "", cls: str = "") -> str:
    return (f'<div class="tile"><div class="label">{escape(label)}</div>'
            f'<div class="value {cls}">{escape(str(value))}</div><div class="sub">{escape(sub)}</div></div>')


def _ms(value) -> str:
    return "—" if value is None else f"{int(value) / 1000:.1f}s"


def _when(value) -> str:
    return value.strftime("%b %d, %H:%M") if value else ""


def _kind(kind) -> str:
    return f'<span class="kind {escape(kind or "")}">{escape((kind or "").replace("_", " "))}</span>'


def _table(headers: list[str], rows: list[str]) -> str:
    if not rows:
        return '<p class="empty">Nothing yet.</p>'
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def render(stats: dict) -> str:
    t, v = stats["totals"], stats["votes"]
    rated = (v["up"] or 0) + (v["down"] or 0)
    tiles = "".join([
        _tile("Questions today", f"{t['today']:,}"),
        _tile("Questions all time", f"{t['all_time']:,}"),
        _tile("Avg response time", _ms(t["avg_ms"]), f"today {_ms(t['avg_ms_today'])}"),
        _tile("👍 Helpful", f"{v['up']:,}", f"{v['up'] / rated:.0%} of votes" if rated else "no votes yet", "up"),
        _tile("👎 Not helpful", f"{v['down']:,}", "", "down"),
        _tile("Couldn't answer", f"{t['unanswered']:,}", f"{t['errors']:,} errors"),
    ])
    thumbs_down = _table(["When", "Question", "Answer"], [
        f'<tr><td class="when">{_when(r["created_at"])}</td><td>{escape(r["question"] or "")} {_kind(r["kind"])}</td>'
        f'<td>{escape(r["answer"] or "")}'
        + (f'<details><summary>SQL / tool call</summary><pre>{escape(r["sql_generated"])}</pre></details>'
           if r["sql_generated"] else "")
        + "</td></tr>"
        for r in stats["thumbs_down"]
    ])
    common = _table(["Question", "Times asked", "Avg time"], [
        f'<tr><td>{escape(r["question"] or "")}</td><td class="num">{r["times"]:,}</td>'
        f'<td class="num">{_ms(r["avg_ms"])}</td></tr>'
        for r in stats["common"]
    ])
    recent = _table(["When", "Question", "Result", "Time"], [
        f'<tr><td class="when">{_when(r["created_at"])}</td><td>{escape(r["question"] or "")}</td>'
        f'<td>{_kind(r["kind"])}</td><td class="num">{_ms(r["response_time_ms"])}</td></tr>'
        for r in stats["recent"]
    ])
    return PAGE.format(tiles=tiles, thumbs_down=thumbs_down, common=common, recent=recent)
