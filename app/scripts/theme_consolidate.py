"""One-off theme consolidation operations for the schema single-source rollout.

Run inside the flask-app container:
    python -m app.scripts.theme_consolidate inspect --store-id 2 [--url https://...]
    python -m app.scripts.theme_consolidate seo-fixes --store-id 2 [--yes]
    python -m app.scripts.theme_consolidate remove-renders --store-id 2 [--yes]
    python -m app.scripts.theme_consolidate cosmetic --store-id 2 [--yes]
    python -m app.scripts.theme_consolidate push-file --store-id 2 --asset-key snippets/x.liquid --file path [--yes]
    python -m app.scripts.theme_consolidate restore --store-id 2 --from-dir /app/data/theme_backups/2/<dir>

Every mutating subcommand snapshots the touched assets first (see theme_backup).
"""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from app.auth import decrypt_value
from app.database import db
from app.scripts.theme_backup import backup_asset, edit_asset, make_backup_dir, restore_from
from app.shopify_client import ShopifyClient

REPORT_DIR = Path("/app/data/reports")
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# The two theme-native Product JSON-LD sources being retired.
THEME_PRODUCT_RENDERS = {
    "layout/theme.liquid": "json-ld-product",
    "sections/main-product.liquid": "structured-data-product",
}
# Files the earlier audit found stacked with dead {% if disabled_by_yoast_seo %} wrappers.
YOAST_WRAPPED_FILES = [
    "layout/gift_card.liquid",
    "sections/header.liquid",
    "layout/checkout.liquid",
    "snippets/breadcrumbs.liquid",
    "sections/main-password.liquid",
    "layout/password.liquid",
]


def _client_for_store(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        raise SystemExit(f"Store {store_id} not found")
    store = dict(store)
    return store, ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])


def _theme_id(client):
    theme_id = client.get_main_theme_id()
    if not theme_id:
        raise SystemExit("No main theme found")
    return theme_id


