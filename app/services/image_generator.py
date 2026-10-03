import base64
import io
import logging
import math
import os
import random
import re
import textwrap

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.settings_manager import get_setting
from app.prompt_defaults import DEFAULTS

logger = logging.getLogger(__name__)


FONT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "static", "fonts", "Inter-Bold.ttf"
)

# Brand text rendered on generated placeholder/collection images.
BRAND_TEXT = "A2Z"


def _load_font(size):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except (OSError, IOError):
        return ImageFont.load_default(size)


def _draw_smoke(img, width, height):
    """Layer soft, translucent ellipses to create a subtle smoke / mist effect."""
    smoke = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(smoke)

    rng = random.Random(42)

    sx, sy = width / 1200, height / 1200
    clouds = [
        # (cx, cy, rx, ry, alpha, color_rgb)
        (int(600 * sx), int(950 * sy), int(500 * sx), int(180 * sy), 18, (200, 210, 220)),
        (int(300 * sx), int(1000 * sy), int(400 * sx), int(160 * sy), 14, (190, 200, 215)),
        (int(900 * sx), int(980 * sy), int(420 * sx), int(150 * sy), 16, (195, 205, 218)),
        (int(600 * sx), int(1050 * sy), int(550 * sx), int(130 * sy), 12, (210, 218, 228)),
        (int(150 * sx), int(1020 * sy), int(350 * sx), int(120 * sy), 10, (205, 212, 222)),
        (int(1050 * sx), int(1010 * sy), int(380 * sx), int(140 * sy), 13, (198, 208, 220)),
        (int(450 * sx), int(900 * sy), int(300 * sx), int(100 * sy), 8, (215, 222, 232)),
        (int(750 * sx), int(920 * sy), int(320 * sx), int(110 * sy), 9, (208, 216, 226)),
    ]

    for cx, cy, rx, ry, alpha, color in clouds:
        cx += rng.randint(-30, 30)
        cy += rng.randint(-20, 20)
        bbox = [cx - rx, cy - ry, cx + rx, cy + ry]
        draw.ellipse(bbox, fill=(*color, alpha))

    blur_r = max(30, int(60 * min(width, height) / 1200))
    smoke = smoke.filter(ImageFilter.GaussianBlur(radius=blur_r))
    img.paste(Image.alpha_composite(img.convert("RGBA"), smoke).convert("RGB"))


def _draw_gradient_line(draw, y, width, thickness=2):
    """Draw a thin accent line that fades from transparent to teal and back."""
    center_x = width // 2
    half_len = 160
    for x in range(center_x - half_len, center_x + half_len):
        dist = abs(x - center_x) / half_len
        opacity = int(255 * (1 - dist * dist))
        r, g, b = 13, 148, 136  # teal #0d9488
        draw.line([(x, y), (x, y + thickness - 1)], fill=(r, g, b, opacity))


