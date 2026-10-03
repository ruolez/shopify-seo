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

// What a "Fix all with AI" run actually writes. An issue is narrow ("no
// subheadings") but the fix rewrites the whole field it lives on, so every
// surface that offers the fix says which field changes.
const FIX_FIELD_LABELS = {
    title: "title",
    meta_title: "meta title",
    meta_description: "meta description",
    description: "description",
    image_alt: "image alt text",
    tags: "tags",
};

function fixFieldsLabel(fields) {
    const names = (fields || []).map(f => FIX_FIELD_LABELS[f] || f.replace(/_/g, " "));
    if (names.length === 0) return "";
    if (names.length === 1) return names[0];
    return names.slice(0, -1).join(", ") + " and " + names[names.length - 1];
}

/* ── Unified detail modal helpers ────────────────────────────────────── */

function renderPillarBars(item) {
    // The ring is the on-page score: the first two bars. Search performance
    // is the result — it moves when Google re-measures, not when you edit.
    const rows = [
        ["Content quality", item.content_score, "Part of the on-page score"],
        ["Keyword alignment", item.keyword_score, "Part of the on-page score"],
        ["Search result", item.search_score, "How the page performs in Google. Not part of the on-page score — it changes as Google re-measures, usually weeks after an edit."],
    ];
    return `<div class="pillar-bars" title="The ring shows the on-page score: content quality and keyword alignment.">` + rows.map(([label, score, hint]) => {
        label = `<span title="${hint}">${label}</span>`;
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
            <div class="tseo-modal-stat" ${issues ? `style="cursor: pointer;" title="View issues" onclick="switchModalTab('checks')"` : ""}>
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
    // Two shapes share this field: resolver candidates (have sources[];
    // confidence is the 0-100 resolution score) and raw on-page n-grams
    // (confidence is a small extraction weight, stored only when keyword
    // resolution hasn't run or failed). One badge scale misread both.
    const isResolved = detectedKeywords.some(kw => Array.isArray(kw.sources));
    let html = '<div class="preview-section">';
    html += `<div class="preview-section-title"><span class="material-symbols-rounded">key</span> ${
        isResolved ? "Resolved Keywords" : "On-page Keywords"
    }${isResolved ? "" : ' <span class="badge badge-warning" style="font-size:10px;">not demand-checked — re-analyze</span>'}</div>`;
    html += '<div class="preview-tags">';
    for (const kw of detectedKeywords) {
        if (kw.packaging_only) {
            html += `<span class="chip-display" style="border: 1px dashed var(--primary);"><strong>${escapeHtml(kw.keyword)}</strong> <span class="badge badge-info" style="font-size: 10px; margin-left: 4px;">pkg</span></span>`;
        } else if (isResolved) {
            const score = Math.round(kw.confidence || 0);
            const cls = score >= 60 ? "badge-success" : score >= 30 ? "badge-warning" : "badge-info";
            const srcs = Array.isArray(kw.sources) ? escapeHtml(kw.sources.join(", ")) : "";
            html += `<span class="chip-display" title="${srcs}"><strong>${escapeHtml(kw.keyword)}</strong> <span class="badge ${cls}" style="font-size: 10px; margin-left: 4px;">${score}</span></span>`;
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

/* ── Per-item issues panel + targeted AI fixes (Checks tab) ──────────── */

/* Flat "what's wrong with this item" list. When keyword analysis exists its
 * checks carry every issue (the four completeness checks share thresholds
 * with the content pillar); before analysis we derive from seo_breakdown. */
function collectItemIssues(item) {
    const ka = item.keyword_analysis;
    if (ka && ka.checks) {
        return Object.entries(ka.checks)
            .filter(([, c]) => c.issue)
            .map(([key, c]) => ({
                key,
                label: SEO_CHECK_LABELS[key] || key.replace(/_/g, " "),
                issue: c.issue,
                fixable: !!c.fix_type,
            }));
    }
    return Object.entries(item.seo_breakdown || {})
        .filter(([, v]) => v.score < 100)
        .map(([key, v]) => ({
            key,
            label: key.replace(/_/g, " "),
            issue: v.detail,
            fixable: true,
        }));
}

/* The server's issue_scanner list is authoritative (same scan as the Issues
 * tab, including duplicate/schema categories). The local derivation renders
 * instantly as a placeholder and is swapped out when the fetch returns. */
function scheduleItemIssuesRefresh(item, itemType) {
    const storeId = item.store_id || (typeof _getSeoStoreId === "function" ? _getSeoStoreId() : "");
    if (!storeId || !item.id) return;
    setTimeout(async () => {
        try {
            const resp = await apiRequest(`/api/seo/issues/item/${itemType}/${item.id}?store_id=${storeId}`);
            const panel = document.querySelector(".item-issues-panel");
            if (!panel || !resp || !Array.isArray(resp.issues)) return;
            // Don't clobber an in-flight or rendered AI-fix flow.
            const fixes = document.getElementById("issue-fixes-container");
            if (fixes && fixes.innerHTML.trim() && !(item.keyword_analysis && item.keyword_analysis.ai_fixes)) return;
            const issues = resp.issues.map((i) => ({
                key: i.key,
                label: i.label,
                issue: i.detail,
                fixable: i.fixable,
                severity: i.severity,
            }));
            panel.outerHTML = renderItemIssuesPanelHtml(issues, item, itemType);
        } catch (e) {
            /* keep the locally derived fallback */
        }
    }, 0);
}

function renderItemIssuesPanel(item, itemType) {
    scheduleItemIssuesRefresh(item, itemType);
    return renderItemIssuesPanelHtml(collectItemIssues(item), item, itemType);
}

function renderItemIssuesPanelHtml(issues, item, itemType) {
    const ka = item.keyword_analysis;
    const fixableCount = issues.filter(i => i.fixable).length;

    let html = '<div class="item-issues-panel">';
    if (issues.length === 0) {
        html += `<div class="item-issues-clear">
            <span class="material-symbols-rounded">verified</span>
            No issues found — this ${itemType} looks healthy.
        </div>`;
        // Targeted fixes (e.g. CTR meta rewrites from the dashboard queue)
        // still need somewhere to render even when every check passes.
        html += '<div id="issue-fixes-container"></div>';
        html += '</div>';
        return html;
    }

    html += `<div class="item-issues-head">
        <span class="item-issues-title"><span class="material-symbols-rounded">report</span> Issues (${issues.length})</span>
        ${ka && fixableCount > 0 ? `<button class="btn btn-primary btn-sm" id="gen-fixes-btn" onclick="generateItemFixes('${itemType}', ${item.id})">
            <span class="material-symbols-rounded">auto_fix_high</span> Fix ${fixableCount} Issue${fixableCount !== 1 ? "s" : ""} with AI
        </button>` : ""}
    </div>`;

    html += '<ul class="item-issues-list">';
    for (const iss of issues) {
        const sev = iss.severity ? `sev-${iss.severity}` : (iss.fixable ? "sev-med" : "sev-low");
        html += `<li class="item-issue">
            <span class="issue-severity-dot ${sev}"></span>
            <span class="item-issue-text"><strong>${escapeHtml(iss.label)}:</strong> ${escapeHtml(iss.issue)}</span>
            ${iss.fixable ? "" : '<span class="badge badge-not-analyzed" title="Informational — no automatic fix">info</span>'}
        </li>`;
    }
    html += '</ul>';

    html += '<div id="issue-fixes-container">';
    if (ka && ka.ai_fixes && ka.ai_fixes.fixes && ka.ai_fixes.fixes.length) {
        html += renderFixProposals(ka.ai_fixes, itemType, item.id, true);
    }
    html += '</div>';

    html += '</div>';
    return html;
}

// Set when a fix flow was launched from a dashboard queue item — its
// action_key gets auto-snoozed after a successful apply (GSC lags ~28 days).
let _pendingFixActionKey = null;

async function generateItemFixes(itemType, itemId, opts) {
    const btn = document.getElementById("gen-fixes-btn");
    const container = document.getElementById("issue-fixes-container");
    if (!container) return;
    _pendingFixActionKey = (opts && opts.actionKey) || null;
    const bannerHtml = opts && opts.banner
        ? `<div style="font-size:12.5px;margin-bottom:10px;padding:8px 12px;background:var(--surface-dim);border-left:3px solid var(--primary);border-radius:6px;">${escapeHtml(opts.banner)}</div>`
        : "";
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="material-symbols-rounded spin">sync</span> Generating fixes...';
    }
    container.innerHTML = bannerHtml + '<div class="loading-spinner" style="padding: 24px 0;"><div class="spinner"></div><p>AI is drafting targeted fixes for the failing checks...</p></div>';

    try {
        const body = { item_type: itemType, item_id: itemId };
        if (opts && opts.onlyFields) body.only_fields = opts.onlyFields;
        if (opts && opts.fixContext) body.fix_context = opts.fixContext;
        const resp = await apiRequest("/api/technical-seo/fix", {
            method: "POST",
            body,
        });
        const task = await pollTaskUntilDone(`/api/tasks/${resp.task_id}`);
        if (task.status === "failed" || !task.result || !task.result.fixes) {
            container.innerHTML = bannerHtml + `<p style="color: var(--error); font-size: 13px;">Fix generation failed: ${escapeHtml(task.error || "no fixes returned")}</p>`;
        } else {
            container.innerHTML = bannerHtml + renderFixProposals(task.result, itemType, itemId, false);
        }
    } catch (err) {
        container.innerHTML = bannerHtml + `<p style="color: var(--error); font-size: 13px;">Fix generation failed: ${escapeHtml(err.message)}</p>`;
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<span class="material-symbols-rounded">auto_fix_high</span> Regenerate Fixes';
        }
    }
}

function renderFixProposals(aiFixes, itemType, itemId, fromCache) {
    const fixes = aiFixes.fixes || [];
    if (!fixes.length) return "";
    let html = '<div class="fix-proposals">';
    html += `<div class="fix-proposals-head">
        <span><span class="material-symbols-rounded" style="color: var(--primary); vertical-align: -4px;">auto_awesome</span>
        AI Fix Proposals${fromCache ? ' <span class="badge badge-not-analyzed" title="From the last generation — regenerate for fresh proposals">saved</span>' : ""}</span>
    </div>`;
    if (aiFixes.summary) {
        html += `<p class="fix-proposals-summary">${escapeHtml(aiFixes.summary)}</p>`;
    }

    fixes.forEach((fix, i) => {
        const field = fix.field || "";
        html += `<div class="fix-proposal">
            <label class="fix-proposal-head">
                <input type="checkbox" class="fix-select" data-fix-idx="${i}" data-field="${escapeAttr(field)}" checked>
                <span class="fix-field-name">${escapeHtml(field.replace(/_/g, " "))}</span>
                ${fix.reason ? `<span class="fix-reason">${escapeHtml(fix.reason)}</span>` : ""}
            </label>`;

        if (field === "image_alt" && Array.isArray(fix.proposed_value)) {
            html += '<div class="fix-proposal-body">';
            fix.proposed_value.forEach((alt, j) => {
                // Older generations returned bare strings (no media_id) — the
                // backend pairs those positionally with the images missing alt.
                const altText = typeof alt === "string" ? alt : (alt.alt || alt.proposed_alt || "");
                const mediaId = typeof alt === "string" ? "" : (alt.media_id || "");
                html += `<div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                    <input type="text" class="ai-field-editable fix-value-alt" data-fix-idx="${i}" data-alt-idx="${j}"
                        data-media-id="${escapeAttr(mediaId)}" value="${escapeAttr(altText)}" style="flex: 1;">
                    <span style="font-size: 11px; color: var(--on-surface-secondary); white-space: nowrap;">Image ${j + 1}</span>
                </div>`;
            });
            html += '</div>';
        } else if (field === "description") {
            html += `<div class="fix-proposal-body">
                <textarea class="ai-field-editable fix-value" data-fix-idx="${i}" rows="6">${escapeHtml(String(fix.proposed_value || ""))}</textarea>
            </div>`;
        } else {
            const val = Array.isArray(fix.proposed_value) ? fix.proposed_value.join(", ") : String(fix.proposed_value || "");
            html += `<div class="fix-proposal-body">
                ${fix.current_value ? `<div class="fix-current">Current: ${escapeHtml(String(fix.current_value).slice(0, 160))}</div>` : ""}
                <input type="text" class="ai-field-editable fix-value" data-fix-idx="${i}" value="${escapeAttr(val)}">
            </div>`;
        }
        html += '</div>';
    });

    html += `<div style="display: flex; justify-content: flex-end; margin-top: 12px;">
        <button class="btn btn-primary" id="apply-fixes-btn" onclick="applySelectedItemFixes('${itemType}', ${itemId})">
            <span class="material-symbols-rounded">publish</span> Apply Selected Fixes
        </button>
    </div>`;
    html += '</div>';
    return html;
}

async function applySelectedItemFixes(itemType, itemId) {
    const container = document.getElementById("issue-fixes-container");
    if (!container) return;

    const fixes = [];
    container.querySelectorAll(".fix-select:checked").forEach(cb => {
        const idx = cb.dataset.fixIdx;
        const proposal = cb.closest(".fix-proposal");
        const field = cb.dataset.field;
        const altInputs = proposal.querySelectorAll(`.fix-value-alt[data-fix-idx="${idx}"]`);
        if (altInputs.length) {
            const alts = [...altInputs]
                .filter(inp => inp.value.trim())
                .map(inp => ({ media_id: inp.dataset.mediaId || "", alt: inp.value.trim() }));
            if (alts.length) fixes.push({ field: "image_alt", value: alts });
            return;
        }
        const input = proposal.querySelector(`.fix-value[data-fix-idx="${idx}"]`);
        const value = input ? input.value.trim() : "";
        if (value) fixes.push({ field, value });
    });

    if (!fixes.length) {
        showToast("No fixes selected", "warning");
        return;
    }

    const btn = document.getElementById("apply-fixes-btn");
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="material-symbols-rounded spin">sync</span> Applying...';
    }

    try {
        const resp = await apiRequest("/api/technical-seo/apply", {
            method: "POST",
            // from_ai_fixes: the server rejects the apply if these proposals
            // were already consumed or invalidated (stale tab protection).
            body: { item_type: itemType, item_id: itemId, fixes, from_ai_fixes: true },
        });
        if (resp.ok) {
            showToast(`Applied: ${(resp.updated_fields || []).join(", ")}${resp.new_score != null ? ` — new score ${resp.new_score}` : ""}`, "success");
            if (_pendingFixActionKey) {
                const ak = _pendingFixActionKey;
                _pendingFixActionKey = null;
                try {
                    const sid = (typeof currentItemStoreId !== "undefined" && currentItemStoreId) || _getSeoStoreId();
                    await apiRequest(`/api/dashboard/action-queue/dismiss?store_id=${sid}`, {
                        method: "POST",
                        body: { action_key: ak, snooze_days: 28 },
                    });
                    showToast("Snoozed in the queue for 28 days while Google re-measures", "success");
                } catch {}
            }
            if (itemType === "product") {
                viewProduct(itemId, { tab: "checks" });
                loadProducts();
            } else {
                viewCollection(itemId, { tab: "checks" });
                loadCollections();
            }
            refreshQueueAfterApply();
        } else {
            showToast(`Apply failed: ${resp.error || "Unknown error"}`, "error");
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '<span class="material-symbols-rounded">publish</span> Apply Selected Fixes';
            }
        }
    } catch (err) {
        showToast(`Apply failed: ${err.message}`, "error");
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<span class="material-symbols-rounded">publish</span> Apply Selected Fixes';
        }
    }
}

