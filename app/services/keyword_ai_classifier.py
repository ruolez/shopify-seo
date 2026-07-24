"""AI intent classification and topic-family clustering for master keywords.

Replaces the rule-based classify_intent labels (which default to "commercial")
and the Jaccard token clusters (which collapse into one mega-cluster) with an
AI pass: one taxonomy-bootstrap call that defines 40-150 topic families, then
batched calls that classify every keyword's intent and family against that
closed vocabulary. Keywords with intent_source='manual' are never overwritten.
"""

import json
import logging
import threading

from app.database import db
from app.prompt_defaults import DEFAULTS
from app.settings_manager import get_setting

logger = logging.getLogger(__name__)

BATCH_SIZE = 150
INTENT_CODES = {
    "t": "transactional",
    "c": "commercial",
    "f": "informational",
    "n": "navigational",
}


def run_ai_classification(store_id, mode="full"):
    """Start a background classification run. Returns {"run_id"} or {"error"}."""
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id FROM keyword_strategy_runs "
            "WHERE store_id = %s AND run_type = 'ai_classify' AND status = 'running'",
            (store_id,),
        )
        if cur.fetchone():
            return {"error": "Classification already running"}
        cur.execute(
            "INSERT INTO keyword_strategy_runs (store_id, run_type, status) "
            "VALUES (%s, 'ai_classify', 'running') RETURNING id",
            (store_id,),
        )
        run_id = cur.fetchone()["id"]

    thread = threading.Thread(
        target=_run_classification, args=(store_id, run_id, mode), daemon=True
    )
    thread.start()
    return {"run_id": run_id}


