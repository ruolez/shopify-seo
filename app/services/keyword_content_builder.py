"""Turn an uncovered keyword cluster into a category (collection) page.

The Keyword Strategy explorer can tell you a family has real transactional
demand and no page to serve it. This is the other half: propose a page, show
exactly what it would contain, and only then write it to Shopify.

Membership is rule-based, so the collection keeps populating itself as the
catalog changes — and so the proposal can be checked against the real catalog
before anything is created.
"""

import json
import logging

from app.database import db
from app.services.ai_output_guard import guard_shopify_fields
from app.services.keyword_identity import variant_key

logger = logging.getLogger(__name__)

# Which relations each rule column actually accepts. Shopify's GraphQL enums
# are permissive — {TAG, CONTAINS} passes schema validation and then fails at
# runtime as a userError, i.e. after the user has approved a preview — so
# proposals are checked here first. Mirrors the live collectionRulesConditions
# for the columns we let a proposal use.
ALLOWED_RULES = {
    "TAG": {"EQUALS", "NOT_EQUALS"},
    "VENDOR": {"EQUALS", "NOT_EQUALS", "CONTAINS", "NOT_CONTAINS", "STARTS_WITH", "ENDS_WITH"},
    "TYPE": {"EQUALS", "NOT_EQUALS", "CONTAINS", "NOT_CONTAINS", "STARTS_WITH", "ENDS_WITH"},
    "TITLE": {"EQUALS", "NOT_EQUALS", "CONTAINS", "NOT_CONTAINS", "STARTS_WITH", "ENDS_WITH"},
}

# Below this a new collection is a thin page — it competes with nothing and
# dilutes the site rather than adding to it.
THIN_COLLECTION_PRODUCTS = 3


def _store(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, id AS store_id, domain, access_token_enc, api_version FROM stores WHERE id = %s",
            (store_id,),
        )
        row = cur.fetchone()
    if not row:
        raise ValueError("Store not found")
    return row


def _client(store):
    from app.auth import decrypt_value
    from app.shopify_client import ShopifyClient

    return ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])


def catalog_facts(store_id, limit=40):
    """Vendors, product types and tags that actually exist, with counts.

    A proposal grounded in these selects real products; one grounded in the
    model's idea of the catalog selects nothing.
    """
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT vendor AS value, COUNT(*) AS n FROM synced_products
               WHERE store_id = %s AND status = 'ACTIVE' AND vendor IS NOT NULL AND vendor != ''
               GROUP BY vendor ORDER BY n DESC LIMIT %s""",
            (store_id, limit),
        )
        vendors = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT product_type AS value, COUNT(*) AS n FROM synced_products
               WHERE store_id = %s AND status = 'ACTIVE' AND product_type IS NOT NULL AND product_type != ''
               GROUP BY product_type ORDER BY n DESC LIMIT %s""",
            (store_id, limit),
        )
        types = [dict(r) for r in cur.fetchall()]
        # Tags are a comma-joined string; Shopify forbids commas inside a tag,
        # so splitting on comma is exact.
        cur.execute(
            """SELECT lower(trim(t)) AS value, COUNT(*) AS n
               FROM synced_products p, unnest(string_to_array(p.tags, ',')) AS t
               WHERE p.store_id = %s AND p.status = 'ACTIVE' AND trim(t) != ''
               GROUP BY 1 ORDER BY n DESC LIMIT %s""",
            (store_id, limit * 3),
        )
        tags = [dict(r) for r in cur.fetchall()]
    return {"vendors": vendors, "product_types": types, "tags": tags}


def validate_rules(rules, applied_disjunctively=False):
    """Returns (clean_rules, errors). Drops anything Shopify would reject at
    runtime rather than letting the failure surface after the user approves."""
    clean, errors = [], []
    for r in rules or []:
        column = str(r.get("column", "")).upper().strip()
        relation = str(r.get("relation", "")).upper().strip()
        condition = str(r.get("condition", "")).strip()
        if column not in ALLOWED_RULES:
            errors.append(f"Unsupported rule column “{column or '(blank)'}”.")
            continue
        if relation not in ALLOWED_RULES[column]:
            errors.append(
                f"{column} does not support {relation or '(blank)'} — "
                f"allowed: {', '.join(sorted(ALLOWED_RULES[column]))}."
            )
            continue
        if not condition:
            errors.append(f"{column} {relation} needs a value.")
            continue
        clean.append({"column": column, "relation": relation, "condition": condition})
    if not clean and not errors:
        errors.append("No membership rules — the collection would be empty.")
    return clean, errors