def generate_placeholder_image(product_title, handle=None):
    width, height = 800, 800
    img = Image.new("RGB", (width, height), "#f8f9fb")
    draw = ImageDraw.Draw(img)

    # Soft radial vignette — very subtle darkening at edges
    vignette = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    v_draw = ImageDraw.Draw(vignette)
    cx, cy = width // 2, height // 2
    max_dist = math.sqrt(cx * cx + cy * cy)
    for ring in range(0, int(max_dist), 8):
        alpha = int(12 * (ring / max_dist) ** 2)
        if alpha > 0:
            v_draw.ellipse(
                [cx - ring, cy - ring, cx + ring, cy + ring],
                outline=(180, 190, 205, alpha),
            )
    vignette = vignette.filter(ImageFilter.GaussianBlur(radius=30))
    img = Image.alpha_composite(img.convert("RGBA"), vignette).convert("RGB")

    # Subtle smoke / mist in the lower portion
    _draw_smoke(img, width, height)

    draw = ImageDraw.Draw(img)

    # A2Z branding — centered vertically in upper half
    a2z_font = _load_font(140)
    a2z_text = BRAND_TEXT
    a2z_bbox = draw.textbbox((0, 0), a2z_text, font=a2z_font)
    a2z_w = a2z_bbox[2] - a2z_bbox[0]
    a2z_h = a2z_bbox[3] - a2z_bbox[1]
    a2z_x = (width - a2z_w) // 2
    a2z_y = 200
    draw.text((a2z_x, a2z_y), a2z_text, fill="#2d3748", font=a2z_font)

    # Gradient teal accent line — well below A2Z
    line_y = a2z_y + a2z_h + 60
    rgba_img = img.convert("RGBA")
    line_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    line_draw = ImageDraw.Draw(line_layer)
    _draw_gradient_line(line_draw, line_y, width, thickness=2)
    img = Image.alpha_composite(rgba_img, line_layer).convert("RGB")
    draw = ImageDraw.Draw(img)

    # "Image Coming Soon" — elegant, lighter weight feel
    sub_font = _load_font(36)
    sub_text = "Image Coming Soon"
    sub_bbox = draw.textbbox((0, 0), sub_text, font=sub_font)
    sub_w = sub_bbox[2] - sub_bbox[0]
    sub_x = (width - sub_w) // 2
    sub_y = line_y + 45
    draw.text((sub_x, sub_y), sub_text, fill="#9ca3af", font=sub_font)

    # Product title — word-wrapped, centered, very light
    if product_title:
        title_font = _load_font(28)
        max_chars = 42
        wrapped = textwrap.wrap(product_title, width=max_chars)[:3]
        display_lines = wrapped
        if len(textwrap.wrap(product_title, width=max_chars)) > 3:
            display_lines[-1] = display_lines[-1][: max_chars - 3] + "..."

        title_y = sub_y + 90
        line_spacing = 40
        for line in display_lines:
            line_bbox = draw.textbbox((0, 0), line, font=title_font)
            line_w = line_bbox[2] - line_bbox[0]
            line_x = (width - line_w) // 2
            draw.text((line_x, title_y), line, fill="#b0b8c4", font=title_font)
            title_y += line_spacing

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)

    if handle:
        filename = f"{handle}.png"
    else:
        safe_title = "".join(
            c if c.isalnum() or c in "-_ " else "" for c in (product_title or "placeholder")
        )[:50]
        filename = f"{safe_title.strip().replace(' ', '-').lower()}.png"
    alt_text = product_title if product_title else "Product image"

    return buf.getvalue(), filename, alt_text


