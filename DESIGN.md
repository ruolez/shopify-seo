---
name: SEO control center
description: A private SEO tool for one Shopify store, drawn as a search results page.
colors:
  primary: "#2B3FD6"
  primary-hover: "#2232B0"
  primary-light: "#EEF0FD"
  primary-surface: "#DDE2FB"
  url-green: "#0F7B3F"
  ink: "#202124"
  ink-secondary: "#5F6368"
  sheet: "#FFFFFF"
  rail: "#F4F5F7"
  hairline: "#E3E5E8"
  hairline-strong: "#CDD1D6"
  success: "#137333"
  warning: "#B06000"
  error: "#C5221F"
  dark-primary: "#8AB4F8"
  dark-sheet: "#1C1D1F"
  dark-surface: "#222326"
  dark-rail: "#18191B"
  dark-ink: "#E8EAED"
  dark-url-green: "#81C995"
typography:
  page-title:
    fontFamily: "Public Sans, system-ui, sans-serif"
    fontSize: "24px"
    fontWeight: 650
    lineHeight: 1.2
    letterSpacing: "-0.012em"
  section-title:
    fontFamily: "Public Sans, system-ui, sans-serif"
    fontSize: "18px"
    fontWeight: 650
    lineHeight: 1.3
  metric:
    fontFamily: "Public Sans, system-ui, sans-serif"
    fontSize: "26px"
    fontWeight: 650
    lineHeight: 1.15
    letterSpacing: "-0.015em"
    fontFeature: "tnum"
  snippet-title:
    fontFamily: "Public Sans, system-ui, sans-serif"
    fontSize: "18px"
    fontWeight: 400
    lineHeight: 1.3
  body:
    fontFamily: "Public Sans, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Public Sans, system-ui, sans-serif"
    fontSize: "12.5px"
    fontWeight: 600
    lineHeight: 1.4
rounded:
  sm: "8px"
  md: "12px"
  modal: "16px"
  pill: "999px"
spacing:
  page-x: "32px"
  page-top: "20px"
  section: "22px"
  row: "14px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.sheet}"
    rounded: "{rounded.pill}"
    padding: "0 16px"
    height: "34px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
  button-secondary:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.primary}"
    rounded: "{rounded.pill}"
    padding: "0 16px"
    height: "34px"
  filter-pill:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "0 12px"
    height: "30px"
  filter-pill-selected:
    backgroundColor: "{colors.primary-light}"
    textColor: "{colors.primary}"
  nav-item-active:
    backgroundColor: "{colors.primary-surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.pill}"
  card:
    backgroundColor: "{colors.sheet}"
    rounded: "{rounded.md}"
    padding: "20px"
  input:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "8px 12px"
    height: "36px"
---

# Design System: SEO control center

## Overview

**Creative North Star: "The Results Page"**

The app edits what Google shows: titles, URLs and descriptions. So it looks like the page where those things live. A white sheet, a cool grey rail, one indigo for links and actions, URL green for paths. Wherever a page appears with its title or description, it is drawn as a search result. Everything else stays out of the way: hairlines instead of shadows, flat metric rows instead of decorated tiles, one tab style, one button shape.

Density is high on purpose. The single user is an expert who scans tables and queues. Hierarchy comes from weight and a short type ramp, not from colour or boxes.

**Key Characteristics:**
- Left rail with every page visible at once, grouped Today / Fix / Create / Research / Setup
- Page header in place of a top bar: group, title, one-line purpose, related pages, actions on the right
- The search snippet as the signature component
- Indigo means "you can act here"; green means a path or a good state; red and amber mean state only

## Colors

Restrained: neutrals tinted cool, one accent, and state colours that only ever carry meaning.

