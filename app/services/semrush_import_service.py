import csv
import io
import json
import logging
from datetime import datetime, timedelta, timezone

from app.database import db

logger = logging.getLogger(__name__)

COLUMN_SIGNATURES = {
    "position_tracking": {
        "required": {"keyword", "position", "search volume"},
        "optional": {"previous position", "cpc", "url", "traffic", "traffic cost", "serp features", "timestamp", "traffic (%)"},
    },
    "position_tracking_overview": {
        "required": {"keyword"},
        "optional": {"tags", "intents", "search volume", "cpc"},
    },
    "organic_research": {
        "required": {"keyword", "position", "traffic"},
        "optional": {"previous position", "volume", "search volume", "traffic (%)", "url", "cpc", "competition", "number of results"},
    },
    "site_audit": {
        "required": {"page url"},
        "optional": {"issue type", "issue", "severity", "description", "category", "status code", "crawl depth", "internal links in", "external links in"},
    },
    "keyword_gap": {
        "required": {"keyword", "volume"},
        "optional": {"cpc", "competition", "keyword difficulty", "common authority"},
    },
}

COLUMN_ALIASES = {
    "vol.": "volume",
    "vol": "volume",
    "search vol.": "search volume",
    "pos.": "position",
    "prev. pos.": "previous position",
    "previous pos.": "previous position",
    "kd": "keyword difficulty",
    "kd%": "keyword difficulty",
    "com.": "competition",
    "competition density": "competition",
    "results": "number of results",
    "sf": "serp features",
    "serp features": "serp features",
    "traffic %": "traffic (%)",
    "url": "url",
    "page": "page url",
    "source url": "page url",
    "status code": "status code",
    "crawl depth": "crawl depth",
    "internal links in": "internal links in",
    "external links in": "external links in",
    "markup": "markup",
    "meta description": "meta description",
    "title": "title",
    "content type": "content type",
}


def _normalize_header(h):
    h = h.strip().lower().replace("\ufeff", "").replace("_", " ")
    return COLUMN_ALIASES.get(h, h)


def detect_delimiter(sample):
    lines = sample.split("\n")[:5]
    counts = {",": 0, ";": 0, "\t": 0}
    for line in lines:
        for d in counts:
            counts[d] += line.count(d)
    return max(counts, key=counts.get) if max(counts.values()) > 0 else ","


def _detect_date_columns(headers):
    """Detect Semrush Position Tracking Overview format with date-stamped columns.
    Columns look like: *.DOMAIN.COM/*_YYYYMMDD, *.DOMAIN.COM/*_YYYYMMDD_TYPE, *.DOMAIN.COM/*_YYYYMMDD_LANDING
    Returns dict with latest date's column indices or None."""
    import re
    date_cols = {}
    for i, h in enumerate(headers):
        m = re.match(r'.*_(\d{8})$', h.strip())
        if m:
            date = m.group(1)
            if date not in date_cols:
                date_cols[date] = {"position": i}
            else:
                date_cols[date]["position"] = i
        m_type = re.match(r'.*_(\d{8})_TYPE$', h.strip(), re.IGNORECASE)
        if m_type:
            date = m_type.group(1)
            if date not in date_cols:
                date_cols[date] = {}
            date_cols[date]["type"] = i
        m_landing = re.match(r'.*_(\d{8})_LANDING$', h.strip(), re.IGNORECASE)
        if m_landing:
            date = m_landing.group(1)
            if date not in date_cols:
                date_cols[date] = {}
            date_cols[date]["landing"] = i

    if not date_cols:
        return None

    dates_sorted = sorted(date_cols.keys(), reverse=True)
    latest = dates_sorted[0]
    previous = dates_sorted[1] if len(dates_sorted) > 1 else None

    result = {"latest_date": latest, "latest_cols": date_cols[latest], "all_dates": dates_sorted}
    if previous:
        result["previous_date"] = previous
        result["previous_cols"] = date_cols[previous]
    return result