def cmd_inspect(args):
    from app.services.serp_scraper import collect_schema_types
    from app.services.url_utils import store_base_url

    store, client = _client_for_store(args.store_id)
    base = store_base_url(store)
    url = args.url
    if not url:
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT handle FROM synced_products WHERE store_id = %s ORDER BY id LIMIT 1",
                (args.store_id,),
            )
            row = cur.fetchone()
        if not row:
            raise SystemExit("No synced products — pass --url")
        url = f"{base}/products/{row['handle']}"

    print(f"Fetching {url} ...")
    resp = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    blocks = []
    for s in scripts:
        raw = (s.get_text() or "").strip()
        try:
            parsed = json.loads(raw)
            valid = True
        except (json.JSONDecodeError, ValueError):
            parsed, valid = None, False
        blocks.append({"valid_json": valid, "content": parsed if valid else raw[:2000]})
    types = collect_schema_types(scripts)

    bizrate_hits = re.findall(r'[^"\'\s]*bizrate[^"\'\s]*', resp.text, flags=re.IGNORECASE)

    theme_id = _theme_id(client)
    theme_state = {}
    import time as _time
    for key in ("layout/theme.liquid", "sections/main-product.liquid",
                "snippets/seo-fixes.liquid", "snippets/ai-seo-schema.liquid",
                "snippets/json-ld-product.liquid", "snippets/structured-data-product.liquid"):
        value = client.get_theme_asset(theme_id, key)
        _time.sleep(0.6)
        entry = {"exists": value is not None}
        if value is not None and key == "layout/theme.liquid":
            entry["renders_seo_fixes"] = "render 'seo-fixes'" in value
            entry["renders_ai_seo_schema"] = "render 'ai-seo-schema'" in value
            entry["renders_json_ld_product"] = "json-ld-product" in value
        if value is not None and key == "sections/main-product.liquid":
            entry["renders_structured_data_product"] = "structured-data-product" in value
        theme_state[key] = entry

    report = {
        "url": url, "http_status": resp.status_code,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "jsonld_script_count": len(scripts),
        "schema_types": types,
        "product_entity_count": sum(1 for t in types if t in ("Product", "ProductGroup")),
        "bizrate_references": sorted(set(bizrate_hits))[:20],
        "theme_state": theme_state,
        "jsonld_blocks": blocks,
        "note": ("Server-side fetch cannot see client-injected JSON-LD (e.g. Bizrate). "
                 "Confirm with Google Rich Results Test before concluding nothing else emits Product markup."),
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"theme_inspect_{ts}.json"
    path.write_text(json.dumps(report, indent=2))

    print(f"\nJSON-LD scripts: {len(scripts)} | types: {types}")
    print(f"Product/ProductGroup entities: {report['product_entity_count']}")
    print(f"Bizrate references in HTML: {len(report['bizrate_references'])}")
    for k, v in theme_state.items():
        print(f"  {k}: {v}")
    invalid = [b for b in blocks if not b["valid_json"]]
    if invalid:
        print(f"WARNING: {len(invalid)} JSON-LD block(s) failed to parse — see report")
    print(f"\nReport: {path} (host: ./data/reports/)")


def cmd_seo_fixes(args):
    from app.services.seo_snippets import generate_seo_snippet

    store, client = _client_for_store(args.store_id)
    theme_id = _theme_id(client)
    backup_dir = make_backup_dir(args.store_id, "seo_fixes_v3")
    current = backup_asset(client, theme_id, "snippets/seo-fixes.liquid", backup_dir)
    if current is None:
        print("snippets/seo-fixes.liquid is not on the theme — nothing to do")
        return
    v3 = generate_seo_snippet(include_website_schema=False)
    edit_asset(client, theme_id, "snippets/seo-fixes.liquid", lambda _cur: v3, backup_dir, yes=args.yes)


def _remove_render_lines(content, snippet_name):
    lines = content.splitlines(keepends=True)
    kept = [ln for ln in lines if snippet_name not in ln]
    return "".join(kept)


def cmd_remove_renders(args):
    store, client = _client_for_store(args.store_id)
    theme_id = _theme_id(client)
    backup_dir = make_backup_dir(args.store_id, "remove_product_schema_renders")
    for asset_key, snippet_name in THEME_PRODUCT_RENDERS.items():
        edit_asset(
            client, theme_id, asset_key,
            lambda c, name=snippet_name: _remove_render_lines(c, name),
            backup_dir, yes=args.yes,
        )
    print(f"\nBackups in {backup_dir} — rollback with: restore --from-dir {backup_dir}")


def cmd_cosmetic(args):
    """Download the Yoast-wrapped files for manual cleanup and delete the
    orphaned yoast_seo/settings shop metafield. The wrapper stripping itself is
    deliberately manual (nested up to 12 deep — edit the downloaded copies,
    then upload each with push-file)."""
    store, client = _client_for_store(args.store_id)
    theme_id = _theme_id(client)
    backup_dir = make_backup_dir(args.store_id, "yoast_cosmetic")
    for key in YOAST_WRAPPED_FILES:
        value = backup_asset(client, theme_id, key, backup_dir)
        status = "downloaded" if value is not None else "not on theme"
        marker = (value or "").count("disabled_by_yoast_seo")
        print(f"  {key}: {status}" + (f" ({marker} yoast markers)" if value else ""))
    print(f"\nFiles in {backup_dir} — edit copies, then upload each with:\n"
          f"  python -m app.scripts.theme_consolidate push-file --store-id {args.store_id} "
          f"--asset-key <key> --file <edited-file>")

    shop_gid = client.get_shop_id()
    if args.yes or input("\nDelete orphaned yoast_seo/settings shop metafield? [y/N] ").strip().lower() == "y":
        deleted = client.delete_metafields([(shop_gid, "yoast_seo", "settings")])
        print(f"  Deleted: {deleted or '(nothing — metafield absent)'}")


def cmd_push_file(args):
    store, client = _client_for_store(args.store_id)
    theme_id = _theme_id(client)
    backup_dir = make_backup_dir(args.store_id, "push_file")
    new_value = Path(args.file).read_text()
    edit_asset(client, theme_id, args.asset_key, lambda _cur: new_value, backup_dir, yes=args.yes)


def cmd_restore(args):
    store, client = _client_for_store(args.store_id)
    restore_from(client, args.from_dir)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (
        ("inspect", cmd_inspect), ("seo-fixes", cmd_seo_fixes),
        ("remove-renders", cmd_remove_renders), ("cosmetic", cmd_cosmetic),
        ("push-file", cmd_push_file), ("restore", cmd_restore),
    ):
        p = sub.add_parser(name)
        p.add_argument("--store-id", type=int, required=True)
        p.add_argument("--yes", action="store_true", help="Skip confirmation prompts")
        p.set_defaults(fn=fn)
        if name == "inspect":
            p.add_argument("--url", help="Product URL to inspect (default: first synced product)")
        if name == "push-file":
            p.add_argument("--asset-key", required=True)
            p.add_argument("--file", required=True)
        if name == "restore":
            p.add_argument("--from-dir", required=True)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
