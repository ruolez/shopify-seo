/* Shared UI components — plain script, global namespace, opt-in only.
 * Load order: app.js → ui.js → <page>.js. Never shadows an app.js global.
 * z-index ladder: page header 50 < sidebar 100 < drawer 160 < modal 200–340 < toast 300* < confirm 350
 * (*toast at 300 predates the ladder; dynamic modals stack 200→340 and stop below confirm). */

/* ── UIModal ─────────────────────────────────────────────────────────── */
const UIModal = (() => {
    const stack = [];
    const BASE_Z = 200;
    const MAX_Z = 340;

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && stack.length) {
            const top = stack[stack.length - 1];
            if (!top.persistent) top.close();
        }
    });

    function register(overlay, opts = {}) {
        const handle = {
            el: overlay,
            persistent: !!opts.persistent,
            open() {
                overlay.classList.add("active");
                overlay.style.zIndex = Math.min(BASE_Z + stack.length * 10, MAX_Z);
                stack.push(handle);
                return handle;
            },
            close() {
                overlay.classList.remove("active");
                const i = stack.indexOf(handle);
                if (i >= 0) stack.splice(i, 1);
                if (opts.onClose) opts.onClose();
                if (handle._dynamic) setTimeout(() => overlay.remove(), 250);
                return handle;
            },
        };
        overlay.addEventListener("click", (e) => {
            if (e.target === overlay && !handle.persistent) handle.close();
        });
        return handle;
    }

    return {
        /** Wrap an existing .modal-overlay element: Esc + overlay-click close, stacking. */
        adopt(id, opts = {}) {
            const overlay = document.getElementById(id);
            if (!overlay) return null;
            return register(overlay, opts);
        },
        /** Create and open a modal without template markup. size: sm | lg | fullscreen */
        open({ title = "", bodyHtml = "", footerHtml = "", size = "sm", onClose = null, persistent = false } = {}) {
            const overlay = document.createElement("div");
            overlay.className = "modal-overlay";
            const cls = size === "fullscreen" ? "modal-fullscreen" : "modal";
            const width = size === "lg" ? "max-width:860px;" : "";
            overlay.innerHTML = `<div class="${cls}" style="${width}">
                <div class="modal-header"><h3>${title}</h3>
                    <button class="icon-btn" data-ui-close><span class="material-symbols-rounded">close</span></button></div>
                <div class="modal-body">${bodyHtml}</div>
                ${footerHtml ? `<div class="modal-footer">${footerHtml}</div>` : ""}
            </div>`;
            document.body.appendChild(overlay);
            const handle = register(overlay, { onClose, persistent });
            handle._dynamic = true;
            overlay.querySelector("[data-ui-close]").addEventListener("click", () => handle.close());
            requestAnimationFrame(() => handle.open());
            return handle;
        },
    };
})();

/* ── UIConfirm ───────────────────────────────────────────────────────── */
const UIConfirm = {
    /** Ask before a write the user cannot preview row by row (bulk applies,
     *  new live pages). Resolves true only on the confirm button. */
    ask({ title = "Are you sure?", message = "", confirmLabel = "Apply to Shopify" } = {}) {
        return new Promise((resolve) => {
            let confirmed = false;
            const modal = UIModal.open({
                title: escapeHtml(title),
                bodyHtml: `<p style="font-size:14px;line-height:1.5;margin:0;">${escapeHtml(message)}</p>`,
                footerHtml: `<button class="btn btn-secondary" data-ui-cancel>Cancel</button>
                    <button class="btn btn-primary" data-ui-confirm>${escapeHtml(confirmLabel)}</button>`,
                onClose: () => resolve(confirmed),
            });
            modal.el.style.zIndex = 350;
            modal.el.querySelector("[data-ui-cancel]").addEventListener("click", () => modal.close());
            modal.el.querySelector("[data-ui-confirm]").addEventListener("click", () => {
                confirmed = true;
                modal.close();
            });
        });
    },
};

/* ── UIHistory ───────────────────────────────────────────────────────── */
/* Everything the app changed on one item, newest first, with Undo. Shared by
 * the product/collection modal and the article window. */
