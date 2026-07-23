/* SEO Issues tab + unified-detail helpers — the keyword-alignment audit
 * merged in from the retired Technical SEO page. Loaded after seo.js on /seo;
 * plain-script globals, same convention as the rest of the app. */

/* ── Check constants (migrated from technical_seo.js) ────────────────── */

const SEO_CHECK_LABELS = {
    title_has_keyword: "Title Contains Keyword",
    keyword_at_title_start: "Keyword Position in Title",
    meta_title_has_keyword: "Meta Title Contains Keyword",
    meta_title_length: "Meta Title Length",
    meta_desc_has_keyword: "Meta Description Contains Keyword",
    meta_desc_length: "Meta Description Length",
    url_has_keyword: "URL Contains Keyword",
    body_first_100_has_keyword: "Keyword in First 100 Words",
    keyword_density: "Keyword Stuffing Guard",
    description_word_count: "Description Word Count",
    heading_structure: "Heading Structure",
    image_alt_coverage: "Image Alt Coverage",
    image_alt_has_keyword: "Alt Text Keyword Balance",
};

// The four completeness checks the keyword analyzer shares with the content
// pillar are rendered once in the "Completeness" group, so the keyword groups
// list only the keyword-specific checks.
const SEO_CHECK_GROUPS = [
    { label: "Title & URL", icon: "title", keys: ["title_has_keyword", "keyword_at_title_start", "url_has_keyword"] },
    { label: "Meta Tags", icon: "sell", keys: ["meta_title_has_keyword", "meta_desc_has_keyword"] },
    { label: "Content", icon: "article", keys: ["body_first_100_has_keyword", "keyword_density", "heading_structure"] },
    { label: "Images", icon: "image", keys: ["image_alt_has_keyword"] },
];

/* ── Unified detail modal helpers ────────────────────────────────────── */

function renderPillarBars(item) {
    const rows = [
        ["Content quality", item.content_score],
        ["Keyword alignment", item.keyword_score],
        ["Search performance", item.search_score],
    ];
    return `<div class="pillar-bars">` + rows.map(([label, score]) => {
        if (score == null) {
            return `<div class="score-item"><span class="score-item-label">${label}</span>
                <div class="score-item-bar"></div>
                <span class="score-item-num" style="color:var(--on-surface-secondary);">—</span></div>`;
        }
        const cls = score >= 80 ? "high" : score >= 50 ? "medium" : "low";
        return `<div class="score-item"><span class="score-item-label">${label}</span>
            <div class="score-item-bar"><div class="score-item-bar-fill ${cls}" style="width:${score}%;"></div></div>
            <span class="score-item-num">${score}</span></div>`;
    }).join("") + `</div>`;
}

function renderUnifiedHero(item, itemType) {
    const ka = item.keyword_analysis;
    const issues = item.issues_count;
    return `<div class="tseo-modal-hero">
        ${renderScoreRing(item.seo_score || 0, 80, item)}
        ${renderPillarBars(item)}
        <div class="tseo-modal-stats">
            <div class="tseo-modal-stat">
                <span class="tseo-stat-label">Issues</span>
                <span class="tseo-stat-value"><span class="badge ${!issues ? "badge-success" : "badge-error"}">${issues != null ? issues : "—"}</span></span>
            </div>
            <div class="tseo-modal-stat">
                <span class="tseo-stat-label">AI-Fixable</span>
                <span class="tseo-stat-value"><span class="badge badge-fixable">${item.fixable_count != null ? item.fixable_count : "—"}</span></span>
            </div>
            <div class="tseo-modal-stat">
                <span class="tseo-stat-label">Keywords Analyzed</span>
                <span class="tseo-stat-value">${ka && ka.analyzed_at ? formatRelativeTime(ka.analyzed_at) : "Never"}</span>
            </div>
            <div class="tseo-modal-stat">
                <span class="tseo-stat-label">Last Optimized</span>
                <span class="tseo-stat-value">${item.last_optimized_at ? formatRelativeTime(item.last_optimized_at) : "Never"}</span>
            </div>
        </div>
        <div class="tseo-hero-actions">
            <button class="btn btn-secondary btn-sm" onclick="reanalyzeItemChecks('${itemType}', ${item.id})">
                <span class="material-symbols-rounded">refresh</span> Re-analyze
            </button>
        </div>
    </div>`;
}