async function reanalyzeItemChecks(itemType, itemId, returnTab = "checks") {
    try {
        showToast("Analyzing keywords & checks...", "info");
        await apiRequest("/api/technical-seo/analyze", {
            method: "POST",
            body: { item_type: itemType, item_id: itemId },
        });
        showToast("Analysis updated", "success");
        if (itemType === "product") viewProduct(itemId, { tab: returnTab });
        else viewCollection(itemId, { tab: returnTab });
    } catch (err) {
        showToast("Analysis failed: " + err.message, "error");
    }
}

/* ── Keywords tab: full resolution view + set-primary ────────────────── */

const KW_SOURCE_META = {
    page: { icon: "description", label: "On-page" },
    gsc: { icon: "monitoring", label: "Search Console" },
    semrush: { icon: "query_stats", label: "Semrush" },
    semrush_semantic: { icon: "query_stats", label: "Semrush (semantic match)" },
    strategy: { icon: "hub", label: "Keyword Strategy" },
};

function _kwSourceChips(sources) {
    return (sources || []).map(s => {
        const m = KW_SOURCE_META[s] || { icon: "help", label: s };
        return `<span class="material-symbols-rounded kw-src-icon" title="${escapeAttr(m.label)}">${m.icon}</span>`;
    }).join("");
}

