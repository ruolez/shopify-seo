/* Interlinking — action-first rebuild.
 *
 * Four tabs (overview / issues / coverage / opportunities) over the trimmed
 * analysis payload. Uses the shared UI kit: UITabs (?tab= deep links),
 * UIDrawer for item detail, UIModal confirm-with-preview for destructive
 * fixes, StoreSelector for store context, pollTaskUntilDone for background
 * work. Every fix ends with reloadAnalysis() so stats, tab counts, and the
 * sidebar badge never go stale.
 */

(() => {
    "use strict";

    /* ── state ─────────────────────────────────────────────────────── */
    let storeId = null;
    let analysis = null;        // trimmed analysis from /api/interlinking/analysis
    let createdAt = null;
    let tabsCtl = null;
    let activeTab = "overview";
    let drawer = null;
    let targetsCache = null;    // available replacement targets
    let recs = null;            // {result, completed_at}
    const orphanPlans = { collection: null, article: null }; // {result, completed_at} per type
    let brokenGroups = [];
    let noncanonGroups = [];
    let fixCtx = null;          // live fix-modal context
    let session = null;         // {kind, groups, idx}
    let coverageSegment = "orphan-products";
    let storeRetries = 0;
    let analysisWatched = false;
    const appliedRecKeys = new Set();

    const $ = (sel) => document.querySelector(sel);
    const num = (v) => (v === null || v === undefined ? 0 : v);

    const TYPE_ICONS = { product: "sell", collection: "category", article: "article", page: "description", blog_index: "newspaper" };

    const LEGACY_TABS = {
        "orphan-products": ["coverage", "orphan-products"],
        "orphan-collections": ["coverage", "orphan-collections"],
        "orphan-articles": ["coverage", "orphan-articles"],
        "under-linked": ["coverage", "under-linked"],
        "not-in-collection": ["coverage", "not-in-collection"],
        "crosslinks": ["opportunities", null],
        "cross-links": ["opportunities", null],
        "broken-links": ["issues", null],
        "non-canonical": ["issues", null],
        "ai-recs": ["opportunities", null],
    };

    /* ── boot ──────────────────────────────────────────────────────── */
    document.addEventListener("DOMContentLoaded", () => {
        applyLegacyTabShim();
        tabsCtl = UITabs.init(document.querySelector(".il-tabs"), {
            panelPrefix: "il-tab-",
            defaultTab: "overview",
            onChange: (name) => { activeTab = name; },
        });
        activeTab = new URLSearchParams(location.search).get("tab") || "overview";

        $("#il-analyze-btn").addEventListener("click", () => runAnalysis());

        document.addEventListener("store:changed", (e) => {
            storeId = e.detail.storeId;
            resetState();
            loadAll();
        });
        document.addEventListener("keydown", onSessionKeys);

        setTimeout(resolveStore, 350);
    });

    function applyLegacyTabShim() {
        const url = new URL(window.location);
        const t = url.searchParams.get("tab");
        if (t && LEGACY_TABS[t]) {
            const [tab, segment] = LEGACY_TABS[t];
            url.searchParams.set("tab", tab);
            history.replaceState(null, "", url);
            if (segment) coverageSegment = segment;
        }
    }

    function resolveStore() {
        storeId = StoreSelector.getStoreId();
        if (!storeId) {
            if (storeRetries++ < 8) return setTimeout(resolveStore, 600);
            renderNoStore();
            return;
        }
        loadAll();
    }

    function resetState() {
        analysis = null;
        createdAt = null;
        targetsCache = null;
        recs = null;
        orphanPlans.collection = null;
        orphanPlans.article = null;
        brokenGroups = [];
        noncanonGroups = [];
        session = null;
        appliedRecKeys.clear();
        if (drawer && drawer.isOpen()) drawer.close();
    }

    /* ── data loading ──────────────────────────────────────────────── */
    async function loadAll() {
        if (!storeId) return;
        UISkeleton.show("#il-tab-" + activeTab, { type: "table", rows: 6 });
        targetsCache = null;
        try {
            const [analysisResp, statusResp, recsResp, collPlanResp, artPlanResp] = await Promise.all([
                apiRequest(`/api/interlinking/analysis?store_id=${storeId}`),
                apiRequest(`/api/interlinking/status?store_id=${storeId}`),
                apiRequest(`/api/interlinking/recommendations/latest?store_id=${storeId}`).catch(() => null),
                apiRequest(`/api/interlinking/orphan-fix/latest?store_id=${storeId}&item_type=collection`).catch(() => null),
                apiRequest(`/api/interlinking/orphan-fix/latest?store_id=${storeId}&item_type=article`).catch(() => null),
            ]);
            analysis = analysisResp.analysis;
            createdAt = analysisResp.created_at;
            if (recsResp && recsResp.result) {
                recs = { result: recsResp.result, completed_at: recsResp.completed_at };
            }
            if (collPlanResp && collPlanResp.result) {
                orphanPlans.collection = { result: collPlanResp.result, completed_at: collPlanResp.completed_at };
            }
            if (artPlanResp && artPlanResp.result) {
                orphanPlans.article = { result: artPlanResp.result, completed_at: artPlanResp.completed_at };
            }
            renderAll();
            handleStatus(statusResp);
            focusFromQuery({
                onFocus: (type, id) => {
                    if (["product", "collection", "article"].includes(type)) {
                        openItemDrawer(type, parseInt(id, 10));
                    }
                },
            });
        } catch (e) {
            $("#il-tab-" + activeTab).innerHTML = errorState(`Failed to load interlinking data: ${escapeHtml(e.message)}`);
        }
    }

    async function reloadAnalysis() {
        if (!storeId) return;
        try {
            const resp = await apiRequest(`/api/interlinking/analysis?store_id=${storeId}`);
            analysis = resp.analysis;
            createdAt = resp.created_at;
            renderAll();
            if (typeof NavBadges !== "undefined") NavBadges.refresh();
        } catch (e) { /* keep the current view; next action retries */ }
    }

    function handleStatus(status) {
        if (!status) return;
        if (status.running && status.task) {
            watchAnalysis(status.task.id);
        } else if (status.task && status.task.status === "failed") {
            showBanner(
                `Last analysis failed: ${escapeHtml(status.task.error || "unknown error")}`,
                "error"
            );
        }
    }

    /* ── analyze flow ──────────────────────────────────────────────── */
    async function runAnalysis() {
        const btn = $("#il-analyze-btn");
        await guardButton(btn, async () => {
            try {
                const resp = await apiRequest("/api/interlinking/analyze", {
                    method: "POST",
                    body: { store_id: storeId },
                });
                if (resp.message === "Analysis already running") {
                    showToast("Analysis is already running", "info");
                } else {
                    showToast("Analysis started", "info");
                }
                if (resp.task_id) await watchAnalysis(resp.task_id);
            } catch (e) {
                showToast(`Could not start analysis: ${e.message}`, "error");
            }
        });
    }

    async function watchAnalysis(taskId) {
        if (analysisWatched) return;
        analysisWatched = true;
        const btn = $("#il-analyze-btn");
        btn.disabled = true;
        setStatusChip("Analyzing store links…", true);
        try {
            await pollTaskUntilDone(`/api/interlinking/task/${taskId}?store_id=${storeId}`, {
                intervalMs: 2500,
                timeoutMs: 30 * 60 * 1000,
            });
            setStatusChip(null);
            showToast("Analysis complete", "success");
            clearBanner();
            await reloadAnalysis();
        } catch (e) {
            setStatusChip(null);
            showBanner(`Analysis failed: ${escapeHtml(e.message)}`, "error");
        } finally {
            analysisWatched = false;
            btn.disabled = false;
        }
    }

    function setStatusChip(text, spin) {
        const chip = $("#il-status-chip");
        if (!text) { chip.style.display = "none"; chip.innerHTML = ""; return; }
        chip.style.display = "inline-flex";
        chip.innerHTML = `${spin ? '<span class="spinner-sm"></span>' : ""}${escapeHtml(text)}`;
    }

    function showBanner(html, type) {
        $("#il-banner").innerHTML = `
            <div class="il-banner il-banner-${type || "info"}">
                <span class="material-symbols-rounded">${type === "error" ? "error" : "info"}</span>
                <span style="flex:1;">${html}</span>
                <button class="icon-btn" data-il-dismiss title="Dismiss">
                    <span class="material-symbols-rounded">close</span>
                </button>
            </div>`;
        $("#il-banner [data-il-dismiss]").addEventListener("click", clearBanner);
    }

    function clearBanner() { $("#il-banner").innerHTML = ""; }

    /* ── top-level rendering ───────────────────────────────────────── */
    function renderAll() {
        renderActionBar();
        renderStats();
        updateTabCounts();
        renderOverview();
        renderIssues();
        renderCoverage();
        renderOpportunities();
    }

    function renderNoStore() {
        $("#il-tab-overview").innerHTML = `
            <div class="empty-state">
                <span class="material-symbols-rounded">storefront</span>
                <p>Connect a store to analyze its internal links.</p>
                <a href="/stores" class="btn btn-primary">Go to Stores</a>
            </div>`;
    }

    function renderActionBar() {
        $("#il-analyze-label").textContent = analysis ? "Re-Analyze" : "Analyze";
        $("#il-last-analyzed").textContent = createdAt ? `Last analyzed ${formatDate(createdAt)}` : "";
        $("#il-stale-badge").style.display = analysis && analysis.stale ? "inline-flex" : "none";
    }

    function renderStats() {
        const strip = $("#il-stats");
        if (!analysis) { strip.style.display = "none"; return; }
        const s = analysis.summary || {};
        const orphans = num(s.orphan_products_count) + num(s.orphan_collections_count) + num(s.orphan_articles_count);
        const items = [
            [num(s.total_links), "internal links"],
            [num(s.avg_score), "avg score"],
            [orphans, "orphan pages"],
            [num(s.under_linked_count), "under-linked"],
            [num(s.broken_links_count), "broken links"],
            [num(s.missing_crosslinks_count), "cross-link opportunities"],
        ];
        strip.style.display = "";
        strip.innerHTML = items
            .map(([v, label]) => `<span class="stat-item"><strong>${v}</strong> ${label}</span>`)
            .join('<span class="stat-divider"></span>');
    }

    function updateTabCounts() {
        const s = (analysis && analysis.summary) || {};
        const counts = {
            issues: num(s.broken_links_count) + num(s.non_canonical_count),
            coverage:
                num(s.orphan_products_count) + num(s.orphan_collections_count) +
                num(s.orphan_articles_count) + num(s.under_linked_count),
            opportunities: num(s.missing_crosslinks_count),
        };
        document.querySelectorAll(".il-tab-count").forEach((el) => {
            const c = counts[el.dataset.count] || 0;
            el.textContent = c ? ` ${c}` : "";
        });
    }

    function emptyAnalysisState(message) {
        return `
            <div class="empty-state">
                <span class="material-symbols-rounded">link</span>
                <p>${message || "Run an analysis to map every internal link in this store."}</p>
                <button class="btn btn-primary" data-il-run-analysis>
                    <span class="material-symbols-rounded">travel_explore</span> Analyze Interlinking
                </button>
            </div>`;
    }

    function errorState(message) {
        return `
            <div class="empty-state">
                <span class="material-symbols-rounded">error</span>
                <p>${message}</p>
                <button class="btn btn-secondary" data-il-retry>Retry</button>
            </div>`;
    }

    document.addEventListener("click", (e) => {
        if (e.target.closest("[data-il-run-analysis]")) $("#il-analyze-btn").click();
        if (e.target.closest("[data-il-retry]")) loadAll();
    });

    /* ── overview tab ──────────────────────────────────────────────── */
    function renderOverview() {
        const panel = $("#il-tab-overview");
        if (!analysis) { panel.innerHTML = emptyAnalysisState(); return; }
        const s = analysis.summary || {};
        const dist = s.score_distribution || {};
        const total = num(dist.excellent) + num(dist.good) + num(dist.poor) + num(dist.orphan) || 1;
        const bar = (label, count, cls) => `
            <div class="il-dist-row">
                <span class="il-dist-label">${label}</span>
                <div class="il-dist-track"><div class="il-dist-fill ${cls}" style="width:${Math.max(2, Math.round((num(count) / total) * 100))}%"></div></div>
                <span class="il-dist-count">${num(count)}</span>
            </div>`;

        const issue = (icon, label, count, tab, segment) => `
            <div class="il-issue-row ${num(count) ? "" : "il-issue-clear"}" tabindex="0" role="button"
                 data-il-goto="${tab}" ${segment ? `data-il-segment="${segment}"` : ""}>
                <span class="material-symbols-rounded">${icon}</span>
                <span style="flex:1;">${label}</span>
                <strong>${num(count)}</strong>
                <span class="material-symbols-rounded il-issue-arrow">chevron_right</span>
            </div>`;

        panel.innerHTML = `
            <div class="il-overview-grid">
                <div class="card">
                    <div class="card-header"><h3>Link health</h3></div>
                    <div style="padding:4px 0 8px;">
                        ${bar("Excellent", dist.excellent, "il-dist-excellent")}
                        ${bar("Good", dist.good, "il-dist-good")}
                        ${bar("Poor", dist.poor, "il-dist-poor")}
                        ${bar("Orphaned", dist.orphan, "il-dist-orphan")}
                    </div>
                    <div style="font-size:13px;color:var(--on-surface-secondary);">
                        ${num(s.total_products)} products · ${num(s.total_collections)} collections ·
                        ${num(s.total_articles)} articles · avg ${num(s.avg_incoming_links)} incoming links
                    </div>
                </div>
                <div class="card">
                    <div class="card-header"><h3>Where to act</h3></div>
                    ${issue("link_off", "Broken links", s.broken_links_count, "issues")}
                    ${issue("alt_route", "Non-canonical links", s.non_canonical_count, "issues")}
                    ${issue("sell", "Orphan products", s.orphan_products_count, "coverage", "orphan-products")}
                    ${issue("category", "Orphan collections", s.orphan_collections_count, "coverage", "orphan-collections")}
                    ${issue("article", "Orphan articles", s.orphan_articles_count, "coverage", "orphan-articles")}
                    ${issue("link", "Under-linked pages", s.under_linked_count, "coverage", "under-linked")}
                    ${issue("category", "Products not in a collection", s.not_in_collection_count, "coverage", "not-in-collection")}
                    ${issue("swap_horiz", "Missing cross-links", s.missing_crosslinks_count, "opportunities")}
                </div>
            </div>`;

        panel.querySelectorAll("[data-il-goto]").forEach((el) => {
            const go = () => {
                if (el.dataset.ilSegment) coverageSegment = el.dataset.ilSegment;
                renderCoverage();
                tabsCtl.activate(el.dataset.ilGoto);
            };
            el.addEventListener("click", go);
            el.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
        });
    }

    /* ── shared table helper (sort / search / paginate) ────────────── */
    function createTable(container, cfg) {
        const st = {
            sortKey: cfg.defaultSort || null,
            sortDir: cfg.defaultDir || "desc",
            query: "",
            page: 0,
        };
        const pageSize = cfg.pageSize || 50;

        container.innerHTML = `
            ${cfg.searchKeys ? `<div class="il-table-toolbar">
                <input type="text" class="form-control il-table-search" placeholder="${escapeAttr(cfg.searchPlaceholder || "Search…")}" style="max-width:260px;">
                <span class="il-table-meta"></span>
            </div>` : ""}
            <div class="table-container"><div class="il-table-body"></div></div>
            <div class="il-pager"></div>`;

        const body = container.querySelector(".il-table-body");
        const pager = container.querySelector(".il-pager");
        const meta = container.querySelector(".il-table-meta");
        const search = container.querySelector(".il-table-search");
        if (search) {
            let t = null;
            search.addEventListener("input", () => {
                clearTimeout(t);
                t = setTimeout(() => { st.query = search.value.trim(); st.page = 0; render(); }, 200);
            });
        }

        function filtered() {
            let rows = cfg.rows();
            if (st.query && cfg.searchKeys) {
                const q = st.query.toLowerCase();
                rows = rows.filter((r) => cfg.searchKeys.some((k) => String(r[k] || "").toLowerCase().includes(q)));
            }
            if (st.sortKey) {
                rows = [...rows].sort((a, b) => {
                    let va = a[st.sortKey], vb = b[st.sortKey];
                    if (typeof va === "string" || typeof vb === "string") {
                        va = String(va || "").toLowerCase();
                        vb = String(vb || "").toLowerCase();
                    } else {
                        va = num(va); vb = num(vb);
                    }
                    if (va < vb) return st.sortDir === "asc" ? -1 : 1;
                    if (va > vb) return st.sortDir === "asc" ? 1 : -1;
                    return 0;
                });
            }
            return rows;
        }

        function render() {
            const rows = filtered();
            const pages = Math.max(1, Math.ceil(rows.length / pageSize));
            if (st.page >= pages) st.page = pages - 1;
            const slice = rows.slice(st.page * pageSize, (st.page + 1) * pageSize);

            if (meta) meta.textContent = rows.length ? `${rows.length} item${rows.length !== 1 ? "s" : ""}` : "";

            if (!rows.length) {
                body.innerHTML = `<div class="empty-state" style="padding:32px 16px;">
                    <span class="material-symbols-rounded">${cfg.emptyIcon || "check_circle"}</span>
                    <p>${cfg.emptyText || "No items."}</p></div>`;
                pager.innerHTML = "";
                return;
            }

            body.innerHTML = `<table><thead><tr>${cfg.columns
                .map((c) => c.sortable
                    ? `<th class="sortable-header ${st.sortKey === c.key ? st.sortDir : ""}" data-sort="${c.key}">${c.label}</th>`
                    : `<th>${c.label}</th>`)
                .join("")}</tr></thead>
                <tbody>${slice.map((r, i) => `
                    <tr ${cfg.onRow ? 'tabindex="0" class="il-row-click"' : ""} data-row="${st.page * pageSize + i}">
                        ${cfg.columns.map((c) => `<td>${c.render(r)}</td>`).join("")}
                    </tr>`).join("")}</tbody></table>`;

            body.querySelectorAll("[data-sort]").forEach((th) => {
                th.addEventListener("click", () => {
                    if (st.sortKey === th.dataset.sort) {
                        st.sortDir = st.sortDir === "asc" ? "desc" : "asc";
                    } else {
                        st.sortKey = th.dataset.sort;
                        st.sortDir = "desc";
                    }
                    render();
                });
            });

            if (cfg.onRow) {
                body.querySelectorAll("tr[data-row]").forEach((tr) => {
                    const open = (e) => {
                        if (e.target.closest("button, a")) return;
                        cfg.onRow(rows[parseInt(tr.dataset.row, 10)]);
                    };
                    tr.addEventListener("click", open);
                    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") open(e); });
                });
            }
            if (cfg.onRender) cfg.onRender(body, slice);

            pager.innerHTML = pages > 1 ? `
                <button class="btn btn-ghost btn-sm" data-pg="prev" ${st.page === 0 ? "disabled" : ""}>‹ Prev</button>
                <span>${st.page * pageSize + 1}–${Math.min((st.page + 1) * pageSize, rows.length)} of ${rows.length}</span>
                <button class="btn btn-ghost btn-sm" data-pg="next" ${st.page >= pages - 1 ? "disabled" : ""}>Next ›</button>` : "";
            pager.querySelectorAll("[data-pg]").forEach((b) => {
                b.addEventListener("click", () => {
                    st.page += b.dataset.pg === "next" ? 1 : -1;
                    render();
                    container.scrollIntoView({ block: "start", behavior: "smooth" });
                });
            });
        }

        render();
        return { render };
    }

    /* ── coverage tab ──────────────────────────────────────────────── */
    const COVERAGE_SEGMENTS = [
        ["orphan-products", "Orphan products", "orphan_products"],
        ["orphan-collections", "Orphan collections", "orphan_collections"],
        ["orphan-articles", "Orphan articles", "orphan_articles"],
        ["under-linked", "Under-linked", "under_linked"],
        ["not-in-collection", "Not in collection", "not_in_collection"],
    ];

    function coverageRows() {
        const key = (COVERAGE_SEGMENTS.find(([id]) => id === coverageSegment) || COVERAGE_SEGMENTS[0])[2];
        const fallbackType = { orphan_products: "product", orphan_collections: "collection", orphan_articles: "article", not_in_collection: "product" }[key] || "product";
        return (analysis[key] || []).map((d) => ({
            ...d,
            item_type: d.item_type || fallbackType,
            incoming_count: num(d.incoming_count),
            outgoing_count: num(d.outgoing_count),
            score: num(d.score),
        }));
    }

    function renderCoverage() {
        const panel = $("#il-tab-coverage");
        if (!analysis) { panel.innerHTML = emptyAnalysisState(); return; }

        const segType = coverageSegment === "orphan-collections" ? "collection"
            : coverageSegment === "orphan-articles" ? "article" : null;
        const segOrphans = segType ? (analysis[ORPHAN_META[segType].listKey] || []) : [];

        panel.innerHTML = `
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                <div class="status-filter-bar" id="il-cov-segments" style="flex:1;min-width:280px;">
                    ${COVERAGE_SEGMENTS.map(([id, label, key]) => `
                        <button class="filter-chip ${coverageSegment === id ? "active" : ""}" data-seg="${id}">
                            ${label} <span class="il-chip-count">${(analysis[key] || []).length}</span>
                        </button>`).join("")}
                </div>
                ${segType && segOrphans.length ? `
                    <button class="btn btn-primary btn-sm" data-il-orphanfix>
                        <span class="material-symbols-rounded">auto_fix_high</span> AI fix orphan ${ORPHAN_META[segType].plural}…
                    </button>` : ""}
                ${segType && orphanPlans[segType] ? `
                    <button class="btn btn-secondary btn-sm" data-il-orphanfix-view title="Review the last generated fix plan">
                        <span class="material-symbols-rounded">fact_check</span> Last plan
                    </button>` : ""}
            </div>
            <div class="card" style="margin-top:12px;"><div id="il-cov-table"></div></div>`;

        panel.querySelectorAll("[data-seg]").forEach((chip) => {
            chip.addEventListener("click", () => {
                coverageSegment = chip.dataset.seg;
                renderCoverage();
            });
        });
        const orphanFixBtn = panel.querySelector("[data-il-orphanfix]");
        if (orphanFixBtn) orphanFixBtn.addEventListener("click", () => openOrphanFixConfig(segType));
        const orphanFixView = panel.querySelector("[data-il-orphanfix-view]");
        if (orphanFixView) orphanFixView.addEventListener("click", () => openOrphanFixReview(segType));

        const segMeta = {
            "orphan-products": "No other page links to these products and they aren't in a collection.",
            "orphan-collections": "No other page links to these collections.",
            "orphan-articles": "No other page links to these articles.",
            "under-linked": "Fewer than 2 incoming links.",
            "not-in-collection": "Products that don't belong to any collection.",
        };

        createTable($("#il-cov-table"), {
            rows: coverageRows,
            searchKeys: ["title", "handle"],
            searchPlaceholder: "Search title or handle…",
            defaultSort: "score",
            defaultDir: "asc",
            emptyIcon: "check_circle",
            emptyText: `Nothing here — ${segMeta[coverageSegment] || ""}`.trim(),
            columns: [
                { key: "title", label: "Title", sortable: true, render: (r) => `
                    <div style="display:flex;align-items:center;gap:8px;">
                        <span class="material-symbols-rounded" style="font-size:16px;color:var(--on-surface-secondary);">${TYPE_ICONS[r.item_type] || "link"}</span>
                        <div><div>${escapeHtml(r.title || r.handle)}</div>
                        <div style="font-size:12px;color:var(--on-surface-secondary);">${escapeHtml(r.handle || "")}</div></div>
                    </div>` },
                { key: "item_type", label: "Type", sortable: true, render: (r) => `<span class="badge badge-info">${escapeHtml(r.item_type)}</span>` },
                { key: "incoming_count", label: "Incoming", sortable: true, render: (r) => String(r.incoming_count) },
                { key: "outgoing_count", label: "Outgoing", sortable: true, render: (r) => String(r.outgoing_count) },
                { key: "score", label: "Score", sortable: true, render: (r) => getScoreBadge(r.score) },
                ...(segType ? [{
                    key: "_fix", label: "", sortable: false,
                    render: () => `<button class="btn btn-primary btn-sm" data-il-row-fix title="See the AI-recommended inbound links for this ${segType}">Fix…</button>`,
                }] : []),
            ],
            onRow: (r) => openItemDrawer(r.item_type, r.id),
            onRender: segType ? (body, slice) => {
                body.querySelectorAll("[data-il-row-fix]").forEach((btn, i) => {
                    btn.addEventListener("click", () => openItemFixModal(segType, slice[i].handle));
                });
            } : null,
        });
    }

    /* ── orphan page AI fix (collections + articles) ───────────────── */
    const ORPHAN_META = {
        collection: { plural: "collections", listKey: "orphan_collections", segment: "orphan-collections" },
        article: { plural: "articles", listKey: "orphan_articles", segment: "orphan-articles" },
    };

    function planKey(p) { return p.handle || p.collection_handle; }
    function planTitle(p) { return p.title || p.collection_title || planKey(p); }
    function fixKey(f) { return `${f.from_type}:${f.from_handle}->${f.to_type}:${f.to_handle}`; }
    // Fixes applied this session — reopened plan modals show them as done
    // instead of pretending nothing happened.
    const appliedFixKeys = new Set();

    function mergePlanEntry(itemType, entry, fullResult) {
        if (!entry) return;
        if (!orphanPlans[itemType] || !orphanPlans[itemType].result) {
            orphanPlans[itemType] = {
                result: fullResult || { plans: [entry] },
                completed_at: new Date().toISOString(),
            };
            return;
        }
        const plans = orphanPlans[itemType].result.plans || (orphanPlans[itemType].result.plans = []);
        const idx = plans.findIndex((p) => planKey(p) === planKey(entry));
        if (idx >= 0) plans[idx] = entry; else plans.push(entry);
        orphanPlans[itemType].completed_at = new Date().toISOString();
    }

    function openOrphanFixConfig(itemType) {
        const meta = ORPHAN_META[itemType];
        const count = (analysis[meta.listKey] || []).length;
        if (!count) { showToast(`No orphan ${meta.plural} to fix`, "info"); return; }
        const saved = loadFilter("interlinkLinksPerColl", "2");
        const modal = UIModal.open({
            title: `AI fix orphan ${meta.plural}`,
            bodyHtml: `
                <p style="margin-top:0;">The AI will pick the best pages to link <em>into</em>
                each of the <strong>${count}</strong> orphan ${meta.plural} — and for ${meta.plural} that
                link out to nothing, it also adds outbound links to related pages. You review the plan
                before anything is applied.</p>
                <div style="display:flex;align-items:center;gap:10px;margin:14px 0 4px;">
                    <label style="font-size:14px;">Links per ${itemType}</label>
                    <select id="il-cf-links" class="form-control" style="width:auto;padding:6px 10px;">
                        ${[1, 2, 3].map((v) => `<option value="${v}" ${String(v) === String(saved) ? "selected" : ""}>${v}</option>`).join("")}
                    </select>
                    <span id="il-cf-est" style="font-size:13px;color:var(--on-surface-secondary);"></span>
                </div>
                <label style="display:flex;align-items:center;gap:8px;margin-top:12px;font-size:14px;cursor:pointer;">
                    <input type="checkbox" id="il-cf-auto" ${loadFilter("interlinkAutoApply", "") === "1" ? "checked" : ""}>
                    <span>Apply all links automatically after generating (skip review)</span>
                </label>
                <p id="il-cf-auto-note" style="font-size:12px;color:var(--on-surface-secondary);margin:6px 0 0 24px;">
                    Runs in the background (~10-20s per link) — track progress in the status chip or Activity.
                </p>`,
            footerHtml: `
                <button class="btn btn-secondary" data-c>Cancel</button>
                <button class="btn btn-primary" data-go>
                    <span class="material-symbols-rounded">auto_awesome</span> Generate plan
                </button>`,
        });
        const sel = modal.el.querySelector("#il-cf-links");
        const est = modal.el.querySelector("#il-cf-est");
        const updateEst = () => { est.textContent = `up to ${count * parseInt(sel.value, 10)} links`; };
        sel.addEventListener("change", () => { saveFilter("interlinkLinksPerColl", sel.value); updateEst(); });
        updateEst();
        modal.el.querySelector("[data-c]").addEventListener("click", () => modal.close());
        modal.el.querySelector("[data-go]").addEventListener("click", async () => {
            const perPage = parseInt(sel.value, 10);
            const auto = modal.el.querySelector("#il-cf-auto").checked;
            saveFilter("interlinkAutoApply", auto ? "1" : "");
            modal.close();
            await runOrphanFixPlan(itemType, perPage, count, auto);
        });
    }

    async function runOrphanFixPlan(itemType, perPage, count, autoApply) {
        setStatusChip("Planning inbound links…", true);
        try {
            const start = await apiRequest("/api/interlinking/orphan-fix", {
                method: "POST",
                body: { store_id: storeId, item_type: itemType, links_per_page: perPage },
            });
            showToast("Generating link plan — also tracked in Activity", "info");
            const task = await pollTaskUntilDone(`/api/interlinking/task/${start.task_id}?store_id=${storeId}`, {
                intervalMs: 3000,
                timeoutMs: batchTimeoutMs(count * 2),
            });
            setStatusChip(null);
            orphanPlans[itemType] = { result: task.result, completed_at: new Date().toISOString() };
            renderCoverage();
            if (autoApply) {
                const fixes = (task.result.plans || []).flatMap((p) => p.fixes || []);
                if (!fixes.length) {
                    showToast("The AI found no viable links to apply", "info");
                    return;
                }
                const rs = await runIssueBatch("ai_links", fixes, "Applying inbound links");
                (rs || []).forEach((r) => {
                    if (r.status === "applied" || r.status === "skipped") {
                        const f = fixes.find((x) => x.from_handle === r.from_handle && x.to_handle === r.to_handle);
                        if (f) appliedFixKeys.add(fixKey(f));
                    }
                });
                renderCoverage();
            } else {
                openOrphanFixReview(itemType);
            }
        } catch (e) {
            setStatusChip(null);
            if (isStillRunningError(e)) {
                showToast("Plan generation is still running — check Activity, then use “Last plan”", "info");
            } else {
                showToast(`Plan generation failed: ${e.message}`, "error");
            }
        }
    }

    function openOrphanFixReview(itemType) {
        const planData = orphanPlans[itemType];
        if (!planData || !planData.result) { showToast("No fix plan yet", "info"); return; }
        const modal = openPlanModalShell("Link plan");
        populatePlanModal(modal, planData.result.plans || [], {
            itemType,
            summary: planData.result.summary,
            completedAt: planData.completed_at,
        });
    }

    async function openItemFixModal(itemType, handle, forceRegen) {
        const cachedPlans = (orphanPlans[itemType] && orphanPlans[itemType].result
            && orphanPlans[itemType].result.plans) || [];
        const cached = !forceRegen
            ? cachedPlans.find((p) => planKey(p) === handle && (p.fixes || []).length)
            : null;
        const modal = openPlanModalShell("Link fix");
        if (cached) {
            populatePlanModal(modal, [cached], { itemType, showRegenerate: true, handle });
            return;
        }

        modal.el.querySelector(".modal-body").innerHTML = `
            <div class="loading-spinner" style="flex-direction:column;gap:12px;">
                <div class="spinner"></div>
                <span style="font-size:13px;color:var(--on-surface-secondary);">
                    Finding the best pages to link here… (~15s)</span>
            </div>`;
        try {
            const perPage = parseInt(loadFilter("interlinkLinksPerColl", "2"), 10) || 2;
            const start = await apiRequest("/api/interlinking/orphan-fix", {
                method: "POST",
                body: { store_id: storeId, item_type: itemType, handles: [handle], links_per_page: perPage },
            });
            const task = await pollTaskUntilDone(`/api/interlinking/task/${start.task_id}?store_id=${storeId}`, {
                intervalMs: 2500,
                timeoutMs: batchTimeoutMs(4),
            });
            const plans = (task.result && task.result.plans) || [];
            const entry = plans.find((p) => planKey(p) === handle) || plans[0];
            mergePlanEntry(itemType, entry, task.result);
            if (!modal.el.isConnected) return; // closed while generating
            populatePlanModal(modal, entry ? [entry] : [], { itemType, showRegenerate: true, handle });
        } catch (e) {
            if (!modal.el.isConnected) return;
            modal.el.querySelector(".modal-body").innerHTML = `
                <div class="empty-state"><span class="material-symbols-rounded">error</span>
                <p>${escapeHtml(isStillRunningError(e)
                    ? "Still generating in the background — check Activity, then reopen this item."
                    : `Plan generation failed: ${e.message}`)}</p></div>`;
        }
    }

    function openPlanModalShell(title) {
        const modal = UIModal.open({
            title,
            bodyHtml: '<div class="loading-spinner"><div class="spinner"></div></div>',
            footerHtml: `
                <span id="il-cf-summary" style="flex:1;font-size:13px;color:var(--on-surface-secondary);"></span>
                <button class="btn btn-ghost" data-regen style="display:none;">Regenerate</button>
                <button class="btn btn-secondary" data-c>Cancel</button>
                <button class="btn btn-primary" data-apply disabled>Apply</button>`,
            size: "lg",
        });
        modal.el.querySelector("[data-c]").addEventListener("click", () => modal.close());
        return modal;
    }

    function populatePlanModal(modal, plans, opts = {}) {
        const el = modal.el;
        const allFixes = [];
        plans.forEach((p) => (p.fixes || []).forEach((f) => allFixes.push(f)));

        const planHtml = plans.map((p) => `
            <div class="il-cf-group ${p.fixes && p.fixes.length ? "" : "il-cf-empty"}">
                <div class="il-cf-head">
                    <strong>${escapeHtml(planTitle(p))}</strong>
                    <code style="font-size:11px;color:var(--on-surface-secondary);">${escapeHtml(planKey(p) || "")}</code>
                    ${p.fixes && p.fixes.length ? "" : `<span style="font-size:12px;color:var(--on-surface-secondary);">${escapeHtml(p.note || "No suggestions")}</span>`}
                </div>
                ${(p.fixes || []).map((f) => {
                    const idx = allFixes.indexOf(f);
                    const isOut = f.direction === "outbound";
                    const otherType = isOut ? f.to_type : f.from_type;
                    const otherTitle = isOut ? (f.to_title || f.to_handle) : (f.from_title || f.from_handle);
                    const applied = appliedFixKeys.has(fixKey(f));
                    return `
                    <label class="il-source-row il-cf-fix" ${applied ? 'style="opacity:.55;"' : ""}>
                        ${applied
                            ? '<span class="material-symbols-rounded" style="color:var(--success);font-size:18px;" title="Already applied">check_circle</span>'
                            : `<input type="checkbox" data-cf-fix="${idx}" checked>`}
                        <span class="il-dir-tag ${isOut ? "il-dir-out" : ""}"
                              title="${isOut ? "This page will link out to the page below" : "The page below will link to this page"}">
                            <span class="material-symbols-rounded">${isOut ? "arrow_outward" : "arrow_insert"}</span>${isOut ? "out" : "in"}
                        </span>
                        <span class="badge badge-info">${escapeHtml(otherType)}</span>
                        <span style="flex:1;">${escapeHtml(otherTitle)}</span>
                        <span class="il-source-anchor">“${escapeHtml(f.suggested_anchor || "")}”</span>
                        <span class="il-src-status" data-cf-status="${idx}"></span>
                    </label>
                    ${f.reason ? `<div class="il-cf-reason">${escapeHtml(f.reason)}</div>` : ""}`;
                }).join("")}
            </div>`).join("");

        el.querySelector(".modal-body").innerHTML = `
            ${opts.summary || opts.completedAt ? `
                <p style="margin-top:0;font-size:13px;color:var(--on-surface-secondary);">
                    ${escapeHtml(opts.summary || "")}
                    ${opts.completedAt ? ` · generated ${formatRelativeTime(opts.completedAt)}` : ""}
                    — each selected link is AI-woven into the source page's content and written to Shopify.
                </p>` : `
                <p style="margin-top:0;font-size:13px;color:var(--on-surface-secondary);">
                    Each selected link is AI-woven into the source page's content and written to Shopify.
                </p>`}
            <div class="il-fix-sources" style="max-height:420px;">${planHtml || '<div class="empty-state"><p>No suggestions.</p></div>'}</div>
            <div class="il-fix-progress" id="il-cf-progress" style="display:none;">
                <div class="progress-bar-container"><div class="progress-bar" id="il-cf-bar" style="width:0%;"></div></div>
                <span id="il-cf-progress-text"></span>
            </div>`;

        const regen = el.querySelector("[data-regen]");
        if (regen && opts.showRegenerate && opts.handle) {
            regen.style.display = "";
            regen.addEventListener("click", () => {
                modal.close();
                openItemFixModal(opts.itemType, opts.handle, true);
            });
        }

        const applyBtn = el.querySelector("[data-apply]");
        let done = false;
        const anyApplied = allFixes.some((f) => appliedFixKeys.has(fixKey(f)));
        const checked = () =>
            [...el.querySelectorAll("[data-cf-fix]:checked")].map((c) => parseInt(c.dataset.cfFix, 10));
        const updateApply = () => {
            const nSel = checked().length;
            if (!nSel && anyApplied) {
                // everything selectable was already applied — Done just closes
                done = true;
                applyBtn.textContent = "Done";
                applyBtn.disabled = false;
                el.querySelector("#il-cf-summary").textContent = "These links have already been applied.";
                return;
            }
            applyBtn.textContent = nSel ? `Apply ${nSel} link${nSel !== 1 ? "s" : ""}` : "Apply";
            applyBtn.disabled = nSel === 0;
        };
        el.querySelectorAll("[data-cf-fix]").forEach((c) => c.addEventListener("change", updateApply));
        updateApply();
        applyBtn.addEventListener("click", async () => {
            if (done) { modal.close(); return; }
            const idxs = checked();
            if (!idxs.length) return;
            const items = idxs.map((i) => allFixes[i]);
            applyBtn.disabled = true;
            if (regen) regen.style.display = "none";
            el.querySelectorAll("[data-cf-fix]").forEach((c) => { c.disabled = true; });
            const bar = el.querySelector("#il-cf-bar");
            const progWrap = el.querySelector("#il-cf-progress");
            const progText = el.querySelector("#il-cf-progress-text");
            progWrap.style.display = "";
            // Each source page is an AI content rewrite (~10-20s) — show life
            // immediately so a small batch doesn't look frozen at 0%.
            bar.classList.add("indeterminate");
            progText.textContent = `0 / ${items.length} — writing links, ~10-20s per source page…`;
            idxs.forEach((i) => {
                const cell = el.querySelector(`[data-cf-status="${i}"]`);
                if (cell) cell.innerHTML = '<span class="spinner-sm"></span>';
            });
            try {
                const start = await apiRequest("/api/interlinking/apply-batch", {
                    method: "POST",
                    body: { store_id: storeId, kind: "ai_links", items },
                });
                const task = await pollTaskUntilDone(`/api/interlinking/task/${start.task_id}?store_id=${storeId}`, {
                    intervalMs: 2500,
                    timeoutMs: batchTimeoutMs(items.length),
                    onProgress: (t) => {
                        const p = t.result && t.result.progress;
                        if (!p || !p.completed) return;
                        bar.classList.remove("indeterminate");
                        bar.style.width = `${Math.round((p.completed / Math.max(1, p.total)) * 100)}%`;
                        progText.textContent = `${p.completed} / ${p.total} — ~10-20s per source page`;
                    },
                });
                progWrap.style.display = "none";
                const rs = (task.result && task.result.results) || [];
                let applied = 0, skipped = 0, errors = 0;
                rs.forEach((r) => {
                    const fi = items.findIndex(
                        (f) => f.from_handle === r.from_handle && f.to_handle === r.to_handle
                    );
                    const cell = fi >= 0 ? el.querySelector(`[data-cf-status="${idxs[fi]}"]`) : null;
                    if (r.status === "applied") {
                        applied += 1;
                        if (fi >= 0) appliedFixKeys.add(fixKey(items[fi]));
                        if (cell) cell.innerHTML = '<span class="material-symbols-rounded" style="color:var(--success);">check_circle</span>';
                    } else if (r.status === "skipped") {
                        skipped += 1;
                        if (fi >= 0) appliedFixKeys.add(fixKey(items[fi]));
                        if (cell) cell.innerHTML = `<span class="material-symbols-rounded" style="color:var(--on-surface-secondary);" title="${escapeAttr(r.reason || "Skipped")}">remove_circle</span>`;
                    } else {
                        errors += 1;
                        if (cell) cell.innerHTML = `<span class="material-symbols-rounded" style="color:var(--error);" title="${escapeAttr(r.reason || "Error")}">error</span><span class="il-src-reason">${escapeHtml((r.reason || "").slice(0, 60))}</span>`;
                    }
                });
                el.querySelector("#il-cf-summary").innerHTML =
                    `<strong>${applied}</strong> applied · ${skipped} skipped · ` +
                    (errors ? `<span style="color:var(--error);"><strong>${errors}</strong> failed</span>` : "0 failed");
                showToast(errors ? `${applied} applied, ${errors} failed` : `${applied} inbound link${applied !== 1 ? "s" : ""} applied`, errors ? "error" : "success");
                done = true;
                applyBtn.textContent = "Done";
                applyBtn.disabled = false;
                el.querySelector("[data-c]").textContent = "Close";
                await reloadAnalysis();
            } catch (e) {
                progWrap.style.display = "none";
                if (isStillRunningError(e)) {
                    el.querySelector("#il-cf-summary").textContent = "Still applying in the background — track in Activity.";
                    showToast("Still applying in the background — track in Activity", "info");
                } else {
                    el.querySelector("#il-cf-summary").innerHTML = `<span style="color:var(--error);">${escapeHtml(e.message)}</span>`;
                    applyBtn.disabled = false;
                    el.querySelectorAll("[data-cf-fix]").forEach((c) => { c.disabled = false; });
                }
                await reloadAnalysis();
            }
        });
    }
    /* ── item drawer ───────────────────────────────────────────────── */
    async function openItemDrawer(itemType, itemId) {
        if (!drawer) drawer = UIDrawer.create({ id: "il-item-drawer", title: "Link detail", width: 560 });
        drawer.setTitle("Link detail").setBody('<div class="loading-spinner"><div class="spinner"></div></div>').open();
        try {
            const d = await apiRequest(`/api/interlinking/item/${itemType}/${itemId}`);
            renderDrawer(d);
        } catch (e) {
            drawer.setBody(`<div class="empty-state"><span class="material-symbols-rounded">error</span>
                <p>Could not load link details: ${escapeHtml(e.message)}</p></div>`);
        }
    }

    function renderDrawer(d) {
        const item = d.item || {};
        const detail = d.detail || {};
        const urls = d.urls || {};
        const linkRow = (l) => `
            <tr><td>${escapeHtml(l.handle || l.href || "")}</td>
                <td><span class="badge badge-info">${escapeHtml(l.link_type || "")}</span>
                    ${l.non_canonical ? '<span class="badge badge-warning">non-canonical</span>' : ""}</td>
                <td>${escapeHtml(l.anchor_text || "")}</td></tr>`;
        const inRow = (l) => `
            <tr><td>${escapeHtml(l.from_title || l.from_handle || "")}</td>
                <td><span class="badge badge-info">${escapeHtml(l.from_type || "")}</span></td>
                <td>${escapeHtml(l.anchor_text || "")}</td></tr>`;
        const table = (rows, empty, header) => rows.length
            ? `<div class="table-container"><table><thead><tr>${header}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`
            : `<p style="font-size:13px;color:var(--on-surface-secondary);margin:6px 0 0;">${empty}</p>`;

        drawer.setTitle(escapeHtml(item.title || item.handle || "Link detail"));
        drawer.setBody(`
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;">
                <span class="badge badge-info">${escapeHtml(item.item_type || "")}</span>
                ${detail.score !== undefined ? getScoreBadge(num(detail.score)) : ""}
                <span style="font-size:13px;color:var(--on-surface-secondary);">
                    ${num(detail.incoming_count)} incoming · ${num(detail.outgoing_count)} outgoing
                </span>
            </div>
            <div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;">
                ${urls.storefront ? `<a class="btn btn-secondary btn-sm" href="${escapeAttr(urls.storefront)}" target="_blank" rel="noopener">
                    <span class="material-symbols-rounded">open_in_new</span> Storefront</a>` : ""}
                ${urls.admin ? `<a class="btn btn-secondary btn-sm" href="${escapeAttr(urls.admin)}" target="_blank" rel="noopener">
                    <span class="material-symbols-rounded">edit</span> Shopify admin</a>` : ""}
                ${["collection", "article"].includes(item.item_type)
                    && (num(detail.incoming_count) === 0 || num(detail.outgoing_count) === 0) ? `
                    <button class="btn btn-primary btn-sm" data-il-drawer-collfix
                            data-handle="${escapeAttr(item.handle || "")}" data-type="${escapeAttr(item.item_type)}">
                        <span class="material-symbols-rounded">auto_fix_high</span> Suggest links (AI)
                    </button>` : ""}
            </div>
            <h4 class="il-drawer-h">Incoming links (${(d.incoming_links || []).length})</h4>
            ${table((d.incoming_links || []).map(inRow), "No other page links here — that's the problem to fix.", "<th>From</th><th>Type</th><th>Anchor</th>")}
            <h4 class="il-drawer-h" style="margin-top:18px;">Outgoing links (${(d.outgoing_links || []).length})</h4>
            ${table((d.outgoing_links || []).map(linkRow), "This page links to nothing else in the store.", "<th>Target</th><th>Type</th><th>Anchor</th>")}
        `);
        const fixBtn = drawer.body().querySelector("[data-il-drawer-collfix]");
        if (fixBtn) {
            fixBtn.addEventListener("click", () => {
                drawer.close();
                openItemFixModal(fixBtn.dataset.type, fixBtn.dataset.handle);
            });
        }
    }

    /* ── issues tab (broken + non-canonical) ───────────────────────── */
    function groupHrefIssues(list) {
        const map = new Map();
        for (const e of list || []) {
            const key = `${e.to_type || ""}|${e.to_href || e.to_handle || ""}`;
            if (!map.has(key)) {
                map.set(key, {
                    to_type: e.to_type || "product",
                    to_handle: e.to_handle || "",
                    to_href: e.to_href || "",
                    to_blog_handle: e.to_blog_handle || "",
                    unverified: !!e.unverified,
                    sources: [],
                    _srcSeen: new Set(),
                });
            }
            const g = map.get(key);
            if (e.unverified) g.unverified = true;
            const srcKey = `${e.from_type}|${e.from_blog_handle || ""}|${e.from_handle}`;
            if (g._srcSeen.has(srcKey)) {
                const src = g.sources.find((s) => s._key === srcKey);
                if (src && e.anchor_text && !src.anchors.includes(e.anchor_text)) src.anchors.push(e.anchor_text);
            } else {
                g._srcSeen.add(srcKey);
                g.sources.push({
                    _key: srcKey,
                    from_type: e.from_type,
                    from_handle: e.from_handle,
                    from_title: e.from_title || e.from_handle,
                    from_blog_handle: e.from_blog_handle || "",
                    anchors: e.anchor_text ? [e.anchor_text] : [],
                });
            }
        }
        return [...map.values()]
            .map((g) => { delete g._srcSeen; return g; })
            .sort((a, b) => b.sources.length - a.sources.length);
    }

    function renderIssues() {
        const panel = $("#il-tab-issues");
        if (!analysis) { panel.innerHTML = emptyAnalysisState(); return; }

        brokenGroups = groupHrefIssues(analysis.broken_links);
        noncanonGroups = groupHrefIssues(analysis.non_canonical_links);

        const unverifiedNote = analysis.verification && analysis.verification.live_checked === false
            ? `<div class="il-banner il-banner-warn" style="margin-bottom:12px;">
                 <span class="material-symbols-rounded">warning</span>
                 <span>Broken links could not be verified against Shopify during the last analysis —
                 some targets may exist as drafts. Re-analyze with a working store connection to confirm.</span>
               </div>`
            : "";

        const groupRows = (groups, kind) => groups.map((g, i) => `
            <div class="il-group" data-kind="${kind}" data-group="${i}">
                <div class="il-group-head" tabindex="0" role="button" data-il-expand>
                    <span class="material-symbols-rounded il-group-chevron">chevron_right</span>
                    <code class="il-group-href">${escapeHtml(g.to_href || `/${g.to_type}s/${g.to_handle}`)}</code>
                    <span class="badge badge-info">${escapeHtml(g.to_type)}</span>
                    ${g.unverified ? '<span class="badge badge-warning" title="Could not be verified against Shopify — may be a draft">unverified</span>' : ""}
                    <span class="il-group-count">${g.sources.length} page${g.sources.length !== 1 ? "s" : ""}</span>
                    <button class="btn btn-primary btn-sm" data-il-fix>${kind === "broken" ? "Fix…" : "Canonicalize…"}</button>
                </div>
                <div class="il-group-sources" style="display:none;">
                    ${g.sources.map((s) => `
                        <div class="il-source-row">
                            <span class="badge badge-info">${escapeHtml(s.from_type)}</span>
                            <span>${escapeHtml(s.from_title)}</span>
                            ${s.anchors.length ? `<span class="il-source-anchor">“${escapeHtml(s.anchors[0])}”${s.anchors.length > 1 ? ` +${s.anchors.length - 1}` : ""}</span>` : ""}
                        </div>`).join("")}
                </div>
            </div>`).join("");

        const card = (title, sub, groups, kind, actions) => `
            <div class="card" style="margin-bottom:16px;">
                <div class="card-header" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
                    <h3 style="margin:0;">${title} <span class="il-chip-count">${groups.length}</span></h3>
                    <span style="flex:1;font-size:13px;color:var(--on-surface-secondary);">${sub}</span>
                    ${actions}
                </div>
                ${groups.length
                    ? groupRows(groups, kind)
                    : `<div class="empty-state" style="padding:24px;"><span class="material-symbols-rounded">check_circle</span><p>None found. 🎉</p></div>`}
            </div>`;

        panel.innerHTML = `
            ${unverifiedNote}
            ${card(
                "Broken links",
                "Links pointing at products, collections, or articles that no longer exist.",
                brokenGroups,
                "broken",
                brokenGroups.length ? `
                    <button class="btn btn-primary btn-sm" data-il-session="broken">
                        <span class="material-symbols-rounded">play_arrow</span> Start fix session
                    </button>
                    <button class="btn btn-danger btn-sm" data-il-remove-all>Remove all…</button>` : ""
            )}
            ${card(
                "Non-canonical links",
                "Links using /collections/…/products/… paths that split SEO signals — canonicalize to /products/….",
                noncanonGroups,
                "noncanonical",
                noncanonGroups.length ? `
                    <button class="btn btn-primary btn-sm" data-il-canon-all>Canonicalize all (${noncanonGroups.length})…</button>` : ""
            )}`;

        panel.querySelectorAll("[data-il-expand]").forEach((head) => {
            const toggle = (e) => {
                if (e.target.closest("button")) return;
                const wrap = head.closest(".il-group");
                const src = wrap.querySelector(".il-group-sources");
                const open = src.style.display !== "none";
                src.style.display = open ? "none" : "";
                wrap.classList.toggle("open", !open);
            };
            head.addEventListener("click", toggle);
            head.addEventListener("keydown", (e) => { if (e.key === "Enter") toggle(e); });
        });
        panel.querySelectorAll("[data-il-fix]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const wrap = btn.closest(".il-group");
                const kind = wrap.dataset.kind;
                const groups = kind === "broken" ? brokenGroups : noncanonGroups;
                openFixModal(kind, groups[parseInt(wrap.dataset.group, 10)], null);
            });
        });
        const sess = panel.querySelector("[data-il-session]");
        if (sess) sess.addEventListener("click", () => startFixSession("broken"));
        const rmAll = panel.querySelector("[data-il-remove-all]");
        if (rmAll) rmAll.addEventListener("click", confirmRemoveAllBroken);
        const canonAll = panel.querySelector("[data-il-canon-all]");
        if (canonAll) canonAll.addEventListener("click", confirmCanonicalizeAll);
    }

    /* ── fix modal ─────────────────────────────────────────────────── */
    function startFixSession(kind) {
        const groups = kind === "broken" ? brokenGroups : noncanonGroups;
        if (!groups.length) return;
        session = { kind, idx: 0 };
        openFixModal(kind, groups[0], session);
    }

    function onSessionKeys(e) {
        if (!session || !fixCtx || !fixCtx.modal) return;
        const tag = (e.target.tagName || "").toLowerCase();
        if (["input", "textarea", "select"].includes(tag)) return;
        if (e.key === "j" || e.key === "ArrowRight") sessionStep(1);
        else if (e.key === "k" || e.key === "ArrowLeft") sessionStep(-1);
    }

    function sessionStep(delta) {
        if (!session) return;
        const sess = session;
        const groups = sess.kind === "broken" ? brokenGroups : noncanonGroups;
        const next = sess.idx + delta;
        if (next < 0 || next >= groups.length) return;
        sess.idx = next;
        if (fixCtx && fixCtx.modal) fixCtx.modal.close();
        session = sess; // the close handler clears it; this is a transition, not an exit
        openFixModal(sess.kind, groups[next], sess);
    }

    function openFixModal(kind, group, sess) {
        const isBroken = kind === "broken";
        const canonicalHref = `/products/${group.to_handle}`;
        const sessionNav = sess ? `
            <span class="il-session-nav">
                <button class="icon-btn" data-il-prev title="Previous (K)"><span class="material-symbols-rounded">chevron_left</span></button>
                ${sess.idx + 1} of ${(isBroken ? brokenGroups : noncanonGroups).length}
                <button class="icon-btn" data-il-next title="Next (J)"><span class="material-symbols-rounded">chevron_right</span></button>
            </span>` : "";

        const bodyHtml = `
            <div class="il-fix-target">
                <span class="material-symbols-rounded" style="color:var(--${isBroken ? "error" : "warning"});">${isBroken ? "link_off" : "alt_route"}</span>
                <code>${escapeHtml(group.to_href || canonicalHref)}</code>
                <span class="badge badge-info">${escapeHtml(group.to_type)}</span>
                ${group.unverified ? '<span class="badge badge-warning">unverified</span>' : ""}
                ${sessionNav}
            </div>
            ${isBroken ? `
                <div class="il-fix-choice">
                    <label class="il-radio"><input type="radio" name="il-fix-mode" value="replace">
                        <span>Replace the link with…</span></label>
                    <div class="il-combobox" id="il-fix-combo" style="display:none;">
                        <input type="text" class="form-control" id="il-fix-input" placeholder="Search products, collections, articles…" autocomplete="off">
                        <div class="il-combo-drop" id="il-fix-drop" style="display:none;"></div>
                        <div class="il-combo-chosen" id="il-fix-chosen" style="display:none;"></div>
                    </div>
                    <div id="il-fix-suggest" style="display:none;"></div>
                    <label class="il-radio"><input type="radio" name="il-fix-mode" value="remove">
                        <span>Remove the link, keep the text</span></label>
                </div>` : `
                <div class="il-fix-choice">
                    <p style="margin:0;font-size:14px;">Rewrite to the canonical URL:</p>
                    <div class="il-canon-preview"><code>${escapeHtml(group.to_href)}</code>
                        <span class="material-symbols-rounded">arrow_forward</span>
                        <code>${escapeHtml(canonicalHref)}</code></div>
                </div>`}
            <h4 class="il-drawer-h" style="margin-top:16px;">Pages that will be edited (${group.sources.length})</h4>
            <div class="il-fix-sources" id="il-fix-sources">
                ${group.sources.map((s, i) => `
                    <div class="il-source-row" data-src="${i}">
                        <span class="badge badge-info">${escapeHtml(s.from_type)}</span>
                        <span style="flex:1;">${escapeHtml(s.from_title)}</span>
                        ${s.anchors.length ? `<span class="il-source-anchor">“${escapeHtml(s.anchors[0])}”</span>` : ""}
                        <span class="il-src-status" data-src-status="${i}"></span>
                    </div>`).join("")}
            </div>
            <div class="il-fix-progress" id="il-fix-progress" style="display:none;">
                <div class="progress-bar-container"><div class="progress-bar" id="il-fix-bar" style="width:0%;"></div></div>
                <span id="il-fix-progress-text"></span>
            </div>`;

        const modal = UIModal.open({
            title: isBroken ? "Fix broken link" : "Canonicalize link",
            bodyHtml,
            footerHtml: `
                <span id="il-fix-summary" style="flex:1;font-size:13px;color:var(--on-surface-secondary);"></span>
                <button class="btn btn-secondary" data-il-cancel>Cancel</button>
                <button class="btn btn-secondary" data-il-retry-errors style="display:none;">Retry errors</button>
                <button class="btn btn-primary" data-il-apply disabled>${isBroken ? "Apply fix" : "Canonicalize"}</button>`,
            size: "lg",
            onClose: () => {
                if (fixCtx && fixCtx.modal === modal) fixCtx = null;
                if (sess && session === sess && !document.querySelector(".modal-overlay.active")) session = null;
            },
        });

        fixCtx = {
            kind, group, modal,
            mode: isBroken ? null : "replace",
            replacement: isBroken ? null : { type: "product", handle: group.to_handle },
            running: false,
            lastResults: null,
        };

        const el = modal.el;
        el.querySelector("[data-il-cancel]").addEventListener("click", () => modal.close());
        const prev = el.querySelector("[data-il-prev]");
        const next = el.querySelector("[data-il-next]");
        if (prev) prev.addEventListener("click", () => sessionStep(-1));
        if (next) next.addEventListener("click", () => sessionStep(1));
        el.querySelector("[data-il-apply]").addEventListener("click", () => executeFix());
        el.querySelector("[data-il-retry-errors]").addEventListener("click", () => executeFix(true));

        if (isBroken) {
            el.querySelectorAll('input[name="il-fix-mode"]').forEach((radio) => {
                radio.addEventListener("change", () => {
                    fixCtx.mode = radio.value;
                    el.querySelector("#il-fix-combo").style.display = radio.value === "replace" ? "" : "none";
                    if (radio.value === "replace") el.querySelector("#il-fix-input").focus();
                    updateApplyState();
                });
            });
            setupCombobox(el);
            loadSuggestion(el, group);
        } else {
            updateApplyState();
        }
    }

    function updateApplyState() {
        if (!fixCtx || !fixCtx.modal) return;
        const btn = fixCtx.modal.el.querySelector("[data-il-apply]");
        const ok = fixCtx.completed || (!fixCtx.running && (
            fixCtx.mode === "remove" || (fixCtx.mode === "replace" && fixCtx.replacement)
        ));
        btn.disabled = !ok;
    }

    /* ── replacement combobox ──────────────────────────────────────── */
    async function loadTargets() {
        if (targetsCache) return targetsCache;
        try {
            targetsCache = await apiRequest(`/api/interlinking/available-targets?store_id=${storeId}`);
        } catch (e) {
            targetsCache = null;
            showToast("Could not load replacement targets", "error");
            return [];
        }
        return targetsCache;
    }

    async function loadSuggestion(el, group) {
        try {
            const resp = await apiRequest(
                `/api/interlinking/suggest-replacement?store_id=${storeId}&type=${encodeURIComponent(group.to_type)}&handle=${encodeURIComponent(group.to_handle)}`
            );
            const s = resp.suggestion;
            const box = el.querySelector("#il-fix-suggest");
            if (!s || !box || !fixCtx || fixCtx.modal.el !== el) return;
            box.style.display = "";
            box.innerHTML = `
                <button class="il-suggest-chip" type="button">
                    <span class="material-symbols-rounded">auto_awesome</span>
                    Suggested: <strong>${escapeHtml(s.title || s.handle)}</strong>
                    <span class="badge badge-info">${escapeHtml(s.type)}</span>
                </button>`;
            box.querySelector("button").addEventListener("click", () => {
                const replaceRadio = el.querySelector('input[name="il-fix-mode"][value="replace"]');
                replaceRadio.checked = true;
                replaceRadio.dispatchEvent(new Event("change"));
                chooseReplacement(el, s);
            });
        } catch (e) { /* suggestion is optional */ }
    }

    function setupCombobox(el) {
        const input = el.querySelector("#il-fix-input");
        const drop = el.querySelector("#il-fix-drop");
        let items = [];
        let selected = 0;

        const paint = () => {
            if (!items.length) {
                drop.innerHTML = `<div class="il-combo-empty">No matches in this store's catalog</div>`;
                return;
            }
            drop.innerHTML = items.map((t, i) => `
                <div class="il-combo-item ${i === selected ? "selected" : ""}" data-i="${i}">
                    <span class="badge badge-info">${escapeHtml(t.type)}</span>
                    <span style="flex:1;">${escapeHtml(t.title || t.handle)}</span>
                    <code style="font-size:11px;">${escapeHtml(t.handle)}</code>
                </div>`).join("");
            drop.querySelectorAll(".il-combo-item").forEach((row) => {
                row.addEventListener("mousedown", (e) => {
                    e.preventDefault();
                    chooseReplacement(el, items[parseInt(row.dataset.i, 10)]);
                });
            });
        };

        const refresh = async () => {
            const all = await loadTargets();
            const q = input.value.trim().toLowerCase();
            items = (q
                ? all.filter((t) => (t.title || "").toLowerCase().includes(q) || (t.handle || "").toLowerCase().includes(q))
                : all
            ).slice(0, 20);
            selected = 0;
            drop.style.display = "";
            paint();
        };

        let t = null;
        input.addEventListener("input", () => {
            // typing invalidates a previous pick — replace requires an explicit selection
            if (fixCtx) { fixCtx.replacement = null; updateApplyState(); }
            el.querySelector("#il-fix-chosen").style.display = "none";
            clearTimeout(t);
            t = setTimeout(refresh, 200);
        });
        input.addEventListener("focus", refresh);
        input.addEventListener("blur", () => setTimeout(() => { drop.style.display = "none"; }, 150));
        input.addEventListener("keydown", (e) => {
            if (drop.style.display === "none") return;
            if (e.key === "ArrowDown") { e.preventDefault(); selected = Math.min(selected + 1, items.length - 1); paint(); }
            else if (e.key === "ArrowUp") { e.preventDefault(); selected = Math.max(selected - 1, 0); paint(); }
            else if (e.key === "Enter") { e.preventDefault(); if (items[selected]) chooseReplacement(el, items[selected]); }
            else if (e.key === "Escape") { drop.style.display = "none"; }
        });
    }

    function chooseReplacement(el, target) {
        if (!fixCtx) return;
        fixCtx.replacement = { type: target.type, handle: target.handle, blog_handle: target.blog_handle };
        const chosen = el.querySelector("#il-fix-chosen");
        chosen.style.display = "";
        chosen.innerHTML = `
            <span class="material-symbols-rounded" style="color:var(--success);">check_circle</span>
            Will link to <strong>${escapeHtml(target.title || target.handle)}</strong>
            <span class="badge badge-info">${escapeHtml(target.type)}</span>`;
        el.querySelector("#il-fix-drop").style.display = "none";
        el.querySelector("#il-fix-input").value = target.title || target.handle;
        updateApplyState();
    }

    /* ── fix execution ─────────────────────────────────────────────── */
    function fixItemsFor(ctx, onlyErrorIdx) {
        const { group } = ctx;
        return group.sources
            .map((s, i) => ({ s, i }))
            .filter(({ i }) => !onlyErrorIdx || onlyErrorIdx.has(i))
            .map(({ s, i }) => ({
                _idx: i,
                to_type: group.to_type,
                to_handle: group.to_handle,
                to_href: group.to_href,
                from_type: s.from_type,
                from_handle: s.from_handle,
                from_blog_handle: s.from_blog_handle || undefined,
                ...(ctx.mode === "remove"
                    ? { mode: "remove" }
                    : {
                        mode: "replace",
                        replacement_type: ctx.replacement.type,
                        replacement_handle: ctx.replacement.handle,
                        replacement_blog_handle: ctx.replacement.blog_handle || undefined,
                    }),
            }));
    }

    async function executeFix(retryErrorsOnly) {
        const ctx = fixCtx;
        if (!ctx || ctx.running) return;
        if (ctx.completed && !retryErrorsOnly) {
            // "Done" after a successful run: close, or move to the next issue
            // when a fix session is active (the fixed group left the list, so
            // the next one now sits at the same index).
            const sess = session;
            ctx.modal.close();
            if (sess) {
                const groups = sess.kind === "broken" ? brokenGroups : noncanonGroups;
                if (groups.length) {
                    sess.idx = Math.min(sess.idx, groups.length - 1);
                    session = sess;
                    openFixModal(sess.kind, groups[sess.idx], sess);
                }
            }
            return;
        }
        const el = ctx.modal.el;
        const onlyErrors = retryErrorsOnly && ctx.lastResults
            ? new Set(ctx.lastResults.filter((r) => r.status === "error").map((r) => r._idx))
            : null;
        const items = fixItemsFor(ctx, onlyErrors);
        if (!items.length) return;

        ctx.running = true;
        updateApplyState();
        el.querySelector("[data-il-retry-errors]").style.display = "none";
        const bar = el.querySelector("#il-fix-bar");
        const progWrap = el.querySelector("#il-fix-progress");
        const progText = el.querySelector("#il-fix-progress-text");
        progWrap.style.display = "";

        const setSrcStatus = (idx, status, reason) => {
            const cell = el.querySelector(`[data-src-status="${idx}"]`);
            if (!cell) return;
            const map = {
                pending: ['<span class="spinner-sm"></span>', ""],
                applied: ['<span class="material-symbols-rounded" style="color:var(--success);">check_circle</span>', "Fixed"],
                fixed: ['<span class="material-symbols-rounded" style="color:var(--success);">check_circle</span>', "Fixed"],
                skipped: ['<span class="material-symbols-rounded" style="color:var(--on-surface-secondary);">remove_circle</span>', reason || "Skipped"],
                error: ['<span class="material-symbols-rounded" style="color:var(--error);">error</span>', reason || "Error"],
            };
            const [icon, label] = map[status] || ["", status];
            cell.innerHTML = `${icon}<span class="il-src-reason">${escapeHtml(label)}</span>`;
        };

        const results = [];
        const finish = () => {
            ctx.running = false;
            ctx.lastResults = results;
            const fixed = results.filter((r) => r.status === "fixed" || r.status === "applied").length;
            const skipped = results.filter((r) => r.status === "skipped").length;
            const errors = results.filter((r) => r.status === "error").length;
            progWrap.style.display = "none";
            el.querySelector("#il-fix-summary").innerHTML =
                `<strong>${fixed}</strong> fixed · ${skipped} skipped · ` +
                (errors ? `<span style="color:var(--error);"><strong>${errors}</strong> failed</span>` : "0 failed");
            if (errors) {
                el.querySelector("[data-il-retry-errors]").style.display = "";
                showToast(`${fixed} fixed, ${errors} failed — see details in the dialog`, "error");
            } else {
                showToast(`${fixed} link${fixed !== 1 ? "s" : ""} fixed`, "success");
                ctx.completed = true;
                const applyBtn = el.querySelector("[data-il-apply]");
                applyBtn.textContent = session ? "Next issue" : "Done";
                applyBtn.disabled = false;
                el.querySelector("[data-il-cancel]").textContent = "Close";
            }
            reloadAnalysis();
        };

        try {
            if (items.length <= 3) {
                let done = 0;
                for (const item of items) {
                    setSrcStatus(item._idx, "pending");
                    try {
                        const resp = await apiRequest("/api/interlinking/fix-broken-link", {
                            method: "POST",
                            body: {
                                store_id: storeId,
                                noncanonical: ctx.kind === "noncanonical",
                                ...item,
                            },
                        });
                        results.push({ _idx: item._idx, status: resp.status, reason: resp.reason });
                        setSrcStatus(item._idx, resp.status, resp.reason);
                    } catch (e) {
                        results.push({ _idx: item._idx, status: "error", reason: e.message });
                        setSrcStatus(item._idx, "error", e.message);
                    }
                    done += 1;
                    bar.style.width = `${Math.round((done / items.length) * 100)}%`;
                    progText.textContent = `${done} / ${items.length}`;
                }
                finish();
            } else {
                items.forEach((i) => setSrcStatus(i._idx, "pending"));
                progText.textContent = `0 / ${items.length} — running in background, also tracked in Activity`;
                const start = await apiRequest("/api/interlinking/apply-batch", {
                    method: "POST",
                    body: {
                        store_id: storeId,
                        kind: ctx.kind === "noncanonical" ? "noncanonical" : "broken",
                        items: items.map(({ _idx, ...rest }) => rest),
                    },
                });
                const task = await pollTaskUntilDone(`/api/interlinking/task/${start.task_id}?store_id=${storeId}`, {
                    intervalMs: 2000,
                    timeoutMs: batchTimeoutMs(items.length),
                    onProgress: (t) => {
                        const p = t.result && t.result.progress;
                        if (!p) return;
                        bar.style.width = `${Math.round((p.completed / Math.max(1, p.total)) * 100)}%`;
                        progText.textContent = `${p.completed} / ${p.total}`;
                    },
                });
                const taskResults = (task.result && task.result.results) || [];
                taskResults.forEach((r, i) => {
                    const idx = items[i] ? items[i]._idx : i;
                    results.push({ _idx: idx, status: r.status, reason: r.reason });
                    setSrcStatus(idx, r.status, r.reason);
                });
                finish();
            }
        } catch (e) {
            ctx.running = false;
            progWrap.style.display = "none";
            el.querySelector("#il-fix-summary").innerHTML =
                `<span style="color:var(--error);">${escapeHtml(e.message)}</span>`;
            el.querySelector("[data-il-retry-errors]").style.display = "";
            updateApplyState();
            reloadAnalysis();
        }
    }

    /* ── bulk actions on issues ────────────────────────────────────── */
    function confirmRemoveAllBroken() {
        const totalSources = brokenGroups.reduce((a, g) => a + g.sources.length, 0);
        const modal = UIModal.open({
            title: "Remove all broken links",
            bodyHtml: `
                <p>This removes <strong>${brokenGroups.length}</strong> broken link target${brokenGroups.length !== 1 ? "s" : ""}
                from <strong>${totalSources}</strong> live page${totalSources !== 1 ? "s" : ""} on Shopify.
                The anchor text is kept; only the links are stripped.</p>
                <p style="font-size:13px;color:var(--on-surface-secondary);">To replace targets individually instead,
                use <em>Start fix session</em>.</p>
                <div class="il-fix-sources" style="max-height:220px;">
                    ${brokenGroups.map((g) => `
                        <div class="il-source-row">
                            <code style="flex:1;">${escapeHtml(g.to_href || g.to_handle)}</code>
                            <span>${g.sources.length} page${g.sources.length !== 1 ? "s" : ""}</span>
                        </div>`).join("")}
                </div>`,
            footerHtml: `
                <button class="btn btn-secondary" data-c>Cancel</button>
                <button class="btn btn-danger" data-go>Remove all links</button>`,
            size: "lg",
        });
        modal.el.querySelector("[data-c]").addEventListener("click", () => modal.close());
        modal.el.querySelector("[data-go]").addEventListener("click", async () => {
            const items = brokenGroups.flatMap((g) => g.sources.map((s) => ({
                to_type: g.to_type, to_handle: g.to_handle, to_href: g.to_href,
                from_type: s.from_type, from_handle: s.from_handle,
                from_blog_handle: s.from_blog_handle || undefined, mode: "remove",
            })));
            modal.close();
            await runIssueBatch("broken", items, "Removing broken links");
        });
    }

    function confirmCanonicalizeAll() {
        const items = noncanonGroups.flatMap((g) => g.sources.map((s) => ({
            to_type: g.to_type, to_handle: g.to_handle, to_href: g.to_href,
            from_type: s.from_type, from_handle: s.from_handle,
            from_blog_handle: s.from_blog_handle || undefined,
            mode: "replace", replacement_type: "product", replacement_handle: g.to_handle,
        })));
        const modal = UIModal.open({
            title: "Canonicalize all links",
            bodyHtml: `<p>Rewrite <strong>${items.length}</strong> non-canonical link${items.length !== 1 ? "s" : ""}
                across <strong>${noncanonGroups.length}</strong> target${noncanonGroups.length !== 1 ? "s" : ""} to their
                canonical <code>/products/…</code> URLs on the live store.</p>`,
            footerHtml: `
                <button class="btn btn-secondary" data-c>Cancel</button>
                <button class="btn btn-primary" data-go>Canonicalize ${items.length} links</button>`,
        });
        modal.el.querySelector("[data-c]").addEventListener("click", () => modal.close());
        modal.el.querySelector("[data-go]").addEventListener("click", async () => {
            modal.close();
            await runIssueBatch("noncanonical", items, "Canonicalizing links");
        });
    }

    // Each item is an AI call + a Shopify write (~6-10s); give the poll 20s
    // per item plus slack so big batches don't hit the default 15-min limit.
    function batchTimeoutMs(count) {
        return Math.max(15 * 60, count * 20 + 300) * 1000;
    }

    function isStillRunningError(e) {
        return /timed out|lost contact/i.test(e.message || "");
    }

    async function runIssueBatch(kind, items, label) {
        setStatusChip(`${label}… 0/${items.length}`, true);
        try {
            const start = await apiRequest("/api/interlinking/apply-batch", {
                method: "POST",
                body: { store_id: storeId, kind, items },
            });
            showToast(`${label} in the background — track progress in Activity`, "info");
            const task = await pollTaskUntilDone(`/api/interlinking/task/${start.task_id}?store_id=${storeId}`, {
                intervalMs: 2500,
                timeoutMs: batchTimeoutMs(items.length),
                onProgress: (t) => {
                    const p = t.result && t.result.progress;
                    if (p) setStatusChip(`${label}… ${p.completed}/${p.total}`, true);
                },
            });
            setStatusChip(null);
            const rs = (task.result && task.result.results) || [];
            const fixed = rs.filter((r) => r.status === "fixed" || r.status === "applied").length;
            const errors = rs.filter((r) => r.status === "error").length;
            showToast(
                errors ? `${fixed} fixed, ${errors} failed — open Activity for details` : `${fixed} link${fixed !== 1 ? "s" : ""} fixed`,
                errors ? "error" : "success"
            );
            await reloadAnalysis();
            return rs;
        } catch (e) {
            setStatusChip(null);
            if (isStillRunningError(e)) {
                showToast(`${label} is still running in the background — track progress in Activity`, "info");
            } else {
                showToast(`${label} failed: ${e.message}`, "error");
            }
            await reloadAnalysis();
            return null;
        }
    }

    /* ── opportunities tab ─────────────────────────────────────────── */
    function renderOpportunities() {
        const panel = $("#il-tab-opportunities");
        if (!analysis) { panel.innerHTML = emptyAnalysisState(); return; }

        const pairs = analysis.missing_crosslinks || [];
        const batchSize = loadFilter("interlinkBatchSize", "10");

        panel.innerHTML = `
            <div class="card" style="margin-bottom:16px;">
                <div class="card-header" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
                    <h3 style="margin:0;">Missing cross-links <span class="il-chip-count">${analysis.missing_crosslinks_truncated || pairs.length}</span></h3>
                    <span style="flex:1;font-size:13px;color:var(--on-surface-secondary);">
                        Collections sharing products that don't link to each other. Applies directly — no AI credits needed.
                        ${analysis.missing_crosslinks_truncated ? `<br><strong>Showing the ${pairs.length} strongest of ${analysis.missing_crosslinks_truncated} pairs</strong> — re-analyze after applying to surface more.` : ""}
                    </span>
                    ${pairs.length ? `<button class="btn btn-primary btn-sm" data-il-apply-all-pairs>Apply all…</button>` : ""}
                </div>
                <div id="il-pairs-table"></div>
            </div>

            <div class="card">
                <div class="card-header" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
                    <h3 style="margin:0;">AI link recommendations</h3>
                    <span style="flex:1;font-size:13px;color:var(--on-surface-secondary);" id="il-recs-meta">
                        ${recs && recs.completed_at ? `Generated ${formatRelativeTime(recs.completed_at)}` : "Context-aware link suggestions for orphaned and under-linked pages."}
                    </span>
                    <select id="il-batch-size" class="form-control" style="width:auto;padding:6px 10px;font-size:13px;">
                        ${[10, 20, 30, 50, 100, 200].map((v) => `<option value="${v}" ${String(v) === String(batchSize) ? "selected" : ""}>${v} suggestions</option>`).join("")}
                    </select>
                    <button class="btn btn-primary btn-sm" id="il-generate-recs">
                        <span class="material-symbols-rounded">auto_awesome</span> ${recs ? "Regenerate" : "Generate"}
                    </button>
                </div>
                <div id="il-recs-body"></div>
            </div>`;

        renderPairsTable();
        renderRecsBody();

        const allBtn = panel.querySelector("[data-il-apply-all-pairs]");
        if (allBtn) allBtn.addEventListener("click", confirmApplyAllPairs);
        panel.querySelector("#il-batch-size").addEventListener("change", (e) => {
            saveFilter("interlinkBatchSize", e.target.value);
        });
        panel.querySelector("#il-generate-recs").addEventListener("click", (e) => generateRecs(e.currentTarget));
    }

    function pairFixes(p) {
        const fixes = [];
        if (!p.a_links_to_b) {
            fixes.push({
                from_type: "collection", from_handle: p.collection_a_handle,
                to_type: "collection", to_handle: p.collection_b_handle,
                suggested_anchor: p.collection_b_title || p.collection_b_handle,
            });
        }
        if (!p.b_links_to_a) {
            fixes.push({
                from_type: "collection", from_handle: p.collection_b_handle,
                to_type: "collection", to_handle: p.collection_a_handle,
                suggested_anchor: p.collection_a_title || p.collection_a_handle,
            });
        }
        return fixes;
    }

    function renderPairsTable() {
        const container = $("#il-pairs-table");
        if (!container) return;
        createTable(container, {
            rows: () => analysis.missing_crosslinks || [],
            searchKeys: ["collection_a_title", "collection_b_title", "collection_a_handle", "collection_b_handle"],
            searchPlaceholder: "Search collections…",
            defaultSort: "relatedness",
            defaultDir: "desc",
            emptyIcon: "check_circle",
            emptyText: "All related collections cross-link to each other.",
            columns: [
                { key: "collection_a_title", label: "Collection A", sortable: true, render: (r) => `
                    <div>${escapeHtml(r.collection_a_title || r.collection_a_handle || "")}</div>
                    <div style="font-size:12px;color:var(--on-surface-secondary);">${escapeHtml(r.collection_a_handle || "")}</div>` },
                { key: "collection_b_title", label: "Collection B", sortable: true, render: (r) => `
                    <div>${escapeHtml(r.collection_b_title || r.collection_b_handle || "")}</div>
                    <div style="font-size:12px;color:var(--on-surface-secondary);">${escapeHtml(r.collection_b_handle || "")}</div>` },
                { key: "relatedness", label: "Relatedness", sortable: true, render: (r) =>
                    r.relatedness !== undefined ? `<strong>${Math.round(num(r.relatedness) * 100)}%</strong>` : "—" },
                { key: "shared_products", label: "Shared products", sortable: true, render: (r) => String(num(r.shared_products)) },
                { key: "_status", label: "Link status", sortable: false, render: (r) => `
                    <span class="badge ${r.a_links_to_b ? "badge-success" : "badge-warning"}">A→B ${r.a_links_to_b ? "✓" : "✗"}</span>
                    <span class="badge ${r.b_links_to_a ? "badge-success" : "badge-warning"}">B→A ${r.b_links_to_a ? "✓" : "✗"}</span>` },
                { key: "_apply", label: "", sortable: false, render: () => `
                    <button class="btn btn-primary btn-sm" data-il-pair-apply>Apply</button>` },
            ],
            onRender: (body, slice) => {
                body.querySelectorAll("[data-il-pair-apply]").forEach((btn, i) => {
                    btn.addEventListener("click", () => applyPair(slice[i], btn));
                });
            },
        });
    }

    async function applyPair(pair, btn) {
        const fixes = pairFixes(pair);
        if (!fixes.length) { showToast("Both directions already linked", "info"); return; }
        await guardButton(btn, async () => {
            try {
                const start = await apiRequest("/api/interlinking/apply-batch", {
                    method: "POST",
                    body: { store_id: storeId, kind: "ai_links", items: fixes },
                });
                btn.textContent = "Applying…";
                const task = await pollTaskUntilDone(`/api/interlinking/task/${start.task_id}?store_id=${storeId}`, { intervalMs: 2000 });
                const rs = (task.result && task.result.results) || [];
                const applied = rs.filter((r) => r.status === "applied").length;
                const errors = rs.filter((r) => r.status === "error");
                if (errors.length) {
                    showToast(`Cross-link failed: ${errors[0].reason || "unknown error"}`, "error");
                } else {
                    showToast(`Cross-links applied (${applied} added)`, "success");
                }
                await reloadAnalysis();
            } catch (e) {
                showToast(`Cross-link failed: ${e.message}`, "error");
            }
        });
    }

    function confirmApplyAllPairs() {
        const pairs = analysis.missing_crosslinks || [];
        const fixes = pairs.flatMap(pairFixes);
        if (!fixes.length) { showToast("Nothing to apply", "info"); return; }
        const modal = UIModal.open({
            title: "Apply all cross-links",
            bodyHtml: `<p>Add <strong>${fixes.length}</strong> link${fixes.length !== 1 ? "s" : ""} across
                <strong>${pairs.length}</strong> collection pair${pairs.length !== 1 ? "s" : ""}.
                Each link is woven into the collection description by AI and written to the live store.</p>`,
            footerHtml: `
                <button class="btn btn-secondary" data-c>Cancel</button>
                <button class="btn btn-primary" data-go>Apply ${fixes.length} links</button>`,
        });
        modal.el.querySelector("[data-c]").addEventListener("click", () => modal.close());
        modal.el.querySelector("[data-go]").addEventListener("click", async () => {
            modal.close();
            await runIssueBatch("ai_links", fixes, "Applying cross-links");
        });
    }

    /* ── AI recommendations ────────────────────────────────────────── */
    function recKey(f) { return `${f.from_type}:${f.from_handle}→${f.to_type}:${f.to_handle}`; }

    function renderRecsBody() {
        const body = $("#il-recs-body");
        if (!body) return;
        if (!recs || !recs.result) {
            body.innerHTML = `
                <div class="empty-state" style="padding:28px;">
                    <span class="material-symbols-rounded">auto_awesome</span>
                    <p>Generate AI recommendations to get prioritized link suggestions with anchor text.</p>
                </div>`;
            return;
        }
        const result = recs.result;
        const fixes = result.priority_fixes || [];
        const crosslinks = result.crosslink_suggestions || [];
        const pending = fixes.filter((f) => !appliedRecKeys.has(recKey(f)));

        body.innerHTML = `
            ${result.summary ? `<div class="ai-field"><div class="ai-field-label">Summary</div>
                <div class="ai-field-value">${escapeHtml(result.summary)}</div></div>` : ""}
            ${fixes.length ? `
                <div class="ai-field">
                    <div class="ai-field-label" style="display:flex;align-items:center;gap:12px;">
                        Priority fixes (${fixes.length})
                        ${pending.length ? `<button class="btn btn-primary btn-sm" id="il-apply-all-fixes">Apply all (${pending.length})</button>` : ""}
                    </div>
                    ${fixes.map((f, i) => {
                        const applied = appliedRecKeys.has(recKey(f));
                        return `
                        <div class="recommendation-card" style="display:flex;align-items:flex-start;gap:12px;${applied ? "opacity:.6;" : ""}">
                            <div style="flex:1;">
                                <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap;">
                                    <span class="badge badge-info">${escapeHtml(f.action || "add_link")}</span>
                                    <strong>${escapeHtml(f.from_title || f.from_handle || "")} → ${escapeHtml(f.to_title || f.to_handle || "")}</strong>
                                </div>
                                ${f.suggested_anchor ? `<div style="font-size:13px;color:var(--on-surface-secondary);">Anchor: “<em>${escapeHtml(f.suggested_anchor)}</em>”</div>` : ""}
                                ${f.reason ? `<div style="font-size:13px;color:var(--on-surface-secondary);margin-top:4px;">${escapeHtml(f.reason)}</div>` : ""}
                            </div>
                            <button class="btn ${applied ? "btn-secondary" : "btn-primary"} btn-sm" data-il-rec="${i}" ${applied ? "disabled" : ""}>
                                ${applied ? "Applied ✓" : "Apply"}
                            </button>
                        </div>`;
                    }).join("")}
                </div>` : ""}
            ${crosslinks.length ? `
                <div class="ai-field">
                    <div class="ai-field-label">Cross-link suggestions (${crosslinks.length})</div>
                    ${crosslinks.map((c, i) => {
                        const hasHandles = c.collection_a_handle && c.collection_b_handle;
                        const key = hasHandles ? `xl:${c.collection_a_handle}↔${c.collection_b_handle}` : null;
                        const applied = key && appliedRecKeys.has(key);
                        return `
                        <div class="recommendation-card" style="display:flex;align-items:flex-start;gap:12px;${applied ? "opacity:.6;" : ""}">
                            <div style="flex:1;">
                                <strong>${escapeHtml(c.collection_a || "")} ↔ ${escapeHtml(c.collection_b || "")}</strong>
                                ${c.shared_products ? `<span style="font-size:13px;margin-left:8px;">(${c.shared_products} shared products)</span>` : ""}
                                ${c.reason ? `<div style="font-size:13px;color:var(--on-surface-secondary);margin-top:4px;">${escapeHtml(c.reason)}</div>` : ""}
                            </div>
                            ${hasHandles ? `<button class="btn ${applied ? "btn-secondary" : "btn-primary"} btn-sm" data-il-rec-xl="${i}" ${applied ? "disabled" : ""}>
                                ${applied ? "Applied ✓" : "Apply"}</button>` : ""}
                        </div>`;
                    }).join("")}
                </div>` : ""}
            ${!fixes.length && !crosslinks.length ? `<div class="empty-state" style="padding:24px;"><span class="material-symbols-rounded">check_circle</span><p>No suggestions — the link graph looks healthy.</p></div>` : ""}`;

        body.querySelectorAll("[data-il-rec]").forEach((btn) => {
            btn.addEventListener("click", () => applyRecFix(fixes[parseInt(btn.dataset.ilRec, 10)], btn));
        });
        body.querySelectorAll("[data-il-rec-xl]").forEach((btn) => {
            btn.addEventListener("click", () => applyRecCrosslink(crosslinks[parseInt(btn.dataset.ilRecXl, 10)], btn));
        });
        const allBtn = body.querySelector("#il-apply-all-fixes");
        if (allBtn) allBtn.addEventListener("click", () => applyAllRecFixes(allBtn));
    }

    async function applyRecFix(fix, btn) {
        await guardButton(btn, async () => {
            try {
                const resp = await apiRequest("/api/interlinking/apply-fix", {
                    method: "POST",
                    body: { store_id: storeId, ...fix },
                });
                appliedRecKeys.add(recKey(fix));
                showToast(resp.status === "skipped" ? `Skipped: ${resp.reason || "link already exists"}` : "Link applied", resp.status === "skipped" ? "info" : "success");
                renderRecsBody();
                await reloadAnalysis();
            } catch (e) {
                showToast(`Apply failed: ${e.message}`, "error");
            }
        });
    }

    async function applyRecCrosslink(c, btn) {
        const fixes = [
            {
                from_type: "collection", from_handle: c.collection_a_handle,
                to_type: "collection", to_handle: c.collection_b_handle,
                suggested_anchor: c.collection_b || c.collection_b_handle,
            },
            {
                from_type: "collection", from_handle: c.collection_b_handle,
                to_type: "collection", to_handle: c.collection_a_handle,
                suggested_anchor: c.collection_a || c.collection_a_handle,
            },
        ];
        await guardButton(btn, async () => {
            try {
                const start = await apiRequest("/api/interlinking/apply-batch", {
                    method: "POST",
                    body: { store_id: storeId, kind: "ai_links", items: fixes },
                });
                btn.textContent = "Applying…";
                const task = await pollTaskUntilDone(`/api/interlinking/task/${start.task_id}?store_id=${storeId}`, { intervalMs: 2000 });
                const errs = ((task.result && task.result.results) || []).filter((r) => r.status === "error");
                if (errs.length) {
                    showToast(`Cross-link failed: ${errs[0].reason || "unknown error"}`, "error");
                } else {
                    appliedRecKeys.add(`xl:${c.collection_a_handle}↔${c.collection_b_handle}`);
                    showToast("Cross-links applied", "success");
                }
                renderRecsBody();
                await reloadAnalysis();
            } catch (e) {
                showToast(`Cross-link failed: ${e.message}`, "error");
            }
        });
    }

    async function applyAllRecFixes(btn) {
        const fixes = ((recs && recs.result && recs.result.priority_fixes) || [])
            .filter((f) => !appliedRecKeys.has(recKey(f)));
        if (!fixes.length) return;
        const modal = UIModal.open({
            title: "Apply all AI suggestions",
            bodyHtml: `<p>Apply <strong>${fixes.length}</strong> link suggestion${fixes.length !== 1 ? "s" : ""}?
                Each link is woven into the source page's content by AI and written to the live store.
                Progress is tracked here and in Activity.</p>`,
            footerHtml: `
                <button class="btn btn-secondary" data-c>Cancel</button>
                <button class="btn btn-primary" data-go>Apply ${fixes.length} links</button>`,
        });
        modal.el.querySelector("[data-c]").addEventListener("click", () => modal.close());
        modal.el.querySelector("[data-go]").addEventListener("click", async () => {
            modal.close();
            btn.disabled = true;
            try {
                const start = await apiRequest("/api/interlinking/apply-batch", {
                    method: "POST",
                    body: { store_id: storeId, kind: "ai_links", items: fixes },
                });
                showToast("Applying suggestions in the background — track in Activity", "info");
                const task = await pollTaskUntilDone(`/api/interlinking/task/${start.task_id}?store_id=${storeId}`, {
                    intervalMs: 2500,
                    timeoutMs: batchTimeoutMs(fixes.length),
                    onProgress: (t) => {
                        const p = t.result && t.result.progress;
                        if (p) btn.textContent = `Applying… ${p.completed}/${p.total}`;
                    },
                });
                const rs = (task.result && task.result.results) || [];
                rs.forEach((r) => {
                    if (r.status === "applied" || r.status === "skipped") {
                        const f = fixes.find((x) => x.from_handle === r.from_handle && x.to_handle === r.to_handle);
                        if (f) appliedRecKeys.add(recKey(f));
                    }
                });
                const applied = rs.filter((r) => r.status === "applied").length;
                const errors = rs.filter((r) => r.status === "error").length;
                showToast(errors ? `${applied} applied, ${errors} failed — open Activity for details` : `${applied} links applied`, errors ? "error" : "success");
                renderRecsBody();
                await reloadAnalysis();
            } catch (e) {
                if (isStillRunningError(e)) {
                    showToast("Still applying in the background — track progress in Activity", "info");
                } else {
                    showToast(`Batch apply failed: ${e.message}`, "error");
                }
                renderRecsBody();
            }
        });
    }

    async function generateRecs(btn) {
        const batch = parseInt(loadFilter("interlinkBatchSize", "10"), 10) || 10;
        await guardButton(btn, async () => {
            try {
                const start = await apiRequest("/api/interlinking/recommendations", {
                    method: "POST",
                    body: {
                        store_id: storeId,
                        max_fixes: batch,
                        max_crosslinks: Math.max(5, Math.floor(batch / 2)),
                    },
                });
                showToast("Generating AI recommendations…", "info");
                const meta = $("#il-recs-meta");
                if (meta) meta.innerHTML = '<span class="spinner-sm"></span> Generating…';
                const task = await pollTaskUntilDone(`/api/interlinking/task/${start.task_id}?store_id=${storeId}`, {
                    intervalMs: 3000,
                    timeoutMs: 20 * 60 * 1000,
                });
                recs = { result: task.result, completed_at: new Date().toISOString() };
                appliedRecKeys.clear();
                if (activeTab === "opportunities") {
                    renderOpportunities();
                    showToast("AI recommendations ready", "success");
                } else {
                    renderOpportunities();
                    showToast("AI recommendations ready — see the Opportunities tab", "success");
                }
            } catch (e) {
                const meta = $("#il-recs-meta");
                if (meta) meta.textContent = "Generation failed.";
                showToast(`Recommendations failed: ${e.message}`, "error");
            }
        });
    }
})();
