---
version: 1
slug: "app-templates-base-html"
primary_target: "app/templates/base.html"
related_targets: ["app/templates/_sidebar.html","app/static/css/style.css"]
---

# App shell and every authenticated page

Scope: the whole authenticated app (base.html, _sidebar.html, style.css, ui.js, app.js and every page template/JS). Mode: Operate. Single expert user (store owner) at a desk; dense screens and shortcuts are welcome.

Task: find what to fix, fix it through the guarded apply pipeline, create and schedule content, read research. Every number links to where you act on it; every area reachable in one click.

## Direction contract

THESIS: You always see a page the way Google shows it, because titles, URLs and descriptions are what this app edits. Refuses the category default of teal cards, glowing buttons and decorative KPI tiles.

OWN-WORLD: White sheet, a cool grey rail (#F4F5F7) with hairline borders. Ink #202124, muted #5F6368. Indigo link blue #2B3FD6 is the one accent (primary actions, links, current nav, active tab). URL green #0F7B3F for paths and good states; amber #B06000 / red #C5221F for state only. Public Sans throughout, one family. Pill-shaped buttons and nav items, 1px hairlines instead of shadows, flat inline KPI rows with dividers. Dark theme: #1F1F1F ground, #2A2A2A surfaces, link #8AB4F8, url #81C995.

STORY: The owner opens Home, sees clicks trend and the ranked queue, opens an item, sees its live snippet and the proposed change, applies, and later sees the result. Research pages always link back to the page that acts.

FIRST VIEWPORT: Left rail (228px): store switcher, "Jump to… ⌘K" field, groups Today / Fix / Create / Research always open with counts, Stores + Settings + theme + logout at the foot. Main: page header with group breadcrumb, title, one-line description (replaces the old info banner), actions on the right (primary pill on the far right). Below: content. No top bar.

FORM: Results Page, IMPECCABLE'S PICK (top of my grounded list), seed:a2201d3fa6d9284f.

SIGNATURE: the search snippet. Pages are previewed as Google results (green breadcrumb URL, blue title, grey description, pixel-width meter), on Home queue rows, item detail and title/meta editors.

## Unresolved

- Snippet previews need title/handle/meta in each payload; where an API lacks them, fall back to a plain row.