def generate_collection_image(collection_title, product_images):
    width, height = 1200, 628
    canvas = Image.new("RGB", (width, height), "#f0f2f5")

    loaded = []
    for img_bytes in product_images:
        try:
            loaded.append(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
        except Exception:
            continue

    if len(loaded) == 0:
        _draw_branded_placeholder(canvas, width, height, collection_title)
    elif len(loaded) == 1:
        _place_cover(canvas, loaded[0], 0, 0, width, height)
    elif len(loaded) == 2:
        half = width // 2
        _place_cover(canvas, loaded[0], 0, 0, half, height)
        _place_cover(canvas, loaded[1], half, 0, half, height)
    else:
        hero_w = int(width * 0.6)
        grid_w = width - hero_w
        _place_cover(canvas, loaded[0], 0, 0, hero_w, height)
        grid_count = min(len(loaded) - 1, 3)
        cell_h = height // grid_count
        for i in range(grid_count):
            _place_cover(canvas, loaded[1 + i], hero_w, i * cell_h, grid_w, cell_h)

    _draw_title_bar(canvas, width, height, collection_title)
    _draw_corner_brand(canvas, width)

    buf = io.BytesIO()
    canvas.save(buf, format="WEBP", quality=80, method=6)
    buf.seek(0)

    safe_title = "".join(
        c if c.isalnum() or c in "-_ " else "" for c in (collection_title or "collection")
    )[:50]
    filename = f"collection-{safe_title.strip().replace(' ', '-').lower()}.webp"
    alt_text = f"{collection_title} - collection promo image" if collection_title else "Collection promo image"

    return buf.getvalue(), filename, alt_text, width, height


def _place_cover(canvas, img, x, y, w, h):
    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    new_w = int(src_w * scale)
    new_h = int(src_h * scale)
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    crop_x = (new_w - w) // 2
    crop_y = (new_h - h) // 2
    cropped = resized.crop((crop_x, crop_y, crop_x + w, crop_y + h))
    canvas.paste(cropped, (x, y))


def _draw_title_bar(canvas, width, height, title):
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    bar_h = 100
    bar_y = height - bar_h
    for i in range(bar_h):
        alpha = int(200 * (i / bar_h))
        draw.rectangle([0, bar_y + i, width, bar_y + i + 1], fill=(0, 0, 0, alpha))

    title_font = _load_font(36)
    tag_font = _load_font(18)

    title_text = title or "Collection"
    if len(title_text) > 50:
        title_text = title_text[:47] + "..."

    draw.text((40, bar_y + 20), title_text, fill=(255, 255, 255, 255), font=title_font)
    draw.text((40, bar_y + 62), "Shop the Collection", fill=(200, 200, 200, 220), font=tag_font)

    canvas.paste(Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB"))


def _draw_corner_brand(canvas, width):
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    brand_font = _load_font(16)
    draw.text((width - 60, 12), BRAND_TEXT, fill=(255, 255, 255, 140), font=brand_font)
    canvas.paste(Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB"))


def _draw_branded_placeholder(canvas, width, height, title):
    draw = ImageDraw.Draw(canvas)
    _draw_smoke(canvas, width, height)
    draw = ImageDraw.Draw(canvas)

    brand_font = _load_font(80)
    brand_text = BRAND_TEXT
    bb = draw.textbbox((0, 0), brand_text, font=brand_font)
    bw = bb[2] - bb[0]
    draw.text(((width - bw) // 2, height // 2 - 80), brand_text, fill="#2d3748", font=brand_font)

    sub_font = _load_font(24)
    sub_text = title or "Collection"
    if len(sub_text) > 60:
        sub_text = sub_text[:57] + "..."
    sb = draw.textbbox((0, 0), sub_text, font=sub_font)
    sw = sb[2] - sb[0]
    draw.text(((width - sw) // 2, height // 2 + 30), sub_text, fill="#9ca3af", font=sub_font)


def _resize_for_ai(image_bytes, max_size=512):
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.thumbnail((max_size, max_size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf.getvalue()
    except Exception:
        return image_bytes


def compress_banner_image(image_bytes, target_width=1200, target_height=628):
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    src_w, src_h = img.size
    scale = max(target_width / src_w, target_height / src_h)
    new_w = int(src_w * scale)
    new_h = int(src_h * scale)
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    crop_x = (new_w - target_width) // 2
    crop_y = (new_h - target_height) // 2
    cropped = resized.crop((crop_x, crop_y, crop_x + target_width, crop_y + target_height))

    buf = io.BytesIO()
    cropped.save(buf, format="WEBP", quality=80, method=6)
    buf.seek(0)
    compressed = buf.getvalue()
    logger.info("Compressed banner: %d bytes -> %d bytes (WebP)", len(image_bytes), len(compressed))
    return compressed, target_width, target_height


def build_seo_alt_text(collection_title, keywords=None):
    title = (collection_title or "").strip()
    if not keywords:
        return f"Shop {title} collection"[:125]

    title_lower = title.lower()
    unique_kws = []
    for kw_entry in keywords:
        kw = kw_entry["keyword"] if isinstance(kw_entry, dict) else str(kw_entry)
        if kw.lower() not in title_lower:
            unique_kws.append(kw)
        if len(unique_kws) >= 2:
            break

    if not unique_kws:
        return f"Shop {title} collection"[:125]

    kw_part = " and ".join(unique_kws)
    alt = f"Shop {title} - {kw_part} collection"
    if len(alt) > 125:
        alt = alt[:122] + "..."
    return alt


def _make_image_result(collection_title, image_bytes, keywords=None):
    raw_size = len(image_bytes)
    compressed, width, height = compress_banner_image(image_bytes)
    alt_text = build_seo_alt_text(collection_title, keywords)

    safe_title = "".join(
        c if c.isalnum() or c in "-_ " else "" for c in (collection_title or "collection")
    )[:50]
    slug = safe_title.strip().replace(" ", "-").lower()

    kw_slug = ""
    if keywords:
        for kw_entry in keywords:
            kw = kw_entry["keyword"] if isinstance(kw_entry, dict) else str(kw_entry)
            candidate = "".join(c if c.isalnum() or c == " " else "" for c in kw)[:30].strip().replace(" ", "-").lower()
            if candidate and candidate != slug:
                kw_slug = f"-{candidate}"
                break

    filename = f"collection-{slug}{kw_slug}.webp"
    logger.info("AI image generated for '%s' (%d bytes raw -> %d bytes compressed)", collection_title, raw_size, len(compressed))
    return compressed, filename, alt_text, width, height


def generate_blog_image_ai(article_title, keywords=None, product_titles=None):
    try:
        import requests as http_req
        from app.ai_agent import _get_ai_config

        config = _get_ai_config()
        image_model = config.get("image_model") or "google/gemini-2.5-flash-image"

        kw_text = ", ".join(keywords) if keywords else ""
        product_context = ""
        if product_titles:
            product_context = (
                f'\nPRODUCT CONTEXT (this blog features these products — reflect their theme visually):\n'
                f'{", ".join(product_titles[:10])}\n'
            )

        blog_image_prompt = get_setting("prompts.blog_image", DEFAULTS["prompts.blog_image"])
        try:
            prompt_text = blog_image_prompt.format(article_title=article_title)
        except (KeyError, IndexError):
            prompt_text = DEFAULTS["prompts.blog_image"].format(article_title=article_title)

        content_parts = [
            {
                "type": "text",
                "text": (
                    prompt_text
                    + (f'\nRelated keywords: {kw_text}' if kw_text else '')
                    + product_context
                ),
            }
        ]

        logger.info("Generating AI blog image with model: %s", image_model)

        payload = {
            "model": image_model,
            "messages": [{"role": "user", "content": content_parts}],
            "max_tokens": 4096,
            "modalities": ["image", "text"],
            "image_config": {"aspect_ratio": "16:9"},
        }

        resp = http_req.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()

        for choice in data.get("choices", []):
            msg = choice.get("message", {})

            for img in msg.get("images", []):
                url = img.get("image_url", {}).get("url", "")
                if url and "base64," in url:
                    b64_data = url.split("base64,", 1)[1]
                    return _make_blog_image_result(article_title, base64.b64decode(b64_data))

            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url and "base64," in url:
                            b64_data = url.split("base64,", 1)[1]
                            return _make_blog_image_result(article_title, base64.b64decode(b64_data))

            if isinstance(content, str) and "data:image" in content:
                match = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", content)
                if match:
                    return _make_blog_image_result(article_title, base64.b64decode(match.group(1)))

        logger.warning("AI blog image generation did not return image for '%s'", article_title)
        return None

    except Exception as e:
        logger.warning("AI blog image generation failed for '%s': %s", article_title, e)
        return None


def _make_blog_image_result(title, image_bytes):
    compressed, width, height = compress_banner_image(image_bytes)
    safe_title = "".join(
        c if c.isalnum() or c in "-_ " else "" for c in (title or "blog")
    )[:50]
    slug = safe_title.strip().replace(" ", "-").lower()
    filename = f"blog-{slug}.webp"
    alt_text = f"{title} - blog article header" if title else "Blog article header"

    logger.info("AI blog image generated for '%s' (%d bytes)", title, len(compressed))
    return compressed, filename, alt_text, width, height


def generate_news_inline_image(subject_prompt, alt_text=None):
    """Generate an inline (in-body) image for a news article.

    Uses the user-editable `prompts.news_inline_image` setting as the style/base
    instruction, combined with the per-image `subject_prompt` produced by the
    news_analyze task. Returns (compressed_bytes, filename, alt_text, width,
    height) at a 4:3 aspect ratio, or None on failure.
    """
    try:
        import requests as http_req
        from app.ai_agent import _get_ai_config

        config = _get_ai_config()
        image_model = config.get("image_model") or "google/gemini-2.5-flash-image"

        base_prompt = get_setting("prompts.news_inline_image", DEFAULTS["prompts.news_inline_image"])
        try:
            prompt_text = base_prompt.format(subject=subject_prompt or "")
        except (KeyError, IndexError):
            prompt_text = f"{base_prompt}\n\nSUBJECT: {subject_prompt or ''}"

        payload = {
            "model": image_model,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt_text}]}],
            "max_tokens": 4096,
            "modalities": ["image", "text"],
            "image_config": {"aspect_ratio": "4:3"},
        }

        logger.info("Generating AI news inline image with model: %s", image_model)
        resp = http_req.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()

        for choice in data.get("choices", []):
            msg = choice.get("message", {})
            for img in msg.get("images", []):
                url = img.get("image_url", {}).get("url", "")
                if url and "base64," in url:
                    b64_data = url.split("base64,", 1)[1]
                    return _make_news_inline_result(alt_text, base64.b64decode(b64_data))
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url and "base64," in url:
                            b64_data = url.split("base64,", 1)[1]
                            return _make_news_inline_result(alt_text, base64.b64decode(b64_data))
            if isinstance(content, str) and "data:image" in content:
                match = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", content)
                if match:
                    return _make_news_inline_result(alt_text, base64.b64decode(match.group(1)))

        logger.warning("AI news inline image generation did not return an image")
        return None
    except Exception as e:
        logger.warning("AI news inline image generation failed: %s", e)
        return None


def _make_news_inline_result(alt_text, image_bytes):
    compressed, width, height = compress_banner_image(image_bytes, target_width=1024, target_height=768)
    safe = "".join(c if c.isalnum() or c in "-_ " else "" for c in (alt_text or "news-inline"))[:50]
    slug = safe.strip().replace(" ", "-").lower() or "news-inline"
    filename = f"news-{slug}.webp"
    alt = alt_text or "News article image"
    logger.info("AI news inline image generated (%d bytes)", len(compressed))
    return compressed, filename, alt, width, height


def generate_blog_hero_image_ai(blog_title, intro_text=None, article_titles=None, tags=None):
    try:
        import requests as http_req
        from app.ai_agent import _get_ai_config

        config = _get_ai_config()
        image_model = config.get("image_model") or "google/gemini-2.5-flash-image"

        context_parts = []
        if article_titles:
            context_parts.append(f"Recent articles: {', '.join(article_titles[:5])}")
        if tags:
            context_parts.append(f"Popular topics: {', '.join(tags[:6])}")
        if intro_text:
            context_parts.append(f"Blog description: {intro_text[:200]}")
        context_str = "\n".join(context_parts)

        blog_hero_prompt = get_setting("prompts.blog_hero_image", DEFAULTS["prompts.blog_hero_image"])
        try:
            prompt_text = blog_hero_prompt.format(blog_title=blog_title)
        except (KeyError, IndexError):
            prompt_text = DEFAULTS["prompts.blog_hero_image"].format(blog_title=blog_title)

        content_parts = [
            {
                "type": "text",
                "text": (
                    prompt_text
                    + (f'\n\nBLOG CONTEXT (use this to guide subject matter and mood):\n{context_str}' if context_str else '')
                ),
            }
        ]

        logger.info("Generating AI blog hero image with model: %s", image_model)

        payload = {
            "model": image_model,
            "messages": [{"role": "user", "content": content_parts}],
            "max_tokens": 4096,
            "modalities": ["image", "text"],
            "image_config": {"aspect_ratio": "16:9"},
        }

        resp = http_req.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()

        for choice in data.get("choices", []):
            msg = choice.get("message", {})

            for img in msg.get("images", []):
                url = img.get("image_url", {}).get("url", "")
                if url and "base64," in url:
                    b64_data = url.split("base64,", 1)[1]
                    return _make_hero_image_result(blog_title, base64.b64decode(b64_data))

            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url and "base64," in url:
                            b64_data = url.split("base64,", 1)[1]
                            return _make_hero_image_result(blog_title, base64.b64decode(b64_data))

            if isinstance(content, str) and "data:image" in content:
                match = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", content)
                if match:
                    return _make_hero_image_result(blog_title, base64.b64decode(match.group(1)))

        logger.warning("AI blog hero image generation did not return image for '%s'", blog_title)
        return None

    except Exception as e:
        logger.warning("AI blog hero image generation failed for '%s': %s", blog_title, e)
        return None


def _make_hero_image_result(title, image_bytes):
    compressed, width, height = compress_banner_image(image_bytes)
    safe_title = "".join(
        c if c.isalnum() or c in "-_ " else "" for c in (title or "blog")
    )[:50]
    slug = safe_title.strip().replace(" ", "-").lower()
    filename = f"blog-hero-{slug}.webp"
    alt_text = f"{title} blog" if title else "Blog landing page banner"

    logger.info("AI blog hero image generated for '%s' (%d bytes)", title, len(compressed))
    return compressed, filename, alt_text, width, height


def generate_landing_hero_image_ai(page_title, page_description=None, keywords=None, product_image_urls=None):
    try:
        import requests as http_req
        from app.ai_agent import _get_ai_config

        config = _get_ai_config()
        image_model = config.get("image_model") or "google/gemini-2.5-flash-image"

        context_parts = []
        if page_description:
            context_parts.append(f"Page description: {page_description[:200]}")
        if keywords:
            context_parts.append(f"Target keywords: {', '.join(keywords[:6])}")
        if product_image_urls:
            context_parts.append(
                f"REFERENCE IMAGES: I have attached {len(product_image_urls)} real product photos from this store. "
                "These show the ACTUAL products with real packaging, labels, and branding. "
                "The hero image MUST include products that accurately match this real packaging — "
                "same box shapes, label designs, colors, and brand names visible on the packaging. "
                "Select 3-5 from different categories and arrange them artfully in the lower third of the frame."
            )
        context_str = "\n".join(context_parts)

        hero_prompt = get_setting("prompts.landing_hero_image", DEFAULTS["prompts.landing_hero_image"])
        try:
            prompt_text = hero_prompt.format(page_title=page_title)
        except (KeyError, IndexError):
            prompt_text = DEFAULTS["prompts.landing_hero_image"].format(page_title=page_title)

        content_parts = [
            {
                "type": "text",
                "text": (
                    prompt_text
                    + (f'\n\nPAGE CONTEXT:\n{context_str}' if context_str else '')
                ),
            }
        ]

        # Attach real product images as visual references (limit to 5)
        if product_image_urls:
            for img_url in product_image_urls[:5]:
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": img_url},
                })

        logger.info("Generating AI landing hero image with model: %s", image_model)

        payload = {
            "model": image_model,
            "messages": [{"role": "user", "content": content_parts}],
            "max_tokens": 4096,
            "modalities": ["image", "text"],
            "image_config": {"aspect_ratio": "16:9"},
        }

        resp = http_req.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()

        for choice in data.get("choices", []):
            msg = choice.get("message", {})

            for img in msg.get("images", []):
                url = img.get("image_url", {}).get("url", "")
                if url and "base64," in url:
                    b64_data = url.split("base64,", 1)[1]
                    return _make_landing_hero_result(page_title, base64.b64decode(b64_data))

            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url and "base64," in url:
                            b64_data = url.split("base64,", 1)[1]
                            return _make_landing_hero_result(page_title, base64.b64decode(b64_data))

            if isinstance(content, str) and "data:image" in content:
                match = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", content)
                if match:
                    return _make_landing_hero_result(page_title, base64.b64decode(match.group(1)))

        logger.warning("AI landing hero image generation did not return image for '%s'", page_title)
        return None

    except Exception as e:
        logger.warning("AI landing hero image generation failed for '%s': %s", page_title, e)
        return None


def _make_landing_hero_result(title, image_bytes):
    compressed, width, height = compress_banner_image(image_bytes)
    safe_title = "".join(
        c if c.isalnum() or c in "-_ " else "" for c in (title or "landing")
    )[:50]
    slug = safe_title.strip().replace(" ", "-").lower()
    filename = f"landing-hero-{slug}.webp"
    alt_text = f"{title} landing page" if title else "Landing page hero banner"

    logger.info("AI landing hero image generated for '%s' (%d bytes)", title, len(compressed))
    return compressed, filename, alt_text, width, height


def generate_brand_logo_image(brand_name, logo_image_bytes, custom_prompt=None):
    try:
        import requests as http_req
        from app.ai_agent import _get_ai_config

        config = _get_ai_config()
        image_model = config.get("image_model") or "google/gemini-2.5-flash-image"

        resized_logo = _resize_for_ai(logo_image_bytes, max_size=512)
        b64_logo = base64.b64encode(resized_logo).decode("utf-8")

        base_prompt = get_setting("prompts.brand_logo", DEFAULTS["prompts.brand_logo"])
        try:
            prompt_text = base_prompt.format(brand_name=brand_name)
        except (KeyError, IndexError):
            prompt_text = DEFAULTS["prompts.brand_logo"].format(brand_name=brand_name)
        if custom_prompt:
            prompt_text += f"\n\nAdditional instructions: {custom_prompt}"

        content_parts = [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_logo}"}},
        ]

        logger.info("Generating AI brand logo image for '%s' with model: %s", brand_name, image_model)

        payload = {
            "model": image_model,
            "messages": [{"role": "user", "content": content_parts}],
            "max_tokens": 4096,
            "modalities": ["image", "text"],
            "image_config": {"aspect_ratio": "16:9"},
        }

        resp = http_req.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()

        for choice in data.get("choices", []):
            msg = choice.get("message", {})

            for img in msg.get("images", []):
                url = img.get("image_url", {}).get("url", "")
                if url and "base64," in url:
                    b64_data = url.split("base64,", 1)[1]
                    return _make_brand_image_result(brand_name, base64.b64decode(b64_data))

            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url and "base64," in url:
                            b64_data = url.split("base64,", 1)[1]
                            return _make_brand_image_result(brand_name, base64.b64decode(b64_data))

            if isinstance(content, str) and "data:image" in content:
                match = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", content)
                if match:
                    return _make_brand_image_result(brand_name, base64.b64decode(match.group(1)))

        logger.warning("AI brand logo generation did not return image for '%s'", brand_name)
        return None

    except Exception as e:
        logger.warning("AI brand logo generation failed for '%s': %s", brand_name, e)
        return None


def _make_brand_image_result(brand_name, image_bytes):
    compressed, width, height = compress_banner_image(image_bytes)
    safe_name = "".join(
        c if c.isalnum() or c in "-_ " else "" for c in (brand_name or "brand")
    )[:50]
    slug = safe_name.strip().replace(" ", "-").lower()
    filename = f"brand-{slug}.webp"
    alt_text = f"{brand_name} brand collection" if brand_name else "Brand collection banner"

    logger.info("AI brand logo image generated for '%s' (%d bytes)", brand_name, len(compressed))
    return compressed, filename, alt_text, width, height


def _get_collection_bg_styles():
    return get_setting("lists.collection_image_styles", DEFAULTS["lists.collection_image_styles"])


def generate_collection_image_ai(collection_title, product_image_bytes_list, product_titles=None, keywords=None):
    try:
        import requests as http_req
        from app.ai_agent import _get_ai_config

        config = _get_ai_config()
        image_model = config.get("image_model") or "google/gemini-2.5-flash-image"

        titles_str = ", ".join(product_titles) if product_titles else "various products"

        style = random.choice(_get_collection_bg_styles())

        collection_prompt = get_setting("prompts.collection_image", DEFAULTS["prompts.collection_image"])
        try:
            prompt_text = collection_prompt.format(
                collection_title=collection_title,
                style_bg=style["bg"],
                style_mood=style["mood"],
                style_palette=style["palette"],
            )
        except (KeyError, IndexError):
            prompt_text = DEFAULTS["prompts.collection_image"].format(
                collection_title=collection_title,
                style_bg=style["bg"],
                style_mood=style["mood"],
                style_palette=style["palette"],
            )

        content_parts = [
            {
                "type": "text",
                "text": prompt_text,
            }
        ]

        for img_bytes in product_image_bytes_list[:6]:
            resized = _resize_for_ai(img_bytes)
            b64 = base64.b64encode(resized).decode("utf-8")
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })

        logger.info("Generating AI collection image with model: %s", image_model)

        payload = {
            "model": image_model,
            "messages": [{"role": "user", "content": content_parts}],
            "max_tokens": 4096,
            "modalities": ["image", "text"],
            "image_config": {"aspect_ratio": "16:9"},
        }

        resp = http_req.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()

        for choice in data.get("choices", []):
            msg = choice.get("message", {})

            for img in msg.get("images", []):
                url = img.get("image_url", {}).get("url", "")
                if url and "base64," in url:
                    b64_data = url.split("base64,", 1)[1]
                    return _make_image_result(collection_title, base64.b64decode(b64_data), keywords)

            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url and "base64," in url:
                            b64_data = url.split("base64,", 1)[1]
                            return _make_image_result(collection_title, base64.b64decode(b64_data), keywords)

            if isinstance(content, str) and "data:image" in content:
                match = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", content)
                if match:
                    return _make_image_result(collection_title, base64.b64decode(match.group(1)), keywords)

        msg = data.get("choices", [{}])[0].get("message", {})
        refusal = msg.get("refusal")
        if refusal:
            logger.warning("AI refused to generate image for '%s': %s", collection_title, refusal)
        else:
            logger.warning("AI response did not contain an image for '%s'. Content: %s",
                            collection_title, str(msg.get("content", ""))[:200])
        return None

    except Exception as e:
        logger.warning("AI image generation failed for '%s': %s", collection_title, e)
        return None


