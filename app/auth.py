import base64
import os

import bcrypt
from cryptography.fernet import Fernet

from app.config import ENCRYPTION_KEY

_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is None:
        key = ENCRYPTION_KEY
        if not key:
            # A silently generated random key would differ per worker process
            # and make everything encrypted unrecoverable after restart.
            raise RuntimeError(
                "ENCRYPTION_KEY is not set. Add it to .env (see .env.example) — "
                "generating a random one would make stored API tokens unreadable."
            )
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def hash_password(password):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password, password_hash):
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def encrypt_value(plaintext):
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext):
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def ensure_default_admin(db):
    with db.get_cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username = 'admin'")
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                ("admin", hash_password("admin")),
            )
