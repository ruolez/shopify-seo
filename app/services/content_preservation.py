"""Hand a rewriting model the content it is about to replace.

Description generators used to see only the current description's word count,
so a full optimize silently erased whatever other tools had added — spec
tables, verdict sections, injected internal links. Every generator that
rewrites a body now gets the existing HTML and one shared preservation rule.
"""

import re

MIN_WORDS_WORTH_PRESERVING = 40
MAX_EXISTING_CHARS = 20000

_TAG_RE = re.compile(r"<[^>]+>")


def existing_description_block(current_html, preserve_rule):
    html = (current_html or "").strip()
    if len(_TAG_RE.sub(" ", html).split()) < MIN_WORDS_WORTH_PRESERVING:
        return None
    return (
        "CURRENT DESCRIPTION (HTML) — revise and extend this; do not start over:\n"
        f"{html[:MAX_EXISTING_CHARS]}\n\n"
        f"{preserve_rule}"
    )