const UIHistory = (() => {
    // Plain-language names for the tools that write to an item.
    const SOURCES = {
        seo_optimize: "Optimize with AI", bulk_optimize: "Bulk optimize", bulk_title: "Bulk titles",
        issue_fix: "Issue fix", ai_search: "AI Search", cannibalization: "Cannibalization fix",
        content_gap: "Content gap", keyword_strategy: "Keyword Strategy", brands: "Brands",
        interlinking: "Interlinking", intro_links: "Intro links", blog: "Blog",
        landing_pages: "Landing pages", undo: "Undo",
        // change types recorded before tools were named
        ai_optimization: "Optimize with AI", technical_fix: "Issue fix", image_alt_text: "Image alt text",
        duplicate_fix: "Duplicates", faq_schema: "FAQ schema",
    };
    const CHANGE_TYPES = { article_refresh: "Refresh for search", restyle: "Restyle", intro_link: "Intro link", article_edit: "Manual edit", revert: "Undo" };
    const FIELDS = {
        title: "title", description_html: "description", body: "body", meta_title: "meta title",
        meta_description: "meta description", tags: "tags", image_alts: "image alt text", summary: "summary",
        image_alt: "image alt text", description: "description", faq_schema: "FAQ schema",
    };

    async function render(panel, itemType, itemId) {
        panel.innerHTML = '<div class="loading-spinner"><div class="spinner"></div></div>';
        try {
            const res = await apiRequest(`/api/changes?item_type=${itemType}&item_id=${itemId}`);
            const rows = res.changes || [];
            if (!rows.length) {
                panel.innerHTML = '<p style="color:var(--on-surface-secondary);padding:16px 0;">No changes recorded for this item yet. Every change the app makes will be listed here, with an undo.</p>';
                return;
            }
            panel.innerHTML = `<p style="font-size:13px;color:var(--on-surface-secondary);margin:8px 0 12px;">Everything the app changed on this item, newest first. Undo restores the previous value on Shopify.</p>
                <div class="table-container"><table class="data-table"><thead><tr><th>When</th><th>Tool</th><th>Changed</th><th></th></tr></thead><tbody>${rows.map((r) => {
                    const fields = Object.keys(r.changes_applied || {}).map((f) => FIELDS[f] || f).join(", ") || "—";
                    const tool = CHANGE_TYPES[r.change_type] || SOURCES[r.source] || SOURCES[r.change_type] || r.source || r.change_type || "—";
                    const state = r.reverted_at
                        ? '<span class="badge">Undone</span>'
                        : r.can_undo
                            ? `<button class="btn btn-secondary btn-sm" data-undo="${r.id}"><span class="material-symbols-rounded">undo</span> Undo</button>`
                            : '<span style="font-size:12px;color:var(--on-surface-secondary);" title="Recorded before undo was available, or not a content field">No undo</span>';
                    return `<tr><td style="white-space:nowrap;">${formatDate(r.created_at)}</td>
                        <td>${escapeHtml(tool)}</td><td>${escapeHtml(fields)}</td><td style="text-align:right;">${state}</td></tr>`;
                }).join("")}</tbody></table></div>`;
            panel.querySelectorAll("[data-undo]").forEach((btn) =>
                btn.addEventListener("click", () => undo(btn.dataset.undo, btn, () => render(panel, itemType, itemId))));
        } catch (e) {
            panel.innerHTML = `<p style="color:var(--error);">${escapeHtml(e.message)}</p>`;
        }
    }

    async function undo(eventId, btn, onDone) {
        const confirmed = await UIConfirm.ask({
            title: "Undo this change?",
            message: "The previous value will be written back to your live store.",
            confirmLabel: "Undo change",
        });
        if (!confirmed) return;
        btn.disabled = true;
        try {
            await apiRequest(`/api/changes/${eventId}/revert`, { method: "POST" });
            showToast("Change undone — previous value restored on Shopify.", "success");
        } catch (e) {
            showToast(e.message, "error");
        }
        onDone();
    }

    const toolLabel = (t) => CHANGE_TYPES[t] || SOURCES[t] || t;

    return { render, toolLabel };
})();

/* With one store there is nothing to choose: hide the page-level store
 * dropdowns that sit beside the global one. Each page still auto-selects the
 * store, so only the redundant control disappears. */
const PAGE_STORE_SELECTS = ["store-select", "gsc-store-select", "sr-store-select", "comp-store-select",
    "cg-store-select", "lp-store-select", "hp-store-select"];

function hideRedundantStoreSelects() {
    for (const id of PAGE_STORE_SELECTS) {
        const sel = document.getElementById(id);
        if (!sel || sel.tagName !== "SELECT") continue;
        const real = [...sel.options].filter((o) => o.value);
        if (real.length !== 1) continue;
        if (!sel.value) {
            sel.value = real[0].value;
            sel.dispatchEvent(new Event("change", { bubbles: true }));
        }
        sel.style.display = "none";
        const label = document.querySelector(`label[for="${id}"]`);
        if (label) label.style.display = "none";
        const group = sel.closest(".form-group");
        if (group) group.style.display = "none";
        hideIfEmpty(sel.closest(".toolbar-row, .page-content > div"));
    }
}

