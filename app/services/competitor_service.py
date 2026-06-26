import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from app.database import db

logger = logging.getLogger(__name__)

_competitor_scheduler = None


def add_competitor(store_id, domain, name=None):
    domain = domain.strip().lower().replace("https://", "").replace("http://", "").rstrip("/")
    with db.get_cursor() as cur:
        cur.execute("SELECT COUNT(*) as c FROM competitors WHERE store_id = %s", (store_id,))
        if cur.fetchone()["c"] >= 5:
            raise ValueError("Maximum 5 competitors per store")
        cur.execute(
            "INSERT INTO competitors (store_id, domain, name) VALUES (%s, %s, %s) ON CONFLICT (store_id, domain) DO UPDATE SET name = EXCLUDED.name RETURNING id",
            (store_id, domain, name or domain),
        )
        return cur.fetchone()["id"]


def remove_competitor(store_id, competitor_id):
    with db.get_cursor() as cur:
        cur.execute("DELETE FROM competitors WHERE id = %s AND store_id = %s", (competitor_id, store_id))


def get_competitors(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT id, domain, name, notes, created_at FROM competitors WHERE store_id = %s ORDER BY created_at", (store_id,))
        return [
            {**r, "created_at": r["created_at"].isoformat() if r["created_at"] else None}
            for r in cur.fetchall()
        ]


def _get_gsc_position_for_keyword(store_id, keyword):
    """Get our actual Google position from GSC data for a keyword."""
    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=30)).isoformat()
    date_to = (today - timedelta(days=3)).isoformat()

    try:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT SUM(position * impressions) / NULLIF(SUM(impressions), 0) as avg_position,
                          SUM(clicks) as clicks, SUM(impressions) as impressions
                   FROM gsc_daily_metrics
                   WHERE store_id = %s AND LOWER(query) = %s AND date >= %s AND date <= %s
                   HAVING SUM(impressions) > 0""",
                (store_id, keyword.lower(), date_from, date_to),
            )
            row = cur.fetchone()
            if row and row["avg_position"]:
                return round(float(row["avg_position"]), 1)
    except Exception:
        pass
    return None


def discover_competitors(store_id, keyword_limit=20):
    """Search top keywords via Brave and find domains that rank most often."""
    from app.services.serp_scraper import brave_search

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT keyword, search_volume FROM semrush_keywords WHERE store_id = %s AND search_volume IS NOT NULL ORDER BY search_volume DESC LIMIT %s",
            (store_id, keyword_limit),
        )
        keywords = cur.fetchall()

    if not keywords:
        return {"error": "No keywords found. Import Semrush data first.", "domains": []}

    with db.get_cursor() as cur:
        cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
        cred = cur.fetchone()
    our_domain = ""
    if cred and cred.get("gsc_property"):
        our_domain = cred["gsc_property"].replace("https://", "").replace("http://", "").rstrip("/").replace("sc-domain:", "")

    # Already tracked competitors
    with db.get_cursor() as cur:
        cur.execute("SELECT domain FROM competitors WHERE store_id = %s", (store_id,))
        tracked = {r["domain"] for r in cur.fetchall()}

    # Domains to ignore (own site, major non-competitors)
    ignore_domains = {
        "amazon.com", "ebay.com", "walmart.com", "target.com", "wikipedia.org",
        "youtube.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
        "reddit.com", "pinterest.com", "tiktok.com", "yelp.com", "linkedin.com",
        "google.com", "bing.com", "yahoo.com", "quora.com",
    }

    domain_stats = {}
    keywords_searched = 0

    for kw_row in keywords:
        keyword = kw_row["keyword"]
        volume = int(kw_row.get("search_volume") or 0)

        try:
            results = brave_search(keyword, count=20)
        except Exception as e:
            logger.warning("Discover: Brave search failed for '%s': %s", keyword, e)
            continue

        if not results:
            continue

        keywords_searched += 1

        for result in results:
            domain = (result.get("domain") or "").lower().replace("www.", "")
            pos = result.get("position", 99)

            if not domain or domain in ignore_domains:
                continue
            if our_domain and our_domain in domain:
                continue

            if domain not in domain_stats:
                domain_stats[domain] = {
                    "domain": domain,
                    "keywords_count": 0,
                    "keywords": [],
                    "total_volume": 0,
                    "positions": [],
                    "best_position": 999,
                    "already_tracked": domain in tracked,
                }

            stats = domain_stats[domain]
            stats["keywords_count"] += 1
            stats["total_volume"] += volume
            stats["positions"].append(pos)
            if pos < stats["best_position"]:
                stats["best_position"] = pos
            if len(stats["keywords"]) < 10:
                stats["keywords"].append({"keyword": keyword, "position": pos, "volume": volume})

        time.sleep(0.3)

    # Calculate averages and score
    domains_list = []
    for stats in domain_stats.values():
        if stats["keywords_count"] < 2:
            continue
        avg_pos = round(sum(stats["positions"]) / len(stats["positions"]), 1)
        # Score: more keywords + higher volume + better positions = higher threat
        score = (stats["keywords_count"] * 10) + (min(stats["total_volume"], 50000) / 100) + ((20 - min(avg_pos, 20)) * 5)
        domains_list.append({
            "domain": stats["domain"],
            "keywords_count": stats["keywords_count"],
            "total_volume": stats["total_volume"],
            "avg_position": avg_pos,
            "best_position": stats["best_position"],
            "threat_score": round(score),
            "top_keywords": sorted(stats["keywords"], key=lambda k: k["volume"], reverse=True)[:5],
            "already_tracked": stats["already_tracked"],
        })

    domains_list.sort(key=lambda d: d["threat_score"], reverse=True)

    return {
        "domains": domains_list[:20],
        "keywords_searched": keywords_searched,
        "total_domains_found": len(domains_list),
    }


def run_competitor_check(store_id, keyword_limit=100):
    from app.services.serp_scraper import brave_search

    with db.get_cursor() as cur:
        cur.execute("SELECT id, domain FROM competitors WHERE store_id = %s", (store_id,))
        competitors = cur.fetchall()

    if not competitors:
        return {"error": "No competitors configured"}

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT keyword, search_volume FROM semrush_keywords WHERE store_id = %s AND search_volume IS NOT NULL ORDER BY search_volume DESC LIMIT %s",
            (store_id, keyword_limit),
        )
        keywords = cur.fetchall()

    if not keywords:
        return {"error": "No keywords to check. Import Semrush data first."}

    with db.get_cursor() as cur:
        cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
        cred = cur.fetchone()
    our_domain = ""
    if cred and cred.get("gsc_property"):
        our_domain = cred["gsc_property"].replace("https://", "").replace("http://", "").rstrip("/").replace("sc-domain:", "")

    comp_domains = {c["id"]: c["domain"] for c in competitors}

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO competitor_check_runs (store_id, status, started_at) VALUES (%s, 'running', NOW()) RETURNING id",
            (store_id,),
        )
        run_id = cur.fetchone()["id"]

    alerts_count = 0
    keywords_checked = 0
    google_failures = 0

    try:
        for kw_row in keywords:
            keyword = kw_row["keyword"]

            # Use Brave Search for competitor position discovery
            results = None
            try:
                results = brave_search(keyword, count=20)
            except Exception as e:
                logger.warning("Brave search failed for '%s': %s", keyword, str(e))
                continue

            if not results:
                continue

            keywords_checked += 1

            # Get our real position from GSC (more accurate than SERP scraping for our own site)
            our_position = _get_gsc_position_for_keyword(store_id, keyword)

            # Also check SERP results for our position as secondary signal
            if not our_position:
                for result in results:
                    result_domain = (result.get("domain") or "").lower()
                    if our_domain and our_domain in result_domain:
                        our_position = result.get("position")
                        break

            for result in results:
                result_domain = (result.get("domain") or "").lower()

                for comp_id, comp_domain in comp_domains.items():
                    if comp_domain in result_domain:
                        pos = result.get("position")
                        url = result.get("url", "")

                        with db.get_cursor() as cur:
                            cur.execute(
                                """INSERT INTO competitor_rankings (store_id, competitor_id, keyword, position, url, checked_at)
                                   VALUES (%s, %s, %s, %s, %s, NOW())
                                   ON CONFLICT (store_id, competitor_id, keyword) DO UPDATE SET
                                     previous_position = competitor_rankings.position,
                                     position = EXCLUDED.position,
                                     url = EXCLUDED.url,
                                     checked_at = NOW()""",
                                (store_id, comp_id, keyword, pos, url),
                            )

                            if pos and pos <= 3:
                                # Only create alert if no unread alert exists for this keyword+competitor
                                cur.execute(
                                    """SELECT id FROM competitor_alerts
                                       WHERE store_id = %s AND keyword = %s AND competitor_domain = %s AND is_read = FALSE
                                       LIMIT 1""",
                                    (store_id, keyword, comp_domain),
                                )
                                if not cur.fetchone():
                                    cur.execute(
                                        """INSERT INTO competitor_alerts
                                           (store_id, alert_type, keyword, our_position, competitor_domain, competitor_position, severity)
                                           VALUES (%s, 'competitor_surge', %s, %s, %s, %s, %s)""",
                                        (store_id, keyword, our_position, comp_domain, pos,
                                         "high" if pos == 1 else "medium"),
                                    )
                                    alerts_count += 1

            time.sleep(0.3)

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE competitor_check_runs SET keywords_checked = %s, competitors_checked = %s, alerts_generated = %s, status = 'completed', completed_at = NOW() WHERE id = %s",
                (keywords_checked, len(competitors), alerts_count, run_id),
            )

        logger.info("Competitor check: %d keywords, %d competitors, %d alerts for store %d",
                     keywords_checked, len(competitors), alerts_count, store_id)
        return {"keywords_checked": keywords_checked, "competitors_checked": len(competitors), "alerts": alerts_count}

    except Exception as e:
        with db.get_cursor() as cur:
            cur.execute("UPDATE competitor_check_runs SET status = 'failed', completed_at = NOW() WHERE id = %s", (run_id,))
        raise


def get_rankings(store_id, keyword=None, limit=500, include_unchallenged=False):
    conditions = ["cr.store_id = %s"]
    params = [store_id]
    if keyword:
        conditions.append("cr.keyword ILIKE %s")
        params.append(f"%{keyword}%")
    where = " AND ".join(conditions)

    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT cr.keyword, c.domain, c.name, cr.position, cr.previous_position, cr.url, cr.checked_at
                FROM competitor_rankings cr
                JOIN competitors c ON c.id = cr.competitor_id
                WHERE {where}
                ORDER BY cr.keyword, cr.position ASC NULLS LAST
                LIMIT %s""",
            params + [limit],
        )
        rows = cur.fetchall()

    grouped = {}
    for r in rows:
        kw = r["keyword"]
        if kw not in grouped:
            grouped[kw] = {"keyword": kw, "competitors": [], "our_position": None, "search_volume": None, "unchallenged": False}
        change = (r["previous_position"] - r["position"]) if r["previous_position"] and r["position"] else None
        grouped[kw]["competitors"].append({
            "domain": r["domain"],
            "name": r["name"],
            "position": r["position"],
            "previous_position": r["previous_position"],
            "change": change,
            "url": r["url"],
        })

    # Merge Semrush keyword gap data (real Google positions from Semrush exports)
    with db.get_cursor() as cur:
        gap_conditions = "store_id = %s AND source_type = 'keyword_gap' AND competitor_positions IS NOT NULL AND competitor_positions::text NOT IN ('null', '{}')"
        gap_params = [store_id]
        if keyword:
            gap_conditions += " AND keyword ILIKE %s"
            gap_params.append(f"%{keyword}%")
        cur.execute(
            f"SELECT keyword, position, search_volume, competitor_positions, url FROM semrush_keywords WHERE {gap_conditions} ORDER BY search_volume DESC NULLS LAST",
            gap_params,
        )
        gap_rows = cur.fetchall()

    # Get tracked competitor domains for matching
    tracked_domains = {}
    with db.get_cursor() as cur:
        cur.execute("SELECT domain, name FROM competitors WHERE store_id = %s", (store_id,))
        for r in cur.fetchall():
            tracked_domains[r["domain"].lower()] = r["name"] or r["domain"]

    for gr in gap_rows:
        kw = gr["keyword"]
        comp_pos = gr["competitor_positions"]
        if isinstance(comp_pos, str):
            import json as _json
            try:
                comp_pos = _json.loads(comp_pos)
            except Exception:
                logger.warning("Invalid JSON in competitor_positions for keyword '%s'", kw)
                continue
        if not comp_pos or not isinstance(comp_pos, dict):
            continue

        if kw not in grouped:
            grouped[kw] = {
                "keyword": kw, "competitors": [], "our_position": None,
                "search_volume": int(gr["search_volume"]) if gr["search_volume"] else None,
                "unchallenged": False,
            }

        entry = grouped[kw]

        # Always update our_position from Semrush if not set yet (Semrush > Brave for our position)
        if entry["our_position"] is None and gr["position"]:
            entry["our_position"] = int(gr["position"])
        if entry["search_volume"] is None and gr["search_volume"]:
            entry["search_volume"] = int(gr["search_volume"])

        # Build lookup of existing competitors by domain
        existing_by_domain = {c["domain"].lower(): c for c in entry["competitors"]}

        for domain, pos in comp_pos.items():
            domain_clean = domain.lower().replace("www.", "")
            if not pos or int(pos) <= 0:
                continue
            semrush_pos = int(pos)
            name = tracked_domains.get(domain_clean, domain)

            if domain_clean in existing_by_domain:
                # Semrush is more accurate — update the existing Brave entry
                existing = existing_by_domain[domain_clean]
                existing["position"] = semrush_pos
                existing["source"] = "semrush"
            else:
                entry["competitors"].append({
                    "domain": domain_clean,
                    "name": name,
                    "position": semrush_pos,
                    "previous_position": None,
                    "change": None,
                    "url": "",
                    "source": "semrush",
                })
                entry["unchallenged"] = False

    # Add unchallenged keywords
    if include_unchallenged:
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT keywords_checked FROM competitor_check_runs WHERE store_id = %s AND status = 'completed' ORDER BY completed_at DESC LIMIT 1",
                (store_id,),
            )
            last_run = cur.fetchone()
            kw_limit = last_run["keywords_checked"] if last_run else 100

            cur.execute(
                "SELECT keyword, search_volume FROM semrush_keywords WHERE store_id = %s AND search_volume IS NOT NULL ORDER BY search_volume DESC LIMIT %s",
                (store_id, kw_limit),
            )
            for kw_row in cur.fetchall():
                kw = kw_row["keyword"]
                if kw not in grouped:
                    if keyword and keyword.lower() not in kw.lower():
                        continue
                    grouped[kw] = {
                        "keyword": kw,
                        "competitors": [],
                        "our_position": None,
                        "search_volume": int(kw_row["search_volume"]) if kw_row["search_volume"] else None,
                        "unchallenged": True,
                    }

    # Enrich with search volume from Semrush
    keywords_needing_volume = [kw for kw in grouped if grouped[kw].get("search_volume") is None]
    if keywords_needing_volume:
        try:
            with db.get_cursor() as cur:
                for kw in keywords_needing_volume:
                    cur.execute(
                        "SELECT search_volume FROM semrush_keywords WHERE store_id = %s AND LOWER(keyword) = %s AND search_volume IS NOT NULL ORDER BY search_volume DESC LIMIT 1",
                        (store_id, kw.lower()),
                    )
                    row = cur.fetchone()
                    if row and row["search_volume"]:
                        grouped[kw]["search_volume"] = int(row["search_volume"])
        except Exception as e:
            logger.warning("Search volume lookup failed: %s", e)

    # Enrich with our GSC positions
    keywords_list = list(grouped.keys())
    if keywords_list:
        today = datetime.now(timezone.utc).date()
        date_from = (today - timedelta(days=30)).isoformat()
        date_to = (today - timedelta(days=3)).isoformat()
        try:
            with db.get_cursor() as cur:
                for kw in keywords_list:
                    cur.execute(
                        """SELECT SUM(position * impressions) / NULLIF(SUM(impressions), 0) as avg_position
                           FROM gsc_daily_metrics
                           WHERE store_id = %s AND LOWER(query) = %s AND date >= %s AND date <= %s
                           HAVING SUM(impressions) > 0""",
                        (store_id, kw.lower(), date_from, date_to),
                    )
                    row = cur.fetchone()
                    if row and row["avg_position"]:
                        grouped[kw]["our_position"] = round(float(row["avg_position"]), 1)
        except Exception as e:
            logger.warning("GSC position lookup failed: %s", e)

    items = list(grouped.values())
    challenged = sum(1 for r in items if r["competitors"])
    unchallenged = sum(1 for r in items if not r["competitors"])
    gaps = sum(1 for r in items if r["competitors"] and (r["our_position"] is None or r["our_position"] > 100))
    weak = sum(1 for r in items if r["competitors"] and r["our_position"] is not None and 20 < r["our_position"] <= 100)
    return {"items": items, "challenged": challenged, "unchallenged": unchallenged, "gaps": gaps, "weak": weak}


