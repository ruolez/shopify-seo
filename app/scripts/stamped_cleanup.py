"""Delete orphaned stamped.* review metafields (fabricated ratings with no
reviews behind them) after verifying per-product review counts against the
Stamped API.

Usage (inside the flask-app container):
    python -m app.scripts.stamped_cleanup --store-id 2 \
        --stamped-public-key pubkey-... --stamped-private-key key-... \
        --stamped-store-hash <hash>                                          # dry run
    python -m app.scripts.stamped_cleanup --store-id 2 ... --apply           # delete orphans

The store hash is in the Stamped dashboard next to the API keys (Settings →
API Keys). Reviews are pulled from the documented v2 endpoint
GET https://stamped.io/api/v2/{storeHash}/dashboard/reviews (HTTP basic auth,
public key as username / private key as password) and aggregated per product.

Offline alternative: --stamped-dump file.json with {"<numeric product id>":
{"rating": 4.8, "count": 3}, ...} exported manually from the Stamped dashboard.

Every run (dry or apply) writes a JSON + CSV report to /app/data/reports/
(host ./data/reports/). The JSON keeps the full metafield values — orphaned
values can be restored from it with metafieldsSet if ever needed.
"""

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import requests

from app.auth import decrypt_value
from app.database import db
from app.shopify_client import ShopifyClient

REPORT_DIR = Path("/app/data/reports")
STAMPED_V2_BASE = "https://stamped.io/api/v2"


def _client_for_store(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        raise SystemExit(f"Store {store_id} not found")
    store = dict(store)
    return store, ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])


def _numeric_id(gid):
    return str(gid or "").split("/")[-1]