def _run_classification(store_id, run_id, mode):
    try:
        from openai import OpenAI

        from app.ai_agent import _get_ai_config

        config = _get_ai_config()
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=config["api_key"],
            timeout=180.0,
        )
        model = config["model"]

        keywords = _load_keywords(store_id, mode)
        if not keywords:
            _finish_run(run_id, summary={"message": "No keywords to classify"})
            return

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE keyword_strategy_runs SET total_items = %s WHERE id = %s",
                (len(keywords), run_id),
            )

        family_ids = _load_family_map(store_id)
        # Incremental runs reuse the existing taxonomy; a full run (or an
        # incremental run on a store that never built one) bootstraps it.
        if mode == "full" or not family_ids:
            families = _build_family_taxonomy(store_id, client, model)
            family_ids = _persist_taxonomy(store_id, families)

        completed = 0
        failed = 0
        for i in range(0, len(keywords), BATCH_SIZE):
            batch = keywords[i : i + BATCH_SIZE]
            results = _classify_batch(client, model, family_ids, batch)
            if results is None:
                failed += len(batch)
            else:
                _apply_batch(store_id, batch, results, family_ids)
            completed += len(batch)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE keyword_strategy_runs SET completed_items = %s, "
                    "failed_items = %s WHERE id = %s",
                    (completed, failed, run_id),
                )

        _refresh_cluster_stats(store_id)

        with db.get_cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FILTER (WHERE intent = 'transactional') AS transactional,
                          COUNT(*) FILTER (WHERE intent = 'commercial') AS commercial,
                          COUNT(*) FILTER (WHERE intent = 'informational') AS informational,
                          COUNT(*) FILTER (WHERE intent = 'navigational') AS navigational,
                          COUNT(*) FILTER (WHERE cluster_id IS NULL) AS unclustered
                   FROM master_keywords WHERE store_id = %s AND status = 'active'""",
                (store_id,),
            )
            counts = dict(cur.fetchone())
            cur.execute(
                "SELECT COUNT(*) AS n FROM master_keyword_clusters WHERE store_id = %s",
                (store_id,),
            )
            counts["families"] = cur.fetchone()["n"]

        _finish_run(run_id, summary=counts)

    except Exception as e:
        logger.error("AI classification failed: %s", e)
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE keyword_strategy_runs SET status='failed', error=%s, "
                "completed_at=NOW() WHERE id=%s",
                (str(e), run_id),
            )


def _load_keywords(store_id, mode):
    where = "store_id = %s AND status = 'active'"
    if mode == "incremental":
        where += (
            " AND (intent_source IS NULL OR intent_source = 'rules'"
            " OR cluster_id IS NULL)"
        )
    else:
        where += " AND COALESCE(intent_source, '') != 'manual'"
    with db.get_cursor() as cur:
        cur.execute(
            f"SELECT id, keyword, COALESCE(search_volume, 0) AS volume "
            f"FROM master_keywords WHERE {where} ORDER BY search_volume DESC NULLS LAST",
            (store_id,),
        )
        return cur.fetchall()


def _load_family_map(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, name, description FROM master_keyword_clusters WHERE store_id = %s",
            (store_id,),
        )
        return {r["name"]: {"id": r["id"], "description": r["description"] or ""} for r in cur.fetchall()}


def _build_family_taxonomy(store_id, client, model):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT COALESCE(vendor, '(none)') AS vendor, COUNT(*) AS count "
            "FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' "
            "GROUP BY vendor ORDER BY count DESC LIMIT 25",
            (store_id,),
        )
        vendors = cur.fetchall()
        cur.execute(
            "SELECT COALESCE(product_type, '(none)') AS product_type, COUNT(*) AS count "
            "FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' "
            "GROUP BY product_type ORDER BY count DESC LIMIT 50",
            (store_id,),
        )
        types = cur.fetchall()
        cur.execute(
            "SELECT keyword, COALESCE(search_volume, 0) AS volume "
            "FROM master_keywords WHERE store_id = %s AND status = 'active' "
            "ORDER BY search_volume DESC NULLS LAST LIMIT 400",
            (store_id,),
        )
        top_kws = cur.fetchall()

    user_msg = (
        "TOP VENDORS/BRANDS:\n"
        + "\n".join(f"- {v['vendor']} ({v['count']} products)" for v in vendors)
        + "\n\nPRODUCT TYPES:\n"
        + "\n".join(f"- {t['product_type']} ({t['count']} products)" for t in types)
        + "\n\nTOP KEYWORDS BY VOLUME:\n"
        + "\n".join(f"- {k['keyword']} ({k['volume']})" for k in top_kws)
    )
    system = get_setting("prompts.keyword_families", DEFAULTS["prompts.keyword_families"])

    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
        temperature=0,
    )
    content = resp.choices[0].message.content or ""
    s, e = content.find("{"), content.rfind("}") + 1
    if s < 0 or e <= s:
        raise ValueError("Family taxonomy call returned no JSON")
    families = json.loads(content[s:e]).get("families", [])

    seen = set()
    clean = []
    for f in families:
        name = (f.get("name") or "").strip().lower()
        if name and name not in seen:
            seen.add(name)
            clean.append({"name": name, "description": (f.get("description") or "").strip()})
    if len(clean) < 10:
        raise ValueError(f"Family taxonomy too small ({len(clean)} families)")
    return clean


def _persist_taxonomy(store_id, families):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE master_keywords SET cluster_id = NULL WHERE store_id = %s",
            (store_id,),
        )
        cur.execute(
            "DELETE FROM master_keyword_clusters WHERE store_id = %s",
            (store_id,),
        )
        family_ids = {}
        for f in families:
            cur.execute(
                """INSERT INTO master_keyword_clusters (store_id, name, description)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (store_id, name) DO UPDATE SET description = EXCLUDED.description
                   RETURNING id""",
                (store_id, f["name"], f["description"]),
            )
            family_ids[f["name"]] = {"id": cur.fetchone()["id"], "description": f["description"]}
    return family_ids


def _classify_batch(client, model, family_ids, batch):
    """Returns {keyword: {"intent", "family"}} or None if the batch failed."""
    families_block = "\n".join(
        f"- {name}" + (f" — {info['description']}" if info["description"] else "")
        for name, info in family_ids.items()
    )
    user_msg = (
        "FAMILIES:\n" + families_block + "\n\nKEYWORDS:\n"
        + "\n".join(k["keyword"] for k in batch)
    )
    system = get_setting("prompts.keyword_classify", DEFAULTS["prompts.keyword_classify"])

    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
                temperature=0,
            )
            content = resp.choices[0].message.content or ""
            s, e = content.find("["), content.rfind("]") + 1
            if s < 0 or e <= s:
                raise ValueError("no JSON array in response")
            entries = json.loads(content[s:e])
            results = {}
            for entry in entries:
                kw = (entry.get("k") or "").strip().lower()
                intent = INTENT_CODES.get(entry.get("i"))
                fam = entry.get("f")
                fam = fam.strip().lower() if isinstance(fam, str) else None
                if kw and intent:
                    results[kw] = {
                        "intent": intent,
                        "family": fam if fam in family_ids else None,
                    }
            if results:
                return results
            raise ValueError("no valid entries parsed")
        except Exception as e:
            if attempt == 0:
                logger.warning("Classify batch failed, retrying: %s", e)
            else:
                logger.error("Classify batch failed after retry: %s", e)
    return None


def _apply_batch(store_id, batch, results, family_ids):
    with db.get_cursor() as cur:
        for kw in batch:
            r = results.get(kw["keyword"])
            if not r:
                continue
            cluster_id = family_ids[r["family"]]["id"] if r["family"] else None
            cur.execute(
                """UPDATE master_keywords
                   SET intent = %s, intent_source = 'ai', ai_classified_at = NOW(),
                       cluster_id = %s, updated_at = NOW()
                   WHERE id = %s AND store_id = %s
                     AND COALESCE(intent_source, '') != 'manual'""",
                (r["intent"], cluster_id, kw["id"], store_id),
            )


def _refresh_cluster_stats(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            """UPDATE master_keyword_clusters mkc SET
                 keyword_count = COALESCE(sub.n, 0),
                 total_volume = COALESCE(sub.vol, 0)
               FROM (SELECT cluster_id, COUNT(*) AS n, SUM(COALESCE(search_volume, 0)) AS vol
                     FROM master_keywords
                     WHERE store_id = %s AND status = 'active' AND cluster_id IS NOT NULL
                     GROUP BY cluster_id) sub
               WHERE mkc.id = sub.cluster_id AND mkc.store_id = %s""",
            (store_id, store_id),
        )
        cur.execute(
            """DELETE FROM master_keyword_clusters mkc
               WHERE mkc.store_id = %s AND NOT EXISTS (
                 SELECT 1 FROM master_keywords mk
                 WHERE mk.cluster_id = mkc.id AND mk.status = 'active')""",
            (store_id,),
        )


def _finish_run(run_id, summary):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE keyword_strategy_runs SET status='completed', summary=%s, "
            "completed_at=NOW() WHERE id=%s",
            (json.dumps(summary), run_id),
        )
