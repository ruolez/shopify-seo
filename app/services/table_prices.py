"""Remove store prices and stock status from content tables.

Prices and stock typed into descriptions go stale on every change while the
theme and the Product schema always show live values, so tables lose their
"Price" / "Availability" rows (key/value spec tables) and columns
(comparison tables).
Product names that mention a manufacturer pre-price ("$1.49 Pre-Priced") are
not labels and stay.
"""

import html as html_lib
import re

_TABLE_RE = re.compile(r"<table\b.*?</table\s*>", re.S | re.I)
_ROW_RE = re.compile(r"\s*<tr\b.*?</tr\s*>", re.S | re.I)
_CELL_RE = re.compile(r"\s*<(t[dh])\b[^>]*>(.*?)</\1\s*>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_MERGED_RE = re.compile(r"\b(?:colspan|rowspan)\s*=", re.I)
_PRICE_LABEL_RE = re.compile(
    r"^(?:(?:typical |avg\.? |average )?price(?: range| point| per [a-z]+)?"
    r"|availability|stock|in stock\??|stock status|stock availability)$", re.I
)


def _is_price_label(cell_html):
    text = html_lib.unescape(_TAG_RE.sub(" ", cell_html)).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip().rstrip(":").strip()
    return bool(_PRICE_LABEL_RE.match(text))


def _strip_table(table):
    """Returns (new_table, rows_removed, columns_removed, skip_reason)."""
    if len(re.findall(r"<table\b", table, re.I)) > 1:
        return table, 0, 0, "nested"

    rows = list(_ROW_RE.finditer(table))
    cells = [list(_CELL_RE.finditer(r.group(0))) for r in rows]

    price_rows = {i for i, c in enumerate(cells) if len(c) >= 2 and _is_price_label(c[0].group(2))}
    if price_rows:
        if len(price_rows) == len(rows):
            return table, 0, 0, "only_price"
        new_rows = ["" if i in price_rows else r.group(0) for i, r in enumerate(rows)]
        return _rebuild(table, rows, new_rows), len(price_rows), 0, None

    header = next((c for c in cells if c), [])
    price_cols = [j for j, cell in enumerate(header) if j >= 1 and _is_price_label(cell.group(2))]
    if not price_cols:
        return table, 0, 0, None
    if _MERGED_RE.search(table) or any(len(c) != len(header) for c in cells if c):
        return table, 0, 0, "merged_cells"

    new_rows = []
    for row, row_cells in zip(rows, cells):
        text = row.group(0)
        for j in sorted(price_cols, reverse=True):
            if j < len(row_cells):
                cell = row_cells[j]
                text = text[:cell.start()] + text[cell.end():]
        new_rows.append(text)
    return _rebuild(table, rows, new_rows), 0, len(price_cols), None


def _rebuild(table, rows, new_rows):
    out, pos = [], 0
    for row, new_row in zip(rows, new_rows):
        out.append(table[pos:row.start()])
        out.append(new_row)
        pos = row.end()
    out.append(table[pos:])
    return "".join(out)


def strip_table_prices(html):
    """Returns {html, rows, columns, skipped}: the content without price rows
    and price columns, how many of each went, and why any price table was
    left alone (nested | only_price | merged_cells)."""
    html = html or ""
    totals = {"rows": 0, "columns": 0, "skipped": []}
    out, pos = [], 0
    for match in _TABLE_RE.finditer(html):
        new_table, rows, columns, skipped = _strip_table(match.group(0))
        out.append(html[pos:match.start()])
        out.append(new_table)
        pos = match.end()
        totals["rows"] += rows
        totals["columns"] += columns
        if skipped:
            totals["skipped"].append(skipped)
    out.append(html[pos:])
    return {"html": "".join(out), **totals}