def _rule_sql(rule):
    """One rule as (sql_fragment, params) against synced_products."""
    col, rel, cond = rule["column"], rule["relation"], rule["condition"]
    if col == "TAG":
        # Exact tag membership. A substring match would let "red" match
        # "bordered", and ILIKE would treat % and _ in the tag as wildcards.
        expr = ("EXISTS (SELECT 1 FROM unnest(string_to_array(p.tags, ',')) t "
                "WHERE lower(trim(t)) = lower(%s))")
        return (expr, [cond]) if rel == "EQUALS" else (f"NOT {expr}", [cond])

    field = {"VENDOR": "p.vendor", "TYPE": "p.product_type", "TITLE": "p.title"}[col]
    if rel == "EQUALS":
        return f"lower({field}) = lower(%s)", [cond]
    if rel == "NOT_EQUALS":
        return f"lower(COALESCE({field}, '')) != lower(%s)", [cond]
    # ILIKE patterns: escape the wildcards so a literal % or _ in a condition
    # stays literal.
    esc = cond.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = {"CONTAINS": f"%{esc}%", "NOT_CONTAINS": f"%{esc}%",
               "STARTS_WITH": f"{esc}%", "ENDS_WITH": f"%{esc}"}[rel]
    negate = rel == "NOT_CONTAINS"
    return f"COALESCE({field}, '') {'NOT ' if negate else ''}ILIKE %s", [pattern]


def match_products(store_id, rules, applied_disjunctively=False, limit=12):
    """Products the rule set would select, as far as local data can tell.

    An approximation, deliberately: a real smart collection also matches draft
    products and honors Online Store publication, neither of which is mirrored
    here. Treat the count as "active products we know about".
    """
    if not rules:
        return {"count": 0, "sample": []}
    fragments, params = [], []
    for r in rules:
        frag, p = _rule_sql(r)
        fragments.append(frag)
        params.extend(p)
    where = (" OR " if applied_disjunctively else " AND ").join(f"({f})" for f in fragments)

    with db.get_cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS n FROM synced_products p "
            f"WHERE p.store_id = %s AND p.status = 'ACTIVE' AND ({where})",
            [store_id, *params],
        )
        count = cur.fetchone()["n"]
        cur.execute(
            f"SELECT p.id, p.title, p.handle, p.vendor, p.product_type, p.images "
            f"FROM synced_products p "
            f"WHERE p.store_id = %s AND p.status = 'ACTIVE' AND ({where}) "
            f"ORDER BY p.title LIMIT %s",
            [store_id, *params, limit],
        )
        sample = []
        for r in cur.fetchall():
            images = r["images"] or []
            if isinstance(images, str):
                try:
                    images = json.loads(images)
                except ValueError:
                    images = []
            sample.append({
                "id": r["id"], "title": r["title"], "handle": r["handle"],
                "vendor": r["vendor"], "product_type": r["product_type"],
                "image": (images[0] or {}).get("url") if images else None,
            })
    return {"count": count, "sample": sample}


