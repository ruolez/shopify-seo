"""Backup/restore helpers for live theme edits.

Every theme asset is snapshotted to /app/data/theme_backups/<store_id>/<ts>_<label>/
(host-visible at ./data/theme_backups/) with a manifest.json before any write.
Rollback = `python -m app.scripts.theme_consolidate restore --from <dir>`.
"""

import difflib
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

BACKUP_ROOT = Path("/app/data/theme_backups")
# Theme REST assets are bucket-limited (~2 req/s) — pace every call.
ASSET_CALL_DELAY = 0.6


def make_backup_dir(store_id, label):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = BACKUP_ROOT / str(store_id) / f"{ts}_{label}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _manifest_path(backup_dir):
    return Path(backup_dir) / "manifest.json"


def _load_manifest(backup_dir):
    p = _manifest_path(backup_dir)
    if p.exists():
        return json.loads(p.read_text())
    return {"assets": []}


def _save_manifest(backup_dir, manifest):
    _manifest_path(backup_dir).write_text(json.dumps(manifest, indent=2))


def backup_asset(client, theme_id, asset_key, backup_dir):
    """Snapshot the current asset value into backup_dir. Returns the value
    (None when the asset doesn't exist on the theme)."""
    value = client.get_theme_asset(theme_id, asset_key)
    time.sleep(ASSET_CALL_DELAY)
    if value is None:
        return None
    fname = asset_key.replace("/", "__")
    (Path(backup_dir) / fname).write_text(value)
    manifest = _load_manifest(backup_dir)
    manifest["theme_id"] = theme_id
    manifest["assets"] = [a for a in manifest["assets"] if a["asset_key"] != asset_key]
    manifest["assets"].append({
        "asset_key": asset_key,
        "file": fname,
        "sha256": hashlib.sha256(value.encode()).hexdigest(),
        "bytes": len(value),
        "backed_up_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_manifest(backup_dir, manifest)
    return value


def edit_asset(client, theme_id, asset_key, transform, backup_dir, yes=False):
    """get → backup → transform(text) → show diff → confirm → upsert.
    Returns True when the asset was changed."""
    current = backup_asset(client, theme_id, asset_key, backup_dir)
    if current is None:
        print(f"  SKIP {asset_key}: asset not found on theme")
        return False
    updated = transform(current)
    if updated == current:
        print(f"  OK   {asset_key}: no change needed")
        return False

    diff = difflib.unified_diff(
        current.splitlines(keepends=True), updated.splitlines(keepends=True),
        fromfile=f"a/{asset_key}", tofile=f"b/{asset_key}",
    )
    print("".join(diff))
    if not yes:
        answer = input(f"Apply this change to {asset_key}? [y/N] ").strip().lower()
        if answer != "y":
            print(f"  SKIP {asset_key}: declined")
            return False
    client.upsert_theme_asset(theme_id, asset_key, updated)
    time.sleep(ASSET_CALL_DELAY)
    print(f"  DONE {asset_key}: updated (backup in {backup_dir})")
    return True


def restore_from(client, backup_dir):
    """Re-upsert every asset recorded in a backup dir's manifest."""
    manifest = _load_manifest(backup_dir)
    theme_id = manifest.get("theme_id")
    if not theme_id or not manifest["assets"]:
        raise SystemExit(f"No restorable manifest in {backup_dir}")
    for a in manifest["assets"]:
        value = (Path(backup_dir) / a["file"]).read_text()
        client.upsert_theme_asset(theme_id, a["asset_key"], value)
        time.sleep(ASSET_CALL_DELAY)
        print(f"  RESTORED {a['asset_key']} ({a['bytes']} bytes)")
