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
from app.services.text_prices import KEEP, TEXT_FIELDS as FIELDS, pair_variants, plan_price_text

REPORT_DIR = Path("/app/data/reports")


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
        variants = pair_variants(row["variants"], product["variants"])
        fields, changes, flags = plan_price_text(row, variants, keep=KEEP.get(row["id"], ()))
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