def _cluster_keywords(store_id, keyword_ids):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, keyword, search_volume, intent FROM master_keywords
               WHERE store_id = %s AND id = ANY(%s)
               ORDER BY search_volume DESC NULLS LAST""",
            (store_id, list(keyword_ids)),
        )
        return [dict(r) for r in cur.fetchall()]


def propose_collection(store_id, keyword_ids):
    """Ask the model for a page covering this keyword cluster, grounded in the
    real catalog. Returns the proposal plus its preview — nothing is written."""
    from openai import OpenAI

    from app.auth import decrypt_value
    from app.prompt_defaults import DEFAULTS
    from app.settings_manager import get_setting

    keywords = _cluster_keywords(store_id, keyword_ids)
    if not keywords:
        raise ValueError("No keywords found")
    primary = keywords[0]

    with db.get_cursor() as cur:
        cur.execute("SELECT api_key_enc, model FROM ai_config ORDER BY id LIMIT 1")
        config = cur.fetchone()
    if not config or not config.get("api_key_enc"):
        raise ValueError("AI is not configured — add an OpenRouter key in Settings")

    facts = catalog_facts(store_id)
    fact_block = (
        "VENDORS: " + ", ".join(f'{v["value"]} ({v["n"]})' for v in facts["vendors"]) + "\n"
        "PRODUCT TYPES: " + ", ".join(f'{t["value"]} ({t["n"]})' for t in facts["product_types"]) + "\n"
        "TAGS: " + ", ".join(f'{t["value"]} ({t["n"]})' for t in facts["tags"][:80])
    )
    kw_block = "\n".join(
        f'- {k["keyword"]} ({(k["search_volume"] or 0):,} searches/mo, {k["intent"] or "commercial"})'
        for k in keywords
    )

    system = get_setting("prompts.collection_rules_proposal",
                         DEFAULTS["prompts.collection_rules_proposal"])
    user = (
        f"PRIMARY KEYWORD: {primary['keyword']} ({(primary['search_volume'] or 0):,} searches/mo)\n\n"
        f"KEYWORD CLUSTER THIS ONE PAGE MUST COVER:\n{kw_block}\n\n"
        f"CATALOG FACTS (use only these values in rules; counts are active products):\n{fact_block}\n"
    )

    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=decrypt_value(config["api_key_enc"]), timeout=120.0, max_retries=1)
    response = client.chat.completions.create(
        model=config.get("model") or "anthropic/claude-sonnet-4",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=3000,
        temperature=0.3,
    )
    text = response.choices[0].message.content or ""
    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError("The model did not return a usable proposal — try again")
    proposal = json.loads(text[start:end])

    proposal["mode"] = "smart"
    proposal["keyword_ids"] = [k["id"] for k in keywords]
    proposal["primary_keyword"] = primary["keyword"]
    proposal["primary_keyword_id"] = primary["id"]
    proposal["secondary_keywords"] = [k["keyword"] for k in keywords[1:]]
    return proposal


def preview_collection(store_id, proposal):
    """Everything the user needs to judge the proposal before it is created:
    what it would contain, and what could go wrong."""
    rules, rule_errors = validate_rules(proposal.get("rules"),
                                        proposal.get("applied_disjunctively"))
    disjunctive = bool(proposal.get("applied_disjunctively"))
    match = match_products(store_id, rules, disjunctive)

    guarded = guard_shopify_fields({
        "body_html": proposal.get("description_html") or "",
        "meta_title": proposal.get("meta_title") or "",
        "meta_description": proposal.get("meta_description") or "",
    })

    handle = (proposal.get("handle") or "").strip().lower().replace(" ", "-")
    warnings = list(rule_errors)

    if rules and match["count"] == 0:
        warnings.append(
            "No active products match these rules — the collection would publish empty. "
            "Check the tag, vendor and type values against the catalog."
        )
    elif 0 < match["count"] < THIN_COLLECTION_PRODUCTS:
        warnings.append(
            f"Only {match['count']} product(s) match. A near-empty category page is a thin page "
            f"and usually hurts more than it helps."
        )

    store = _store(store_id)
    if handle:
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT title FROM synced_collections WHERE store_id = %s AND lower(handle) = %s",
                (store_id, handle),
            )
            clash = cur.fetchone()
        if clash:
            warnings.append(
                f"The handle “{handle}” is already used by “{clash['title']}”. Shopify would "
                f"silently create /collections/{handle}-1 instead — pick a different handle."
            )

    # Does a page for this already exist? The most useful duplicate guard is
    # not the keyword ledger but the plain question "which collections are
    # already associated with any keyword in this cluster" — including the
    # audit's own mapping, which is weak evidence of ownership but strong
    # evidence that a page on this topic exists.
    existing = []
    keyword_ids = proposal.get("keyword_ids") or []
    if keyword_ids:
        try:
            from app.services.keyword_coverage import get_ownership_map
            seen = set()
            for kw_id, entry in get_ownership_map(store_id, keyword_ids).items():
                for owner in entry.get("owners", []):
                    if owner["item_type"] != "collection" or owner["item_id"] in seen:
                        continue
                    seen.add(owner["item_id"])
                    existing.append({
                        "item_id": owner["item_id"],
                        "title": owner.get("title"),
                        "handle": owner.get("handle"),
                        "role": owner.get("role"),
                    })
        except Exception:
            logger.warning("Existing-collection check failed", exc_info=True)
    if existing:
        names = ", ".join(f'“{c["title"]}”' for c in existing[:3])
        warnings.append(
            f"{len(existing)} existing collection(s) already cover keywords in this cluster: {names}"
            + (" …" if len(existing) > 3 else "")
            + ". Optimizing one of those usually beats adding another page."
        )

    # Cannibalization: an exact-query check against GSC, plus the variant-key
    # ownership ledger, which catches plural and word-order variants GSC's
    # exact match misses.
    cannibal = {}
    primary = proposal.get("primary_keyword")
    if primary:
        try:
            from app.services.content_gap_service import check_cannibalization_risk
            cannibal = check_cannibalization_risk(store_id, primary) or {}
        except Exception:
            logger.warning("Cannibalization check failed for %s", primary, exc_info=True)
        try:
            from app.services.keyword_resolver import get_owned_primary_keywords
            owner = get_owned_primary_keywords(
                store_id, item_types=("product", "collection")).get(variant_key(primary))
            if owner:
                warnings.append(
                    f"“{owner['keyword']}” is already the primary keyword of an existing "
                    f"{owner['item_type']}. A second page for it would compete with your own."
                )
        except Exception:
            logger.warning("Ownership check failed for %s", primary, exc_info=True)

    return {
        "rules": rules,
        "applied_disjunctively": disjunctive,
        "match_count": match["count"],
        "sample": match["sample"],
        "thin_threshold": THIN_COLLECTION_PRODUCTS,
        "handle": handle,
        "title": proposal.get("title"),
        "meta_title": guarded["meta_title"],
        "meta_description": guarded["meta_description"],
        "description_html": guarded["body_html"],
        "warnings": warnings,
        "blocking": bool(rule_errors) or match["count"] == 0,
        "cannibalization": cannibal,
        "existing_collections": existing,
        "store_domain": store["domain"],
        # Local matching can't see drafts or publication state, so the real
        # count can differ; the UI says so rather than implying precision.
        "count_is_approximate": True,
    }


def create_collection_from_proposal(store_id, proposal, generate_image=False):
    """Create the collection, then hand it the keyword.

    Order matters. The local synced_collections row must exist before the
    keyword can be assigned to it, and the assignment must land before the
    resolver runs, or the resolver won't see the manual claim and the pin
    won't take until the next TTL refresh.
    """
    from app.services.content_gap_service import create_collection_on_shopify
    from app.services.keyword_strategy_service import assign_keyword_to_content

    rules, errors = validate_rules(proposal.get("rules"), proposal.get("applied_disjunctively"))
    if errors:
        raise ValueError("; ".join(errors))

    store = _store(store_id)
    guarded = guard_shopify_fields({
        "body_html": proposal.get("description_html") or "",
        "meta_title": proposal.get("meta_title") or "",
        "meta_description": proposal.get("meta_description") or "",
    })

    created = create_collection_on_shopify(_client(store), store, {
        "title": proposal["title"],
        "handle": (proposal.get("handle") or "").strip().lower() or None,
        "body_html": guarded["body_html"],
        "meta_title": guarded["meta_title"],
        "meta_description": guarded["meta_description"],
        "mode": "smart",
        "rules": rules,
        "applied_disjunctively": bool(proposal.get("applied_disjunctively")),
        "generate_image": generate_image,
        "image_keywords": [proposal.get("primary_keyword") or ""],
    })

    # Only the primary keyword is claimed. Claiming the whole cluster would
    # break: one item may hold only one accepted assignment, so the next pin on
    # this page would silently demote the rest, and every extra claim reserves
    # its variant_key store-wide, walking real products off good keywords. The
    # cluster is covered by the page's copy, not by the ledger.
    assigned = None
    kw_id = proposal.get("primary_keyword_id")
    if kw_id:
        try:
            assigned = assign_keyword_to_content(store_id, kw_id, "collection", created["local_id"])
        except Exception:
            logger.warning("Keyword assignment failed for new collection %s", created["local_id"],
                           exc_info=True)

    analysis = None
    try:
        from app.routes.technical_seo_routes import _resolve_and_analyze, _upsert_analysis
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_collections WHERE id = %s", (created["local_id"],))
            item = cur.fetchone()
        if item:
            analysis, resolution = _resolve_and_analyze("collection", dict(item))
            _upsert_analysis(store_id, "collection", created["local_id"], analysis, resolution)
    except Exception:
        logger.warning("Analysis of new collection %s failed", created["local_id"], exc_info=True)

    created["assigned_keyword"] = (assigned or {}).get("keyword")
    created["seo_score"] = (analysis or {}).get("score")
    return created