def _build_column_map_for_type(headers, csv_type):
    """Build a column map against a specific signature — used when the caller
    forces a type over the detected one (the detected type's map would import
    garbage or zero rows)."""
    normalized = [_normalize_header(h) for h in headers]
    norm_set = set(normalized)
    sig = COLUMN_SIGNATURES.get(csv_type)
    if not sig:
        return {}
    col_map = {}
    for req in sig["required"]:
        if req in norm_set:
            col_map[req] = normalized.index(req)
        else:
            for i, n in enumerate(normalized):
                if req in n:
                    col_map[req] = i
                    break
    for opt in sig["optional"]:
        if opt in norm_set:
            col_map[opt] = normalized.index(opt)
    return col_map


def detect_csv_type(headers):
    normalized = [_normalize_header(h) for h in headers]
    norm_set = set(normalized)

    date_info = _detect_date_columns(headers)
    if date_info and "keyword" in norm_set:
        col_map = {"keyword": normalized.index("keyword")}
        if "tags" in norm_set:
            col_map["tags"] = normalized.index("tags")
        if "intents" in norm_set:
            col_map["intents"] = normalized.index("intents")
        if "search volume" in norm_set:
            col_map["search volume"] = normalized.index("search volume")
        if "cpc" in norm_set:
            col_map["cpc"] = normalized.index("cpc")
        col_map["_date_info"] = date_info
        return {"type": "position_tracking_overview", "confidence": 0.95, "column_map": col_map}

    best_type = None
    best_score = 0
    best_map = {}

    for csv_type, sig in COLUMN_SIGNATURES.items():
        if csv_type == "position_tracking_overview":
            continue
        score = 0
        col_map = {}
        for req in sig["required"]:
            if req in norm_set:
                score += 2
                col_map[req] = normalized.index(req)
            else:
                for i, n in enumerate(normalized):
                    if req in n:
                        score += 1
                        col_map[req] = i
                        break
        for opt in sig["optional"]:
            if opt in norm_set:
                score += 1
                col_map[opt] = normalized.index(opt)

        if score > best_score:
            best_score = score
            best_type = csv_type
            best_map = col_map

    min_required = len(COLUMN_SIGNATURES.get(best_type, {}).get("required", set())) if best_type else 0
    matched_required = sum(1 for r in COLUMN_SIGNATURES.get(best_type, {}).get("required", set()) if r in best_map)

    if matched_required < min_required:
        return {"type": "unknown", "confidence": 0, "column_map": {}}

    confidence = min(best_score / (min_required * 2 + 3), 1.0)
    return {"type": best_type, "confidence": round(confidence, 2), "column_map": best_map}


def _find_header_row(text, delimiter):
    """Semrush CSVs often have metadata lines before the actual column headers.
    Find the row that looks like column headers by checking for known column names."""
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    known_cols = {"keyword", "position", "search volume", "volume", "url", "page url",
                  "traffic", "cpc", "competition", "issue", "severity", "query",
                  "status code", "crawl depth", "internal links in", "page"}

    lines = []
    for i, row in enumerate(reader):
        lines.append(row)
        if i > 15:
            break

    best_row = 0
    best_score = 0
    for i, row in enumerate(lines):
        norm = {_normalize_header(c) for c in row if c.strip()}
        score = len(norm & known_cols)
        if score > best_score:
            best_score = score
            best_row = i

    return best_row, lines


def parse_csv_preview(file_content, max_rows=10):
    try:
        text = file_content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = file_content.decode("latin-1")

    delimiter = detect_delimiter(text)
    header_row_idx, initial_lines = _find_header_row(text, delimiter)

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    for _ in range(header_row_idx):
        next(reader, None)

    headers = next(reader, [])
    detection = detect_csv_type(headers)

    rows = []
    total = 0
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        total += 1
        if len(rows) < max_rows:
            rows.append(row)

    skipped = header_row_idx

    return {
        "type": detection["type"],
        "confidence": detection["confidence"],
        "columns": headers,
        "normalized_columns": [_normalize_header(h) for h in headers],
        "column_map": detection["column_map"],
        "preview_rows": rows,
        "total_rows": total,
        "delimiter": delimiter,
        "skipped_rows": skipped,
    }


def _get_col(row, col_map, key, default=None):
    idx = col_map.get(key)
    if idx is not None and idx < len(row):
        val = row[idx].strip()
        return val if val else default
    return default