/* A toolbar left with nothing visible (its store select hidden, its button
 * moved to the header) would leave a blank band; drop it. */
function hideIfEmpty(row) {
    if (!row || row.classList.contains("page-content")) return;
    const visible = [...row.children].some((c) => getComputedStyle(c).display !== "none");
    if (!visible) row.style.display = "none";
}

/* Page-level actions live in the header, right of the title. Templates mark
 * them with data-page-action; listeners travel with the moved node. */
function adoptPageActions() {
    const host = document.querySelector(".page-header .page-actions");
    if (!host) return;
    document.querySelectorAll(".page-content [data-page-action]").forEach((el) => {
        const from = el.parentElement;
        host.appendChild(el);
        hideIfEmpty(from && from.closest(".toolbar-row, .page-content > div"));
    });
}
document.addEventListener("DOMContentLoaded", adoptPageActions);
document.addEventListener("DOMContentLoaded", () => {
    setTimeout(hideRedundantStoreSelects, 1200);
    setTimeout(hideRedundantStoreSelects, 3000);
});

/* ── UISerp — a page shown the way Google shows it ───────────────────── */
/* The app edits titles, URLs and descriptions, so wherever one is on screen
 * it is drawn as a search result: green breadcrumb URL, blue title, grey
 * description, and a meter for how much of the title fits (~580px). */
