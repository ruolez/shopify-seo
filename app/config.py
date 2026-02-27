import os
import secrets

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://seo_user:seo_pass_2025@localhost:5432/shopify_seo"
)
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")
FLASK_ENV = os.environ.get("FLASK_ENV", "development")