def _walk_review_nodes(obj, found):
    """Collect dicts that look like review records (carry a productId) from an
    arbitrarily nested API response — resilient to envelope-shape changes."""
    if isinstance(obj, dict):
        if obj.get("productId") not in (None, ""):
            found.append(obj)
        else:
            for v in obj.values():
                _walk_review_nodes(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _walk_review_nodes(v, found)


def fetch_stamped_counts(public_key, private_key, store_hash):
    """{numeric_product_id: {"rating": avg float|None, "count": int}} aggregated
    from every review in the Stamped v2 dashboard API (basic auth)."""
    if not store_hash:
        raise SystemExit("Provide --stamped-store-hash (Stamped dashboard → Settings → API Keys)")
    auth = (public_key, private_key)
    per_product = {}
    page = 1
    seen_ids = set()
    while page < 500:
        resp = requests.get(
            f"{STAMPED_V2_BASE}/{store_hash}/dashboard/reviews",
            params={"page": page},
            auth=auth,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        reviews = []
        _walk_review_nodes(data, reviews)
        if not reviews:
            break
        progressed = False
        for r in reviews:
            rid = str(r.get("id") or "")
            if rid and rid in seen_ids:
                continue
            if rid:
                seen_ids.add(rid)
            progressed = True
            pid = str(r.get("productId"))
            rating = r.get("reviewRating", r.get("rating"))
            slot = per_product.setdefault(pid, {"count": 0, "_sum": 0.0, "_rated": 0})
            slot["count"] += 1
            try:
                if rating not in (None, ""):
                    slot["_sum"] += float(rating)
                    slot["_rated"] += 1
            except (ValueError, TypeError):
                pass
        if not progressed:
            break
        total = data.get("total") if isinstance(data, dict) else None
        if total is not None and len(seen_ids) >= int(total):
            break
        page += 1

    return {
        pid: {"rating": (s["_sum"] / s["_rated"]) if s["_rated"] else None, "count": s["count"]}
        for pid, s in per_product.items()
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store-id", type=int, required=True)
    ap.add_argument("--apply", action="store_true", help="Delete orphaned metafields (default: dry run)")
    ap.add_argument("--stamped-public-key")
    ap.add_argument("--stamped-private-key")
    ap.add_argument("--stamped-store-hash", help="From Stamped dashboard → Settings → API Keys")
    ap.add_argument("--store-url", help="Store URL registered with Stamped (default: primary domain)")
    ap.add_argument("--stamped-dump", help="Path to offline {productId: {rating, count}} JSON instead of the API")
    args = ap.parse_args()

    store, client = _client_for_store(args.store_id)
    store_url = args.store_url or store.get("primary_domain") or store.get("domain")

    print(f"Scanning stamped.* metafields for store {args.store_id} ({store_url})...")
    carriers = []
    for p in client.iter_products_metafields("stamped"):
        fields = {m["key"]: m for m in p["metafields"]}
        if "reviews_average" in fields or "reviews_count" in fields:
            carriers.append({**p, "fields": fields})
    print(f"  {len(carriers)} products carry stamped review metafields")
    if not carriers:
        return

    if args.stamped_dump:
        stamped = {str(k): v for k, v in json.loads(Path(args.stamped_dump).read_text()).items()}
        print(f"  Using offline dump: {len(stamped)} entries")
    else:
        if not (args.stamped_public_key and args.stamped_private_key):
            raise SystemExit("Provide --stamped-public-key and --stamped-private-key (or --stamped-dump file.json)")
        stamped = fetch_stamped_counts(args.stamped_public_key, args.stamped_private_key, args.stamped_store_hash)
        total_reviews = sum(v["count"] for v in stamped.values())
        print(f"  Stamped API: {total_reviews} reviews across {len(stamped)} products")

    if not stamped:
        raise SystemExit(
            "Stamped returned zero reviews — that would mark every carrier an orphan. "
            "Verify the store hash / keys before proceeding; refusing to continue."
        )

    rows = []
    for c in carriers:
        pid = _numeric_id(c["shopify_id"])
        live = stamped.get(pid)
        stored_avg = (c["fields"].get("reviews_average") or {}).get("value")
        stored_count = (c["fields"].get("reviews_count") or {}).get("value")
        live_count = (live or {}).get("count", 0)
        if live_count > 0:
            try:
                mismatch = int(float(stored_count or 0)) != int(live_count)
            except (ValueError, TypeError):
                mismatch = True
            verdict = "mismatch" if mismatch else "active"
        else:
            verdict = "orphan"
        rows.append({
            "shopify_id": c["shopify_id"], "product_id": pid, "handle": c["handle"], "title": c["title"],
            "stored_average": stored_avg, "stored_count": stored_count,
            "live_rating": (live or {}).get("rating"), "live_count": live_count,
            "verdict": verdict,
            "metafields": [{"id": m["id"], "namespace": m["namespace"], "key": m["key"],
                            "value": m["value"], "type": m["type"]} for m in c["metafields"]
                           if m["key"] in ("reviews_average", "reviews_count")],
        })

    orphans = [r for r in rows if r["verdict"] == "orphan"]
    active = [r for r in rows if r["verdict"] == "active"]
    mismatches = [r for r in rows if r["verdict"] == "mismatch"]
    print(f"\nVerdicts: {len(orphans)} orphan (delete), {len(active)} active (keep), "
          f"{len(mismatches)} mismatch (keep, review manually)")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "store_id": args.store_id, "store_url": store_url, "generated_at": ts,
        "mode": "apply" if args.apply else "dry_run", "deleted": False, "rows": rows,
    }
    json_path = REPORT_DIR / f"stamped_cleanup_{ts}.json"
    csv_path = REPORT_DIR / f"stamped_cleanup_{ts}.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["product_id", "handle", "title", "stored_average",
                                          "stored_count", "live_rating", "live_count", "verdict"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in w.fieldnames})

    if args.apply and orphans:
        triples = []
        for r in orphans:
            for m in r["metafields"]:
                triples.append((r["shopify_id"], m["namespace"], m["key"]))
        print(f"\nDeleting {len(triples)} metafields across {len(orphans)} orphan products...")
        deleted = client.delete_metafields(triples)
        report["deleted"] = True
        report["deleted_count"] = len(deleted)
        print(f"  Deleted {len(deleted)} metafields")
    elif args.apply:
        print("\nNothing to delete.")
    else:
        print("\nDry run — nothing deleted. Review the report, then re-run with --apply.")

    json_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nReport: {json_path}\n        {csv_path}\n(host: ./data/reports/)")


if __name__ == "__main__":
    main()