def get_alerts(store_id, is_read=None, severity=None, limit=200):
    conditions = ["store_id = %s"]
    params = [store_id]
    if is_read is not None:
        conditions.append("is_read = %s")
        params.append(is_read)
    if severity:
        conditions.append("severity = %s")
        params.append(severity)
    where = " AND ".join(conditions)

    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT id, alert_type, keyword, our_position, competitor_domain, competitor_position,
                       severity, is_read, created_at
                FROM competitor_alerts WHERE {where}
                ORDER BY created_at DESC LIMIT %s""",
            params + [limit],
        )
        return [
            {**r, "created_at": r["created_at"].isoformat() if r["created_at"] else None}
            for r in cur.fetchall()
        ]


def mark_alert_read(store_id, alert_id):
    with db.get_cursor() as cur:
        cur.execute("UPDATE competitor_alerts SET is_read = TRUE WHERE id = %s AND store_id = %s", (alert_id, store_id))


def get_overview(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT COUNT(*) as c FROM competitors WHERE store_id = %s", (store_id,))
        comp_count = cur.fetchone()["c"]

        cur.execute("SELECT COUNT(*) as c FROM competitor_alerts WHERE store_id = %s AND is_read = FALSE", (store_id,))
        unread_alerts = cur.fetchone()["c"]

        cur.execute("SELECT COUNT(DISTINCT keyword) as c FROM competitor_rankings WHERE store_id = %s", (store_id,))
        keywords_tracked = cur.fetchone()["c"]

        cur.execute(
            "SELECT completed_at FROM competitor_check_runs WHERE store_id = %s AND status = 'completed' ORDER BY completed_at DESC LIMIT 1",
            (store_id,),
        )
        last_check = cur.fetchone()

    return {
        "competitors": comp_count,
        "unread_alerts": unread_alerts,
        "keywords_tracked": keywords_tracked,
        "last_check": last_check["completed_at"].isoformat() if last_check and last_check["completed_at"] else None,
    }


def get_check_history(store_id, limit=20):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, keywords_checked, competitors_checked, alerts_generated, status, started_at, completed_at FROM competitor_check_runs WHERE store_id = %s ORDER BY started_at DESC LIMIT %s",
            (store_id, limit),
        )
        return [
            {**r, "started_at": r["started_at"].isoformat() if r["started_at"] else None,
             "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None}
            for r in cur.fetchall()
        ]


class CompetitorCheckScheduler:
    def __init__(self, interval_hours=168):
        self.interval = interval_hours * 3600
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Competitor check scheduler started (every %d hours)", self.interval // 3600)

    def stop(self):
        self._stop_event.set()

    def _run(self):
        self._stop_event.wait(600)
        while not self._stop_event.is_set():
            try:
                self._check_all_stores()
            except Exception as e:
                logger.error("Scheduled competitor check error: %s", str(e))
            self._stop_event.wait(self.interval)

    def _check_all_stores(self):
        with db.get_cursor() as cur:
            cur.execute("SELECT DISTINCT store_id FROM competitors")
            stores = cur.fetchall()
        for s in stores:
            try:
                with db.get_cursor() as cur:
                    cur.execute(
                        "SELECT completed_at FROM competitor_check_runs WHERE store_id = %s AND status = 'completed' ORDER BY completed_at DESC LIMIT 1",
                        (s["store_id"],),
                    )
                    last = cur.fetchone()
                    if last and last["completed_at"]:
                        days_since = (datetime.now(timezone.utc) - last["completed_at"].replace(tzinfo=timezone.utc)).days
                        if days_since < 6:
                            continue
                run_competitor_check(s["store_id"])
            except Exception as e:
                logger.error("Competitor check failed for store %d: %s", s["store_id"], str(e))


def init_scheduler():
    global _competitor_scheduler
    if _competitor_scheduler is None:
        _competitor_scheduler = CompetitorCheckScheduler(interval_hours=168)
        _competitor_scheduler.start()
