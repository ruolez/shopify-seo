"""Refresh store prices written into description and meta text.

A one-time swap after a repricing: every dollar amount that states the
store's price is recomputed from the product's current variant prices.
Amounts printed on the packaging ("$1.49 Pre-Priced", "2 for $1.29") are not
store prices and stay. Anything that cannot be tied to a variant is flagged,
never guessed.
"""

import re

# Amounts the review pass read as packaging or market context that the
# rules cannot tell from a store price: product id -> raw amounts.
KEEP = {
    192283: {"$2.49", "$2.79"},
    197872: {"$1.69"},
    8466: {"$1.19"},
    8307: {"$1.29"},
    5881: {"$1.29"},
    8407: {"$1.99"},
    8435: {"$0.99", "$1.49"},
    8448: {"$1.49"},
    8092: {"$0.99"},
    8449: {"$1.49"},
    8402: {"$0.25"},
    8268: {"$2.00"},
    5058: {"$2.49"},
    7640: {"$1.39"},
}

# canonical field -> synced_products column
TEXT_FIELDS = {"body": "description", "meta_description": "meta_description"}

_AMOUNT = r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)"
_MENTION_RE = re.compile(_AMOUNT + r"(?:\s?([–-])\s?\$?\s?(\d[\d,]*(?:\.\d{1,2})?))?")
_TAG_RE = re.compile(r"<[^>]+>")

_PACKAGING_RE = re.compile(r"pre[\s-]?pric|prepric|\bPP\b|printed|marked|label|stamp|cents?\b|¢", re.I)
_N_FOR_RE = re.compile(r"\d\s*(?:-|\s)for(?:-|\s)$", re.I)
_STORE_BEFORE_RE = re.compile(
    r"(?:total price of|bulk pricing of|our price|priced at just|only|for just|costs?|retails? (?:at|for))\s*$", re.I)
_MSRP_AFTER_RE = re.compile(r"^\s*MSRP", re.I)
_COMPARATIVE_RE = re.compile(r"(?:under|less than|below|as low as|lower than)\s*$", re.I)
_RANGE_START_RE = re.compile(r"(?:from|between)\s*$", re.I)
_RANGE_JOIN_RE = re.compile(r"^\s*(?:to|and)\s*$", re.I)
_UNIT_RE = re.compile(r"^\s*(?:per|/|each)\s*(?:individual\s+)?([a-z]*)", re.I)
_UNIT_BEFORE_RE = re.compile(r"(?:cost|price)[\s-]+per[\s-]+(?:individual\s+)?([a-z]+)\b(?!.*\$)", re.I)
_BASED_ON_RE = re.compile(r"based on (?:the |a )?" + _AMOUNT, re.I)

_STICK_NOUNS = ("cigar", "cigarillo", "stick", "smoke", "unit", "wrap", "cigarette", "tube")
_PACK_NOUNS = ("pack", "pouch", "can", "box")
_PACKS_OF_RE = re.compile(
    r"(\d+)\s*(?:packs?|pouch(?:es)?|cans?|tins?|boxes|bags)\s*(?:of|x)\s*(\d+)"
    r"|(\d+)\s*/\s*(\d+)\b|(\d+)\s*x\s*(\d+)(?!\s*(?:g|ml|oz|mg)\b)", re.I)
_STICKS_RE = re.compile(r"box of (\d+)|(\d+)\s*(?:ct|count)\b|(\d+)\s*(?:cigars|cigarillos|wraps|sticks|cigarettes|tubes)\b", re.I)
_PACKS_RE = re.compile(r"(\d+)\s*(?:packs|pouches|cans)\b", re.I)


def _num(raw):
    return float(raw.replace(",", ""))


def _fmt(value, like):
    return f"${value:,.2f}" if "." in like else f"${round(value):,}"


def _text_before(text, pos, size):
    return _TAG_RE.sub("", text[max(0, pos - 200):pos])[-size:]


def _text_after(text, pos, size):
    return _TAG_RE.sub("", text[pos:pos + 200])[:size]


def _counts(names):
    """(packs, sticks) from the first title that states them."""
    for name in names:
        pair = _PACKS_OF_RE.search(name or "")
        if pair:
            a, b = [int(g) for g in pair.groups() if g][:2]
            return a, a * b
    for name in names:
        sticks = _STICKS_RE.search(name or "")
        packs = _PACKS_RE.search(name or "")
        if sticks or packs:
            return (int(packs.group(1)) if packs else None,
                    int(next(g for g in sticks.groups() if g)) if sticks else None)
    return None, None


def _is_packaging(text, m, title, variants, keep):
    raw = m.group(0)
    if raw in keep or raw in (title or "") or any(raw in (v["title"] or "") for v in variants):
        return True
    before, after = _text_before(text, m.start(), 32), _text_after(text, m.end(), 22)
    if _STORE_BEFORE_RE.search(before) or _MSRP_AFTER_RE.match(after):
        return False
    return bool(_N_FOR_RE.search(before) or _PACKAGING_RE.search(before + raw + after))


def _variants_once_at(amount, variants):
    """Variants whose old price was this amount, one per distinct new price."""
    hits = {}
    for v in variants:
        if v["old"] is not None and abs(_num(v["old"]) - amount) < 0.005:
            hits.setdefault(_num(v["new"]), v)
    return list(hits.values())


def _single(variants):
    news = {_num(v["new"]) for v in variants}
    olds = {_num(v["old"]) for v in variants if v["old"] is not None}
    return variants[0] if len(news) == 1 and len(olds) <= 1 else None


