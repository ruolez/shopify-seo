"""Nightly: rewrite prices stated in product text after a repricing.

The store sync notes products whose variant prices changed while their
description or meta description states a price (price_text_pending keeps the
old prices the text was written against). After the sync, this job swaps
those amounts for the current prices through the apply pipeline (source
price_refresh, undoable from the page's history). Amounts the rules cannot
tie to a variant are left as they are and counted as flagged.

Setting prices.auto_refresh_text (on by default) turns it off.
"""

import logging

from app.database import db
from app.services.text_prices import KEEP, pair_variants, plan_price_text
from app.settings_manager import get_setting

logger = logging.getLogger(__name__)


def run_pending(store_id):
    if not get_setting("prices.auto_refresh_text", True):
        return "off"
    from app.services.apply_pipeline import ApplyRefused, _client_for, apply_item_changes

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT q.product_id, q.old_variants, p.id, p.title, p.variants, p.description, p.meta_description "
            "FROM price_text_pending q LEFT JOIN synced_products p ON p.id = q.product_id AND p.store_id = q.store_id "
            "WHERE q.store_id = %s ORDER BY q.created_at",
            (store_id,),
        )
        rows = cur.fetchall()
    if not rows:
        return "nothing repriced"

    client = None
    counts = {"updated": 0, "unchanged": 0, "refused": 0, "flagged": 0}
    errors = []
    for row in rows:
        fields, _changes, flags = ({}, [], [])
        if row["id"]:
            variants = pair_variants(row["old_variants"], row["variants"])
            fields, _changes, flags = plan_price_text(row, variants, keep=KEEP.get(row["id"], ()))
        counts["flagged"] += len(flags)
        if fields:
            client = client or _client_for(store_id)
            try:
                apply_item_changes(
                    store_id, "product", row["id"], fields,
                    source="price_refresh", change_type="price_refreshed", client=client,
                    restore_media=False, stamp=False,
                )
                counts["updated"] += 1
            except ApplyRefused as refused:
                counts["refused"] += 1
                logger.warning("Price text refresh refused for product %s: %s", row["id"], refused)
            except Exception as e:
                errors.append(f"product {row['id']}: {e}")
                continue
        else:
            counts["unchanged"] += 1
        with db.get_cursor() as cur:
            cur.execute("DELETE FROM price_text_pending WHERE store_id = %s AND product_id = %s",
                        (store_id, row["product_id"]))

    summary = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
    if errors:
        raise RuntimeError(f"{len(errors)} failed ({summary}); first: {errors[0]}")
    return summary
