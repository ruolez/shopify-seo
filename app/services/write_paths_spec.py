"""Ratchet: content changes to existing pages go through apply_pipeline.

Scans the source for direct ShopifyClient update_* calls that carry content
fields. The allowlist holds the deliberate exceptions (publishing pages the
app owns, creating new objects). A new direct writer fails this test: route
it through apply_item_changes, or add it here with a reason.
"""

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[1]
CONTENT_KEYS = {"title", "description_html", "body", "body_html", "meta_title",
                "meta_description", "seo_title", "seo_description", "summary"}
WRITE_METHODS = {"update_product", "update_collection", "update_article", "update_page"}

ALLOWED = {
    # Pages and posts the app generates and owns end to end.
    "routes/ai_seo_routes.py::aeo_llms_apply",
    "routes/blog_routes.py::_build_and_push_landing",
    "routes/blog_routes.py::_wizard_step_publish",
    "routes/blog_routes.py::_run_session_media_task",
    "routes/blog_routes.py::_run",
    "routes/brand_routes.py::generate_brand_landing_page",
    "routes/landing_routes.py::publish_page",
    "routes/news_routes.py::_regenerate_news_hub",
    "routes/news_routes.py::_step_publish",
    "services/indexnow_service.py::setup_key",
    # Metas on a collection created in the same call.
    "services/content_gap_service.py::create_collection_on_shopify",
}


def direct_content_writers():
    found = set()
    for path in APP.rglob("*.py"):
        if path.name.endswith("_spec.py") or path.name == "shopify_client.py":
            continue
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(fn):
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr in WRITE_METHODS and len(call.args) >= 2):
                    continue
                arg = call.args[1]
                if isinstance(arg, ast.Dict):
                    keys = {k.value for k in arg.keys if isinstance(k, ast.Constant)}
                    if not keys & CONTENT_KEYS:
                        continue
                found.add(f"{path.relative_to(APP)}::{fn.name}")
    return found


def test_no_new_direct_content_writers():
    assert direct_content_writers() - ALLOWED == set()


def test_allowlist_has_no_stale_entries():
    assert ALLOWED - direct_content_writers() == set()