# ── Visual blog pipeline (hero + inline article images) ───────────

def _slugify_for_filename(text, max_len=50):
    safe = "".join(c if c.isalnum() or c in "-_ " else "" for c in (text or ""))[:max_len]
    return safe.strip().replace(" ", "-").lower().strip("-") or "image"


def _openrouter_image(prompt_text, aspect_ratio="4:3", reference_images=None, model=None):
    """Single OpenRouter image call. Returns raw image bytes or None.

    reference_images: optional list of image bytes fed as multimodal input so
    the model can reproduce real products in the scene.
    """
    import requests as http_req
    from app.ai_agent import _get_ai_config

    config = _get_ai_config()
    image_model = model or config.get("image_model") or "google/gemini-2.5-flash-image"

    content_parts = [{"type": "text", "text": prompt_text}]
    for ref in (reference_images or [])[:6]:
        try:
            small = _resize_for_ai(ref, max_size=512)
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + base64.b64encode(small).decode("utf-8")},
            })
        except Exception:
            continue

    payload = {
        "model": image_model,
        "messages": [{"role": "user", "content": content_parts}],
        "max_tokens": 4096,
        "modalities": ["image", "text"],
        "image_config": {"aspect_ratio": aspect_ratio},
    }
    logger.info("Generating AI article image (%s, %d refs) with model: %s", aspect_ratio, len(content_parts) - 1, image_model)
    resp = http_req.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()

    for choice in data.get("choices", []):
        msg = choice.get("message", {})
        for img in msg.get("images", []) or []:
            url = img.get("image_url", {}).get("url", "")
            if url and "base64," in url:
                return base64.b64decode(url.split("base64,", 1)[1])
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if url and "base64," in url:
                        return base64.b64decode(url.split("base64,", 1)[1])
        if isinstance(content, str) and "data:image" in content:
            match = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", content)
            if match:
                return base64.b64decode(match.group(1))
    logger.warning("AI image generation returned no image (model %s)", image_model)
    return None


