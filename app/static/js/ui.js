/* Shared UI components — plain script, global namespace, opt-in only.
 * Load order: app.js → ui.js → <page>.js. Never shadows an app.js global.
 * z-index ladder: topbar 50 < sidebar 100 < drawer 160 < modal 200–340 < toast 300* < confirm 350
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

    function render() {
        if (!stores.length) { host.innerHTML = ""; return; }
        if (stores.length === 1) {
            host.innerHTML = `<a href="/stores" class="global-store-single" title="Manage stores">${escapeHtml(label(stores[0]))}</a>`;
            return;
        }
        host.innerHTML = `<select class="form-control">${stores
            .map((s) => `<option value="${s.id}" ${s.id === activeId ? "selected" : ""}>${escapeHtml(label(s))}</option>`)
            .join("")}<option value="__manage">Manage stores…</option></select>`;
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
                el.textContent = n ? (n > 99 ? "99+" : String(n)) : "";
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
    const REGISTRY = [
        { label: "Home", icon: "dashboard", href: "/" },
        { label: "SEO", icon: "search", href: "/seo" },
        { label: "Duplicates", icon: "difference", href: "/duplicates" },
        { label: "SEO Issues", icon: "speed", href: "/seo?tab=issues" },
        { label: "AI Search", icon: "smart_toy", href: "/ai-seo" },
        { label: "Interlinking", icon: "link", href: "/interlinking" },
        { label: "Interlinking Issues", icon: "link_off", href: "/interlinking?tab=issues" },
        { label: "Image Manager", icon: "image", href: "/images" },
        { label: "Content Calendar", icon: "calendar_month", href: "/content/calendar" },
        { label: "Blog Posts", icon: "edit_note", href: "/blog" },
        { label: "News", icon: "newspaper", href: "/news" },
        { label: "Landing Pages", icon: "web", href: "/landing-pages" },
        { label: "Home Page Carousel", icon: "home", href: "/home-page" },
        { label: "Brands", icon: "loyalty", href: "/brands" },
        { label: "Search Console", icon: "monitoring", href: "/gsc" },
        { label: "Keyword Strategy", icon: "hub", href: "/keyword-strategy" },
        { label: "Content Gaps", icon: "target", href: "/content-gaps" },
        { label: "Competitors", icon: "groups", href: "/competitors" },
        { label: "Semrush Data", icon: "query_stats", href: "/semrush" },
        { label: "Stores", icon: "store", href: "/stores" },
        { label: "Settings", icon: "settings", href: "/settings" },
        { label: "Open Activity", icon: "checklist", run: () => { const b = document.getElementById("tasksPanelToggle"); if (b) b.click(); } },
        { label: "Toggle dark mode", icon: "dark_mode", run: () => { const b = document.getElementById("theme-toggle"); if (b) b.click(); } },
    ];
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
                    <span class="cmdk-hint">${m.href ? "page" : "action"}</span>
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