def _safe_int(val):
    if val is None:
        return None
    try:
        return int(str(val).replace(",", "").replace(" ", ""))
    except (ValueError, TypeError):
        return None


def _safe_position(val):
    """Semrush exports use 0 for "not ranking" — store NULL, never 0, or the
    overview counts phantom top-3 wins and 0s poison AVG(position)."""
    pos = _safe_int(val)
    return pos if pos and pos > 0 else None


def _safe_float(val):
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").replace(" ", "").replace("%", ""))
    except (ValueError, TypeError):
        return None


def process_import(store_id, file_content, filename, force_type=None):
    try:
        text = file_content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = file_content.decode("latin-1")

    delimiter = detect_delimiter(text)
    header_row_idx, _ = _find_header_row(text, delimiter)

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    for _ in range(header_row_idx):
        next(reader, None)

    headers = next(reader, [])
    detection = detect_csv_type(headers)

    import_type = force_type or detection["type"]
    if import_type == "unknown":
        raise ValueError("Could not detect CSV type. Please check the file format.")

    if force_type and force_type != detection["type"]:
        col_map = _build_column_map_for_type(headers, force_type)
        sig = COLUMN_SIGNATURES.get(force_type, {})
        missing = [r for r in sig.get("required", set()) if r not in col_map]
        if missing:
            raise ValueError(
                f"CSV is missing required columns for type '{force_type}': {', '.join(sorted(missing))}"
            )
    else:
        col_map = detection["column_map"]

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO semrush_imports (store_id, import_type, filename, status)
               VALUES (%s, %s, %s, 'running') RETURNING id""",
            (store_id, import_type, filename),
        )
        import_id = cur.fetchone()["id"]

    try:
        all_rows = list(reader)
        logger.info("Import %s: header_row=%d, headers=%s, col_map_keys=%s, data_rows=%d",
                     import_type, header_row_idx, headers[:5], list(col_map.keys()), len(all_rows))

        if import_type == "position_tracking_overview":
            row_count = _import_position_overview(store_id, import_id, all_rows, col_map)
        elif import_type in ("position_tracking", "organic_research"):
            row_count = _import_keywords(store_id, import_id, import_type, all_rows, col_map)
        elif import_type == "keyword_gap":
            row_count = _import_keyword_gap(store_id, import_id, all_rows, col_map, headers)
        elif import_type == "site_audit":
            row_count = _import_audit(store_id, import_id, all_rows, col_map, headers)
        else:
            raise ValueError(f"Unknown import type: {import_type}")

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE semrush_imports SET row_count = %s, status = 'completed' WHERE id = %s",
                (row_count, import_id),
            )

        logger.info("Semrush import completed: %d rows (%s) for store %d", row_count, import_type, store_id)
        return {"import_id": import_id, "type": import_type, "row_count": row_count, "status": "completed"}

    except Exception as e:
        logger.error("Semrush import failed: %s", str(e))
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE semrush_imports SET status = 'failed', error_message = %s WHERE id = %s",
                (str(e), import_id),
            )
        raise


def _import_keywords(store_id, import_id, source_type, rows, col_map):
    count = 0
    batch = []
    for row in rows:
        keyword = _get_col(row, col_map, "keyword")
        if not keyword:
            continue
        batch.append((
            store_id, import_id, source_type, keyword,
            _safe_position(_get_col(row, col_map, "position")),
            _safe_int(_get_col(row, col_map, "search volume") or _get_col(row, col_map, "volume")),
            _safe_float(_get_col(row, col_map, "cpc")),
            _safe_float(_get_col(row, col_map, "competition")),
            _safe_float(_get_col(row, col_map, "traffic")),
            _safe_float(_get_col(row, col_map, "traffic (%)")),
            _safe_float(_get_col(row, col_map, "traffic cost")),
            _get_col(row, col_map, "url", ""),
            _get_col(row, col_map, "serp features"),
        ))
        if len(batch) >= 500:
            count += _flush_keywords(batch)
            batch = []

    if batch:
        count += _flush_keywords(batch)
    return count


def _flush_keywords(batch):
    with db.get_cursor() as cur:
        for row in batch:
            cur.execute(
                """INSERT INTO semrush_keywords
                   (store_id, import_id, source_type, keyword, position, search_volume,
                    cpc, competition, traffic, traffic_percent, traffic_cost, url, serp_features)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (store_id, keyword, COALESCE(url, ''), source_type) DO UPDATE SET
                     previous_position = semrush_keywords.position,
                     position = EXCLUDED.position,
                     search_volume = EXCLUDED.search_volume,
                     cpc = EXCLUDED.cpc,
                     competition = EXCLUDED.competition,
                     traffic = EXCLUDED.traffic,
                     traffic_percent = EXCLUDED.traffic_percent,
                     traffic_cost = EXCLUDED.traffic_cost,
                     serp_features = EXCLUDED.serp_features,
                     import_id = EXCLUDED.import_id""",
                row,
            )
    return len(batch)


def _import_position_overview(store_id, import_id, rows, col_map):
    """Import Semrush Position Tracking Rankings Overview format.
    Columns have date-stamped positions like *.DOMAIN.COM/*_20260326."""
    date_info = col_map.get("_date_info")
    if not date_info:
        raise ValueError("No date columns detected in position tracking overview")

    latest_cols = date_info["latest_cols"]
    previous_cols = date_info.get("previous_cols", {})
    kw_idx = col_map.get("keyword")
    vol_idx = col_map.get("search volume")
    cpc_idx = col_map.get("cpc")

    pos_idx = latest_cols.get("position")
    landing_idx = latest_cols.get("landing")
    prev_pos_idx = previous_cols.get("position")

    count = 0
    batch = []
    for row in rows:
        if kw_idx is None or kw_idx >= len(row):
            continue
        keyword = row[kw_idx].strip()
        if not keyword:
            continue

        position = _safe_position(row[pos_idx]) if pos_idx is not None and pos_idx < len(row) else None
        prev_position = _safe_position(row[prev_pos_idx]) if prev_pos_idx is not None and prev_pos_idx < len(row) else None
        url = row[landing_idx].strip() if landing_idx is not None and landing_idx < len(row) else ""
        volume = _safe_int(row[vol_idx]) if vol_idx is not None and vol_idx < len(row) else None
        cpc = _safe_float(row[cpc_idx]) if cpc_idx is not None and cpc_idx < len(row) else None

        batch.append((
            store_id, import_id, "position_tracking", keyword,
            position, volume, cpc, None, None, None, None,
            url, None,
        ))
        if len(batch) >= 500:
            count += _flush_keywords(batch)
            batch = []

    if batch:
        count += _flush_keywords(batch)
    return count


def _import_keyword_gap(store_id, import_id, rows, col_map, headers):
    import re
    norm_headers = [_normalize_header(h) for h in headers]

    # Detect domain-name columns for positions
    # Semrush Keyword Gap uses two formats:
    #   1. "domain.com Position" — header contains "position" plus domain
    #   2. "domain.com" — header IS a domain (no "(pages)" suffix)
    domain_pattern = re.compile(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z]{2,})+$')

    competitor_cols = {}
    our_domain_col = None
    our_url_col = None

    # Get store domain to identify "our" column
    store_domain = ""
    try:
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT COALESCE(s.primary_domain, s.domain) as domain FROM stores s WHERE s.id = %s",
                (store_id,),
            )
            row = cur.fetchone()
            if row:
                store_domain = (row["domain"] or "").replace("https://", "").replace("http://", "").rstrip("/").lower()
    except Exception:
        pass

    for i, h in enumerate(norm_headers):
        raw = headers[i].strip()
        # Skip already-mapped columns, and "(pages)" / "(url)" columns
        if i in col_map.values():
            continue
        if "(pages)" in h or "(url)" in h:
            # But track our URL column
            if store_domain and store_domain in h:
                our_url_col = i
            continue

        is_domain = domain_pattern.match(h)
        has_position = "position" in h and h not in ("position", "previous position")

        if is_domain or has_position:
            domain_name = raw.replace(" Position", "").replace(" position", "").strip()
            # Check if this is our domain
            if store_domain and store_domain in domain_name.lower():
                our_domain_col = i
                # Also find the corresponding (pages) column for our URL
                pages_header = f"{raw} (pages)"
                for j, ph in enumerate(headers):
                    if ph.strip().lower() == pages_header.lower():
                        our_url_col = j
                        break
            else:
                competitor_cols[domain_name] = i

    logger.info("Keyword gap import: our_domain_col=%s (store=%s), competitor_cols=%s",
                our_domain_col, store_domain, list(competitor_cols.keys()))

    count = 0
    batch = []
    for row in rows:
        keyword = _get_col(row, col_map, "keyword")
        if not keyword:
            continue

        comp_positions = {}
        for name, idx in competitor_cols.items():
            if idx < len(row) and row[idx].strip():
                val = _safe_int(row[idx])
                if val and val > 0:
                    comp_positions[name] = val

        # Our position: from dedicated column, or fallback to col_map — same
        # 0-means-not-ranking guard the competitor columns above get.
        our_pos = None
        if our_domain_col is not None and our_domain_col < len(row) and row[our_domain_col].strip():
            our_pos = _safe_position(row[our_domain_col])
        if our_pos is None:
            our_pos = _safe_position(_get_col(row, col_map, "position") or _get_col(row, col_map, "your position"))

        # Our URL from (pages) column
        our_url = ""
        if our_url_col is not None and our_url_col < len(row):
            our_url = row[our_url_col].strip()

        batch.append((
            store_id, import_id, "keyword_gap", keyword,
            our_pos,
            _safe_int(_get_col(row, col_map, "volume")),
            _safe_float(_get_col(row, col_map, "cpc")),
            _safe_float(_get_col(row, col_map, "competition") or _get_col(row, col_map, "competition density")),
            json.dumps(comp_positions) if comp_positions else None,
            our_url,
        ))
        if len(batch) >= 500:
            count += _flush_keyword_gap(batch)
            batch = []

    if batch:
        count += _flush_keyword_gap(batch)
    return count


def _flush_keyword_gap(batch):
    with db.get_cursor() as cur:
        for row in batch:
            if len(row) == 10:
                # New format with URL
                cur.execute(
                    """INSERT INTO semrush_keywords
                       (store_id, import_id, source_type, keyword, position, search_volume,
                        cpc, competition, competitor_positions, url)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (store_id, keyword, COALESCE(url, ''), source_type) DO UPDATE SET
                         previous_position = semrush_keywords.position,
                         position = EXCLUDED.position,
                         search_volume = EXCLUDED.search_volume,
                         cpc = EXCLUDED.cpc,
                         competition = EXCLUDED.competition,
                         competitor_positions = EXCLUDED.competitor_positions,
                         url = COALESCE(EXCLUDED.url, semrush_keywords.url),
                         import_id = EXCLUDED.import_id""",
                    row,
                )
            else:
                # Legacy format without URL
                cur.execute(
                    """INSERT INTO semrush_keywords
                       (store_id, import_id, source_type, keyword, position, search_volume,
                        cpc, competition, competitor_positions)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (store_id, keyword, COALESCE(url, ''), source_type) DO UPDATE SET
                         previous_position = semrush_keywords.position,
                         position = EXCLUDED.position,
                         search_volume = EXCLUDED.search_volume,
                         cpc = EXCLUDED.cpc,
                         competition = EXCLUDED.competition,
                         competitor_positions = EXCLUDED.competitor_positions,
                         import_id = EXCLUDED.import_id""",
                    row,
                )
    return len(batch)


def _import_audit(store_id, import_id, rows, col_map, headers=None):
    count = 0
    is_mega_export = "status code" in col_map

    issue_columns = {}
    if headers and not is_mega_export:
        page_idx = col_map.get("page url", 0)
        for i, h in enumerate(headers):
            if i == page_idx:
                continue
            name = h.strip()
            if name:
                issue_columns[i] = name

    with db.get_cursor() as cur:
        cur.execute("DELETE FROM semrush_audit_issues WHERE store_id = %s", (store_id,))
        for row in rows:
            page_url = _get_col(row, col_map, "page url")
            if not page_url:
                continue

            if issue_columns and not is_mega_export:
                for col_idx, issue_name in issue_columns.items():
                    if col_idx >= len(row):
                        continue
                    val = row[col_idx].strip()
                    if not val or val == "0" or val.lower() == "false" or val == "":
                        continue
                    severity = "error" if "error" in issue_name.lower() or "missing" in issue_name.lower() or "broken" in issue_name.lower() else "warning" if "duplicate" in issue_name.lower() or "too" in issue_name.lower() or "low" in issue_name.lower() else "notice"
                    cur.execute(
                        """INSERT INTO semrush_audit_issues
                           (store_id, import_id, page_url, issue_type, severity, description)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (store_id, import_id, page_url, issue_name, severity,
                         f"{issue_name}: {val}" if val != "1" and val.lower() != "true" else issue_name),
                    )
                    count += 1
                continue

            if is_mega_export:
                status_code = _get_col(row, col_map, "status code")
                crawl_depth = _get_col(row, col_map, "crawl depth")
                internal_links = _get_col(row, col_map, "internal links in")

                issues = []
                sc = _safe_int(status_code)
                if sc and sc >= 400:
                    issues.append(("error", f"HTTP {status_code}", f"Page returns status code {status_code}"))
                elif sc and sc >= 300:
                    issues.append(("warning", f"Redirect ({status_code})", f"Page redirects with status {status_code}"))

                cd = _safe_int(crawl_depth)
                if cd and cd > 3:
                    issues.append(("warning", "Deep crawl depth", f"Page is {crawl_depth} clicks from homepage (should be 3 or fewer)"))

                il = _safe_int(internal_links)
                if il is not None and il == 0:
                    issues.append(("warning", "Orphan page", "Page has 0 internal links pointing to it"))

                if not issues:
                    issues.append(("notice", "Crawled", f"Status {status_code or '200'}, depth {crawl_depth or '?'}, {internal_links or '?'} internal links"))

                for severity, issue_type, description in issues:
                    cur.execute(
                        """INSERT INTO semrush_audit_issues
                           (store_id, import_id, page_url, issue_type, severity, description)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (store_id, import_id, page_url, issue_type, severity, description),
                    )
                    count += 1
            else:
                issue_type = _get_col(row, col_map, "issue type") or _get_col(row, col_map, "issue")
                severity = _get_col(row, col_map, "severity")
                description = _get_col(row, col_map, "description")
                if not issue_type and not severity and not description:
                    continue
                cur.execute(
                    """INSERT INTO semrush_audit_issues
                       (store_id, import_id, page_url, issue_type, severity, description)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (store_id, import_id, page_url, issue_type, severity or "notice", description),
                )
                count += 1
    return count


def get_imports(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, import_type, filename, row_count, status, error_message, created_at FROM semrush_imports WHERE store_id = %s ORDER BY created_at DESC",
            (store_id,),
        )
        return [
            {**r, "created_at": r["created_at"].isoformat() if r["created_at"] else None}
            for r in cur.fetchall()
        ]


def delete_import(import_id, store_id):
    with db.get_cursor() as cur:
        cur.execute("DELETE FROM semrush_imports WHERE id = %s AND store_id = %s", (import_id, store_id))
    return True


def get_keywords(store_id, source_type=None, sort_by="search_volume", sort_dir="desc",
                 limit=10000, offset=0, search=None):
    valid_sorts = {"keyword": "keyword", "position": "position", "previous_position": "previous_position",
                   "search_volume": "search_volume", "cpc": "cpc", "traffic": "traffic", "url": "url",
                   "competition": "competition"}
    order_col = valid_sorts.get(sort_by, "search_volume")
    direction = "DESC" if sort_dir == "desc" else "ASC"
    nulls = "NULLS LAST" if direction == "DESC" else "NULLS FIRST"

    conditions = ["store_id = %s"]
    params = [store_id]
    if source_type:
        conditions.append("source_type = %s")
        params.append(source_type)
    if search:
        conditions.append("keyword ILIKE %s")
        params.append(f"%{search}%")

    where = " AND ".join(conditions)

    with db.get_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) as total FROM semrush_keywords WHERE {where}", params)
        total = cur.fetchone()["total"]

        cur.execute(
            f"""SELECT keyword, position, previous_position, search_volume, cpc, competition,
                       traffic, traffic_percent, traffic_cost, url, serp_features, source_type,
                       competitor_positions
                FROM semrush_keywords
                WHERE {where}
                ORDER BY {order_col} {direction} {nulls}
                LIMIT %s OFFSET %s""",
            params + [limit, offset],
        )
        rows = cur.fetchall()

    return {
        "total": total,
        "keywords": [
            {
                "keyword": r["keyword"],
                "position": r["position"],
                "previous_position": r["previous_position"],
                "change": (r["previous_position"] - r["position"]) if r["previous_position"] and r["position"] else None,
                "search_volume": r["search_volume"],
                "cpc": float(r["cpc"]) if r["cpc"] else None,
                "competition": float(r["competition"]) if r["competition"] else None,
                "traffic": float(r["traffic"]) if r["traffic"] else None,
                "traffic_percent": float(r["traffic_percent"]) if r["traffic_percent"] else None,
                "url": r["url"],
                "serp_features": r["serp_features"],
                "source_type": r["source_type"],
                "competitor_positions": r["competitor_positions"],
            }
            for r in rows
        ],
    }


def get_striking_distance(store_id, pos_min=4, pos_max=20, limit=500, search=None):
    conditions = ["store_id = %s", "position >= %s", "position <= %s"]
    params = [store_id, pos_min, pos_max]
    if search:
        conditions.append("keyword ILIKE %s")
        params.append(f"%{search}%")
    where = " AND ".join(conditions)

    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT keyword, position, previous_position, search_volume, cpc, competition,
                       traffic, url, source_type
                FROM semrush_keywords
                WHERE {where}
                ORDER BY search_volume DESC NULLS LAST
                LIMIT %s""",
            params + [limit],
        )
        rows = cur.fetchall()

    return [
        {
            "keyword": r["keyword"],
            "position": r["position"],
            "previous_position": r["previous_position"],
            "change": (r["previous_position"] - r["position"]) if r["previous_position"] and r["position"] else None,
            "search_volume": r["search_volume"],
            "cpc": float(r["cpc"]) if r["cpc"] else None,
            "competition": float(r["competition"]) if r["competition"] else None,
            "traffic": float(r["traffic"]) if r["traffic"] else None,
            "url": r["url"],
            "potential": int((r["search_volume"] or 0) * 0.1) if r["position"] and r["position"] <= 10 else int((r["search_volume"] or 0) * 0.03),
        }
        for r in rows
    ]


def get_keywords_cross_reference(store_id, limit=500, search=None):
    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=30)).isoformat()
    date_to = today.isoformat()

    conditions = ["sk.store_id = %s"]
    params = [date_from, date_to, store_id]
    if search:
        conditions.append("sk.keyword ILIKE %s")
        params.append(f"%{search}%")
    where = " AND ".join(conditions)
    params.append(limit)

    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT sk.keyword, sk.position as sr_position, sk.search_volume as sr_volume, sk.cpc as sr_cpc, sk.url,
                       COALESCE(gsc.clicks, 0) as gsc_clicks,
                       COALESCE(gsc.impressions, 0) as gsc_impressions,
                       gsc.ctr as gsc_ctr,
                       gsc.position as gsc_position
                FROM semrush_keywords sk
                LEFT JOIN LATERAL (
                    SELECT SUM(clicks) as clicks, SUM(impressions) as impressions,
                           CASE WHEN SUM(impressions) > 0 THEN SUM(clicks)/SUM(impressions) ELSE 0 END as ctr,
                           SUM(position * impressions) / NULLIF(SUM(impressions), 0) as position
                    FROM gsc_daily_metrics g
                    WHERE g.store_id = sk.store_id AND LOWER(g.query) = LOWER(sk.keyword)
                          AND g.date >= %s AND g.date <= %s
                ) gsc ON true
                WHERE {where}
                ORDER BY sk.search_volume DESC NULLS LAST
                LIMIT %s""",
            params,
        )
        rows = cur.fetchall()

    return [
        {
            "keyword": r["keyword"],
            "sr_position": r["sr_position"],
            "sr_volume": r["sr_volume"],
            "sr_cpc": float(r["sr_cpc"]) if r["sr_cpc"] else None,
            "url": r["url"],
            "gsc_clicks": float(r["gsc_clicks"] or 0),
            "gsc_impressions": float(r["gsc_impressions"] or 0),
            "gsc_ctr": round(float(r["gsc_ctr"] or 0), 4),
            "gsc_position": round(float(r["gsc_position"] or 0), 1) if r["gsc_position"] else None,
        }
        for r in rows
    ]


def get_audit_issues(store_id, severity=None, limit=500):
    conditions = ["store_id = %s"]
    params = [store_id]
    if severity:
        conditions.append("LOWER(severity) = LOWER(%s)")
        params.append(severity)
    where = " AND ".join(conditions)

    with db.get_cursor() as cur:
        cur.execute(
            f"SELECT page_url, issue_type, severity, description FROM semrush_audit_issues WHERE {where} ORDER BY severity, page_url LIMIT %s",
            params + [limit],
        )
        return [dict(r) for r in cur.fetchall()]


def get_audit_summary(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT LOWER(severity) as severity, COUNT(*) as count
               FROM semrush_audit_issues WHERE store_id = %s
               GROUP BY LOWER(severity)""",
            (store_id,),
        )
        rows = cur.fetchall()
    result = {"error": 0, "warning": 0, "notice": 0, "total": 0}
    for r in rows:
        sev = r["severity"] or "notice"
        result[sev] = r["count"]
        result["total"] += r["count"]
    return result


def get_overview(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) as total_keywords,
                      AVG(position) as avg_position,
                      SUM(search_volume) as total_volume,
                      COUNT(*) FILTER (WHERE position <= 3) as top3,
                      COUNT(*) FILTER (WHERE position > 3 AND position <= 10) as top10,
                      COUNT(*) FILTER (WHERE position > 10 AND position <= 20) as top20,
                      COUNT(*) FILTER (WHERE position > 20) as beyond20,
                      COUNT(*) FILTER (WHERE position IS NULL) as not_ranking
               FROM semrush_keywords WHERE store_id = %s""",
            (store_id,),
        )
        row = cur.fetchone()

    return {
        "total_keywords": row["total_keywords"],
        "avg_position": round(float(row["avg_position"] or 0), 1),
        "total_volume": int(row["total_volume"] or 0),
        "top3": row["top3"],
        "top10": row["top10"],
        "top20": row["top20"],
        "beyond20": row["beyond20"],
        "not_ranking": row["not_ranking"],
    }


def get_semrush_metrics_for_store(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT url,
                      COUNT(*) as keyword_count,
                      SUM(search_volume) as total_volume,
                      MIN(NULLIF(position, 0)) as best_position
               FROM semrush_keywords
               WHERE store_id = %s AND url IS NOT NULL AND url != ''
               GROUP BY url""",
            (store_id,),
        )
        rows = cur.fetchall()

    from app.services.url_utils import url_path_key

    metrics = {}
    for r in rows:
        # Host/scheme-insensitive path key — the old base-URL string replace
        # silently kept full URLs whenever Semrush and the GSC property
        # disagreed on www/scheme, so nothing downstream ever matched.
        path = url_path_key(r["url"])
        if not path:
            continue

        metrics[path] = {
            "semrush_volume": int(r["total_volume"] or 0),
            "semrush_position": r["best_position"],
            "semrush_keyword_count": r["keyword_count"],
        }
    return metrics


def get_keywords_by_list(store_id, keyword_list):
    if not keyword_list:
        return {}
    with db.get_cursor() as cur:
        placeholders = ",".join(["%s"] * len(keyword_list))
        cur.execute(
            f"""SELECT LOWER(keyword) as kw, search_volume, cpc, competition, position
                FROM semrush_keywords
                WHERE store_id = %s AND LOWER(keyword) IN ({placeholders})
                ORDER BY search_volume DESC NULLS LAST""",
            [store_id] + [k.lower() for k in keyword_list],
        )
        rows = cur.fetchall()

    result = {}
    for r in rows:
        if r["kw"] not in result:
            result[r["kw"]] = {
                "search_volume": r["search_volume"],
                "cpc": float(r["cpc"]) if r["cpc"] else None,
                "competition": float(r["competition"]) if r["competition"] else None,
                "position": r["position"],
            }
    return result