const UISerp = (() => {
    const TITLE_PX = 580;
    let ctx = null;
    function titleWidth(text) {
        if (!ctx) ctx = document.createElement("canvas").getContext("2d");
        ctx.font = "20px Arial";
        return ctx.measureText(text || "").width;
    }
    function crumbs(domain, path) {
        const host = String(domain || "").replace(/^https?:\/\//, "").replace(/\/$/, "");
        const parts = String(path || "").split("/").filter(Boolean);
        return [host].concat(parts).filter(Boolean).map(escapeHtml).join(" › ");
    }
    /** opts: { domain, path, title, description, own (bool), href, meter (bool), lead (html before url) } */
    function html(o) {
        const w = titleWidth(o.title);
        const pct = Math.min(100, Math.round((w / TITLE_PX) * 100));
        const cut = w > TITLE_PX;
        const title = o.href
            ? `<a class="serp-title" href="${escapeAttr(o.href)}">${escapeHtml(o.title || "Untitled")}</a>`
            : `<span class="serp-title">${escapeHtml(o.title || "Untitled")}</span>`;
        const desc = o.description
            ? `<div class="serp-desc">${escapeHtml(o.description)}</div>`
            : `<div class="serp-desc serp-desc-missing">${o.own === false ? "No meta description. Google writes its own from the page." : ""}</div>`;
        const meter = o.meter === false ? "" : `<div class="serp-meter${cut ? " is-cut" : ""}" title="${cut ? "Title is cut off in Google" : "Title fits in Google"} (${Math.round(w)} of ${TITLE_PX}px)"><i style="width:${pct}%"></i></div>`;
        return `<div class="serp">
            <div class="serp-url">${o.lead || ""}<span>${crumbs(o.domain, o.path)}</span></div>
            ${title}${desc}${meter}
        </div>`;
    }
    function storeDomain() {
        const stores = (typeof StoreSelector !== "undefined" && StoreSelector.getStores()) || [];
        const s = stores.find((x) => x.id === StoreSelector.getStoreId()) || stores[0];
        return s ? (s.primary_domain || s.domain || "") : "";
    }
    return { html, titleWidth, storeDomain, TITLE_PX };
})();

/* ── UISkeleton ──────────────────────────────────────────────────────── */
const UISkeleton = {
    /** type: table | cards | stats | detail */
    show(container, { type = "table", rows = 6 } = {}) {
        if (typeof container === "string") container = document.querySelector(container);
        if (!container) return;
        let html = "";
        if (type === "table") {
            html = `<div class="ui-skeleton-table">${Array.from({ length: rows }, () => `<div class="ui-skeleton ui-skeleton-row"></div>`).join("")}</div>`;
        } else if (type === "cards" || type === "stats") {
            html = `<div class="ui-skeleton-cards">${Array.from({ length: type === "stats" ? 4 : rows }, () => `<div class="ui-skeleton ui-skeleton-card"></div>`).join("")}</div>`;
        } else {
            html = `<div class="ui-skeleton ui-skeleton-block" style="height:40px;width:60%;"></div>
                <div class="ui-skeleton ui-skeleton-block" style="height:16px;width:90%;"></div>
                <div class="ui-skeleton ui-skeleton-block" style="height:16px;width:80%;"></div>
                <div class="ui-skeleton ui-skeleton-block" style="height:120px;width:100%;"></div>`;
        }
        container.innerHTML = html;
    },
};

/* ── UIDrawer ────────────────────────────────────────────────────────── */
const UIDrawer = {
    /** Right-side sliding panel. Returns {el, open, close, toggle, setBody, setTitle, isOpen}. */
    create({ id, title = "", width = 380 } = {}) {
        let el = id ? document.getElementById(id) : null;
        if (!el) {
            el = document.createElement("div");
            if (id) el.id = id;
            el.className = "ui-drawer";
            el.style.width = width + "px";
            el.innerHTML = `<div class="ui-drawer-header">
                    <b class="ui-drawer-title">${title}</b>
                    <button class="icon-btn" data-ui-drawer-close><span class="material-symbols-rounded">close</span></button>
                </div>
                <div class="ui-drawer-body"></div>`;
            document.body.appendChild(el);
        }
        const handle = {
            el,
            open() { el.classList.add("open"); if (handle.onOpenCb) handle.onOpenCb(); return handle; },
            close() { el.classList.remove("open"); return handle; },
            toggle() { return el.classList.contains("open") ? handle.close() : handle.open(); },
            isOpen() { return el.classList.contains("open"); },
            setTitle(t) { el.querySelector(".ui-drawer-title").innerHTML = t; return handle; },
            setBody(html) { el.querySelector(".ui-drawer-body").innerHTML = html; return handle; },
            body() { return el.querySelector(".ui-drawer-body"); },
            onOpen(fn) { handle.onOpenCb = fn; return handle; },
        };
        el.querySelector("[data-ui-drawer-close]").addEventListener("click", () => handle.close());
        return handle;
    },
};

/* ── UITabs — tab switcher synced to ?tab= (deep-linkable) ───────────── */
const UITabs = {
    /** Wire .tab[data-tab] buttons inside container to .tab-panel#<prefix><name> panels.
     *  Syncs the active tab to the ?tab= query param via replaceState. */
    init(container, { panelPrefix = "tab-", onChange = null, defaultTab = null } = {}) {
        if (typeof container === "string") container = document.querySelector(container);
        if (!container) return null;
        const tabs = [...container.querySelectorAll(".tab[data-tab]")];
        const activate = (name, push = true) => {
            tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
            document.querySelectorAll(".tab-panel").forEach((p) => {
                if (p.id.startsWith(panelPrefix)) p.classList.toggle("active", p.id === panelPrefix + name);
            });
            if (push) {
                const url = new URL(window.location);
                url.searchParams.set("tab", name);
                history.replaceState(null, "", url);
            }
            if (onChange) onChange(name);
        };
        tabs.forEach((t) => t.addEventListener("click", () => activate(t.dataset.tab)));
        const initial = new URLSearchParams(location.search).get("tab") || defaultTab || (tabs[0] && tabs[0].dataset.tab);
        if (initial && tabs.some((t) => t.dataset.tab === initial)) activate(initial, false);
        return { activate, tabs };
    },
};

/* ── Deep-link contract: ?tab=<t>&focus=<type>:<id>&action=<verb> ────── */
/** Parse the deep-link params and hand them to the page. The page supplies
 *  handlers: { onFocus(type, id, action) }. Tab switching is UITabs' job
 *  (it reads ?tab= itself). Returns the parsed params. */
function focusFromQuery(handlers = {}) {
    const params = new URLSearchParams(location.search);
    const focus = params.get("focus");
    const action = params.get("action") || null;
    if (focus && handlers.onFocus) {
        const [type, ...rest] = focus.split(":");
        const id = rest.join(":");
        setTimeout(() => handlers.onFocus(type, id, action), 50);
    }
    return { tab: params.get("tab"), focus, action };
}

/* ── StoreSelector — global topbar store context ─────────────────────── */
const StoreSelector = (() => {
    let stores = [];
    let activeId = null;
    let host = null;

    async function init() {
        host = document.getElementById("global-store-select");
        if (!host) return;
        try {
            const [list, sess] = await Promise.all([
                apiRequest("/api/stores"),
                apiRequest("/api/session/active-store"),
            ]);
            stores = (list || []).filter((s) => s.is_active);
            const stored = parseInt(loadFilter("globalStore", "") || 0, 10);
            activeId = sess.store_id || stored || (stores[0] && stores[0].id) || null;
            if (activeId && !sess.store_id) {
                try {
                    await apiRequest("/api/session/active-store", {
                        method: "POST",
                        body: JSON.stringify({ store_id: activeId }),
                    });
                } catch (e) { /* session sync is best-effort */ }
            }
            render();
        } catch (e) { /* topbar stays plain if stores can't load */ }
    }

    function label(s) { return s.shop_name || s.name; }

    function storeDomain(s) {
        return String(s.primary_domain || s.domain || "").replace(/^https?:\/\//, "").replace(/\/$/, "");
    }

    function storeCard(s, multi) {
        const name = label(s);
        return `<span class="store-mark">${escapeHtml((name || "S").trim().charAt(0).toUpperCase())}</span>
            <span class="store-text"><b>${escapeHtml(name)}</b><small>${escapeHtml(storeDomain(s))}</small></span>
            ${multi ? '<span class="material-symbols-rounded store-chevron">unfold_more</span>' : ""}`;
    }

    function render() {
        if (!stores.length) {
            host.innerHTML = `<a href="/stores" class="store-card"><span class="store-mark">+</span><span class="store-text"><b>Connect a store</b><small>No store yet</small></span></a>`;
            return;
        }
        const active = stores.find((s) => s.id === activeId) || stores[0];
        if (stores.length === 1) {
            host.innerHTML = `<a href="/stores" class="store-card" title="Manage stores">${storeCard(active, false)}</a>`;
            return;
        }
        host.innerHTML = `<label class="store-card store-card-select">${storeCard(active, true)}
            <select aria-label="Active store">${stores
            .map((s) => `<option value="${s.id}" ${s.id === activeId ? "selected" : ""}>${escapeHtml(label(s))}</option>`)
            .join("")}<option value="__manage">Manage stores…</option></select></label>`;
        host.querySelector("select").addEventListener("change", async (e) => {
            if (e.target.value === "__manage") { window.location.href = "/stores"; return; }
            const id = parseInt(e.target.value, 10);
            try {
                await apiRequest("/api/session/active-store", { method: "POST", body: JSON.stringify({ store_id: id }) });
            } catch (err) {
                showToast("Failed to switch store", "error");
                e.target.value = String(activeId);
                return;
            }
            activeId = id;
            saveFilter("globalStore", id);
            render();
            document.dispatchEvent(new CustomEvent("store:changed", { detail: { storeId: id } }));
        });
    }

    /** Transition shim: keep a page's legacy store <select> in lockstep with
     *  the global selector without touching the page's own change logic. */
    function bindLegacy(sel) {
        if (!sel) return;
        document.addEventListener("store:changed", (e) => {
            const v = String(e.detail.storeId);
            if (sel.value !== v && [...sel.options].some((o) => o.value === v)) {
                sel.value = v;
                sel.dispatchEvent(new Event("change"));
            }
        });
    }

    return { init, getStoreId: () => activeId, getStores: () => stores, bindLegacy };
})();

/* ── NavBadges — pending-work counts painted onto the sidebar ────────── */
const NavBadges = (() => {
    async function refresh() {
        try {
            const counts = await apiRequest("/api/nav/badges");
            const tips = counts._tooltips || {};
            document.querySelectorAll(".nav-badge[data-badge]").forEach((el) => {
                const key = el.dataset.badge;
                const n = counts[key] || 0;
                el.textContent = n ? (n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/\.0$/, "") + "k" : String(n)) : "";
                // The number sums several kinds of work; hovering says which.
                if (n && tips[key]) el.title = tips[key];
                else el.removeAttribute("title");
            });
        } catch (e) { /* badges are decoration — never block the page */ }
    }
    function init() {
        if (!document.querySelector(".nav-badge[data-badge]")) return;
        refresh();
        setInterval(() => { if (!document.hidden) refresh(); }, 60000);
        // The interval skips hidden tabs, so a backgrounded page kept showing
        // whatever the badge said when you left it — for hours, if you never
        // switched back within a tick. Repaint the moment it regains focus.
        document.addEventListener("visibilitychange", () => {
            if (!document.hidden) refresh();
        });
    }
    return { init, refresh };
})();

/* ── CommandPalette — Cmd/Ctrl+K quick navigation & actions ──────────── */
const CommandPalette = (() => {
    const SHORTCUTS = [
        { label: "Page SEO › Issues", group: "Fix", icon: "rule", href: "/seo?tab=issues" },
        { label: "Internal links › Issues", group: "Fix", icon: "link_off", href: "/interlinking?tab=issues" },
        { label: "Blog posts › Create post", group: "Create", icon: "add_notes", href: "/blog?tab=create" },
        { label: "Blog posts › Intro links", group: "Create", icon: "link", href: "/blog?tab=intro-links" },
        { label: "News › Create news", group: "Create", icon: "add_notes", href: "/news?tab=create" },
        { label: "Search Console › Indexing", group: "Research", icon: "travel_explore", href: "/gsc?tab=indexing" },
        { label: "Search Console › Cannibalization", group: "Research", icon: "call_split", href: "/gsc?tab=cannibalization" },
        { label: "Open Activity", group: "Action", icon: "pending_actions", run: () => { const b = document.getElementById("tasksPanelToggle"); if (b) b.click(); } },
        { label: "Switch light / dark", group: "Action", icon: "contrast", run: () => { const b = document.getElementById("theme-toggle"); if (b) b.click(); } },
    ];
    const REGISTRY = (window.NAV_PALETTE || []).concat(SHORTCUTS);
    let overlay = null;
    let selected = 0;
    let matches = [];

    function build() {
        overlay = document.createElement("div");
        overlay.className = "cmdk-overlay";
        overlay.innerHTML = `<div class="cmdk">
            <input type="text" placeholder="Jump to a page or action…" autocomplete="off" spellcheck="false">
            <div class="cmdk-results"></div>
        </div>`;
        document.body.appendChild(overlay);
        overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
        const input = overlay.querySelector("input");
        input.addEventListener("input", () => { selected = 0; renderResults(input.value); });
        input.addEventListener("keydown", (e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); selected = Math.min(selected + 1, matches.length - 1); paint(); }
            else if (e.key === "ArrowUp") { e.preventDefault(); selected = Math.max(selected - 1, 0); paint(); }
            else if (e.key === "Enter") { e.preventDefault(); if (matches[selected]) exec(matches[selected]); }
            else if (e.key === "Escape") close();
        });
    }

    function score(item, q) {
        const l = item.label.toLowerCase();
        if (l.startsWith(q)) return 0;
        if (l.includes(q)) return 1;
        // subsequence match
        let i = 0;
        for (const ch of l) if (ch === q[i]) i++;
        return i === q.length ? 2 : -1;
    }

    function renderResults(query) {
        const q = (query || "").trim().toLowerCase();
        matches = !q
            ? REGISTRY.slice()
            : REGISTRY.map((r) => [score(r, q), r]).filter(([s]) => s >= 0).sort((a, b) => a[0] - b[0]).map(([, r]) => r);
        paint();
    }

    function paint() {
        const box = overlay.querySelector(".cmdk-results");
        box.innerHTML = matches.length
            ? matches.map((m, i) => `<div class="cmdk-item ${i === selected ? "selected" : ""}" data-i="${i}">
                    <span class="material-symbols-rounded">${m.icon}</span>${escapeHtml(m.label)}
                    <span class="cmdk-hint">${escapeHtml(m.group || (m.href ? "Page" : "Action"))}</span>
                </div>`).join("")
            : `<div class="cmdk-empty">No matches</div>`;
        box.querySelectorAll(".cmdk-item").forEach((el) =>
            el.addEventListener("click", () => exec(matches[+el.dataset.i])));
        const sel = box.querySelector(".cmdk-item.selected");
        if (sel) sel.scrollIntoView({ block: "nearest" });
    }

    function exec(item) {
        close();
        if (item.href) window.location.href = item.href;
        else if (item.run) item.run();
    }

    function open() {
        if (!overlay) build();
        overlay.classList.add("active");
        const input = overlay.querySelector("input");
        input.value = "";
        selected = 0;
        renderResults("");
        input.focus();
    }
    function close() { if (overlay) overlay.classList.remove("active"); }

    function init() {
        document.addEventListener("keydown", (e) => {
            if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
                e.preventDefault();
                overlay && overlay.classList.contains("active") ? close() : open();
            }
        });
        const btn = document.getElementById("cmdk-button");
        if (btn) btn.addEventListener("click", open);
    }
    return { init, open, close };
})();

/* ── boot ────────────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", () => {
    StoreSelector.init();
    NavBadges.init();
    CommandPalette.init();
});
