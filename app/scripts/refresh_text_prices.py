"""Swap store prices written into product descriptions and meta descriptions
for the current catalog prices.

Usage (inside the flask-app container):
    python -m app.scripts.refresh_text_prices --store-id 2            # dry run
    python -m app.scripts.refresh_text_prices --store-id 2 --apply    # write to Shopify

Prices come live from Shopify; the mirror's variant prices are the "old"
prices the text was written against, which ties each amount to its variant.
Run it before the next product sync, which overwrites those old prices.
Writes go through apply_item_changes (source price_refresh) and can be undone
from each page's history. Every run writes JSON + CSV reports to
/app/data/reports/.
"""

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from app.database import db
from app.services.apply_pipeline import ApplyRefused, _client_for, apply_item_changes
from app.services.text_prices import refresh_text_prices

REPORT_DIR = Path("/app/data/reports")
FIELDS = {"body": "description", "meta_description": "meta_description"}

# Amounts the review pass read as packaging or market context that the
# rules cannot tell from a store price: product id -> raw amounts.
KEEP = {
    192283: {"$2.49", "$2.79"},
    197872: {"$1.69"},
    8466: {"$1.19"},
    8307: {"$1.29"},
    5881: {"$1.29"},
    8407: {"$1.99"},
    8435: {"$0.99", "$1.49"},
    8448: {"$1.49"},
    8092: {"$0.99"},
    8449: {"$1.49"},
    8402: {"$0.25"},
    8268: {"$2.00"},
    5058: {"$2.49"},
    7640: {"$1.39"},
}


def _products(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, shopify_id, title, variants, description, meta_description FROM synced_products "
            "WHERE store_id = %s AND (description ~ '\\$\\s?[0-9]' OR meta_description ~ '\\$\\s?[0-9]') ORDER BY id",
            (store_id,),
        )
        return cur.fetchall()


def _variants(mirror_variants, live_variants):
    old = {v["id"]: v.get("price") for v in mirror_variants or []}
    return [{"title": v["title"], "old": old.get(v["id"]), "new": v["price"]} for v in live_variants]


def _plan(row, variants):
    fields, changes, flags = {}, [], []
    for field, column in FIELDS.items():
        result = refresh_text_prices(row[column], row["title"], variants, keep=KEEP.get(row["id"], ()))
        if result["text"] != (row[column] or ""):
            fields[field] = result["text"]
        changes += [(field, *c) for c in result["changes"]]
        flags += [(field, *f) for f in result["flags"]]
    return fields, changes, flags


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    client = _client_for(args.store_id)
    live = {p["shopify_id"]: p for p in client.get_all_products()}
    counts, records = Counter(), []

    for row in _products(args.store_id):
        product = live.get(row["shopify_id"])
        if not product:
            counts["missing_in_shopify"] += 1
            continue
        variants = _variants(row["variants"], product["variants"])
        fields, changes, flags = _plan(row, variants)
        counts["scanned"] += 1
        counts["amounts_changed"] += len(changes)
        counts["amounts_flagged"] += len(flags)
        status = "ok" if fields else "noop"
        event_id = None
        if fields and args.apply:
            try:
                result = apply_item_changes(
                    args.store_id, "product", row["id"], fields,
                    source="price_refresh", change_type="price_refreshed", client=client,
                    restore_media=False, stamp=False,
                )
                status, event_id = result["status"], result.get("event_id")
            except ApplyRefused as refused:
                status = f"refused:{refused.code}"
            print(f"product {row['id']}: {status}", flush=True)
        if fields:
            counts[f"pages_{status}"] += 1
            counts[f"pages_with_{'+'.join(sorted(fields))}"] += 1
        if fields or flags:
            records.append({
                "id": row["id"], "title": row["title"], "status": status, "event_id": event_id,
                "changes": changes, "flags": flags, "variants": variants,
                "before": {f: row[c] for f, c in FIELDS.items() if f in fields}, "after": fields,
            })

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    mode = "apply" if args.apply else "dry"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORT_DIR / f"refresh_text_prices_{mode}_{stamp}.json"
    csv_path = REPORT_DIR / f"refresh_text_prices_{mode}_{stamp}.csv"
    json_path.write_text(json.dumps({"summary": dict(counts), "items": records}, indent=2, default=str))
    with csv_path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "title", "status", "field", "old", "new", "flag"])
        for r in records:
            for field, old, new in r["changes"]:
                writer.writerow([r["id"], r["title"], r["status"], field, old, new, ""])
            for field, amount, reason in r["flags"]:
                writer.writerow([r["id"], r["title"], r["status"], field, amount, "", reason])

    print(json.dumps(dict(counts), indent=2))
    print(f"Report: {json_path}\n        {csv_path}")


if __name__ == "__main__":
    main()