function renderKeywordChips(detectedKeywords) {
    if (!detectedKeywords || detectedKeywords.length === 0) return "";
    let html = '<div class="preview-section">';
    html += '<div class="preview-section-title"><span class="material-symbols-rounded">key</span> Detected Keywords</div>';
    html += '<div class="preview-tags">';
    for (const kw of detectedKeywords) {
        if (kw.packaging_only) {
            html += `<span class="chip-display" style="border: 1px dashed var(--primary);"><strong>${escapeHtml(kw.keyword)}</strong> <span class="badge badge-info" style="font-size: 10px; margin-left: 4px;">pkg</span></span>`;
        } else {
            const conf = kw.confidence >= 15 ? "badge-success" : kw.confidence >= 8 ? "badge-warning" : "badge-info";
            html += `<span class="chip-display"><strong>${escapeHtml(kw.keyword)}</strong> <span class="badge ${conf}" style="font-size: 10px; margin-left: 4px;">${kw.confidence}</span></span>`;
        }
    }
    html += '</div></div>';
    return html;
}

/* One visual paradigm for all checks: the content pillar renders as a
 * "Completeness" group, keyword checks keep their 4 groups, search
 * performance closes the list. Groups with problems start expanded. */
function renderCheckGroups(item) {
    const groups = [];

    if (item.seo_breakdown) {
        const checks = Object.entries(item.seo_breakdown).map(([k, v]) => ({
            key: k,
            label: k.replace(/_/g, " "),
            score: v.score,
            detail: v.detail,
            issue: null,
        }));
        groups.push({ label: "Completeness", icon: "fact_check", checks, expand: checks.some(c => c.score < 100) });
    }

    const ka = item.keyword_analysis;
    if (ka && ka.checks) {
        for (const g of SEO_CHECK_GROUPS) {
            const checks = g.keys
                .filter(k => ka.checks[k])
                .map(k => ({ key: k, label: SEO_CHECK_LABELS[k] || k.replace(/_/g, " "), ...ka.checks[k] }));
            if (checks.length === 0) continue;
            groups.push({ label: g.label, icon: g.icon, checks, expand: checks.some(c => c.issue) });
        }
    }

    if (item.search_breakdown) {
        const checks = Object.entries(item.search_breakdown).map(([k, v]) => ({
            key: k,
            label: k.replace(/_/g, " "),
            score: v.score,
            detail: v.detail,
            issue: null,
        }));
        groups.push({ label: "Search Performance", icon: "monitoring", checks, expand: checks.some(c => c.score < 50) });
    }

    if (groups.length === 0) return "";

    let html = '<div class="preview-section">';
    html += '<div class="preview-section-title"><span class="material-symbols-rounded">checklist</span> Check-by-Check Breakdown</div>';
    if (!ka) {
        html += `<div class="analyze-cta-banner" style="margin-bottom: 10px;">
            <span class="material-symbols-rounded">info</span>
            <span>Keyword alignment hasn't been analyzed for this item yet.</span>
            <button class="btn btn-secondary btn-sm" onclick="reanalyzeItemChecks('${item.products_count !== undefined ? "collection" : "product"}', ${item.id})">Analyze now</button>
        </div>`;
    }

    groups.forEach((group, gIdx) => {
        const groupAvg = Math.round(group.checks.reduce((s, c) => s + c.score, 0) / group.checks.length);
        const avgCls = groupAvg >= 80 ? "color: var(--success);" : groupAvg >= 50 ? "color: var(--warning);" : "color: var(--error);";
        html += `<div class="tseo-check-group${group.expand ? "" : " collapsed"}" id="seo-check-group-${gIdx}">
            <div class="tseo-check-group-header" onclick="toggleSeoCheckGroup(${gIdx})">
                <span class="material-symbols-rounded">${group.icon}</span>
                <span class="tseo-group-label">${group.label}</span>
                <span class="tseo-group-score" style="${avgCls}">${groupAvg}</span>
                <span class="material-symbols-rounded tseo-chevron">expand_more</span>
            </div>
            <div class="tseo-check-group-body">`;
        for (const check of group.checks) {
            const checkFill = check.score >= 80 ? "high" : check.score >= 50 ? "medium" : "low";
            html += `<div class="tseo-check-item">
                <div class="score-row">
                    <span class="score-label">${escapeHtml(check.label)}</span>
                    <div class="score-bar"><div class="score-bar-fill ${checkFill}" style="width: ${check.score}%;"></div></div>
                    <span class="score-value">${check.score}</span>
                </div>
                <div style="padding: 2px 0 4px 0; font-size: 12px; color: var(--on-surface-secondary);">${escapeHtml(check.detail || "")}</div>
                ${check.issue ? `<div style="padding: 0 0 4px 0;"><span class="badge badge-warning" style="font-size: 11px;">${escapeHtml(check.issue)}</span></div>` : ""}
            </div>`;
        }
        html += `</div></div>`;
    });
    html += '</div>';
    return html;
}