function _fmtNum(v) {
    return v != null ? Math.round(v).toLocaleString() : '<span class="traffic-none">—</span>';
}

function renderKeywordsPanel(item, itemType) {
    const resolution = item.keyword_analysis && item.keyword_analysis.keyword_resolution;
    if (!resolution || !resolution.candidates || !resolution.candidates.length) {
        return `<div class="analyze-cta-banner">
            <span class="material-symbols-rounded">travel_explore</span>
            <span>Keywords haven't been resolved for this ${itemType} yet — run the analysis to cross-reference on-page phrases with Search Console and Semrush demand data.</span>
            <button class="btn btn-primary btn-sm" onclick="reanalyzeItemChecks('${itemType}', ${item.id}, 'keywords')">Resolve keywords</button>
        </div>`;
    }

    const primary = resolution.primary || {};
    let html = "";

    // Primary hero
    html += `<div class="kw-primary-hero">
        <div class="kw-primary-main">
            <div class="kw-primary-label">Primary Keyword${primary.manual ? ' <span class="badge badge-info">manual</span>' : ""}</div>
            <div class="kw-primary-kw">${escapeHtml(primary.keyword || "—")}</div>
            <div class="kw-primary-meta">
                ${_kwSourceChips(primary.sources)}
                ${primary.volume != null ? `<span>vol ${primary.volume.toLocaleString()}</span>` : ""}
                ${primary.gsc_position != null ? `<span>GSC pos ${primary.gsc_position}</span>` : ""}
                ${primary.semrush_position != null ? `<span>SR pos ${primary.semrush_position}</span>` : ""}
                ${primary.intent ? `<span class="badge badge-not-analyzed">${escapeHtml(primary.intent)}</span>` : ""}
            </div>
        </div>
        <div style="text-align: right;">
            ${primary.score != null ? renderScoreRing(Math.min(Math.round(primary.score), 100), 56) : ""}
            <div style="margin-top: 8px;">
                <button class="btn btn-secondary btn-sm" onclick="reanalyzeItemChecks('${itemType}', ${item.id}, 'keywords')">
                    <span class="material-symbols-rounded">refresh</span> Re-resolve
                </button>
            </div>
        </div>
    </div>`;

    if (resolution.note) {
        html += `<div class="analyze-cta-banner" style="margin-top: 10px;">
            <span class="material-symbols-rounded">lightbulb</span>
            <span>Your manual pick is kept, but the data suggests a ${escapeHtml(resolution.note)}</span>
        </div>`;
    }
    if (primary.cannibalization) {
        html += `<div class="analyze-cta-banner" style="margin-top: 10px;">
            <span class="material-symbols-rounded">warning</span>
            <span>Cannibalization: another page already ranks position ${primary.cannibalization.position} for this keyword — <span style="font-family: var(--font-mono); font-size: 12px;">${escapeHtml(primary.cannibalization.competing_url || "")}</span></span>
        </div>`;
    }

    // Candidates table
    html += `<div class="table-container" style="margin-top: 16px;"><table class="data-table">
        <thead><tr>
            <th>Keyword</th><th>Sources</th><th>Volume</th><th>Clicks</th><th>Impr.</th>
            <th>GSC Pos</th><th>SR Pos</th><th>Score</th><th></th><th style="width: 110px;"></th>
        </tr></thead><tbody>`;
    for (const c of resolution.candidates) {
        const isPrimary = primary.keyword && c.keyword === primary.keyword;
        html += `<tr${isPrimary ? ' style="background: var(--primary-light);"' : ""}>
            <td><strong>${escapeHtml(c.keyword)}</strong>${c.manual ? ' <span class="badge badge-info" style="font-size:10px;">manual</span>' : ""}${c.packaging_only ? ' <span class="badge badge-not-analyzed" style="font-size:10px;">pkg</span>' : ""}</td>
            <td><span class="kw-src-chips">${_kwSourceChips(c.sources)}</span></td>
            <td>${_fmtNum(c.volume)}</td>
            <td>${_fmtNum(c.gsc_clicks)}</td>
            <td>${_fmtNum(c.gsc_impressions)}</td>
            <td>${c.gsc_position != null ? renderPositionBadge(c.gsc_position) : '<span class="traffic-none">—</span>'}</td>
            <td>${c.semrush_position != null ? renderPositionBadge(c.semrush_position) : '<span class="traffic-none">—</span>'}</td>
            <td><strong>${c.score != null ? c.score : "—"}</strong></td>
            <td>${c.cannibalization ? `<span class="material-symbols-rounded" style="color: var(--warning); font-size: 18px;" title="Cannibalization: ${escapeAttr(c.cannibalization.competing_url || "")} ranks position ${c.cannibalization.position}">warning</span>` : ""}</td>
            <td>${isPrimary
                ? '<span class="badge badge-success">primary</span>'
                : `<button class="btn btn-secondary btn-sm" onclick="setPrimaryKeyword('${itemType}', ${item.id}, this.dataset.kw)" data-kw="${escapeAttr(c.keyword)}">Set primary</button>`}</td>
        </tr>`;
    }
    html += "</tbody></table></div>";

    html += `<p style="font-size: 12px; color: var(--on-surface-secondary); margin-top: 10px;">
        Score = on-page relevance × search demand (Semrush volume / GSC impressions) × ranking opportunity,
        halved when another page already owns the keyword. A manually set primary always wins.
    </p>`;

    return html;
}

