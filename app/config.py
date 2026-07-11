import logging
import os
import secrets

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://seo_user:seo_pass_2025@localhost:5432/shopify_seo"
)
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    # Random per process: sessions won't survive restarts and won't be shared
    # across gunicorn workers. Fine for a quick dev run, never for real use.
    SECRET_KEY = secrets.token_hex(32)
    logging.getLogger(__name__).warning(
        "SECRET_KEY is not set — using a random per-process key; sessions will not persist. "
        "Set it in .env (see .env.example)."
    )
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")
FLASK_ENV = os.environ.get("FLASK_ENV", "development")
