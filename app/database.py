import json
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from app.config import DATABASE_URL

psycopg2.extras.register_uuid()


class Database:
    def __init__(self, dsn=None):
        self.dsn = dsn or DATABASE_URL

    @contextmanager
    def get_conn(self):
        conn = psycopg2.connect(self.dsn)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def get_cursor(self):
        with self.get_conn() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                yield cur
            finally:
                cur.close()

    def init_tables(self):
        with self.get_cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(1)");
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
                    api_version VARCHAR(20) DEFAULT '2024-10',
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
            cur.execute("SELECT pg_advisory_unlock(1)")


db = Database()