function toggleSeoCheckGroup(idx) {
    const el = document.getElementById(`seo-check-group-${idx}`);
    if (el) el.classList.toggle("collapsed");
}

async function reanalyzeItemChecks(itemType, itemId) {
    try {
        showToast("Analyzing keyword alignment...", "info");
        await apiRequest("/api/technical-seo/analyze", {
            method: "POST",
            body: { item_type: itemType, item_id: itemId },
        });
        showToast("Analysis updated", "success");
        if (itemType === "product") viewProduct(itemId);
        else viewCollection(itemId);
    } catch (err) {
        showToast("Analysis failed: " + err.message, "error");
    }
}

/* Issues-count badge for the Products/Collections tables. A dash means the
 * keyword pillar hasn't been analyzed yet (content-only count would mislead). */
function renderIssuesBadge(item) {
    const n = item.issues_count;
    if (n == null) return '<span class="badge badge-not-analyzed">—</span>';
    if (item.keyword_analyzed_at == null) {
        return `<span class="badge ${n ? "badge-error" : "badge-not-analyzed"}" title="Content checks only — keyword alignment not analyzed yet">${n}</span>`;
    }
    return n === 0
        ? '<span class="badge badge-success">0</span>'
        : `<span class="badge badge-error">${n}</span>`;
}

/* ── Issues tab ──────────────────────────────────────────────────────── */

let issuesData = null;
let issuesSeverityFilter = loadFilter("issuesSeverity", "");
let issuesTypeFilter = loadFilter("issuesType", "");
const expandedIssueKeys = new Set();
const issueItemsCache = new Map();

const ISSUE_SEVERITY_LABELS = { high: "High", med: "Medium", low: "Low" };