### Primary
- **Result Link Indigo** (#2B3FD6; dark #8AB4F8): primary buttons, links, the active tab, the current nav item, snippet titles. Nothing decorative.

### Secondary
- **URL Green** (#0F7B3F; dark #81C995): snippet breadcrumbs, the store domain under its name, the impressions series in charts.

### Neutral
- **Ink** (#202124): text and numbers.
- **Muted Ink** (#5F6368): descriptions, labels, table heads.
- **Sheet** (#FFFFFF; dark #1C1D1F / surface #222326): page and cards.
- **Rail Grey** (#F4F5F7; dark #18191B): the sidebar only.
- **Hairline** (#E3E5E8; dark #36383C): every divider and card edge.

### State
- **Good** (#137333), **Fair** (#B06000), **Poor** (#C5221F). Score scale is 80 / 50 everywhere (`scoreColor` in app.js).

### Named Rules
**The One Ink Rule.** Indigo marks what you can act on. If it isn't a link, a primary action or the current selection, it is not indigo.

**The State-Only Rule.** Red, amber and green appear only when they say something about a value. No decorative tints, no per-section accent colours.

## Typography

**Font:** Public Sans (one family for everything), system-ui fallback. Numbers use tabular figures.

### Hierarchy
- **Page title** (650, 24px, 1.2): one per page, in the header.
- **Section title** (650, 18px): "What to do next" and peers.
- **Metric** (650, 26px, tabular): headline numbers in KPI rows.
- **Snippet title** (400, 18px): the blue title of a search result.
- **Body** (400, 14px, 1.5): rows, descriptions; prose capped near 78ch.
- **Label** (600, 12.5px): table heads, nav group labels, metric labels. Sentence case, never uppercase.

### Named Rules
**The Sentence Case Rule.** No uppercase labels or tracked-out kickers anywhere. Labels read like words.

## Layout

Fixed left rail (232px) and a fluid main column (max 1440px; 32px side padding, 20px top). Every page opens with the page header, then its content. Home uses a two-column grid (main + 300px aside) that stacks below 1180px. Below 900px the rail slides over the page from a menu button in the header; below 640px queue rows stack.

## Elevation & Depth

Flat. Surfaces separate with 1px hairlines. Shadows exist only for things that float above the page: modals and the command palette (`0 18px 48px rgba(32,33,36,.18)`), menus and toasts (`0 8px 24px rgba(32,33,36,.12)`), and a 1px lift on a hovered primary button.

**The Flat Sheet Rule.** A card at rest has a hairline, never a shadow.

## Shapes

Pills for anything you press or toggle (buttons, filter pills, nav items, the jump field). 12px corners for cards and tables, 8px for inputs, 16px for modals. No side stripes on cards or rows.

## Components

### Buttons
- **Shape:** pill (999px), 34px tall (30px small).
- **Primary:** solid indigo, white text; hover darker indigo.
- **Secondary:** white with a strong hairline and indigo text; hover tints indigo.
- **Ghost / icon:** transparent, round 34px icon buttons.

### Filter pills
- Outlined pills. Selected = indigo tint + indigo text, never solid, so a filter never looks like an action or a tab.

### Tabs
- One style: text tabs on a hairline with a 3px indigo underline under the active one. Counts sit in a small grey pill beside the label.

### Cards and KPI rows
- Cards: white, hairline, 12px, 20px padding.
- KPI row: one hairline box split by dividers; each metric opens the page that acts on it.

### Navigation
- Rail: store card (name + green domain), "Jump to… ⌘K" pill, groups always open, active item = indigo-tinted pill with a filled icon, counts in muted or indigo text, errors as a red pill.
- Page header: group, title, purpose line, "Related" links, actions on the right. Templates mark header actions with `data-page-action`.

### Search snippet (signature)
- `UISerp.html({domain, path, title, description, own, href, lead})` in ui.js: green breadcrumb URL, 18px indigo title, two-line grey description, and a 3px meter of how much of the title fits in Google (580px at 20px Arial; amber when cut off). Missing descriptions say Google writes its own.

## Do's and Don'ts

### Do:
- **Do** use the search snippet wherever a page's title or description is shown.
- **Do** add every new page to `app/nav.py`; the rail, header, related links and ⌘K read it.
- **Do** put a page's main action in the header with `data-page-action`.
- **Do** link every number to the page where you act on it.

### Don't:
- **Don't** add uppercase labels, eyebrow text above titles, or per-section accent colours.
- **Don't** use gradients, glows or coloured side stripes.
- **Don't** give filter pills a solid fill; solid means action.
- **Don't** add a per-page store picker; the rail owns the store.