def _unit_price(amount, unit, base, title):
    packs, sticks = _counts([base["title"], title])
    count = sticks if unit.startswith(_STICK_NOUNS) else packs if unit.startswith(_PACK_NOUNS) else None
    scaled = amount * _num(base["new"]) / _num(base["old"]) if base["old"] and _num(base["old"]) else None
    if not count:
        return scaled, None if scaled is not None else "no_count"
    counted = _num(base["new"]) / count
    if scaled and not 0.6 <= counted / scaled <= 1.6:
        return None, "count_mismatch"
    return counted, None


def _new_value(text, mentions, i, title, variants, comparative):
    """(new value or None, flag reason or None) for a single-amount mention."""
    m = mentions[i]
    amount = _num(m.group(1))
    news = sorted(_num(v["new"]) for v in variants)
    unit_match = _UNIT_RE.match(_text_after(text, m.end(), 40))
    pair_next = i + 1 < len(mentions) and _RANGE_JOIN_RE.match(text[m.end():mentions[i + 1].start()])
    pair_prev = i > 0 and _RANGE_JOIN_RE.match(text[mentions[i - 1].end():m.start()])
    if not unit_match and pair_next:
        unit_match = _UNIT_RE.match(_text_after(text, mentions[i + 1].end(), 40))
    if not unit_match:
        unit_match = _UNIT_BEFORE_RE.search(_text_before(text, m.start(), 60))
    unit = unit_match.group(1).lower() if unit_match else ""

    # "per box", "per unit" on a box-sized amount: the whole variant's price.
    if unit_match and amount < news[0] / 2:
        based = _BASED_ON_RE.search(_text_after(text, m.end(), 120))
        based_on = _variants_once_at(_num(based.group(1)), variants) if based else []
        base = based_on[0] if len(based_on) == 1 else _single(variants)
        if base is None:
            return None, "no_variant"
        if not comparative and base["old"] and _num(base["old"]) == _num(base["new"]):
            return amount, None
        return _unit_price(amount, unit, base, title)

    matched = _variants_once_at(amount, variants)
    if len(matched) == 1:
        return _num(matched[0]["new"]), None
    if len(matched) > 1:
        return None, "no_variant"
    if any(abs(n - amount) < 0.005 for n in news):
        return amount, None
    starts_range = _RANGE_START_RE.search(_text_before(text, m.start(), 20))
    if starts_range and (pair_next or not pair_prev):
        return news[0], None
    if pair_prev:
        return news[-1], None
    if amount < news[0] / 2:
        return None, "no_unit"
    single = _single(variants)
    if single:
        return _num(single["new"]), None
    return None, "no_variant"


def refresh_text_prices(text, title, variants, keep=()):
    """Returns {text, changes: [(old, new)], flags: [(amount, reason)]}.

    variants: [{title, old, new}] with prices as strings; old is the price the
    text was written against (None when unknown). keep: raw amounts that are
    not store prices and must stay."""
    text = text or ""
    mentions = list(_MENTION_RE.finditer(text))
    out, pos, changes, flags = [], 0, [], []
    for i, m in enumerate(mentions):
        raw, replacement = m.group(0), None
        if not variants or _is_packaging(text, m, title, variants, keep):
            pass
        elif m.group(3):
            news = sorted(_num(v["new"]) for v in variants)
            lo, hi = _fmt(news[0], m.group(1)), _fmt(news[-1], m.group(3))
            replacement = lo if lo == hi else f"{lo}{m.group(2)}{hi}"
        else:
            comparative = _COMPARATIVE_RE.search(_text_before(text, m.start(), 20))
            value, reason = _new_value(text, mentions, i, title, variants, comparative)
            if reason:
                flags.append((raw, reason))
            elif comparative:
                if value > _num(m.group(1)) + 0.005:
                    flags.append((raw, "comparative_false"))
            else:
                replacement = _fmt(value, m.group(1))
        if replacement and replacement != raw:
            changes.append((raw, replacement))
            out.append(text[pos:m.start()] + replacement)
            pos = m.end()
    out.append(text[pos:])
    return {"text": "".join(out), "changes": changes, "flags": flags}


def _price_key(value):
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def prices_changed(old_variants, new_variants):
    """True when any variant both lists has a different price now. Variants
    that were added or removed do not count: the text never named them."""
    old = {v.get("id"): _price_key(v.get("price")) for v in old_variants or []}
    return any(
        v.get("id") in old and old[v.get("id")] is not None and _price_key(v.get("price")) != old[v.get("id")]
        for v in new_variants or []
    )


def pair_variants(old_variants, new_variants):
    """[{title, old, new}] for refresh_text_prices: old = the price the text
    was written against (None when unknown), new = the current price."""
    old = {v.get("id"): v.get("price") for v in old_variants or []}
    return [{"title": v.get("title"), "old": old.get(v.get("id")), "new": v.get("price")} for v in new_variants or []]


def plan_price_text(row, variants, keep=()):
    """Refresh every text field of a product row. Returns (fields to write
    keyed canonically, changes [(field, old, new)], flags [(field, amount, reason)])."""
    fields, changes, flags = {}, [], []
    for field, column in TEXT_FIELDS.items():
        result = refresh_text_prices(row.get(column), row.get("title"), variants, keep=keep)
        if result["text"] != (row.get(column) or ""):
            fields[field] = result["text"]
        changes += [(field, *c) for c in result["changes"]]
        flags += [(field, *f) for f in result["flags"]]
    return fields, changes, flags