def _format_prompt(key, **kwargs):
    template = get_setting(key, DEFAULTS[key])
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return DEFAULTS[key].format(**kwargs)


def generate_article_hero_image(article_title, subject_prompt=None, alt_text=None, keywords=None, reference_images=None):
    """Hero (featured) image for a visual blog article: 16:9 → 1200×628 WebP.

    When reference_images (real product photos) are given, the product-scene
    hero prompt is used so the model reproduces the actual product(s), exactly
    like inline product images. Returns (bytes, filename, alt_text, width,
    height) or None. No text is ever rendered into the image; the alt is
    LLM-authored when provided.
    """
    try:
        subject = (subject_prompt or "").strip() or f"An evocative editorial scene representing: {article_title}"
        refs = [r for r in (reference_images or []) if r]
        key = "prompts.article_hero_product_scene_image" if refs else "prompts.article_hero_image"
        prompt_text = _format_prompt(key, article_title=article_title, subject=subject)
        if keywords:
            prompt_text += f"\nTopic context: {', '.join(str(k) for k in keywords[:4])}"
        raw = _openrouter_image(prompt_text, aspect_ratio="16:9", reference_images=refs)
        if not raw:
            return None
        compressed, width, height = compress_banner_image(raw)
        filename = f"{_slugify_for_filename(article_title, 60)}.webp"
        alt = (alt_text or "").strip()[:125] or f"{article_title}"[:125]
        logger.info("Article hero image generated for '%s' (%d bytes, %d refs)", article_title, len(compressed), len(refs))
        return compressed, filename, alt, width, height
    except Exception as e:
        logger.warning("Article hero image failed for '%s': %s", article_title, e)
        return None