async function loadIssues() {
    const container = document.getElementById("issues-container");
    const storeId = await _ensureStoreId();
    if (!storeId) {
        container.innerHTML = '<div class="empty-state"><span class="material-symbols-rounded">store</span><p>No stores found. Add a store first.</p></div>';
        return;
    }

    UISkeleton.show(container, { type: "cards", rows: 6 });
    try {
        issuesData = await apiRequest(`/api/seo/issues?store_id=${storeId}`);
        issueItemsCache.clear();
        renderIssuesTab();
        // Deep link: /seo?tab=issues&issue=<key> auto-expands that row once.
        const issueParam = new URLSearchParams(location.search).get("issue");
        if (issueParam && issuesData.issues.some(i => i.key === issueParam) && !expandedIssueKeys.has(issueParam)) {
            history.replaceState(null, "", "/seo?tab=issues");
            toggleIssueRow(issueParam);
        }
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><span class="material-symbols-rounded">error</span><p>${escapeHtml(err.message)}</p></div>`;
    }
}

function _visibleIssues() {
    let issues = (issuesData && issuesData.issues) || [];
    if (issuesSeverityFilter) issues = issues.filter(i => i.severity === issuesSeverityFilter);
    if (issuesTypeFilter) issues = issues.filter(i => (i.by_type[issuesTypeFilter] || 0) > 0);
    return issues;
}

function renderIssuesTab() {
    const container = document.getElementById("issues-container");
    if (!issuesData) return;

    const all = issuesData.issues || [];
    const cov = issuesData.coverage || {};
    const covTotal = (cov.products?.total || 0) + (cov.collections?.total || 0);
    const covAnalyzed = (cov.products?.keyword_analyzed || 0) + (cov.collections?.keyword_analyzed || 0);
    const covPct = covTotal ? Math.round((covAnalyzed / covTotal) * 100) : 0;

    const totalIssues = all.reduce((s, i) => s + i.count, 0);
    const highCount = all.filter(i => i.severity === "high").reduce((s, i) => s + i.count, 0);
    const fixableCount = all.filter(i => i.fixable).reduce((s, i) => s + i.count, 0);

    let html = `<div class="stats-grid kpi-strip">
        <div class="stat-card">
            <div class="stat-value">${totalIssues.toLocaleString()}</div>
            <div class="stat-label">Total Issues</div>
            <div class="stat-sub">${all.length} issue type${all.length !== 1 ? "s" : ""}</div>
        </div>
        <div class="stat-card">
            <div class="stat-value" style="color:${highCount ? "var(--error)" : "var(--success)"};">${highCount.toLocaleString()}</div>
            <div class="stat-label">High Severity</div>
            <div class="stat-sub">${all.filter(i => i.severity === "high").length} type${all.filter(i => i.severity === "high").length !== 1 ? "s" : ""}</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${fixableCount.toLocaleString()}</div>
            <div class="stat-label">Fixable with AI</div>
            <div class="stat-sub">bulk fix per issue type</div>
        </div>
        <div class="stat-card">
            <div class="stat-value" style="color:${covPct >= 90 ? "var(--success)" : "var(--warning)"};">${covPct}%</div>
            <div class="stat-label">Keyword Coverage</div>
            <div class="stat-sub">${covAnalyzed.toLocaleString()} of ${covTotal.toLocaleString()} analyzed ·
                <a href="#" data-issues-reanalyze-all style="color: var(--primary);">Re-analyze all</a></div>
        </div>
    </div>`;

    if (covTotal && covAnalyzed / covTotal < 0.5) {
        html += `<div class="analyze-cta-banner">
            <span class="material-symbols-rounded">travel_explore</span>
            <span>Keyword alignment hasn't been analyzed for <strong>${(covTotal - covAnalyzed).toLocaleString()}</strong> of ${covTotal.toLocaleString()} items — keyword issues below are incomplete.</span>
            <button class="btn btn-primary btn-sm" data-issues-analyze-all>Analyze all</button>
        </div>`;
    }

    html += `<div class="toolbar-row" style="margin-top: 4px;">
        <div class="status-filter-bar" style="margin-bottom: 0;">
            <button class="filter-chip ${issuesSeverityFilter === "" ? "active" : ""}" data-sev-filter="">All</button>
            <button class="filter-chip ${issuesSeverityFilter === "high" ? "active" : ""}" data-sev-filter="high">High</button>
            <button class="filter-chip ${issuesSeverityFilter === "med" ? "active" : ""}" data-sev-filter="med">Medium</button>
            <button class="filter-chip ${issuesSeverityFilter === "low" ? "active" : ""}" data-sev-filter="low">Low</button>
        </div>
        <div class="status-filter-bar" style="margin-left: auto; margin-bottom: 0;">
            <button class="filter-chip ${issuesTypeFilter === "" ? "active" : ""}" data-type-filter="">All Items</button>
            <button class="filter-chip ${issuesTypeFilter === "product" ? "active" : ""}" data-type-filter="product">Products</button>
            <button class="filter-chip ${issuesTypeFilter === "collection" ? "active" : ""}" data-type-filter="collection">Collections</button>
        </div>
    </div>`;

    const visible = _visibleIssues();
    if (all.length === 0) {
        html += `<div class="empty-state">
            <span class="material-symbols-rounded" style="color: var(--success); opacity: 1;">verified</span>
            <p>No issues found — your store looks healthy.</p>
        </div>`;
    } else if (visible.length === 0) {
        html += `<div class="empty-state">
            <span class="material-symbols-rounded">filter_list_off</span>
            <p>No issues match the current filters.</p>
        </div>`;
    } else {
        html += visible.map(renderIssueRow).join("");
    }

    container.innerHTML = html;

    // Re-render any previously expanded rows from cache
    for (const key of expandedIssueKeys) {
        const body = container.querySelector(`.issue-row[data-issue-key="${key}"] .issue-row-body`);
        if (body && issueItemsCache.has(key)) body.innerHTML = renderIssueItems(issueItemsCache.get(key), key);
    }
}

function renderIssueRow(issue) {
    const expanded = expandedIssueKeys.has(issue.key);
    const byType = Object.entries(issue.by_type)
        .map(([t, n]) => `${n} ${t}${n !== 1 ? "s" : ""}`)
        .join(" · ");
    return `<div class="issue-row${expanded ? " expanded" : ""}" data-issue-key="${issue.key}">
        <div class="issue-row-header" data-issue-toggle="${issue.key}">
            <span class="issue-severity-dot sev-${issue.severity}" title="${ISSUE_SEVERITY_LABELS[issue.severity] || issue.severity}"></span>
            <span class="issue-row-label">${escapeHtml(issue.label)}</span>
            <span class="issue-row-count">${byType}</span>
            <span class="issue-pillar-tag">${issue.source === "keyword" ? "Keywords" : "Content"}</span>
            ${issue.fixable
                ? `<button class="btn btn-primary btn-sm" data-issue-fix="${issue.key}"><span class="material-symbols-rounded">auto_fix_high</span> Fix all with AI</button>`
                : issue.action_href
                    ? `<a class="btn btn-secondary btn-sm" href="${escapeAttr(issue.action_href)}" data-issue-action><span class="material-symbols-rounded">arrow_outward</span> ${escapeHtml(issue.action_label || "Open")}</a>`
                    : `<span class="badge badge-not-analyzed" title="Informational — no safe automatic fix">info only</span>`}
            <span class="material-symbols-rounded issue-chevron">expand_more</span>
        </div>
        <div class="issue-row-body">${expanded ? '<div class="loading-spinner" style="padding:20px 0;"><div class="spinner"></div></div>' : ""}</div>
    </div>`;
}

function renderIssueItems(items, issueKey) {
    if (!items || items.length === 0) {
        return '<p style="padding: 10px 14px; font-size: 13px; color: var(--on-surface-secondary);">No items.</p>';
    }
    const issue = (issuesData?.issues || []).find(i => i.key === issueKey);
    const actionLink = issue && issue.action_href
        ? `<a class="btn btn-secondary btn-sm" href="${escapeAttr(issue.action_href)}" data-issue-action>${escapeHtml(issue.action_label || "Open")}</a>`
        : "";
    return items.map(it => `<div class="issue-item-row">
        ${it.keyword_score != null ? renderScoreRing(it.keyword_score, 32) : '<span class="badge badge-not-analyzed">—</span>'}
        <div class="issue-item-main">
            <div class="issue-item-title">${escapeHtml(it.title)}</div>
            <div class="issue-item-detail">${escapeHtml(it.detail || "")}</div>
        </div>
        <span class="badge badge-info" style="font-size: 10px;">${it.item_type}</span>
        ${actionLink}
        <button class="btn btn-secondary btn-sm" data-view-item="${it.item_type}:${it.id}">View</button>
    </div>`).join("");
}

async function toggleIssueRow(key) {
    const row = document.querySelector(`.issue-row[data-issue-key="${key}"]`);
    if (!row) return;
    const body = row.querySelector(".issue-row-body");

    if (expandedIssueKeys.has(key)) {
        expandedIssueKeys.delete(key);
        row.classList.remove("expanded");
        body.innerHTML = "";
        return;
    }

    expandedIssueKeys.add(key);
    row.classList.add("expanded");

    if (issueItemsCache.has(key)) {
        body.innerHTML = renderIssueItems(issueItemsCache.get(key), key);
        return;
    }

    body.innerHTML = '<div class="loading-spinner" style="padding:20px 0;"><div class="spinner"></div></div>';
    try {
        const storeId = await _ensureStoreId();
        let url = `/api/seo/issues/${key}/items?store_id=${storeId}&limit=100`;
        if (issuesTypeFilter) url += `&item_type=${issuesTypeFilter}`;
        const data = await apiRequest(url);
        issueItemsCache.set(key, data.items);
        if (expandedIssueKeys.has(key)) body.innerHTML = renderIssueItems(data.items, key);
    } catch (err) {
        body.innerHTML = `<p style="padding: 10px 14px; color: var(--error); font-size: 13px;">${escapeHtml(err.message)}</p>`;
    }
}

/* Bulk progress for issue fixes / analyze-all. Own poller (not seo.js's
 * pollBulkTask) so completion can be awaited and chained per item type. */
function pollIssuesBulk(bulkTaskId, total, subtitleText) {
    const overlay = document.getElementById("bulk-progress-overlay");
    const bar = document.getElementById("bulk-progress-bar");
    const stats = document.getElementById("bulk-progress-stats");
    const current = document.getElementById("bulk-progress-current");
    const subtitle = document.getElementById("bulk-progress-subtitle");

    bar.style.width = "0%";
    stats.textContent = `0 / ${total} completed`;
    current.textContent = "";
    subtitle.textContent = subtitleText;
    overlay.classList.add("active");

    return pollTaskUntilDone(`/api/tasks/bulk/${bulkTaskId}`, {
        timeoutMs: Infinity,
        onProgress: (t) => {
            const done = t.completed + t.failed;
            const pct = total > 0 ? Math.round((done / total) * 100) : 0;
            bar.style.width = `${pct}%`;
            stats.textContent = `${done} / ${total} completed` + (t.failed > 0 ? ` (${t.failed} failed)` : "");
            subtitle.textContent = subtitleText;
            if (t.current_product_title) current.textContent = `Currently: ${t.current_product_title}`;
        },
    }).finally(() => {
        overlay.classList.remove("active");
    });
}

async function fixIssueType(key) {
    const issue = (issuesData?.issues || []).find(i => i.key === key);
    if (!issue || !issue.fixable) return;

    const types = ["product", "collection"].filter(t => (issue.by_type[t] || 0) > 0 && (!issuesTypeFilter || issuesTypeFilter === t));
    if (types.length === 0) return;

    const summary = types.map(t => `${issue.by_type[t]} ${t}${issue.by_type[t] !== 1 ? "s" : ""}`).join(" and ");
    document.getElementById("confirm-dialog-title").textContent = `Fix: ${issue.label}`;
    document.getElementById("confirm-dialog-message").innerHTML =
        `This will use AI to fix <strong>${escapeHtml(issue.label.toLowerCase())}</strong> on ${summary} and apply the changes to Shopify.`;
    const okBtn = document.getElementById("confirm-dialog-ok");
    okBtn.textContent = "Fix all with AI";
    okBtn.onclick = async () => {
        closeConfirmDialog();
        const storeId = await _ensureStoreId();
        let failedRuns = 0;
        for (const itemType of types) {
            try {
                const resp = await apiRequest(`/api/seo/issues/${key}/fix`, {
                    method: "POST",
                    body: { store_id: parseInt(storeId), item_type: itemType, limit: 500 },
                });
                const task = await pollIssuesBulk(resp.bulk_task_id, resp.total, `Fixing ${issue.label.toLowerCase()} (${itemType}s)...`);
                showToast(`${issue.label} — ${itemType}s: ${task.completed} fixed${task.failed ? `, ${task.failed} failed` : ""}`, task.failed ? "warning" : "success");
            } catch (err) {
                failedRuns++;
                showToast(`Fix failed for ${itemType}s: ${err.message}`, "error");
            }
        }
        expandedIssueKeys.delete(key);
        loadIssues();
        // Keep the item tables in sync with the applied changes
        if (types.includes("product")) loadProducts();
        if (types.includes("collection")) loadCollections();
    };
    _openConfirmDialog();
}

async function analyzeAllForIssues(force = false) {
    const cov = issuesData?.coverage || {};
    const total = (cov.products?.total || 0) + (cov.collections?.total || 0);
    const missing = ((cov.products?.total || 0) - (cov.products?.keyword_analyzed || 0))
        + ((cov.collections?.total || 0) - (cov.collections?.keyword_analyzed || 0));

    document.getElementById("confirm-dialog-title").textContent = force ? "Re-analyze All Items" : "Analyze Keyword Alignment";
    document.getElementById("confirm-dialog-message").innerHTML = force
        ? `This will re-run the keyword-alignment analysis on <strong>ALL ${total.toLocaleString()}</strong> items (useful after scoring rules change). It runs locally (no AI cost) and doesn't change anything on Shopify.`
        : `This will run the keyword-alignment analysis on <strong>${missing.toLocaleString()}</strong> unanalyzed item${missing !== 1 ? "s" : ""}. It runs locally (no AI cost) and doesn't change anything on Shopify.`;
    const okBtn = document.getElementById("confirm-dialog-ok");
    okBtn.textContent = force ? "Re-analyze all" : "Analyze all";
    okBtn.onclick = async () => {
        closeConfirmDialog();
        try {
            const storeId = await _ensureStoreId();
            const resp = await apiRequest("/api/seo/issues/analyze", {
                method: "POST",
                body: { store_id: parseInt(storeId), force },
            });
            for (const t of resp.tasks || []) {
                await pollIssuesBulk(t.bulk_task_id, t.total, `Analyzing ${t.item_type}s...`);
            }
            showToast("Keyword analysis complete", "success");
            loadIssues();
        } catch (err) {
            showToast(`Analysis failed: ${err.message}`, "error");
        }
    };
    _openConfirmDialog();
}

function initIssuesEvents() {
    const container = document.getElementById("issues-container");
    if (!container) return;

    container.addEventListener("click", (e) => {
        if (e.target.closest("a[data-issue-action]")) return; // navigate, don't toggle
        const fixBtn = e.target.closest("[data-issue-fix]");
        if (fixBtn) {
            e.stopPropagation();
            fixIssueType(fixBtn.dataset.issueFix);
            return;
        }
        const viewBtn = e.target.closest("[data-view-item]");
        if (viewBtn) {
            const [type, id] = viewBtn.dataset.viewItem.split(":");
            if (type === "product") viewProduct(parseInt(id));
            else viewCollection(parseInt(id));
            return;
        }
        const analyzeBtn = e.target.closest("[data-issues-analyze-all]");
        if (analyzeBtn) {
            analyzeAllForIssues();
            return;
        }
        const reanalyzeBtn = e.target.closest("[data-issues-reanalyze-all]");
        if (reanalyzeBtn) {
            e.preventDefault();
            analyzeAllForIssues(true);
            return;
        }
        const sevChip = e.target.closest("[data-sev-filter]");
        if (sevChip) {
            issuesSeverityFilter = sevChip.dataset.sevFilter;
            saveFilter("issuesSeverity", issuesSeverityFilter);
            renderIssuesTab();
            return;
        }
        const typeChip = e.target.closest("[data-type-filter]");
        if (typeChip) {
            issuesTypeFilter = typeChip.dataset.typeFilter;
            saveFilter("issuesType", issuesTypeFilter);
            issueItemsCache.clear();
            renderIssuesTab();
            for (const key of [...expandedIssueKeys]) {
                expandedIssueKeys.delete(key);
                toggleIssueRow(key);
            }
            return;
        }
        const toggle = e.target.closest("[data-issue-toggle]");
        if (toggle) toggleIssueRow(toggle.dataset.issueToggle);
    });
}

document.addEventListener("DOMContentLoaded", initIssuesEvents);

document.addEventListener("store:changed", () => {
    issuesData = null;
    issueItemsCache.clear();
    expandedIssueKeys.clear();
    const panel = document.getElementById("tab-issues");
    if (panel && panel.classList.contains("active")) loadIssues();
});