async function setPrimaryKeyword(itemType, itemId, keyword) {
    document.getElementById("confirm-dialog-title").textContent = "Set Primary Keyword";
    document.getElementById("confirm-dialog-message").innerHTML =
        `Make <strong>"${escapeHtml(keyword)}"</strong> the primary keyword for this ${itemType}? All checks, scoring, and AI optimizations will target it until you change it.`;
    const okBtn = document.getElementById("confirm-dialog-ok");
    okBtn.textContent = "Set Primary";
    okBtn.onclick = async () => {
        closeConfirmDialog();
        try {
            showToast("Setting primary keyword...", "info");
            await apiRequest(`/api/seo/items/${itemType}/${itemId}/primary-keyword`, {
                method: "POST",
                body: { keyword },
            });
            showToast(`Primary keyword set to "${keyword}"`, "success");
            if (itemType === "product") viewProduct(itemId, { tab: "keywords" });
            else viewCollection(itemId, { tab: "keywords" });
        } catch (err) {
            showToast(`Failed: ${err.message}`, "error");
        }
    };
    _openConfirmDialog();
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

// Once per page load: silently re-analyze items whose stored verdict predates
// their own last content change. Local only — no AI cost, no Shopify write —
// so it runs without asking. A wrong verdict must not survive just because
// nobody happened to press the button.
let _outdatedSweepDone = false;

async function refreshOutdatedAnalyses(storeId) {
    if (_outdatedSweepDone) return;
    const cov = issuesData?.coverage || {};
    const outdated = (cov.products?.outdated || 0) + (cov.collections?.outdated || 0);
    if (!outdated) return;
    _outdatedSweepDone = true;

    const banner = document.createElement("div");
    banner.className = "analyze-cta-banner";
    banner.innerHTML = `<span class="material-symbols-rounded spin">sync</span>
        <span>Re-analyzing <strong>${outdated.toLocaleString()}</strong> item${outdated === 1 ? "" : "s"} whose
        content changed since they were last analyzed — the issues below may be out of date until this finishes.</span>`;
    document.getElementById("issues-container")?.prepend(banner);

    try {
        const resp = await apiRequest("/api/seo/issues/analyze", {
            method: "POST",
            body: { store_id: parseInt(storeId), scope: "outdated" },
        });
        for (const t of resp.tasks || []) {
            await pollTaskUntilDone(`/api/tasks/bulk/${t.bulk_task_id}`, { timeoutMs: Infinity });
        }
        banner.remove();
        loadIssues();
    } catch (err) {
        banner.innerHTML = `<span class="material-symbols-rounded">error</span>
            <span>Couldn't refresh outdated analyses: ${escapeHtml(err.message)}. The counts below may be stale —
            use <b>Re-analyze All</b>.</span>`;
    }
}

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
        refreshOutdatedAnalyses(storeId);
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

// Keyword issues are read from STORED analyses, so "analyzed" alone is not a
// useful number — a row written months ago is reported as current fact. This
// tile reports FRESHNESS instead of mere existence: the old one showed 100%
// while most of the catalog was still on a five-month-old verdict.
function renderFreshnessCard(cov, covTotal, covAnalyzed, covPct) {
    const outdated = (cov.products?.outdated || 0) + (cov.collections?.outdated || 0);
    const stale = (cov.products?.stale || 0) + (cov.collections?.stale || 0);
    const staleDays = cov.stale_days || 30;
    const unanalyzed = covTotal - covAnalyzed;
    const fresh = Math.max(0, covAnalyzed - outdated - stale);
    const freshPct = covTotal ? Math.round((fresh / covTotal) * 100) : 0;

    let color = "var(--success)";
    let sub;
    if (outdated) {
        color = "var(--error)";
        sub = `<strong>${outdated.toLocaleString()}</strong> changed since they were analyzed`
            + (stale ? ` · ${stale.toLocaleString()} over ${staleDays}d old` : "");
    } else if (stale || unanalyzed) {
        color = "var(--warning)";
        const bits = [];
        if (stale) bits.push(`<strong>${stale.toLocaleString()}</strong> older than ${staleDays} days`);
        if (unanalyzed) bits.push(`${unanalyzed.toLocaleString()} never analyzed`);
        sub = bits.join(" · ");
    } else {
        // A bare "100%" looks identical to the old always-100% coverage tile,
        // so show the age it is 100% *as of*.
        const oldest = cov.oldest_analyzed_at ? formatRelativeTime(cov.oldest_analyzed_at) : null;
        sub = `all ${covTotal.toLocaleString()} up to date`
            + (oldest ? ` · oldest checked ${oldest}` : "");
    }

    return `<div class="stat-card">
        <div class="stat-value" style="color:${color};">${freshPct}%</div>
        <div class="stat-label" title="Keyword issues are read from stored analyses. An analysis older than the page it describes reports issues that may no longer exist — or hides ones that do.">Analysis freshness</div>
        <div class="stat-sub">${sub} ·
            <a href="#" data-issues-reanalyze-all style="color: var(--primary);">Re-analyze all</a></div>
    </div>`;
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
            <div class="stat-sub" title="One page can hold several issues. The sidebar SEO badge counts pages, not issues — which is why the two numbers differ.">${all.length} issue type${all.length !== 1 ? "s" : ""}${
                issuesData.items_with_issues != null
                    ? ` · across <strong>${issuesData.items_with_issues.toLocaleString()}</strong> page${issuesData.items_with_issues === 1 ? "" : "s"}`
                    : ""}</div>
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
        ${renderFreshnessCard(cov, covTotal, covAnalyzed, covPct)}
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
        <button class="btn btn-secondary btn-sm" data-issues-reanalyze-all style="margin-left: 12px;"
            title="Re-resolve keywords and re-score every product & collection with the current rules (runs locally, no AI cost)">
            <span class="material-symbols-rounded">refresh</span> Re-analyze All
        </button>
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

    // Re-render previously expanded rows — from cache when fresh, otherwise
    // re-fetch (a quick-edit invalidates its issue's cache; without the fetch
    // the row would show its spinner forever).
    for (const key of expandedIssueKeys) {
        const body = container.querySelector(`.issue-row[data-issue-key="${key}"] .issue-row-body`);
        if (!body) continue;
        if (issueItemsCache.has(key)) {
            body.innerHTML = renderIssueItems(issueItemsCache.get(key), key);
        } else {
            fetchIssueItems(key, body);
        }
    }
}

async function fetchIssueItems(key, body) {
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
            ${issue.fixable && issue.fix_fields?.length
                ? `<span class="issue-write-tag" title="An AI fix for this issue rewrites the ${escapeAttr(fixFieldsLabel(issue.fix_fields))} — no other field is touched">
                       <span class="material-symbols-rounded">edit_note</span>${escapeHtml(fixFieldsLabel(issue.fix_fields))}</span>`
                : ""}
            ${issue.fixable
                ? `<button class="btn btn-primary btn-sm" data-issue-fix="${issue.key}"><span class="material-symbols-rounded">auto_fix_high</span> Fix all with AI</button>`
                : issue.bulk_fix
                ? `<button class="btn btn-primary btn-sm" data-issue-bulkfix="${issue.key}" title="Deterministic fix — no AI, no new content"><span class="material-symbols-rounded">bolt</span> ${escapeHtml(issue.bulk_fix.label)}</button>`
                : issue.action_href
                    ? `<a class="btn btn-secondary btn-sm" href="${escapeAttr(issue.action_href)}" data-issue-action><span class="material-symbols-rounded">arrow_outward</span> ${escapeHtml(issue.action_label || "Open")}</a>`
                    : `<span class="badge badge-not-analyzed" title="Informational — no safe automatic fix">info only</span>`}
            <span class="material-symbols-rounded issue-chevron">expand_more</span>
        </div>
        <div class="issue-row-body">${expanded ? '<div class="loading-spinner" style="padding:20px 0;"><div class="spinner"></div></div>' : ""}</div>
    </div>`;
}

/* Fields simple enough to edit inline from an issue row, with the char count
 * the scorer's good band aims for. */
const QUICK_EDIT_FIELDS = {
    title: { label: "Title", hint: "" },
    meta_title: { label: "Meta title", hint: "aim ~50, max 60 chars" },
    meta_description: { label: "Meta description", hint: "aim ~150, max 160 chars" },
};

function renderIssueItems(items, issueKey) {
    if (!items || items.length === 0) {
        return '<p style="padding: 10px 14px; font-size: 13px; color: var(--on-surface-secondary);">No items.</p>';
    }
    const issue = (issuesData?.issues || []).find(i => i.key === issueKey);
    const actionLink = issue && issue.action_href
        ? `<a class="btn btn-secondary btn-sm" href="${escapeAttr(issue.action_href)}" data-issue-action>${escapeHtml(issue.action_label || "Open")}</a>`
        : "";
    const editField = issue && issue.fixable && issue.fix_fields && QUICK_EDIT_FIELDS[issue.fix_fields[0]]
        ? issue.fix_fields[0] : null;
    return items.map(it => {
        const editorId = `qe-${issueKey}-${it.item_type}-${it.id}`;
        const current = editField ? (it[editField] || (editField === "title" ? it.title : "") || "") : "";
        const fieldMeta = editField ? QUICK_EDIT_FIELDS[editField] : null;
        const editBtn = editField
            ? `<button class="btn btn-secondary btn-sm" data-quick-edit="${editorId}" title="Type the ${fieldMeta.label.toLowerCase()} yourself and push it to Shopify"><span class="material-symbols-rounded">edit</span></button>`
            : "";
        const editor = editField
            ? `<div class="qe-editor" id="${editorId}" style="display:none; padding: 8px 14px 12px 56px;">
                <div style="display:flex; gap:8px; align-items:center;">
                    <input type="text" id="${editorId}-input" value="${escapeAttr(current)}"
                        style="flex:1; padding:7px 10px; font-size:13px; background:var(--surface); color:var(--on-surface); border:1px solid var(--outline); border-radius:6px;"
                        oninput="document.getElementById('${editorId}-count').textContent = this.value.length + ' chars'"
                        onkeydown="if (event.key === 'Enter') quickEditSave('${it.item_type}', ${it.id}, '${editField}', '${issueKey}', this)">
                    <span id="${editorId}-count" style="font-size:11.5px; color:var(--on-surface-secondary); white-space:nowrap;">${current.length} chars</span>
                    <button class="btn btn-primary btn-sm" onclick="quickEditSave('${it.item_type}', ${it.id}, '${editField}', '${issueKey}', this)">Save to Shopify</button>
                    <button class="btn btn-secondary btn-sm" onclick="document.getElementById('${editorId}').style.display='none'">Cancel</button>
                </div>
                ${fieldMeta.hint ? `<div style="font-size:11.5px; color:var(--on-surface-secondary); margin-top:4px;">${fieldMeta.label} — ${fieldMeta.hint}</div>` : ""}
            </div>`
            : "";
        return `<div class="issue-item-row">
        ${it.keyword_score != null ? renderScoreRing(it.keyword_score, 32) : '<span class="badge badge-not-analyzed">—</span>'}
        <div class="issue-item-main">
            <div class="issue-item-title">${escapeHtml(it.title)}</div>
            <div class="issue-item-detail">${escapeHtml(it.detail || "")}</div>
        </div>
        <span class="badge badge-info" style="font-size: 10px;">${it.item_type}</span>
        ${actionLink}
        ${editBtn}
        <button class="btn btn-secondary btn-sm" data-view-item="${it.item_type}:${it.id}">View</button>
    </div>${editor}`;
    }).join("");
}

window.quickEditSave = async function (itemType, itemId, field, issueKey, el) {
    const editor = el.closest(".qe-editor");
    const input = editor.querySelector("input");
    const value = input.value.trim();
    if (!value) {
        showToast("Value cannot be empty", "error");
        return;
    }
    const saveBtn = editor.querySelector(".btn-primary");
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = "Saving..."; }
    try {
        const resp = await apiRequest("/api/technical-seo/apply", {
            method: "POST",
            body: { item_type: itemType, item_id: itemId, fixes: [{ field, value }] },
        });
        showToast(`Updated on Shopify${resp.new_score != null ? ` — keyword score now ${resp.new_score}` : ""}`, "success");
        issueItemsCache.delete(issueKey);
        loadIssues();
        refreshQueueAfterApply();
    } catch (e) {
        showToast(`Update failed: ${e.message}`, "error");
        if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = "Save to Shopify"; }
    }
};

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

    await fetchIssueItems(key, body);
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
    const fieldsLabel = fixFieldsLabel(issue.fix_fields);
    document.getElementById("confirm-dialog-title").textContent = `Fix: ${issue.label}`;
    document.getElementById("confirm-dialog-message").innerHTML =
        `AI will rewrite the <strong>${escapeHtml(fieldsLabel)}</strong> of ${summary} to fix
         <strong>${escapeHtml(issue.label.toLowerCase())}</strong>, then apply the changes to Shopify.
         <br><span style="color: var(--on-surface-secondary); font-size: 12.5px;">
         Scoped to this issue — no other field is touched, and items that re-analyze clean are skipped.
         Each item is re-analyzed after the write.</span>`;
    const okBtn = document.getElementById("confirm-dialog-ok");
    okBtn.textContent = `Rewrite ${fieldsLabel}`;
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
                const task = await pollIssuesBulk(
                    resp.bulk_task_id, resp.total,
                    `Rewriting ${fieldsLabel} to fix ${issue.label.toLowerCase()} (${itemType}s)...`,
                );
                const skipped = task.skipped || 0;
                const fixed = Math.max(0, task.completed - skipped);
                showToast(`${issue.label} — ${fixed} ${itemType}${fixed !== 1 ? "s" : ""}: ${fieldsLabel} rewritten`
                    + (skipped ? `, ${skipped} already clean on re-analysis` : "")
                    + (task.failed ? `, ${task.failed} failed` : ""), task.failed ? "warning" : "success");
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
        refreshQueueAfterApply();
    };
    _openConfirmDialog();
}

// Deterministic bulk fix — declared on the issue definition, no AI involved.
// Distinct from fixIssueType(), which runs the generate-then-apply pipeline.
async function runIssueBulkFix(key) {
    const issue = (issuesData?.issues || []).find(i => i.key === key);
    if (!issue?.bulk_fix) return;

    document.getElementById("confirm-dialog-title").textContent = issue.bulk_fix.label;
    document.getElementById("confirm-dialog-message").innerHTML =
        `${escapeHtml(issue.bulk_fix.confirm || "")}
         <br><br><strong>${issue.count}</strong> collection${issue.count === 1 ? "" : "s"} will be updated on Shopify.`;
    const okBtn = document.getElementById("confirm-dialog-ok");
    okBtn.textContent = issue.bulk_fix.label;
    okBtn.onclick = async () => {
        closeConfirmDialog();
        try {
            const storeId = await _ensureStoreId();
            const resp = await apiRequest(issue.bulk_fix.endpoint, {
                method: "POST",
                body: { store_id: parseInt(storeId) },
            });
            const task = await pollIssuesBulk(resp.bulk_task_id, resp.total, `${issue.bulk_fix.label}…`);
            showToast(`${task.completed} updated` + (task.failed ? `, ${task.failed} failed` : ""),
                task.failed ? "warning" : "success");
        } catch (err) {
            showToast(`Failed: ${err.message}`, "error");
        }
        expandedIssueKeys.delete(key);
        loadIssues();
        loadCollections();
        if (typeof NavBadges !== "undefined") NavBadges.refresh();
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
        const bulkFixBtn = e.target.closest("[data-issue-bulkfix]");
        if (bulkFixBtn) {
            e.stopPropagation();
            runIssueBulkFix(bulkFixBtn.dataset.issueBulkfix);
            return;
        }
        const viewBtn = e.target.closest("[data-view-item]");
        if (viewBtn) {
            const [type, id] = viewBtn.dataset.viewItem.split(":");
            // Coming from an issue row — land on the Checks tab
            if (type === "product") viewProduct(parseInt(id), { tab: "checks" });
            else viewCollection(parseInt(id), { tab: "checks" });
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
        const quickEditBtn = e.target.closest("[data-quick-edit]");
        if (quickEditBtn) {
            const editor = document.getElementById(quickEditBtn.dataset.quickEdit);
            if (editor) {
                const open = editor.style.display !== "none";
                editor.style.display = open ? "none" : "";
                if (!open) editor.querySelector("input").focus();
            }
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