def generate_article_inline_image(spec, article_slug, index, reference_images=None):
    """Inline (in-body) image from a media-plan spec: 4:3 → 1024×768 WebP.

    spec: {prompt, alt, caption, kind, ...}. When reference_images (real product
    photos) are given, the product-scene prompt is used so the model reproduces
    the actual product. Returns (bytes, filename, alt, width, height) or None.
    """
    try:
        subject = (spec.get("prompt") or "").strip()
        if not subject:
            return None
        refs = [r for r in (reference_images or []) if r]
        key = "prompts.article_product_scene_image" if refs else "prompts.article_inline_image"
        prompt_text = _format_prompt(key, subject=subject)
        raw = _openrouter_image(prompt_text, aspect_ratio="4:3", reference_images=refs)
        if not raw:
            return None
        compressed, width, height = compress_banner_image(raw, target_width=1024, target_height=768)
        alt = (spec.get("alt") or "").strip()[:125] or subject[:125]
        filename = f"{_slugify_for_filename(article_slug, 50)}-{int(index)}-{_slugify_for_filename(alt, 40)}.webp"
        logger.info("Article inline image %s generated (%d bytes, %d refs)", index, len(compressed), len(refs))
        return compressed, filename, alt, width, height
    except Exception as e:
        logger.warning("Article inline image %s failed: %s", index, e)
        return None
