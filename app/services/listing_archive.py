"""Show every post on a listing page, not just the newest.

The blog landing and news hub used to keep only the newest 50 / 100 posts and
hide the theme's pagination, so older posts could not be reached from any
page a visitor uses. Now the newest posts show as before and the rest sit
below in a month-by-month archive: plain <details> sections, so every link is
in the page's HTML for visitors and for Google, with no script needed.
"""

import calendar
import html


def _month_label(sort_key_value):
    try:
        year, month = int(sort_key_value[:4]), int(sort_key_value[5:7])
        return f"{calendar.month_name[month]} {year}"
    except (ValueError, IndexError, TypeError):
        return "Earlier"


def split_archive(posts, recent_count, sort_key):
    """(recent, [(month label, posts)]) — posts must already be newest first.
    sort_key(post) returns an ISO date string ('' when unknown)."""
    recent, older = posts[:recent_count], posts[recent_count:]
    months = []
    for post in older:
        label = _month_label(sort_key(post))
        if months and months[-1][0] == label:
            months[-1][1].append(post)
        else:
            months.append((label, [post]))
    return recent, months


def archive_html(months, render_grid, css_prefix, heading="Older posts"):
    """Collapsed month sections. render_grid(posts) builds the card grid."""
    if not months:
        return ""
    sections = "".join(
        f'<details class="{css_prefix}__month"><summary>{html.escape(label)} <span>({len(posts)})</span></summary>'
        f"{render_grid(posts)}</details>"
        for label, posts in months
    )
    return f'<div class="{css_prefix}__archive"><h2 class="{css_prefix}__section-title">{html.escape(heading)}</h2>{sections}</div>'


def archive_css(css_prefix):
    p = f".{css_prefix}"
    return f"""
{p}__archive {{ max-width: 1400px !important; margin: 8px auto 40px !important; }}
{p}__month {{ border-top: 1px solid #e5e5e5 !important; padding: 4px 0 !important; }}
{p}__month > summary {{ cursor: pointer !important; padding: 12px 4px !important; font-size: 16px !important; font-weight: 600 !important; color: #222 !important; list-style: revert !important; }}
{p}__month > summary span {{ font-weight: 400 !important; color: #888 !important; }}
{p}__month[open] > summary {{ margin-bottom: 12px !important; }}
"""
