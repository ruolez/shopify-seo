"""Remove "Price" rows and columns from description tables.

Usage (inside the flask-app container):
    python -m app.scripts.strip_table_prices --store-id 2            # dry run
    python -m app.scripts.strip_table_prices --store-id 2 --apply    # write to Shopify

Optional: --types product,collection (default all four), --limit N.

The dry run works from the app's mirror and runs the same plan_apply checks
the write will, so it predicts refusals too. --apply writes through
apply_item_changes (source price_strip): every change is recorded in
seo_change_events and can be undone from the page's history. Every run writes
a JSON report with the full before/after bodies plus a CSV summary to
/app/data/reports/ (host ./data/reports/).
"""

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from app.database import db
from app.services.apply_pipeline import (
    ITEM_CONFIG,
    ApplyRefused,
    _client_for,
    _load_mirror,
    _resolved_primary,
    apply_item_changes,
    plan_apply,
)
from app.services.table_prices import strip_table_prices

REPORT_DIR = Path("/app/data/reports")
ITEM_TYPES = ("product", "collection", "article", "page")


def _items(store_id, item_type, limit):
    cfg = ITEM_CONFIG[item_type]
    body_col = cfg["columns"]["body"]
    with db.get_cursor() as cur:
        cur.execute(
            f"SELECT id, title FROM {cfg['table']} WHERE store_id = %s AND {body_col} ILIKE '%%<table%%' "
            f"ORDER BY id {'LIMIT %s' % int(limit) if limit else ''}",
            (store_id,),
        )
        return cur.fetchall()


def _plan(store_id, item_type, item_id):
    _, mirror = _load_mirror(item_type, item_id)
    stripped = strip_table_prices(mirror["body"])
    plan = None
    if stripped["html"] != mirror["body"]:
        primary = _resolved_primary(store_id, item_type, item_id)
        plan = plan_apply(mirror, mirror, {"body": stripped["html"]},
                          primary_keyword=primary, restore_media=False)
    return mirror, stripped, plan


def _apply_one(store_id, item_type, item_id, client):
    """Push one item; a stale mirror is refreshed by the pipeline, so the strip
    is recomputed from what Shopify holds and tried once more."""
    for attempt in (1, 2):
        _, mirror = _load_mirror(item_type, item_id)
        stripped = strip_table_prices(mirror["body"])
        if stripped["html"] == mirror["body"]:
            return "noop", None
        try:
            result = apply_item_changes(
                store_id, item_type, item_id, {"body": stripped["html"]},
                source="price_strip", change_type="price_removed", client=client,
                restore_media=False, stamp=False,
            )
            return result["status"], result.get("event_id")
        except ApplyRefused as refused:
            if refused.status != "stale" or attempt == 2:
                return f"refused:{refused.code}", None
    return "refused:stale", None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store-id", type=int, required=True)
    parser.add_argument("--types", default=",".join(ITEM_TYPES))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    types = [t for t in args.types.split(",") if t]
    client = _client_for(args.store_id) if args.apply else None
    records, summary = [], {}

    for item_type in types:
        counts = Counter()
        for item in _items(args.store_id, item_type, args.limit):
            mirror, stripped, plan = _plan(args.store_id, item_type, item["id"])
            counts["with_tables"] += 1
            for reason in stripped["skipped"]:
                counts[f"skipped_table:{reason}"] += 1
            if plan is None:
                if stripped["skipped"]:
                    records.append({"type": item_type, "id": item["id"], "title": item["title"],
                                    "status": "skipped_only", "skipped": stripped["skipped"]})
                continue

            counts["to_change"] += 1
            counts["rows_removed"] += stripped["rows"]
            counts["columns_removed"] += stripped["columns"]
            status, event_id = plan["status"], None
            if plan["status"] == "blocked":
                status = f"refused:{plan['code']}"
            elif args.apply:
                status, event_id = _apply_one(args.store_id, item_type, item["id"], client)
                print(f"{item_type} {item['id']}: {status}", flush=True)
            counts[status] += 1
            records.append({
                "type": item_type, "id": item["id"], "title": item["title"], "status": status,
                "event_id": event_id, "rows": stripped["rows"], "columns": stripped["columns"],
                "skipped": stripped["skipped"], "missing": (plan or {}).get("missing"),
                "before": mirror["body"], "after": stripped["html"],
            })
        summary[item_type] = dict(counts)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    mode = "apply" if args.apply else "dry"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORT_DIR / f"strip_table_prices_{mode}_{stamp}.json"
    csv_path = REPORT_DIR / f"strip_table_prices_{mode}_{stamp}.csv"
    json_path.write_text(json.dumps({"summary": summary, "items": records}, indent=2, default=str))
    with csv_path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["type", "id", "title", "status", "rows", "columns", "skipped", "missing", "event_id"])
        for r in records:
            writer.writerow([r["type"], r["id"], r["title"], r["status"], r.get("rows", 0),
                             r.get("columns", 0), ";".join(r["skipped"]), ";".join(r.get("missing") or []),
                             r.get("event_id") or ""])

    print(json.dumps(summary, indent=2))
    print(f"Report: {json_path}\n        {csv_path}")


if __name__ == "__main__":
    main()
