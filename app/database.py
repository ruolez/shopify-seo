import json
import logging
import os
import threading
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool

from app.config import DATABASE_URL

psycopg2.extras.register_uuid()

logger = logging.getLogger(__name__)

# Arbitrary app-wide advisory-lock key: whoever holds it runs boot recovery
# and the singleton schedulers.
LEADER_LOCK_KEY = 725101


class Database:
    def __init__(self, dsn=None):
        self.dsn = dsn or DATABASE_URL
        self._pool = None
        self._pool_pid = None
        self._pool_lock = threading.Lock()
        self._leader_conn = None

    def _get_pool(self):
        # Fork-aware: gunicorn --preload builds the pool in the master before
        # forking; workers must NOT reuse the master's sockets (protocol
        # corruption). Each process lazily builds its own pool.
        pid = os.getpid()
        if self._pool is None or self._pool_pid != pid:
            with self._pool_lock:
                if self._pool is None or self._pool_pid != pid:
                    self._pool = psycopg2.pool.ThreadedConnectionPool(
                        minconn=2, maxconn=16, dsn=self.dsn
                    )
                    self._pool_pid = pid
        return self._pool

    @contextmanager
    def get_conn(self):
        pool = self._get_pool()
        # ThreadedConnectionPool raises when exhausted instead of blocking —
        # wait briefly so a burst of background threads queues instead of failing.
        deadline = time.monotonic() + 30
        while True:
            try:
                conn = pool.getconn()
                break
            except psycopg2.pool.PoolError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        broken = False
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                broken = True
            raise
        finally:
            try:
                pool.putconn(conn, close=broken or bool(conn.closed))
            except Exception:
                logger.exception("Failed to return connection to pool")

    @contextmanager
    def get_cursor(self):
        with self.get_conn() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                yield cur
            finally:
                cur.close()

    def try_acquire_leader(self):
        """Session-level advisory lock held for the process lifetime.

        Returns True if this process should run boot recovery and the
        singleton schedulers. Uses a dedicated non-pooled connection — the
        lock lives exactly as long as that session.
        """
        if self._leader_conn is not None:
            return True
        try:
            conn = psycopg2.connect(self.dsn)
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("SELECT pg_try_advisory_lock(%s)", (LEADER_LOCK_KEY,))
            got = bool(cur.fetchone()[0])
            cur.close()
            if got:
                self._leader_conn = conn
                return True
            conn.close()
            return False
        except Exception:
            logger.exception("Leader election failed — assuming leadership")
            return True

    def init_tables(self, run_recovery=True):
        with self.get_cursor() as cur:
            # Transaction-scoped: auto-releases at commit. The session-level
            # variant would leak the lock on the pooled connection.
            cur.execute("SELECT pg_advisory_xact_lock(1)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(100) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stores (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    domain VARCHAR(255) NOT NULL,
                    access_token_enc TEXT NOT NULL,
                    api_version VARCHAR(20) DEFAULT '2026-01',
                    is_active BOOLEAN DEFAULT TRUE,
                    last_synced_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key VARCHAR(100) PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_config (
                    id SERIAL PRIMARY KEY,
                    api_key_enc TEXT,
                    model VARCHAR(100) DEFAULT 'anthropic/claude-sonnet-4',
                    daily_budget NUMERIC(10,2) DEFAULT 10.00,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS synced_products (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    shopify_id VARCHAR(100) NOT NULL,
                    title VARCHAR(500),
                    handle VARCHAR(500),
                    description TEXT,
                    vendor VARCHAR(255),
                    product_type VARCHAR(255),
                    tags TEXT,
                    meta_title VARCHAR(500),
                    meta_description TEXT,
                    images JSONB DEFAULT '[]',
                    variants JSONB DEFAULT '[]',
                    status VARCHAR(50),
                    synced_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, shopify_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS synced_collections (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    shopify_id VARCHAR(100) NOT NULL,
                    title VARCHAR(500),
                    handle VARCHAR(500),
                    description TEXT,
                    meta_title VARCHAR(500),
                    meta_description TEXT,
                    collection_type VARCHAR(50),
                    products_count INTEGER DEFAULT 0,
                    image_url TEXT,
                    synced_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, shopify_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seo_audits (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    resource_type VARCHAR(50) NOT NULL,
                    resource_id INTEGER NOT NULL,
                    score INTEGER DEFAULT 0,
                    breakdown JSONB DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_tasks (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    task_type VARCHAR(100) NOT NULL,
                    status VARCHAR(50) DEFAULT 'pending',
                    input JSONB DEFAULT '{}',
                    result JSONB,
                    error TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    completed_at TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sync_jobs (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    status VARCHAR(50) DEFAULT 'pending',
                    products_synced INTEGER DEFAULT 0,
                    collections_synced INTEGER DEFAULT 0,
                    error TEXT,
                    started_at TIMESTAMP DEFAULT NOW(),
                    completed_at TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS serp_cache (
                    id SERIAL PRIMARY KEY,
                    cache_key VARCHAR(64) UNIQUE NOT NULL,
                    data JSONB NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bulk_tasks (
                    id SERIAL PRIMARY KEY,
                    status VARCHAR(50) DEFAULT 'pending',
                    total INTEGER DEFAULT 0,
                    completed INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0,
                    current_product_title VARCHAR(500),
                    auto_apply BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    completed_at TIMESTAMP
                )
            """)
            for _ddl in [
                "ALTER TABLE bulk_tasks ADD COLUMN store_id INTEGER",
                "ALTER TABLE bulk_tasks ADD COLUMN kind VARCHAR(50)",
                "ALTER TABLE bulk_tasks ADD COLUMN resource_type VARCHAR(20)",
            ]:
                cur.execute(f"DO $$ BEGIN {_ddl}; EXCEPTION WHEN duplicate_column THEN NULL; END $$")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bulk_task_items (
                    id SERIAL PRIMARY KEY,
                    bulk_task_id INTEGER REFERENCES bulk_tasks(id) ON DELETE CASCADE,
                    product_id INTEGER REFERENCES synced_products(id) ON DELETE CASCADE,
                    status VARCHAR(50) DEFAULT 'pending',
                    ai_result JSONB,
                    apply_result JSONB,
                    error TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    completed_at TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schema_markup (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    resource_id VARCHAR(100) NOT NULL,
                    resource_type VARCHAR(50) NOT NULL,
                    schema_type VARCHAR(50) NOT NULL,
                    schema_json JSONB NOT NULL,
                    is_valid BOOLEAN DEFAULT TRUE,
                    validation_errors JSONB,
                    pushed_to_shopify BOOLEAN DEFAULT FALSE,
                    pushed_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, resource_id, schema_type)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schema_settings (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE UNIQUE,
                    organization_name VARCHAR(500),
                    organization_logo_url TEXT,
                    organization_url TEXT,
                    default_currency VARCHAR(10) DEFAULT 'USD',
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            for _ddl in [
                "ALTER TABLE schema_settings ADD COLUMN organization_description TEXT",
                "ALTER TABLE schema_settings ADD COLUMN organization_email VARCHAR(255)",
                "ALTER TABLE schema_settings ADD COLUMN organization_phone VARCHAR(50)",
                "ALTER TABLE schema_settings ADD COLUMN organization_same_as JSONB DEFAULT '[]'",
                "ALTER TABLE schema_settings ADD COLUMN organization_knows_about JSONB DEFAULT '[]'",
            ]:
                cur.execute(
                    f"DO $$ BEGIN {_ddl}; EXCEPTION WHEN duplicate_column THEN NULL; END $$"
                )
            cur.execute("""
                CREATE TABLE IF NOT EXISTS generated_collections (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    tag VARCHAR(500) NOT NULL,
                    collection_id INTEGER REFERENCES synced_collections(id) ON DELETE SET NULL,
                    shopify_collection_id VARCHAR(100),
                    products_count INTEGER DEFAULT 0,
                    status VARCHAR(50) DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, tag)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS collection_product_map (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    collection_shopify_id VARCHAR(100) NOT NULL,
                    product_shopify_id VARCHAR(100) NOT NULL,
                    synced_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, collection_shopify_id, product_shopify_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS interlinking_analysis (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    analysis_data JSONB NOT NULL DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS keyword_taxonomies (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    taxonomy JSONB NOT NULL DEFAULT '{}',
                    product_assignments JSONB NOT NULL DEFAULT '[]',
                    product_count INTEGER DEFAULT 0,
                    status VARCHAR(50) DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT NOW(),
                    completed_at TIMESTAMP,
                    UNIQUE(store_id)
                )
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE ai_config ADD COLUMN brave_api_key_enc TEXT;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE bulk_tasks ADD COLUMN task_type VARCHAR(50) DEFAULT 'product';
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE bulk_task_items ADD COLUMN collection_id INTEGER REFERENCES synced_collections(id) ON DELETE CASCADE;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE synced_collections ADD COLUMN published BOOLEAN DEFAULT FALSE;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE synced_products ADD COLUMN last_optimized_at TIMESTAMP;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE synced_collections ADD COLUMN last_optimized_at TIMESTAMP;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE sync_jobs ADD COLUMN current_phase VARCHAR(50);
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE sync_jobs ADD COLUMN total_products INTEGER DEFAULT 0;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE sync_jobs ADD COLUMN total_collections INTEGER DEFAULT 0;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE sync_jobs ADD COLUMN collections_mapped INTEGER DEFAULT 0;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE bulk_tasks ADD COLUMN skip_tags BOOLEAN DEFAULT TRUE;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE bulk_tasks ADD COLUMN skip_title BOOLEAN DEFAULT FALSE;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE generated_collections ADD COLUMN parent_collection_id INTEGER REFERENCES synced_collections(id) ON DELETE SET NULL;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE generated_collections ADD COLUMN taxonomy_category VARCHAR(500);
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE generated_collections ADD COLUMN generation_mode VARCHAR(50) DEFAULT 'tags';
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE ai_config ADD COLUMN image_model VARCHAR(100) DEFAULT 'google/gemini-2.5-flash-image';
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE generated_collections ADD COLUMN image_url TEXT;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                UPDATE generated_collections gc
                SET image_url = sc.image_url
                FROM synced_collections sc
                WHERE gc.collection_id = sc.id
                  AND gc.generation_mode = 'brands'
                  AND sc.image_url IS NOT NULL AND sc.image_url <> ''
                  AND (gc.image_url IS NULL OR gc.image_url = '')
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS placeholder_images (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER REFERENCES synced_products(id) ON DELETE CASCADE,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    shopify_media_id VARCHAR(100),
                    shopify_media_url TEXT,
                    alt_text VARCHAR(500),
                    uploaded_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(product_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS technical_seo_analyses (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    item_type VARCHAR(50) NOT NULL,
                    item_id INTEGER NOT NULL,
                    score INTEGER DEFAULT 0,
                    detected_keywords JSONB DEFAULT '[]',
                    checks JSONB DEFAULT '{}',
                    issues_count INTEGER DEFAULT 0,
                    fixable_count INTEGER DEFAULT 0,
                    ai_fixes JSONB,
                    ai_task_id INTEGER REFERENCES ai_tasks(id),
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, item_type, item_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS synced_blogs (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    shopify_id VARCHAR(100) NOT NULL,
                    title VARCHAR(500),
                    handle VARCHAR(500),
                    comment_policy VARCHAR(50),
                    articles_count INTEGER DEFAULT 0,
                    synced_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, shopify_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS synced_articles (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    blog_id INTEGER REFERENCES synced_blogs(id) ON DELETE CASCADE,
                    shopify_id VARCHAR(100) NOT NULL,
                    shopify_blog_id VARCHAR(100),
                    title VARCHAR(500),
                    handle VARCHAR(500),
                    body TEXT,
                    summary TEXT,
                    tags TEXT,
                    author VARCHAR(255),
                    meta_title VARCHAR(500),
                    meta_description TEXT,
                    image_url TEXT,
                    image_alt VARCHAR(500),
                    is_published BOOLEAN DEFAULT FALSE,
                    published_at TIMESTAMP,
                    last_optimized_at TIMESTAMP,
                    synced_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, shopify_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS article_wizard_sessions (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    status VARCHAR(50) DEFAULT 'draft',
                    current_step INTEGER DEFAULT 1,
                    topic VARCHAR(500),
                    target_keywords JSONB DEFAULT '[]',
                    target_product_ids JSONB DEFAULT '[]',
                    target_collection_ids JSONB DEFAULT '[]',
                    blog_id INTEGER,
                    research_data JSONB,
                    outline_data JSONB,
                    generated_article JSONB,
                    featured_image_url TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE article_wizard_sessions ADD COLUMN shopify_article_id VARCHAR(100);
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE article_wizard_sessions ADD COLUMN scheduled_for TIMESTAMP;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE article_wizard_sessions ADD COLUMN error TEXT;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS blog_schedule_settings (
                    store_id INTEGER PRIMARY KEY REFERENCES stores(id) ON DELETE CASCADE,
                    interval_days INTEGER DEFAULT 7,
                    publish_time TIME DEFAULT '09:00',
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS blog_landing_pages (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    blog_id INTEGER REFERENCES synced_blogs(id) ON DELETE CASCADE,
                    shopify_page_id VARCHAR(100),
                    page_handle VARCHAR(500),
                    page_title VARCHAR(500),
                    articles_count INTEGER DEFAULT 0,
                    tags_count INTEGER DEFAULT 0,
                    last_generated_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, blog_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brand_landing_pages (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    shopify_page_id VARCHAR(100),
                    page_handle VARCHAR(500),
                    page_title VARCHAR(500),
                    brands_count INTEGER DEFAULT 0,
                    last_generated_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS blog_migrations (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    status VARCHAR(50) DEFAULT 'pending',
                    total_articles INTEGER DEFAULT 0,
                    migrated_articles INTEGER DEFAULT 0,
                    redirects_created INTEGER DEFAULT 0,
                    failed_articles INTEGER DEFAULT 0,
                    error TEXT,
                    started_at TIMESTAMP DEFAULT NOW(),
                    completed_at TIMESTAMP
                )
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE blog_landing_pages ADD COLUMN intro_text TEXT;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE bulk_task_items ADD COLUMN article_id INTEGER REFERENCES synced_articles(id) ON DELETE CASCADE;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE blog_landing_pages ADD COLUMN hero_image_url TEXT;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE blog_landing_pages ADD COLUMN hero_image_alt VARCHAR(500);
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            # --- News section additions ---
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE synced_articles ADD COLUMN template_suffix VARCHAR(100);
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE synced_articles ADD COLUMN article_type VARCHAR(50);
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE synced_articles ADD COLUMN category VARCHAR(160);
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE synced_articles ADD COLUMN date_modified TIMESTAMP;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS news_sessions (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    status VARCHAR(30) DEFAULT 'draft',
                    source_text TEXT,
                    source_url TEXT,
                    source_urls JSONB DEFAULT '[]',
                    analyze_result JSONB,
                    edited_result JSONB,
                    category VARCHAR(160),
                    tags JSONB DEFAULT '[]',
                    meta_title VARCHAR(500),
                    meta_description TEXT,
                    summary TEXT,
                    hero_image_url TEXT,
                    hero_image_alt VARCHAR(500),
                    hero_image_prompt TEXT,
                    inline_images JSONB DEFAULT '[]',
                    shopify_article_id VARCHAR(100),
                    article_handle VARCHAR(500),
                    error TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE news_sessions ADD COLUMN source_urls JSONB DEFAULT '[]';
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE news_sessions ADD COLUMN scheduled_for TIMESTAMP;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS store_daily_snapshot (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    snapshot_date DATE NOT NULL,
                    products_total INTEGER DEFAULT 0,
                    products_active INTEGER DEFAULT 0,
                    collections_total INTEGER DEFAULT 0,
                    articles_total INTEGER DEFAULT 0,
                    avg_seo_score NUMERIC(5,1),
                    score_good INTEGER DEFAULT 0,
                    score_fair INTEGER DEFAULT 0,
                    score_poor INTEGER DEFAULT 0,
                    meta_title_pct NUMERIC(5,1),
                    meta_desc_pct NUMERIC(5,1),
                    alt_text_pct NUMERIC(5,1),
                    schema_pushed_pct NUMERIC(5,1),
                    keyword_mapped_pct NUMERIC(5,1),
                    technical_issues INTEGER DEFAULT 0,
                    fixable_issues INTEGER DEFAULT 0,
                    orphan_pages INTEGER DEFAULT 0,
                    broken_links INTEGER DEFAULT 0,
                    semrush_keywords INTEGER DEFAULT 0,
                    semrush_top10 INTEGER DEFAULT 0,
                    semrush_striking INTEGER DEFAULT 0,
                    content_gaps_new INTEGER DEFAULT 0,
                    content_gaps_addressed INTEGER DEFAULT 0,
                    aeo_assets_published INTEGER DEFAULT 0,
                    gsc_clicks_7d INTEGER DEFAULT 0,
                    gsc_position_7d NUMERIC(6,1),
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, snapshot_date)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dashboard_events (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    event_type VARCHAR(50) NOT NULL,
                    severity VARCHAR(20) DEFAULT 'info',
                    subject VARCHAR(500),
                    dedup_key VARCHAR(300) NOT NULL,
                    payload JSONB DEFAULT '{}',
                    status VARCHAR(20) DEFAULT 'open',
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, dedup_key)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_dashboard_events_store_created
                ON dashboard_events(store_id, created_at)
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE content_gaps ADD COLUMN addressed_at TIMESTAMP;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS organic_orders (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    order_gid VARCHAR(100) NOT NULL,
                    order_name VARCHAR(50),
                    created_at TIMESTAMP NOT NULL,
                    total_price NUMERIC(12,2) DEFAULT 0,
                    currency VARCHAR(10),
                    attribution VARCHAR(20) DEFAULT 'unknown',
                    landing_page TEXT,
                    landing_path VARCHAR(500),
                    referrer_url TEXT,
                    source VARCHAR(200),
                    source_type VARCHAR(50),
                    journey JSONB DEFAULT '{}',
                    pulled_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, order_gid)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_organic_orders_store_created
                ON organic_orders(store_id, created_at)
            """)
            # Backfill: any article without an explicit type is a blog post.
            cur.execute(
                "UPDATE synced_articles SET article_type = 'blog' WHERE article_type IS NULL"
            )
            # Fail any orphaned tasks: background threads do not survive a restart,
            # so a task left 'pending'/'running' at boot is dead. Failing them here
            # stops the UI from polling a never-completing task forever.
            # Leader-gated: a second process (extra worker, dev run against the
            # same DB) must not kill jobs that are alive in the leader.
            if run_recovery:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', "
                    "error = COALESCE(error, 'Interrupted — server restarted'), completed_at = NOW() "
                    "WHERE status IN ('pending', 'running')"
                )
                cur.execute(
                    "DO $$ BEGIN "
                    "UPDATE bulk_tasks SET status = 'failed', completed_at = NOW() "
                    "WHERE status IN ('pending', 'running'); "
                    "EXCEPTION WHEN undefined_table THEN NULL; END $$"
                )
                cur.execute(
                    "DO $$ BEGIN "
                    "UPDATE ai_tracking_runs SET status = 'failed', "
                    "error = COALESCE(error, 'Interrupted — server restarted'), completed_at = NOW() "
                    "WHERE status IN ('pending', 'running'); "
                    "EXCEPTION WHEN undefined_table THEN NULL; END $$"
                )
                # News sessions in a transient pipeline state are dead too.
                # 'scheduled' is a resting state (awaiting its publish time) and
                # must survive restarts untouched.
                cur.execute(
                    "UPDATE news_sessions SET status = 'failed', "
                    "error = COALESCE(error, 'Interrupted — server restarted'), updated_at = NOW() "
                    "WHERE status IN ('analyzing', 'auto_queued', 'auto_analyzing', 'auto_hero', "
                    "'auto_inline', 'auto_links', 'auto_publishing')"
                )
                # Scheduled blog pipeline sessions in a transient state are dead too.
                # 'scheduled' is a resting state and survives restarts; the
                # scheduled_for guard keeps manual wizard sessions untouched.
                cur.execute(
                    "UPDATE article_wizard_sessions SET status = 'failed', "
                    "error = COALESCE(error, 'Interrupted — server restarted'), updated_at = NOW() "
                    "WHERE scheduled_for IS NOT NULL AND status IN ('auto_queued', 'auto_researching', "
                    "'auto_outlining', 'auto_generating', 'auto_image', 'auto_publishing')"
                )
            cur.execute("""
                CREATE TABLE IF NOT EXISTS news_hub_pages (
                    store_id INTEGER PRIMARY KEY REFERENCES stores(id) ON DELETE CASCADE,
                    shopify_page_id VARCHAR(100),
                    handle VARCHAR(200) DEFAULT 'news',
                    intro_text TEXT,
                    hero_image_url TEXT,
                    hero_image_alt VARCHAR(500),
                    last_generated_at TIMESTAMP
                )
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE stores ADD COLUMN shop_name VARCHAR(255);
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE stores ADD COLUMN primary_domain VARCHAR(255);
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            # Migrate stores off end-of-life API versions (YYYY-MM sorts
            # lexicographically, so string comparison is chronological).
            cur.execute(
                "UPDATE stores SET api_version = '2026-01' WHERE api_version < '2025-07'"
            )
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE sync_jobs ADD COLUMN blogs_synced INTEGER DEFAULT 0;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE sync_jobs ADD COLUMN articles_synced INTEGER DEFAULT 0;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE placeholder_images ADD COLUMN filename VARCHAR(500);
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE placeholder_images ADD COLUMN seo_optimized BOOLEAN DEFAULT FALSE;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS synced_pages (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    shopify_id VARCHAR(100) NOT NULL,
                    title VARCHAR(500),
                    handle VARCHAR(500),
                    body_html TEXT,
                    seo_title VARCHAR(500),
                    seo_description TEXT,
                    is_published BOOLEAN DEFAULT FALSE,
                    published_at TIMESTAMP,
                    template_suffix VARCHAR(100),
                    seo_score INTEGER DEFAULT 0,
                    seo_breakdown JSONB DEFAULT '{}',
                    is_homepage BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, shopify_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS landing_pages (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    synced_page_id INTEGER REFERENCES synced_pages(id) ON DELETE SET NULL,
                    shopify_page_id VARCHAR(100),
                    title VARCHAR(500) NOT NULL,
                    handle VARCHAR(500),
                    meta_title VARCHAR(500),
                    meta_description TEXT,
                    is_published BOOLEAN DEFAULT FALSE,
                    sections JSONB DEFAULT '[]',
                    seo_score INTEGER DEFAULT 0,
                    seo_breakdown JSONB DEFAULT '{}',
                    schema_json JSONB,
                    last_generated_html TEXT,
                    last_published_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, handle)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS landing_page_versions (
                    id SERIAL PRIMARY KEY,
                    landing_page_id INTEGER REFERENCES landing_pages(id) ON DELETE CASCADE,
                    label VARCHAR(200) NOT NULL,
                    title VARCHAR(500),
                    handle VARCHAR(500),
                    meta_title VARCHAR(500),
                    meta_description TEXT,
                    sections JSONB DEFAULT '[]',
                    schema_json JSONB,
                    last_generated_html TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS home_pages (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER UNIQUE REFERENCES stores(id) ON DELETE CASCADE,
                    meta_title VARCHAR(255),
                    meta_description VARCHAR(500),
                    slides JSONB DEFAULT '[]',
                    seo_score INTEGER DEFAULT 0,
                    seo_breakdown JSONB DEFAULT '{}',
                    section_key VARCHAR(100) DEFAULT 'ai_home_carousel',
                    index_json_backup TEXT,
                    is_published BOOLEAN DEFAULT FALSE,
                    last_published_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE synced_pages ADD COLUMN theme_sections JSONB;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE synced_pages ADD COLUMN menus JSONB;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            # ── Phase 1: GSC Integration + SEO Progress Dashboard ──
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE ai_config ADD COLUMN gsc_client_id_enc TEXT;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE ai_config ADD COLUMN gsc_client_secret_enc TEXT;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS gsc_credentials (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    access_token_enc TEXT NOT NULL,
                    refresh_token_enc TEXT NOT NULL,
                    token_expiry TIMESTAMP,
                    gsc_property VARCHAR(500),
                    connected_email VARCHAR(255),
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS gsc_daily_metrics (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    date DATE NOT NULL,
                    page TEXT,
                    query TEXT,
                    clicks DOUBLE PRECISION DEFAULT 0,
                    impressions DOUBLE PRECISION DEFAULT 0,
                    ctr DOUBLE PRECISION DEFAULT 0,
                    position DOUBLE PRECISION DEFAULT 0,
                    device VARCHAR(20),
                    country VARCHAR(10),
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, date, page, query, device, country)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_gsc_metrics_store_date
                ON gsc_daily_metrics(store_id, date)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_gsc_metrics_page
                ON gsc_daily_metrics(store_id, page)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_gsc_metrics_query
                ON gsc_daily_metrics(store_id, query)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS gsc_data_pulls (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    pull_type VARCHAR(50) DEFAULT 'incremental',
                    date_from DATE,
                    date_to DATE,
                    rows_pulled INTEGER DEFAULT 0,
                    status VARCHAR(50) DEFAULT 'pending',
                    error_message TEXT,
                    started_at TIMESTAMP DEFAULT NOW(),
                    completed_at TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seo_score_history (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    item_type VARCHAR(50) NOT NULL,
                    item_id INTEGER NOT NULL,
                    shopify_id VARCHAR(100),
                    title VARCHAR(500),
                    score INTEGER DEFAULT 0,
                    breakdown JSONB DEFAULT '{}',
                    recorded_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_score_history_store_date
                ON seo_score_history(store_id, recorded_at)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cannibalization_fixes (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    query TEXT NOT NULL,
                    page_url TEXT NOT NULL,
                    item_type VARCHAR(50),
                    item_id INTEGER,
                    action VARCHAR(50),
                    target_keyword TEXT,
                    old_meta_title TEXT,
                    new_meta_title TEXT,
                    old_meta_description TEXT,
                    new_meta_description TEXT,
                    old_title TEXT,
                    new_title TEXT,
                    image_alts_changed INTEGER DEFAULT 0,
                    status VARCHAR(50) DEFAULT 'pending',
                    applied_at TIMESTAMP DEFAULT NOW(),
                    verified_at TIMESTAMP,
                    UNIQUE(store_id, query, page_url)
                )
            """)
            # ── SEO Change Events (impact tracking) ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seo_change_events (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    item_type VARCHAR(50) NOT NULL,
                    item_id INTEGER NOT NULL,
                    change_type VARCHAR(50) NOT NULL,
                    changes_applied JSONB,
                    seo_score_before INTEGER,
                    search_score_before INTEGER,
                    composite_score_before INTEGER,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_seo_change_events_item ON seo_change_events(store_id, item_type, item_id)")
            # ── Phase 2: Semrush CSV Import ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS semrush_imports (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    import_type VARCHAR(50) NOT NULL,
                    filename VARCHAR(500),
                    row_count INTEGER DEFAULT 0,
                    status VARCHAR(50) DEFAULT 'pending',
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS semrush_keywords (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    import_id INTEGER REFERENCES semrush_imports(id) ON DELETE CASCADE,
                    source_type VARCHAR(50) NOT NULL,
                    keyword VARCHAR(500) NOT NULL,
                    position INTEGER,
                    previous_position INTEGER,
                    search_volume INTEGER,
                    cpc NUMERIC(10,2),
                    competition NUMERIC(5,4),
                    traffic NUMERIC(10,2),
                    traffic_percent NUMERIC(6,3),
                    traffic_cost NUMERIC(12,2),
                    url TEXT,
                    serp_features TEXT,
                    competitor_positions JSONB,
                    data_timestamp DATE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_semrush_kw_unique ON semrush_keywords(store_id, keyword, COALESCE(url, ''), source_type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_semrush_kw_store ON semrush_keywords(store_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_semrush_kw_keyword ON semrush_keywords(store_id, keyword)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_semrush_kw_url ON semrush_keywords(store_id, url)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_semrush_kw_import ON semrush_keywords(import_id)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS semrush_audit_issues (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    import_id INTEGER REFERENCES semrush_imports(id) ON DELETE CASCADE,
                    page_url TEXT NOT NULL,
                    issue_type VARCHAR(255),
                    severity VARCHAR(50),
                    description TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_semrush_audit_store ON semrush_audit_issues(store_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_semrush_audit_import ON semrush_audit_issues(import_id)")
            # ── Phase 4: Content Gap Analysis + Competitor Monitoring ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS content_gaps (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    keyword VARCHAR(500) NOT NULL,
                    search_volume INTEGER,
                    cpc NUMERIC(10,2),
                    competition NUMERIC(5,4),
                    intent VARCHAR(50),
                    gap_type VARCHAR(50),
                    our_position INTEGER,
                    our_url TEXT,
                    competitor_positions JSONB,
                    opportunity_score NUMERIC(12,2) DEFAULT 0,
                    suggested_content_type VARCHAR(50),
                    matched_product_id INTEGER,
                    matched_collection_id INTEGER,
                    status VARCHAR(50) DEFAULT 'new',
                    brief JSONB,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, keyword)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_content_gaps_store ON content_gaps(store_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_content_gaps_score ON content_gaps(store_id, opportunity_score DESC)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS competitors (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    domain VARCHAR(255) NOT NULL,
                    name VARCHAR(255),
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, domain)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS competitor_rankings (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    competitor_id INTEGER REFERENCES competitors(id) ON DELETE CASCADE,
                    keyword VARCHAR(500) NOT NULL,
                    position INTEGER,
                    url TEXT,
                    previous_position INTEGER,
                    checked_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, competitor_id, keyword)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_comp_rank_store ON competitor_rankings(store_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_comp_rank_keyword ON competitor_rankings(store_id, keyword)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS competitor_alerts (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    alert_type VARCHAR(50) NOT NULL,
                    keyword VARCHAR(500),
                    our_position INTEGER,
                    competitor_domain VARCHAR(255),
                    competitor_position INTEGER,
                    severity VARCHAR(20) DEFAULT 'medium',
                    is_read BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_comp_alerts_store ON competitor_alerts(store_id, is_read)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS competitor_check_runs (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    keywords_checked INTEGER DEFAULT 0,
                    competitors_checked INTEGER DEFAULT 0,
                    alerts_generated INTEGER DEFAULT 0,
                    status VARCHAR(50) DEFAULT 'pending',
                    started_at TIMESTAMP DEFAULT NOW(),
                    completed_at TIMESTAMP
                )
            """)
            # --- Keyword Strategy & Site Audit tables ---
            cur.execute("""
                CREATE TABLE IF NOT EXISTS master_keyword_clusters (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    name VARCHAR(500) NOT NULL,
                    parent_cluster_id INTEGER REFERENCES master_keyword_clusters(id) ON DELETE SET NULL,
                    keyword_count INTEGER DEFAULT 0,
                    total_volume INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, name)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS master_keywords (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    keyword VARCHAR(500) NOT NULL,
                    search_volume INTEGER DEFAULT 0,
                    cpc NUMERIC(10,2),
                    competition NUMERIC(5,4),
                    best_position NUMERIC(6,1),
                    clicks DOUBLE PRECISION DEFAULT 0,
                    impressions DOUBLE PRECISION DEFAULT 0,
                    ctr DOUBLE PRECISION DEFAULT 0,
                    intent VARCHAR(50),
                    source VARCHAR(20) NOT NULL DEFAULT 'manual',
                    current_url TEXT,
                    cluster_id INTEGER REFERENCES master_keyword_clusters(id) ON DELETE SET NULL,
                    status VARCHAR(50) DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, keyword)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_master_kw_store ON master_keywords(store_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_master_kw_cluster ON master_keywords(store_id, cluster_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_master_kw_volume ON master_keywords(store_id, search_volume DESC)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS content_keyword_map (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    master_keyword_id INTEGER REFERENCES master_keywords(id) ON DELETE CASCADE,
                    item_type VARCHAR(50) NOT NULL,
                    item_id INTEGER NOT NULL,
                    found_in JSONB DEFAULT '[]',
                    is_primary BOOLEAN DEFAULT FALSE,
                    relevance_score NUMERIC(6,2) DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, master_keyword_id, item_type, item_id)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ckm_item ON content_keyword_map(store_id, item_type, item_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ckm_keyword ON content_keyword_map(store_id, master_keyword_id)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS keyword_assignments (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    master_keyword_id INTEGER REFERENCES master_keywords(id) ON DELETE CASCADE,
                    assigned_item_type VARCHAR(50),
                    assigned_item_id INTEGER,
                    assignment_reason TEXT,
                    priority VARCHAR(20) DEFAULT 'medium',
                    status VARCHAR(50) DEFAULT 'proposed',
                    proposed_changes JSONB DEFAULT '{}',
                    cannibalization_items JSONB DEFAULT '[]',
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, master_keyword_id)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ka_store ON keyword_assignments(store_id, status)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS keyword_strategy_runs (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    run_type VARCHAR(50) NOT NULL,
                    status VARCHAR(50) DEFAULT 'pending',
                    total_items INTEGER DEFAULT 0,
                    completed_items INTEGER DEFAULT 0,
                    failed_items INTEGER DEFAULT 0,
                    summary JSONB DEFAULT '{}',
                    error TEXT,
                    started_at TIMESTAMP DEFAULT NOW(),
                    completed_at TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS keyword_strategy_changes (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    assignment_id INTEGER REFERENCES keyword_assignments(id) ON DELETE SET NULL,
                    item_type VARCHAR(50) NOT NULL,
                    item_id INTEGER NOT NULL,
                    field_name VARCHAR(100) NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    applied_at TIMESTAMP DEFAULT NOW(),
                    reverted_at TIMESTAMP
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ksc_store ON keyword_strategy_changes(store_id)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_seo_scores (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    resource_type VARCHAR(50) NOT NULL,
                    resource_id INTEGER NOT NULL,
                    score INTEGER DEFAULT 0,
                    breakdown JSONB DEFAULT '{}',
                    live_crawl JSONB,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, resource_type, resource_id)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_seo_scores_store ON ai_seo_scores(store_id, resource_type)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_prompts (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    prompt TEXT NOT NULL,
                    intent VARCHAR(50) DEFAULT 'buyer',
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_prompts_store ON ai_prompts(store_id)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_tracking_runs (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    status VARCHAR(50) DEFAULT 'pending',
                    models JSONB DEFAULT '[]',
                    prompt_count INTEGER DEFAULT 0,
                    brand_mention_rate NUMERIC(5,2),
                    citation_rate NUMERIC(5,2),
                    share_of_voice NUMERIC(5,2),
                    avg_rank NUMERIC(6,2),
                    cost_estimate NUMERIC(10,4) DEFAULT 0,
                    summary JSONB DEFAULT '{}',
                    error TEXT,
                    started_at TIMESTAMP DEFAULT NOW(),
                    completed_at TIMESTAMP
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_tracking_runs_store ON ai_tracking_runs(store_id, started_at)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_mention_results (
                    id SERIAL PRIMARY KEY,
                    run_id INTEGER REFERENCES ai_tracking_runs(id) ON DELETE CASCADE,
                    prompt_id INTEGER REFERENCES ai_prompts(id) ON DELETE SET NULL,
                    model VARCHAR(100) NOT NULL,
                    prompt TEXT,
                    response_text TEXT,
                    brand_mentioned BOOLEAN DEFAULT FALSE,
                    domain_cited BOOLEAN DEFAULT FALSE,
                    brand_rank INTEGER,
                    products_mentioned JSONB DEFAULT '[]',
                    competitors_mentioned JSONB DEFAULT '[]',
                    tokens_used INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_mention_results_run ON ai_mention_results(run_id)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_excluded_brands (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    brand_key VARCHAR(120) NOT NULL,
                    name VARCHAR(255),
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, brand_key)
                )
            """)

            # Per-store tracked/excluded state for AI-recommended brands, keyed by
            # the run's own canonical brand key so state survives name variants
            # across runs. Supersedes ai_excluded_brands (kept for migration).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_brand_prefs (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    brand_key VARCHAR(120) NOT NULL,
                    name VARCHAR(255),
                    domain VARCHAR(255),
                    status VARCHAR(20) NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, brand_key)
                )
            """)
            cur.execute("""
                INSERT INTO ai_brand_prefs (store_id, brand_key, name, status)
                SELECT store_id, brand_key, name, 'excluded' FROM ai_excluded_brands
                ON CONFLICT (store_id, brand_key) DO NOTHING
            """)

            # Cache of resolved official websites for AI-recommended brands so we
            # only pay the Brave lookup once per brand (empty domain = looked-up,
            # none found — avoids re-querying on every panel load).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brand_domains (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    brand_key VARCHAR(120) NOT NULL,
                    name VARCHAR(255),
                    domain VARCHAR(255),
                    resolved_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, brand_key)
                )
            """)

            for _col, _ddl in [
                ("runs_per_prompt", "ALTER TABLE ai_config ADD COLUMN runs_per_prompt INTEGER DEFAULT 3"),
            ]:
                cur.execute(
                    f"DO $$ BEGIN {_ddl}; EXCEPTION WHEN duplicate_column THEN NULL; END $$"
                )
            for _ddl in [
                "ALTER TABLE ai_mention_results ADD COLUMN recommended_brands JSONB DEFAULT '[]'",
                "ALTER TABLE ai_mention_results ADD COLUMN brand_known BOOLEAN",
                "ALTER TABLE ai_mention_results ADD COLUMN run_index INTEGER DEFAULT 0",
                "ALTER TABLE ai_mention_results ADD COLUMN product_brands JSONB DEFAULT '[]'",
                "ALTER TABLE ai_tracking_runs ADD COLUMN gap_analysis JSONB",
            ]:
                cur.execute(
                    f"DO $$ BEGIN {_ddl}; EXCEPTION WHEN duplicate_column THEN NULL; END $$"
                )

            cur.execute("""
                CREATE TABLE IF NOT EXISTS aeo_assets (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    asset_type VARCHAR(50) NOT NULL,
                    content TEXT,
                    status VARCHAR(50) DEFAULT 'draft',
                    published_at TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(store_id, asset_type)
                )
            """)

            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE ai_config ADD COLUMN tracking_models JSONB DEFAULT '["openai/gpt-4o-mini","anthropic/claude-haiku-4.5","google/gemini-3-flash-preview"]';
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE ai_config ADD COLUMN tracking_interval_hours INTEGER DEFAULT 168;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$
            """)

            # Blog topic discovery: AI market research runs + suggested topics
            cur.execute("""
                CREATE TABLE IF NOT EXISTS topic_research_runs (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    task_id INTEGER REFERENCES ai_tasks(id) ON DELETE SET NULL,
                    status VARCHAR(50) DEFAULT 'pending',
                    market_summary TEXT,
                    rejected_topics JSONB DEFAULT '[]',
                    error TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    completed_at TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS topic_suggestions (
                    id SERIAL PRIMARY KEY,
                    run_id INTEGER REFERENCES topic_research_runs(id) ON DELETE CASCADE,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    position INTEGER DEFAULT 0,
                    topic VARCHAR(500) NOT NULL,
                    h1 VARCHAR(500),
                    meta_title VARCHAR(255),
                    meta_description TEXT,
                    url_slug VARCHAR(255),
                    keywords JSONB DEFAULT '{}',
                    search_intent VARCHAR(30),
                    rationale TEXT,
                    cannibalization_note TEXT,
                    target_products JSONB DEFAULT '[]',
                    target_collections JSONB DEFAULT '[]',
                    target_articles JSONB DEFAULT '[]',
                    status VARCHAR(20) DEFAULT 'suggested',
                    wizard_session_id INTEGER REFERENCES article_wizard_sessions(id) ON DELETE SET NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            for _ddl in [
                "ALTER TABLE ai_config ADD COLUMN topic_research_model VARCHAR(100)",
                "ALTER TABLE ai_config ADD COLUMN topic_suggestion_count INTEGER DEFAULT 3",
                "ALTER TABLE topic_suggestions ADD COLUMN opportunity_type VARCHAR(20) DEFAULT 'quick_win'",
            ]:
                cur.execute(
                    f"DO $$ BEGIN {_ddl}; EXCEPTION WHEN duplicate_column THEN NULL; END $$"
                )

            cur.execute("SELECT pg_advisory_unlock(1)")


db = Database()
