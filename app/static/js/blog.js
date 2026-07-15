let articleSort = loadFilter("articleSort", { by: "title", dir: "asc" });
let articleFilters = loadFilter("articleFilters", { status: "" });
let selectedArticleIds = new Set();
let currentArticles = [];
let lastClickedArticleIndex = -1;
let currentWizardSession = null;
let wizardPollTimer = null;

// ── Init ─────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
    initBlogTabs();
    setupModalDismiss("article-modal", closeArticleModal);
});

document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
        const modal = document.getElementById("article-modal");
        if (modal && modal.classList.contains("active")) closeArticleModal();
    }
});

function initBlogTabs() {
    const savedTab = loadFilter("blogTab", "articles");

    document.querySelectorAll(".tab").forEach((tab) => {
        tab.addEventListener("click", () => {
            document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
            document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
            tab.classList.add("active");
            document.getElementById("tab-" + tab.dataset.tab).classList.add("active");
            saveFilter("blogTab", tab.dataset.tab);

            if (tab.dataset.tab === "articles") loadArticles();
            if (tab.dataset.tab === "create") initWizard();
        });
    });

    const tabBtn = document.querySelector(`.tab[data-tab="${savedTab}"]`);
    if (tabBtn && savedTab !== "articles") {
        tabBtn.click();
    } else {
        loadArticles();
    }
}

// ── Articles Tab ─────────────────────────────────────────────────

async function loadArticles() {
    const container = document.getElementById("articles-container");
    try {
        let url = `/api/blog/articles?sort_by=${articleSort.by}&sort_dir=${articleSort.dir}`;
        if (articleFilters.status) url += `&status=${articleFilters.status}`;
        const articles = await apiRequest(url);
        currentArticles = articles;
        if (articles.length === 0 && !articleFilters.status) {
            container.innerHTML = `
                <div class="empty-state">
                    <span class="material-symbols-rounded">edit_note</span>
                    <p>No articles synced yet. Sync your store or create a new post.</p>
                    <button class="btn btn-primary" id="sync-blogs-btn" onclick="syncBlogs()">
                        <span class="material-symbols-rounded">sync</span>
                        Sync from Shopify
                    </button>
                </div>`;
            return;
        }
        renderArticlesTable(container, articles);
        loadLandingPageStatus();
        loadEmptyBlogsStatus();
        loadBlogSeoCard();
        loadSeoSnippetStatus();
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>${escapeHtml(err.message)}</p></div>`;
    }
}

function renderArticlesTable(container, articles) {
    let html = "";

    const totalCount = currentArticles.length;
    const pubCount = currentArticles.filter(a => a.is_published).length;
    const draftCount = totalCount - pubCount;

    // Articles header with filters + sync
    html += `<div class="articles-header">
        <div class="status-filter-bar">
            <button class="filter-chip ${articleFilters.status === "" ? "active" : ""}" onclick="setArticleStatusFilter('')">All (${totalCount})</button>
            <button class="filter-chip ${articleFilters.status === "published" ? "active" : ""}" onclick="setArticleStatusFilter('published')">Published (${pubCount})</button>
            <button class="filter-chip ${articleFilters.status === "draft" ? "active" : ""}" onclick="setArticleStatusFilter('draft')">Draft (${draftCount})</button>
        </div>
        <button class="btn btn-secondary btn-sm" id="sync-blogs-btn" onclick="syncBlogs()">
            <span class="material-symbols-rounded">sync</span>
            Refresh from Shopify
        </button>
    </div>`;

    if (articles.length === 0) {
        html += `<div class="empty-state">
            <span class="material-symbols-rounded">filter_list_off</span>
            <p>No articles match the current filters.</p>
            <button class="btn btn-secondary" onclick="clearArticleFilters()">
                <span class="material-symbols-rounded">clear_all</span>
                Clear Filters
            </button>
        </div>`;
        container.innerHTML = html;
        return;
    }

    // Bulk toolbar
    if (selectedArticleIds.size > 0) {
        html += `<div class="bulk-toolbar">
            <span class="bulk-count">${selectedArticleIds.size} article${selectedArticleIds.size > 1 ? "s" : ""} selected</span>
            <button class="btn btn-sm btn-primary" onclick="startBulkOptimizeArticles()">
                <span class="material-symbols-rounded">bolt</span>
                Bulk Optimize & Apply
            </button>
            <button class="btn btn-sm btn-error" onclick="confirmBulkDeleteArticles()">
                <span class="material-symbols-rounded">delete</span>
                Delete Selected
            </button>
        </div>`;
    }

    // Table
    html += '<div class="table-container"><table><thead><tr>';
    html += '<th class="checkbox-col"><input type="checkbox" class="table-checkbox" id="select-all-articles" onchange="toggleSelectAllArticles(this)"></th>';
    html += '<th class="row-num-col">#</th>';

    const columns = [
        { key: "title", label: "Title", sortable: true },
        { key: "status", label: "Status", sortable: true },
        { key: "seo_score", label: "SEO Score", sortable: false },
        { key: "last_optimized_at", label: "Last Optimized", sortable: true },
    ];

    columns.forEach((col) => {
        if (col.sortable) {
            const arrow = articleSort.by === col.key ? (articleSort.dir === "asc" ? " ↑" : " ↓") : "";
            html += `<th class="sortable" onclick="sortArticles('${col.key}')">${col.label}${arrow}</th>`;
        } else {
            html += `<th>${col.label}</th>`;
        }
    });
    html += "</tr></thead><tbody>";

    articles.forEach((a, i) => {
        const checked = selectedArticleIds.has(a.id) ? "checked" : "";
        const statusBadge = a.is_published
            ? '<span class="badge badge-success">Published</span>'
            : '<span class="badge badge-warning">Draft</span>';

        html += `<tr onclick="openArticleDetail(${a.id})" style="cursor:pointer;">
            <td class="checkbox-col" onclick="event.stopPropagation()">
                <input type="checkbox" class="table-checkbox" ${checked} onchange="toggleArticleSelect(${a.id}, ${i}, event)">
            </td>
            <td class="row-num-col">${i + 1}</td>
            <td><strong>${escapeHtml(a.title)}</strong></td>
            <td>${statusBadge}</td>
            <td><span class="score-inline">${getScoreBadge(a.seo_score)}${getScoreBarMini(a.seo_score)}</span></td>
            <td>${formatOptimizedDate(a.last_optimized_at)}</td>
        </tr>`;
    });

    html += "</tbody></table></div>";
    container.innerHTML = html;
}

function formatOptimizedDate(dateStr) {
    if (!dateStr) return '<span style="color: var(--on-surface-secondary);">Never</span>';
    const d = new Date(dateStr);
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function getScoreBarMini(score) {
    const s = score || 0;
    let color = "#ef4444";
    if (s >= 70) color = "#22c55e";
    else if (s >= 40) color = "#eab308";
    return `<span class="score-bar-mini"><span class="score-bar-mini-fill" style="width:${s}%;background:${color};"></span></span>`;
}

function toggleBlogSettings() {
    const panel = document.getElementById("blog-settings-panel");
    const toolbar = document.getElementById("blog-toolbar");
    const gearBtn = document.getElementById("settings-gear-btn");
    if (!panel) return;
    const isOpen = panel.classList.toggle("open");
    toolbar.classList.toggle("panel-open", isOpen);
    gearBtn.classList.toggle("active", isOpen);
    saveFilter("blogSettingsOpen", isOpen);
}

function setArticleStatusFilter(status) {
    articleFilters.status = articleFilters.status === status ? "" : status;
    saveFilter("articleFilters", articleFilters);
    selectedArticleIds.clear();
    lastClickedArticleIndex = -1;
    loadArticles();
}

function clearArticleFilters() {
    articleFilters = { status: "" };
    saveFilter("articleFilters", articleFilters);
    selectedArticleIds.clear();
    lastClickedArticleIndex = -1;
    loadArticles();
}

function sortArticles(key) {
    if (articleSort.by === key) {
        articleSort.dir = articleSort.dir === "asc" ? "desc" : "asc";
    } else {
        articleSort.by = key;
        articleSort.dir = "asc";
    }
    saveFilter("articleSort", articleSort);
    loadArticles();
}

function toggleSelectAllArticles(checkbox) {
    if (checkbox.checked) {
        currentArticles.forEach((a) => selectedArticleIds.add(a.id));
    } else {
        selectedArticleIds.clear();
    }
    lastClickedArticleIndex = -1;
    loadArticles();
}

function toggleArticleSelect(id, index, event) {
    if (event.shiftKey && lastClickedArticleIndex >= 0) {
        const start = Math.min(lastClickedArticleIndex, index);
        const end = Math.max(lastClickedArticleIndex, index);
        for (let i = start; i <= end; i++) {
            selectedArticleIds.add(currentArticles[i].id);
        }
    } else {
        if (selectedArticleIds.has(id)) {
            selectedArticleIds.delete(id);
        } else {
            selectedArticleIds.add(id);
        }
    }
    lastClickedArticleIndex = index;
    loadArticles();
}

// ── Article Detail Modal ─────────────────────────────────────────

async function openArticleDetail(articleId) {
    const modal = document.getElementById("article-modal");
    const content = document.getElementById("article-detail-content");
    const title = document.getElementById("article-modal-title");

    content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div><p>Loading...</p></div>';
    modal.classList.add("active");

    try {
        const article = await apiRequest(`/api/blog/articles/${articleId}`);
        title.textContent = article.title;
        renderArticleDetail(content, article);
    } catch (err) {
        content.innerHTML = `<p>${escapeHtml(err.message)}</p>`;
    }
}

function renderArticleDetail(container, article) {
    const breakdown = article.seo_breakdown || {};

    let breakdownHtml = "";
    const labels = {
        meta_title: "Meta Title",
        meta_description: "Meta Description",
        word_count: "Word Count",
        headings: "Heading Structure",
        internal_links: "Internal Links",
        image: "Featured Image",
        summary: "Summary/Excerpt",
    };
    for (const [key, label] of Object.entries(labels)) {
        const item = breakdown[key] || {};
        breakdownHtml += `
            <div class="seo-check-item">
                <div class="seo-check-header">
                    <span class="seo-check-label">${label}</span>
                    ${getScoreBadge(item.score || 0)}
                </div>
                <p class="seo-check-detail">${escapeHtml(item.detail || "—")}</p>
            </div>`;
    }

    let html = `
        <div style="display: grid; grid-template-columns: 1fr 350px; gap: 24px; height: 100%;">
            <div style="overflow-y: auto; padding-right: 12px;">
                <div class="card">
                    <h4>Article Content</h4>
                    <div class="form-group">
                        <label>Title</label>
                        <input type="text" class="form-control" id="article-edit-title" value="${escapeHtml(article.title || "")}">
                    </div>
                    <div class="form-group">
                        <label>Meta Title</label>
                        <input type="text" class="form-control" id="article-edit-meta-title" value="${escapeHtml(article.meta_title || "")}">
                        <small style="color: var(--on-surface-secondary);">${(article.meta_title || "").length} / 60 chars</small>
                    </div>
                    <div class="form-group">
                        <label>Meta Description</label>
                        <textarea class="form-control" id="article-edit-meta-desc" rows="3">${escapeHtml(article.meta_description || "")}</textarea>
                        <small style="color: var(--on-surface-secondary);">${(article.meta_description || "").length} / 160 chars</small>
                    </div>
                    <div class="form-group">
                        <label>Summary / Excerpt</label>
                        <textarea class="form-control" id="article-edit-summary" rows="2">${escapeHtml(article.summary || "")}</textarea>
                    </div>
                    <div class="form-group">
                        <label>Tags</label>
                        <input type="text" class="form-control" id="article-edit-tags" value="${escapeHtml(article.tags || "")}">
                    </div>
                    <div class="form-group">
                        <label>Body HTML</label>
                        <textarea class="form-control" id="article-edit-body" rows="15" style="font-family: monospace; font-size: 13px;">${escapeHtml(article.body || "")}</textarea>
                    </div>
                    <div style="display: flex; gap: 8px; margin-top: 16px; flex-wrap: wrap;">
                        <button class="btn btn-primary" onclick="applyArticleChanges(${article.id})">
                            <span class="material-symbols-rounded">save</span>
                            Apply to Shopify
                        </button>
                        <button class="btn btn-secondary" onclick="analyzeArticleAI(${article.id})">
                            <span class="material-symbols-rounded">auto_awesome</span>
                            AI Analyze
                        </button>
                        <button class="btn btn-secondary" onclick="openRefreshLinksPanel(${article.id}, ${article.store_id})">
                            <span class="material-symbols-rounded">link</span>
                            Refresh Links
                        </button>
                    </div>
                    <div id="refresh-links-panel-container"></div>
                </div>
            </div>
            <div style="overflow-y: auto;">
                <div class="card">
                    <h4>SEO Score: ${getScoreBadge(article.seo_score)}</h4>
                    <div class="seo-breakdown">${breakdownHtml}</div>
                </div>
                <div class="card" style="margin-top: 16px;">
                    <h4>Featured Image</h4>
                    ${article.image_url ? `
                    <img src="${article.image_url}" alt="${escapeHtml(article.image_alt || "")}" style="width: 100%; border-radius: 8px;">
                    <small style="color: var(--on-surface-secondary); display: block; margin-top: 8px;">Alt: ${escapeHtml(article.image_alt || "No alt text")}</small>`
                    : `<p style="font-size: 13px; color: var(--on-surface-secondary); margin: 0;">No featured image.</p>`}
                    <button class="btn btn-secondary btn-sm" style="margin-top: 10px;" onclick="regenerateArticleImage(${article.id}, this)">
                        <span class="material-symbols-rounded">auto_awesome</span>
                        ${article.image_url ? "Regenerate image" : "Generate image"}
                    </button>
                </div>
                <div class="card" style="margin-top: 16px;">
                    <h4>Info</h4>
                    <p><strong>Author:</strong> ${escapeHtml(article.author || "")}</p>
                    <p><strong>Status:</strong> ${article.is_published ? "Published" : "Draft"}</p>
                    <p><strong>Store:</strong> ${escapeHtml(article.store_name || "")}</p>
                </div>
                <div id="article-ai-result" style="margin-top: 16px;"></div>
            </div>
        </div>`;

    container.innerHTML = html;
}

function closeArticleModal() {
    document.getElementById("article-modal").classList.remove("active");
}

async function regenerateArticleImage(articleId, btn) {
    const idle = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded">hourglass_empty</span> Generating…';
    try {
        const res = await apiRequest(`/api/blog/articles/${articleId}/regenerate-image`, { method: "POST", body: {} });
        showToast("Generating a new featured image — this may take ~30s", "info");
        const interval = setInterval(async () => {
            try {
                const task = await apiRequest(`/api/tasks/${res.task_id}`);
                if (task.status === "completed") {
                    clearInterval(interval);
                    showToast("Featured image updated on Shopify", "success");
                    openArticleDetail(articleId);
                } else if (task.status === "failed") {
                    clearInterval(interval);
                    showToast("Image generation failed: " + (task.error || "Unknown error"), "error");
                    btn.disabled = false;
                    btn.innerHTML = idle;
                }
            } catch {
                clearInterval(interval);
            }
        }, 3000);
    } catch (err) {
        showToast(`Failed to start: ${err.message}`, "error");
        btn.disabled = false;
        btn.innerHTML = idle;
    }
}

async function applyArticleChanges(articleId) {
    const fields = {
        title: document.getElementById("article-edit-title").value,
        meta_title: document.getElementById("article-edit-meta-title").value,
        meta_description: document.getElementById("article-edit-meta-desc").value,
        summary: document.getElementById("article-edit-summary").value,
        tags: document.getElementById("article-edit-tags").value,
        body: document.getElementById("article-edit-body").value,
    };

    try {
        await apiRequest(`/api/blog/articles/${articleId}/apply`, {
            method: "POST",
            body: fields,
        });
        showToast("Changes applied to Shopify", "success");
        loadArticles();
    } catch (err) {
        showToast(err.message, "error");
    }
}

async function analyzeArticleAI(articleId) {
    const btn = typeof event !== "undefined" && event && event.target instanceof Element ? event.target.closest("button") : null;
    return guardButton(btn, async () => {
        const resultDiv = document.getElementById("article-ai-result");
        resultDiv.innerHTML = '<div class="card"><div class="loading-spinner"><div class="spinner"></div><p>AI analyzing article...</p></div></div>';

        try {
            const { task_id } = await apiRequest(`/api/blog/articles/${articleId}/analyze`, { method: "POST" });
            pollArticleAITask(task_id, articleId);
        } catch (err) {
            resultDiv.innerHTML = `<div class="card"><p style="color: var(--error);">${escapeHtml(err.message)}</p></div>`;
        }
    });
}

function pollArticleAITask(taskId, articleId) {
    const resultDiv = document.getElementById("article-ai-result");
    const poll = async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            if (task.status === "completed" && task.result) {
                renderArticleAIResult(resultDiv, task.result, articleId);
            } else if (task.status === "failed") {
                resultDiv.innerHTML = `<div class="card"><p style="color: var(--error);">Analysis failed: ${escapeHtml(task.error || "Unknown error")}</p></div>`;
            } else {
                setTimeout(poll, 2000);
            }
        } catch (err) {
            resultDiv.innerHTML = `<div class="card"><p style="color: var(--error);">${escapeHtml(err.message)}</p></div>`;
        }
    };
    poll();
}

let lastArticleAiResult = null;

function applyArticleAiField(targetId, key, msg) {
    if (!lastArticleAiResult) return;
    const el = document.getElementById(targetId);
    if (!el) return;
    el.value = lastArticleAiResult[key] || "";
    showToast(msg, "success");
}

function renderArticleAIResult(container, result, articleId) {
    lastArticleAiResult = result;
    let html = '<div class="card"><h4>AI Recommendations</h4>';

    if (result.optimized_meta_title) {
        html += `<div class="form-group">
            <label>Suggested Meta Title</label>
            <div class="ai-suggestion" style="padding: 8px 12px; background: var(--primary-light); border-radius: 8px; margin-bottom: 8px;">${escapeHtml(result.optimized_meta_title)}</div>
            <button class="btn btn-sm btn-secondary" onclick="applyArticleAiField('article-edit-meta-title', 'optimized_meta_title', 'Applied')">Use This</button>
        </div>`;
    }

    if (result.optimized_meta_description) {
        html += `<div class="form-group">
            <label>Suggested Meta Description</label>
            <div class="ai-suggestion" style="padding: 8px 12px; background: var(--primary-light); border-radius: 8px; margin-bottom: 8px;">${escapeHtml(result.optimized_meta_description)}</div>
            <button class="btn btn-sm btn-secondary" onclick="applyArticleAiField('article-edit-meta-desc', 'optimized_meta_description', 'Applied')">Use This</button>
        </div>`;
    }

    if (result.summary) {
        html += `<div class="form-group"><label>Summary</label><p>${escapeHtml(result.summary)}</p></div>`;
    }

    if (result.additional_recommendations && result.additional_recommendations.length) {
        html += "<div class='form-group'><label>Recommendations</label><ul>";
        result.additional_recommendations.forEach((r) => {
            html += `<li>${escapeHtml(r)}</li>`;
        });
        html += "</ul></div>";
    }

    if (result.optimized_body) {
        html += `<div class="form-group">
            <button class="btn btn-sm btn-primary" onclick="applyArticleAiField('article-edit-body', 'optimized_body', 'Body updated')">
                <span class="material-symbols-rounded">content_paste</span>
                Apply Optimized Body
            </button>
        </div>`;
    }

    html += "</div>";
    container.innerHTML = html;
}

// ── Bulk Operations ──────────────────────────────────────────────

async function startBulkOptimizeArticles() {
    if (selectedArticleIds.size === 0) return;

    const overlay = document.getElementById("blog-bulk-progress-overlay");
    overlay.classList.add("active");
    document.getElementById("blog-bulk-progress-bar").style.width = "0%";
    document.getElementById("blog-bulk-progress-stats").textContent = `0 / ${selectedArticleIds.size} completed`;
    document.getElementById("blog-bulk-progress-current").textContent = "";

    try {
        const { bulk_task_id } = await apiRequest("/api/blog/bulk-optimize", {
            method: "POST",
            body: { article_ids: [...selectedArticleIds], auto_apply: true },
        });
        pollBulkArticleProgress(bulk_task_id);
    } catch (err) {
        overlay.classList.remove("active");
        showToast(err.message, "error");
    }
}

function pollBulkArticleProgress(bulkTaskId) {
    let consecutiveErrors = 0;
    const poll = async () => {
        try {
            const task = await apiRequest(`/api/blog/bulk-status/${bulkTaskId}`);
            consecutiveErrors = 0;
            const total = task.total || 1;
            const done = (task.completed || 0) + (task.failed || 0);
            const pct = Math.round((done / total) * 100);

            document.getElementById("blog-bulk-progress-bar").style.width = pct + "%";
            document.getElementById("blog-bulk-progress-stats").textContent = `${done} / ${total} completed${task.failed ? ` (${task.failed} failed)` : ""}`;
            document.getElementById("blog-bulk-progress-current").textContent = task.current_product_title || "";

            if (task.status === "completed" || task.status === "failed") {
                setTimeout(() => {
                    document.getElementById("blog-bulk-progress-overlay").classList.remove("active");
                    selectedArticleIds.clear();
                    loadArticles();
                    showToast(`Bulk optimization complete: ${task.completed} succeeded, ${task.failed} failed`, task.failed ? "warning" : "success");
                }, 1500);
            } else {
                setTimeout(poll, 2000);
            }
        } catch (err) {
            consecutiveErrors++;
            if (consecutiveErrors >= 5) {
                document.getElementById("blog-bulk-progress-overlay").classList.remove("active");
                showToast(err.message, "error");
                return;
            }
            setTimeout(poll, 2000);
        }
    };
    poll();
}

function confirmBulkDeleteArticles() {
    if (selectedArticleIds.size === 0) return;
    showBlogConfirmDialog(
        "Delete Articles",
        `Delete ${selectedArticleIds.size} selected article(s) from Shopify? This cannot be undone.`,
        async () => {
            try {
                await apiRequest("/api/blog/articles/delete", {
                    method: "POST",
                    body: { article_ids: [...selectedArticleIds] },
                });
                selectedArticleIds.clear();
                showToast("Articles deleted", "success");
                loadArticles();
            } catch (err) {
                showToast(err.message, "error");
            }
        }
    );
}

async function getFirstStoreId() {
    try {
        const stores = await apiRequest("/api/stores");
        return stores.length > 0 ? stores[0].id : null;
    } catch {
        return null;
    }
}

// ── Wizard ───────────────────────────────────────────────────────

async function initWizard() {
    showWizardStep(1);
}

function showWizardStep(step) {
    document.querySelectorAll(".wizard-step-enhanced").forEach((s) => {
        const stepNum = parseInt(s.dataset.step);
        s.classList.toggle("active", stepNum === step);
        s.classList.toggle("completed", stepNum < step);
        const indicator = s.querySelector(".step-indicator");
        if (indicator) {
            indicator.innerHTML = stepNum < step ? '<span class="material-symbols-rounded" style="font-size:16px;">check</span>' : stepNum;
        }
    });
    document.querySelectorAll(".wizard-connector").forEach((c) => {
        const afterStep = parseInt(c.dataset.after);
        c.classList.toggle("filled", afterStep < step);
    });
    document.querySelectorAll(".wizard-panel").forEach((p) => p.classList.remove("active"));
    const panel = document.getElementById(`wizard-step-${step}`);
    if (panel) panel.classList.add("active");
}

async function wizardStartResearch() {
    const topic = document.getElementById("wizard-topic").value.trim();
    const keywordsText = document.getElementById("wizard-keywords").value.trim();
    const keywords = keywordsText ? keywordsText.split("\n").map((k) => k.trim()).filter(Boolean) : [];

    if (!topic) { showToast("Please enter a topic", "error"); return; }

    const storeId = await getFirstStoreId();
    if (!storeId) { showToast("No store found", "error"); return; }

    try {
        const { session_id } = await apiRequest("/api/blog/wizard/start", {
            method: "POST",
            body: { store_id: storeId, topic, keywords },
        });
        currentWizardSession = session_id;

        showWizardStep(2);
        document.getElementById("research-content").innerHTML =
            '<div class="loading-spinner"><div class="spinner"></div><p>Researching topic and competitors...</p></div>';

        await apiRequest(`/api/blog/wizard/${session_id}/research`, { method: "POST" });
        pollWizardSession(session_id, "researched", () => renderResearchResults(session_id));
    } catch (err) {
        showToast(err.message, "error");
    }
}

function pollWizardSession(sessionId, targetStatus, onComplete) {
    if (wizardPollTimer) clearTimeout(wizardPollTimer);
    let consecutiveErrors = 0;
    const poll = async () => {
        try {
            const session = await apiRequest(`/api/blog/wizard/${sessionId}`);
            consecutiveErrors = 0;
            if (session.status === targetStatus) {
                onComplete(session);
            } else if (session.status === "error") {
                showToast("An error occurred. Please try again.", "error");
                showWizardStep(1);
            } else {
                wizardPollTimer = setTimeout(poll, 2000);
            }
        } catch (err) {
            consecutiveErrors++;
            if (consecutiveErrors >= 5) {
                showToast(err.message, "error");
                showWizardStep(1);
                return;
            }
            wizardPollTimer = setTimeout(poll, 2000);
        }
    };
    poll();
}

let wizardKeywords = [];
let wizardSelectedProducts = [];
let wizardSelectedCollections = [];
let productSearchTimer = null;
let collectionSearchTimer = null;
let wizardProductResults = [];
let wizardCollectionResults = [];

async function renderResearchResults(sessionId) {
    const session = await apiRequest(`/api/blog/wizard/${sessionId}`);
    const research = session.research_data || {};
    const container = document.getElementById("research-content");

    wizardKeywords = [...(session.target_keywords || [])];

    let html = '<div style="max-width: 800px; margin: 0 auto;">';

    // Competitor analysis
    const comp = research.competitor_analysis || {};
    html += `<div class="card">
        <h4>Competitor Analysis</h4>
        <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px;">
            <div style="text-align: center; padding: 12px; background: var(--primary-light); border-radius: 8px;">
                <div style="font-size: 24px; font-weight: 700; color: var(--primary);">${comp.avg_word_count || "—"}</div>
                <div style="font-size: 13px; color: var(--on-surface-secondary);">Avg Word Count</div>
            </div>
            <div style="text-align: center; padding: 12px; background: var(--primary-light); border-radius: 8px;">
                <div style="font-size: 24px; font-weight: 700; color: var(--primary);">${research.recommended_word_count || "—"}</div>
                <div style="font-size: 13px; color: var(--on-surface-secondary);">Target Words</div>
            </div>
            <div style="text-align: center; padding: 12px; background: var(--primary-light); border-radius: 8px;">
                <div style="font-size: 24px; font-weight: 700; color: var(--primary);">${(comp.top_domains || []).length}</div>
                <div style="font-size: 13px; color: var(--on-surface-secondary);">Competitors</div>
            </div>
        </div>
    </div>`;

    // Keywords management
    html += `<div class="card" style="margin-top: 16px;">
        <h4>Target Keywords</h4>
        <p style="color: var(--on-surface-secondary); font-size: 13px; margin-bottom: 12px;">The first keyword is your <strong>primary keyword</strong> — it drives the title, URL, meta tags, and first paragraph. Click the star on any keyword to promote it to primary.</p>
        <div id="wizard-keyword-chips" style="display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px;"></div>
        <div style="display: flex; gap: 8px;">
            <input type="text" id="wizard-keyword-input" class="form-control" placeholder="Type exact keyword and press Enter" style="flex: 1;" onkeydown="if(event.key==='Enter'){event.preventDefault();addWizardKeyword();}">
            <button class="btn btn-secondary" onclick="addWizardKeyword()" style="white-space: nowrap;">Add</button>
        </div>
    </div>`;

    // AI-suggested keywords
    const kwOps = research.keyword_opportunities || [];
    if (kwOps.length) {
        html += `<div class="card" style="margin-top: 16px;">
            <h4>AI-Suggested Keywords</h4>
            <p style="color: var(--on-surface-secondary); font-size: 13px; margin-bottom: 12px;">Click to add to your target keywords.</p>
            <div id="wizard-suggested-keywords" style="display: flex; flex-wrap: wrap; gap: 8px;">`;
        kwOps.forEach((kw) => {
            html += `<span class="badge badge-info wizard-suggestion" style="cursor: pointer;" data-keyword="${escapeHtml(kw)}" onclick="addSuggestedKeyword(this)">${escapeHtml(kw)}</span>`;
        });
        html += `</div></div>`;
    }

    // Content angle suggestions
    const angles = research.content_angle_suggestions || [];
    if (angles.length) {
        html += `<div class="card" style="margin-top: 16px;">
            <h4>Content Angles</h4>
            <ul>`;
        angles.forEach((a) => { html += `<li>${escapeHtml(a)}</li>`; });
        html += `</ul></div>`;
    }

    // Internal Linking — Products
    const relProducts = research.related_products || [];
    wizardSelectedProducts = relProducts.map((p) => ({ id: p.id, title: p.title, handle: p.handle, reason: p.reason || "" }));

    html += `<div class="card" style="margin-top: 16px;">
        <h4>Products for Internal Linking</h4>
        <p style="color: var(--on-surface-secondary); font-size: 13px; margin-bottom: 12px;">AI-discovered products are pre-selected. Toggle items or search to add more.</p>
        <div id="wizard-product-chips" style="display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px;"></div>
        <div style="position: relative;">
            <input type="text" id="wizard-product-search" class="form-control" placeholder="Search products to add..."
                oninput="debounceProductSearch(this.value)">
            <div id="wizard-product-dropdown" class="autocomplete-dropdown" style="display: none;"></div>
        </div>
    </div>`;

    // Internal Linking — Collections
    const relColls = research.related_collections || [];
    wizardSelectedCollections = relColls.map((c) => ({ id: c.id, title: c.title, handle: c.handle, reason: c.reason || "" }));

    html += `<div class="card" style="margin-top: 16px;">
        <h4>Collections for Internal Linking</h4>
        <p style="color: var(--on-surface-secondary); font-size: 13px; margin-bottom: 12px;">AI-discovered collections are pre-selected. Toggle items or search to add more.</p>
        <div id="wizard-collection-chips" style="display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px;"></div>
        <div style="position: relative;">
            <input type="text" id="wizard-collection-search" class="form-control" placeholder="Search collections to add..."
                oninput="debounceCollectionSearch(this.value)">
            <div id="wizard-collection-dropdown" class="autocomplete-dropdown" style="display: none;"></div>
        </div>
    </div>`;

    html += `<button class="btn btn-primary" onclick="wizardGenerateOutline()" style="margin-top: 20px;">
        <span class="material-symbols-rounded">list_alt</span>
        Generate Outline
    </button>`;

    html += "</div>";
    container.innerHTML = html;
    renderKeywordChips();
    renderProductChips();
    renderCollectionChips();
}

function renderKeywordChips() {
    const container = document.getElementById("wizard-keyword-chips");
    if (!container) return;
    container.innerHTML = wizardKeywords.map((kw, i) => {
        if (i === 0) {
            return `<span class="keyword-chip-primary">
                <span class="material-symbols-rounded" style="font-size: 15px;">star</span>
                Primary: ${escapeHtml(kw)}
                <span class="material-symbols-rounded" style="font-size: 16px; cursor: pointer;" onclick="removeWizardKeyword(${i})">close</span>
            </span>`;
        }
        return `<span class="badge badge-success keyword-chip" style="display: inline-flex; align-items: center; gap: 4px; padding: 6px 10px;">
            <span class="material-symbols-rounded make-primary-btn" onclick="makeWizardKeywordPrimary(${i})" title="Make primary keyword">star</span>
            ${escapeHtml(kw)}
            <span class="material-symbols-rounded" style="font-size: 16px; cursor: pointer;" onclick="removeWizardKeyword(${i})">close</span>
        </span>`;
    }).join("");

    // Update suggestion badges
    document.querySelectorAll(".wizard-suggestion").forEach((el) => {
        const kw = el.dataset.keyword;
        if (wizardKeywords.includes(kw)) {
            el.style.opacity = "0.4";
            el.style.textDecoration = "line-through";
        } else {
            el.style.opacity = "1";
            el.style.textDecoration = "none";
        }
    });
}

function addWizardKeyword() {
    const input = document.getElementById("wizard-keyword-input");
    const value = input.value;
    if (!value) return;
    if (!wizardKeywords.includes(value)) {
        wizardKeywords.push(value);
        renderKeywordChips();
    }
    input.value = "";
    input.focus();
}

function addSuggestedKeyword(el) {
    const kw = el.dataset.keyword;
    if (wizardKeywords.includes(kw)) {
        wizardKeywords = wizardKeywords.filter((k) => k !== kw);
    } else {
        wizardKeywords.push(kw);
    }
    renderKeywordChips();
}

function removeWizardKeyword(index) {
    wizardKeywords.splice(index, 1);
    renderKeywordChips();
}

function makeWizardKeywordPrimary(index) {
    if (index <= 0 || index >= wizardKeywords.length) return;
    const kw = wizardKeywords.splice(index, 1)[0];
    wizardKeywords.unshift(kw);
    renderKeywordChips();
}

// ── Product/Collection Linking Helpers ──────────────────────────

function renderProductChips() {
    const container = document.getElementById("wizard-product-chips");
    if (!container) return;
    if (!wizardSelectedProducts.length) {
        container.innerHTML = '<span style="color: var(--on-surface-secondary); font-size: 13px;">No products selected</span>';
        return;
    }
    container.innerHTML = wizardSelectedProducts.map((p, i) =>
        `<span class="badge badge-success" style="display: inline-flex; align-items: center; gap: 4px; padding: 6px 10px;" title="${escapeHtml(p.reason || "")}">
            ${escapeHtml(p.title)}
            <span class="material-symbols-rounded" style="font-size: 16px; cursor: pointer;" onclick="removeSelectedProduct(${i})">close</span>
        </span>`
    ).join("");
}

function renderCollectionChips() {
    const container = document.getElementById("wizard-collection-chips");
    if (!container) return;
    if (!wizardSelectedCollections.length) {
        container.innerHTML = '<span style="color: var(--on-surface-secondary); font-size: 13px;">No collections selected</span>';
        return;
    }
    container.innerHTML = wizardSelectedCollections.map((c, i) =>
        `<span class="badge badge-info" style="display: inline-flex; align-items: center; gap: 4px; padding: 6px 10px;" title="${escapeHtml(c.reason || "")}">
            ${escapeHtml(c.title)}
            <span class="material-symbols-rounded" style="font-size: 16px; cursor: pointer;" onclick="removeSelectedCollection(${i})">close</span>
        </span>`
    ).join("");
}

function removeSelectedProduct(index) {
    wizardSelectedProducts.splice(index, 1);
    renderProductChips();
}

function removeSelectedCollection(index) {
    wizardSelectedCollections.splice(index, 1);
    renderCollectionChips();
}

function debounceProductSearch(query) {
    if (productSearchTimer) clearTimeout(productSearchTimer);
    const dropdown = document.getElementById("wizard-product-dropdown");
    if (!query.trim()) { dropdown.style.display = "none"; return; }
    productSearchTimer = setTimeout(() => searchProducts(query.trim()), 300);
}

function debounceCollectionSearch(query) {
    if (collectionSearchTimer) clearTimeout(collectionSearchTimer);
    const dropdown = document.getElementById("wizard-collection-dropdown");
    if (!query.trim()) { dropdown.style.display = "none"; return; }
    collectionSearchTimer = setTimeout(() => searchCollections(query.trim()), 300);
}

async function searchProducts(query) {
    const dropdown = document.getElementById("wizard-product-dropdown");
    try {
        const storeId = await getFirstStoreId();
        const results = await apiRequest(`/api/blog/wizard/search/products?store_id=${storeId}&q=${encodeURIComponent(query)}`);
        wizardProductResults = results;
        if (!results.length) {
            dropdown.innerHTML = '<div class="autocomplete-item" style="color: var(--on-surface-secondary);">No products found</div>';
        } else {
            dropdown.innerHTML = results.map((p, i) => {
                const already = wizardSelectedProducts.some((s) => s.id === p.id);
                return `<div class="autocomplete-item${already ? " disabled" : ""}" onclick="${already ? "" : `addSearchedProduct(${i})`}">
                    <div>${escapeHtml(p.title)}${already ? ' <span style="color: var(--success); font-size: 12px;">(added)</span>' : ""}</div>
                    <div class="autocomplete-item-meta">${escapeHtml(p.vendor || "")}${p.vendor && p.product_type ? " · " : ""}${escapeHtml(p.product_type || "")}</div>
                </div>`;
            }).join("");
        }
        dropdown.style.display = "block";
    } catch (err) {
        dropdown.style.display = "none";
    }
}

async function searchCollections(query) {
    const dropdown = document.getElementById("wizard-collection-dropdown");
    try {
        const storeId = await getFirstStoreId();
        const results = await apiRequest(`/api/blog/wizard/search/collections?store_id=${storeId}&q=${encodeURIComponent(query)}`);
        wizardCollectionResults = results;
        if (!results.length) {
            dropdown.innerHTML = '<div class="autocomplete-item" style="color: var(--on-surface-secondary);">No collections found</div>';
        } else {
            dropdown.innerHTML = results.map((c, i) => {
                const already = wizardSelectedCollections.some((s) => s.id === c.id);
                return `<div class="autocomplete-item${already ? " disabled" : ""}" onclick="${already ? "" : `addSearchedCollection(${i})`}">
                    <div>${escapeHtml(c.title)}${already ? ' <span style="color: var(--success); font-size: 12px;">(added)</span>' : ""}</div>
                    <div class="autocomplete-item-meta">${escapeHtml(c.collection_type || "")}${c.products_count != null ? ` · ${c.products_count} products` : ""}</div>
                </div>`;
            }).join("");
        }
        dropdown.style.display = "block";
    } catch (err) {
        dropdown.style.display = "none";
    }
}

function addSearchedProduct(idx) {
    const p = wizardProductResults[idx];
    if (!p) return;
    if (wizardSelectedProducts.some((s) => s.id === p.id)) return;
    wizardSelectedProducts.push({ id: p.id, title: p.title, handle: p.handle, reason: "Manually added" });
    renderProductChips();
    document.getElementById("wizard-product-search").value = "";
    document.getElementById("wizard-product-dropdown").style.display = "none";
}

function addSearchedCollection(idx) {
    const c = wizardCollectionResults[idx];
    if (!c) return;
    if (wizardSelectedCollections.some((s) => s.id === c.id)) return;
    wizardSelectedCollections.push({ id: c.id, title: c.title, handle: c.handle, reason: "Manually added" });
    renderCollectionChips();
    document.getElementById("wizard-collection-search").value = "";
    document.getElementById("wizard-collection-dropdown").style.display = "none";
}

// Close dropdowns on outside click
document.addEventListener("click", (e) => {
    if (!e.target.closest("#wizard-product-search") && !e.target.closest("#wizard-product-dropdown")) {
        const d = document.getElementById("wizard-product-dropdown");
        if (d) d.style.display = "none";
    }
    if (!e.target.closest("#wizard-collection-search") && !e.target.closest("#wizard-collection-dropdown")) {
        const d = document.getElementById("wizard-collection-dropdown");
        if (d) d.style.display = "none";
    }
});

async function wizardGenerateOutline() {
    if (!currentWizardSession) return;

    if (!wizardKeywords.length) {
        showToast("Add at least one target keyword", "error");
        return;
    }

    // Save finalized keywords and linking selections to session
    await apiRequest(`/api/blog/wizard/${currentWizardSession}/keywords`, {
        method: "PUT",
        body: { keywords: wizardKeywords },
    });
    await apiRequest(`/api/blog/wizard/${currentWizardSession}/linking`, {
        method: "PUT",
        body: {
            product_ids: wizardSelectedProducts.map((p) => p.id),
            collection_ids: wizardSelectedCollections.map((c) => c.id),
        },
    });

    showWizardStep(3);
    document.getElementById("outline-content").innerHTML =
        '<div class="loading-spinner"><div class="spinner"></div><p>Generating outline...</p></div>';

    try {
        await apiRequest(`/api/blog/wizard/${currentWizardSession}/outline`, { method: "POST" });
        pollWizardSession(currentWizardSession, "outlined", () => renderOutline(currentWizardSession));
    } catch (err) {
        showToast(err.message, "error");
    }
}

async function renderOutline(sessionId) {
    const session = await apiRequest(`/api/blog/wizard/${sessionId}`);
    const outline = session.outline_data || {};
    const container = document.getElementById("outline-content");

    let html = '<div style="max-width: 800px; margin: 0 auto;">';

    html += `<div class="card">
        <h4>Article Structure</h4>
        <div class="form-group">
            <label>Suggested Title</label>
            <input type="text" class="form-control" id="outline-title" value="${escapeHtml(outline.suggested_title || session.topic || "")}">
        </div>
        <div class="form-group">
            <label>Meta Title</label>
            <input type="text" class="form-control" id="outline-meta-title" value="${escapeHtml(outline.meta_title || "")}">
            <small style="color: var(--on-surface-secondary);">${(outline.meta_title || "").length} / 60 chars</small>
        </div>
        <div class="form-group">
            <label>Meta Description</label>
            <textarea class="form-control" id="outline-meta-desc" rows="2">${escapeHtml(outline.meta_description || "")}</textarea>
            <small style="color: var(--on-surface-secondary);">${(outline.meta_description || "").length} / 160 chars</small>
        </div>
    </div>`;

    // Outline sections
    const sections = outline.outline || [];
    html += `<div class="card" style="margin-top: 16px;">
        <h4>Outline</h4>
        <div id="outline-sections">`;
    sections.forEach((s, i) => {
        const indent = s.level === "h3" ? "margin-left: 24px;" : "";
        html += `<div class="outline-item" style="${indent} padding: 8px 12px; border: 1px solid var(--outline); border-radius: 8px; margin-bottom: 8px; display: flex; align-items: center; gap: 8px;">
            <span class="badge ${s.level === "h2" ? "badge-info" : "badge-default"}">${(s.level || "h2").toUpperCase()}</span>
            <input type="text" class="form-control" value="${escapeHtml(s.heading || "")}" style="flex: 1;" data-outline-idx="${i}">
            <small style="white-space: nowrap; color: var(--on-surface-secondary);">${escapeHtml((s.keywords || []).join(", "))}</small>
        </div>`;
    });
    html += `</div></div>`;

    // Internal link plan
    const links = outline.internal_link_plan || [];
    if (links.length) {
        html += `<div class="card" style="margin-top: 16px;">
            <h4>Internal Link Plan</h4>
            <ul>`;
        links.forEach((l) => {
            html += `<li>"${escapeHtml(l.text)}" → <code>${escapeHtml(l.url)}</code> (in ${escapeHtml(l.section || "")})</li>`;
        });
        html += `</ul></div>`;
    }

    html += `<button class="btn btn-primary" onclick="wizardGenerateArticle()" style="margin-top: 20px;">
        <span class="material-symbols-rounded">auto_awesome</span>
        Generate Article
    </button>`;

    html += "</div>";
    container.innerHTML = html;
}

async function wizardGenerateArticle() {
    if (!currentWizardSession) return;

    // Save outline edits
    const outlineInputs = document.querySelectorAll("[data-outline-idx]");
    const session = await apiRequest(`/api/blog/wizard/${currentWizardSession}`);
    const outline = session.outline_data || {};
    const sections = outline.outline || [];
    outlineInputs.forEach((input) => {
        const idx = parseInt(input.dataset.outlineIdx);
        if (sections[idx]) sections[idx].heading = input.value;
    });
    outline.suggested_title = document.getElementById("outline-title")?.value || outline.suggested_title;
    outline.meta_title = document.getElementById("outline-meta-title")?.value || outline.meta_title;
    outline.meta_description = document.getElementById("outline-meta-desc")?.value || outline.meta_description;

    await apiRequest(`/api/blog/wizard/${currentWizardSession}/outline`, {
        method: "PUT",
        body: { outline_data: outline },
    });

    showWizardStep(4);
    document.getElementById("generate-content").innerHTML =
        '<div class="loading-spinner"><div class="spinner"></div><p>Writing your article... This may take a minute.</p></div>';

    try {
        await apiRequest(`/api/blog/wizard/${currentWizardSession}/generate`, { method: "POST" });
        pollWizardSession(currentWizardSession, "generated", () => renderGeneratedArticle(currentWizardSession));
    } catch (err) {
        showToast(err.message, "error");
    }
}

async function renderGeneratedArticle(sessionId) {
    const session = await apiRequest(`/api/blog/wizard/${sessionId}`);
    const article = session.generated_article || {};
    const container = document.getElementById("generate-content");

    let html = '<div style="max-width: 900px; margin: 0 auto;">';

    html += `<div class="card">
        <h4>Article Generated</h4>
        <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 16px;">
            <div style="text-align: center; padding: 12px; background: var(--primary-light); border-radius: 8px;">
                <div style="font-size: 24px; font-weight: 700; color: var(--primary);">${article.word_count || "—"}</div>
                <div style="font-size: 13px; color: var(--on-surface-secondary);">Words</div>
            </div>
            <div style="text-align: center; padding: 12px; background: var(--primary-light); border-radius: 8px;">
                <div style="font-size: 24px; font-weight: 700; color: var(--primary);">${article.internal_links_count || 0}</div>
                <div style="font-size: 13px; color: var(--on-surface-secondary);">Internal Links</div>
            </div>
            <div style="text-align: center; padding: 12px; background: var(--primary-light); border-radius: 8px;">
                <div style="font-size: 24px; font-weight: 700; color: var(--primary);">${(article.keywords_used || []).length}</div>
                <div style="font-size: 13px; color: var(--on-surface-secondary);">Keywords</div>
            </div>
        </div>
    </div>`;

    html += `<button class="btn btn-primary" onclick="wizardReview()" style="margin-top: 16px;">
        <span class="material-symbols-rounded">rate_review</span>
        Review & Edit
    </button>`;

    html += "</div>";
    container.innerHTML = html;

    showWizardStep(5);
    renderReview(sessionId);
}

async function wizardReview() {
    showWizardStep(5);
    if (currentWizardSession) renderReview(currentWizardSession);
}

async function renderReview(sessionId) {
    const session = await apiRequest(`/api/blog/wizard/${sessionId}`);
    const article = session.generated_article || {};
    const container = document.getElementById("review-content");
    const featuredImageUrl = session.featured_image_url || "";

    let html = `<div style="display: grid; grid-template-columns: 1fr 350px; gap: 24px; height: calc(100vh - 250px);">
        <div style="display: flex; flex-direction: column; gap: 16px; overflow-y: auto;">
            <div class="card">
                <h4>Article Content</h4>
                <div class="form-group">
                    <label>Title</label>
                    <input type="text" class="form-control" id="review-title" value="${escapeHtml(article.title || session.topic || "")}">
                </div>
                <div class="form-group">
                    <label>Body HTML</label>
                    <textarea class="form-control" id="review-body" rows="20" style="font-family: monospace; font-size: 13px;">${escapeHtml(article.body_html || "")}</textarea>
                </div>
            </div>
        </div>
        <div style="overflow-y: auto;">
            <div class="card">
                <h4>Featured Image</h4>
                <div id="featured-image-container">
                    ${featuredImageUrl
                        ? `<img src="${escapeHtml(featuredImageUrl)}" alt="Featured image" style="width: 100%; border-radius: 8px; margin-bottom: 12px;">
                           <button class="btn btn-secondary" onclick="wizardGenerateImage()" style="width: 100%;">
                               <span class="material-symbols-rounded">refresh</span>
                               Regenerate Image
                           </button>`
                        : `<div style="border: 2px dashed var(--outline); border-radius: 8px; padding: 24px; text-align: center;">
                               <span class="material-symbols-rounded" style="font-size: 40px; color: var(--on-surface-secondary);">image</span>
                               <p style="color: var(--on-surface-secondary); font-size: 13px; margin: 8px 0 0;">AI-generated blog banner using your title, keywords, and products</p>
                           </div>
                           <button class="btn btn-primary" onclick="wizardGenerateImage()" style="width: 100%; margin-top: 12px;">
                               <span class="material-symbols-rounded">auto_awesome</span>
                               Generate Featured Image
                           </button>`
                    }
                </div>
            </div>
            <div class="card" style="margin-top: 16px;">
                <h4>SEO Fields</h4>
                <div class="form-group">
                    <label>Meta Title</label>
                    <input type="text" class="form-control" id="review-meta-title" value="${escapeHtml(article.meta_title || "")}">
                    <small style="color: var(--on-surface-secondary);">${(article.meta_title || "").length} / 60 chars</small>
                </div>
                <div class="form-group">
                    <label>Meta Description</label>
                    <textarea class="form-control" id="review-meta-desc" rows="3">${escapeHtml(article.meta_description || "")}</textarea>
                    <small style="color: var(--on-surface-secondary);">${(article.meta_description || "").length} / 160 chars</small>
                </div>
                <div class="form-group">
                    <label>Summary / Excerpt</label>
                    <textarea class="form-control" id="review-summary" rows="2">${escapeHtml(article.summary || "")}</textarea>
                </div>
                <div class="form-group">
                    <label>Tags (comma separated)</label>
                    <input type="text" class="form-control" id="review-tags" value="${escapeHtml((article.tags || []).join(", "))}">
                </div>
                <div class="form-group">
                    <label>URL Slug (optional)</label>
                    <input type="text" class="form-control" id="review-handle" value="${escapeHtml(article.handle || "")}" placeholder="auto-generated from title if blank">
                    <small id="review-handle-preview" style="color: var(--on-surface-secondary);"></small>
                </div>
            </div>
            <div class="card" style="margin-top: 16px;">
                <h4>Preview</h4>
                <div id="review-preview" style="max-height: 400px; overflow-y: auto; border: 1px solid var(--outline); border-radius: 8px; padding: 16px;">
                    ${article.body_html || ""}
                </div>
            </div>
        </div>
    </div>
    <div style="margin-top: 16px; display: flex; gap: 8px; align-items: center;">
        <p style="font-size: 13px; color: var(--on-surface-secondary); margin: 0; flex: 1;">Publishing to <strong>News</strong> blog</p>
        <button class="btn btn-primary" onclick="wizardPublish(false)">
            <span class="material-symbols-rounded">save</span>
            Save as Draft
        </button>
        <button class="btn btn-primary" onclick="wizardPublish(true)" style="background: var(--success);">
            <span class="material-symbols-rounded">publish</span>
            Publish Now
        </button>
    </div>`;

    container.innerHTML = html;

    // Live preview
    const bodyTextarea = document.getElementById("review-body");
    if (bodyTextarea) {
        bodyTextarea.addEventListener("input", () => {
            document.getElementById("review-preview").innerHTML = bodyTextarea.value;
        });
    }

    // Live URL slug preview
    const handleInput = document.getElementById("review-handle");
    const handlePreview = document.getElementById("review-handle-preview");
    if (handleInput && handlePreview) {
        const updateHandlePreview = () => {
            const slug = slugify(handleInput.value);
            handlePreview.textContent = `Will publish to: /blogs/news/${slug || "(auto from title)"}`;
        };
        handleInput.addEventListener("input", updateHandlePreview);
        updateHandlePreview();
    }
}

async function wizardGenerateImage() {
    if (!currentWizardSession) return;
    const container = document.getElementById("featured-image-container");
    container.innerHTML = `
        <div style="border: 2px dashed var(--outline); border-radius: 8px; padding: 24px; text-align: center;">
            <div class="spinner" style="margin: 0 auto 12px;"></div>
            <p style="color: var(--on-surface-secondary); font-size: 13px; margin: 0;">Generating featured image...</p>
        </div>`;
    try {
        const { task_id } = await apiRequest(`/api/blog/wizard/${currentWizardSession}/generate-image`, { method: "POST" });
        pollImageGeneration(task_id);
    } catch (err) {
        showToast(err.message, "error");
        container.innerHTML = `
            <div style="border: 2px dashed var(--error); border-radius: 8px; padding: 24px; text-align: center;">
                <span class="material-symbols-rounded" style="font-size: 40px; color: var(--error);">error</span>
                <p style="color: var(--error); font-size: 13px; margin: 8px 0 0;">Image generation failed</p>
            </div>
            <button class="btn btn-primary" onclick="wizardGenerateImage()" style="width: 100%; margin-top: 12px;">
                <span class="material-symbols-rounded">refresh</span>
                Try Again
            </button>`;
    }
}

function pollImageGeneration(taskId) {
    // Capture the session id now — resetWizard() nulls the global mid-poll,
    // which would otherwise make later ticks request /api/blog/wizard/null.
    const sessionId = currentWizardSession;
    if (!sessionId) return;
    const poll = async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            if (task.status === "completed") {
                const session = await apiRequest(`/api/blog/wizard/${sessionId}`);
                const imageUrl = session.featured_image_url || "";
                const container = document.getElementById("featured-image-container");
                if (imageUrl) {
                    container.innerHTML = `
                        <img src="${escapeHtml(imageUrl)}" alt="Featured image" style="width: 100%; border-radius: 8px; margin-bottom: 12px;">
                        <button class="btn btn-secondary" onclick="wizardGenerateImage()" style="width: 100%;">
                            <span class="material-symbols-rounded">refresh</span>
                            Regenerate Image
                        </button>`;
                } else {
                    container.innerHTML = `
                        <div style="border: 2px dashed var(--warning); border-radius: 8px; padding: 24px; text-align: center;">
                            <span class="material-symbols-rounded" style="font-size: 40px; color: var(--warning);">warning</span>
                            <p style="color: var(--on-surface-secondary); font-size: 13px; margin: 8px 0 0;">No image was returned</p>
                        </div>
                        <button class="btn btn-primary" onclick="wizardGenerateImage()" style="width: 100%; margin-top: 12px;">
                            <span class="material-symbols-rounded">refresh</span>
                            Try Again
                        </button>`;
                }
            } else if (task.status === "failed") {
                const container = document.getElementById("featured-image-container");
                container.innerHTML = `
                    <div style="border: 2px dashed var(--error); border-radius: 8px; padding: 24px; text-align: center;">
                        <span class="material-symbols-rounded" style="font-size: 40px; color: var(--error);">error</span>
                        <p style="color: var(--error); font-size: 13px; margin: 8px 0 0;">${escapeHtml(task.error || "Image generation failed")}</p>
                    </div>
                    <button class="btn btn-primary" onclick="wizardGenerateImage()" style="width: 100%; margin-top: 12px;">
                        <span class="material-symbols-rounded">refresh</span>
                        Try Again
                    </button>`;
            } else {
                setTimeout(poll, 2000);
            }
        } catch (err) {
            showToast(err.message, "error");
        }
    };
    poll();
}

async function wizardPublish(isPublished) {
    if (!currentWizardSession) return;

    // Save edits
    const generatedArticle = {
        title: document.getElementById("review-title")?.value || "",
        body_html: document.getElementById("review-body")?.value || "",
        meta_title: document.getElementById("review-meta-title")?.value || "",
        meta_description: document.getElementById("review-meta-desc")?.value || "",
        summary: document.getElementById("review-summary")?.value || "",
        tags: (document.getElementById("review-tags")?.value || "").split(",").map((t) => t.trim()).filter(Boolean),
        handle: slugify(document.getElementById("review-handle")?.value || ""),
    };

    await apiRequest(`/api/blog/wizard/${currentWizardSession}/review`, {
        method: "PUT",
        body: { generated_article: generatedArticle },
    });

    const action = isPublished ? "publish" : "save as draft";
    showBlogConfirmDialog(
        isPublished ? "Publish Article" : "Save as Draft",
        `Are you sure you want to ${action} this article to Shopify?`,
        async () => {
            try {
                const result = await apiRequest(`/api/blog/wizard/${currentWizardSession}/publish`, {
                    method: "POST",
                    body: { is_published: isPublished },
                });

                showWizardStep(6);
                const publishContent = document.getElementById("publish-content");
                publishContent.innerHTML = `
                    <div style="max-width: 500px; margin: 40px auto; text-align: center;">
                        <span class="material-symbols-rounded" style="font-size: 64px; color: var(--success);">check_circle</span>
                        <h3 style="margin-top: 16px;">Article ${isPublished ? "Published" : "Saved as Draft"}!</h3>
                        <p style="color: var(--on-surface-secondary);">Your article has been created on Shopify.</p>
                        <button class="btn btn-primary" onclick="resetWizard()" style="margin-top: 24px;">
                            <span class="material-symbols-rounded">add</span>
                            Create Another
                        </button>
                        <button class="btn btn-secondary" onclick="document.querySelector('[data-tab=articles]').click()" style="margin-top: 24px;">
                            View Articles
                        </button>
                    </div>`;

                currentWizardSession = null;
            } catch (err) {
                showToast(err.message, "error");
            }
        }
    );
}

function resetWizard() {
    currentWizardSession = null;
    if (wizardPollTimer) { clearTimeout(wizardPollTimer); wizardPollTimer = null; }
    document.getElementById("wizard-topic").value = "";
    document.getElementById("wizard-keywords").value = "";
    showWizardStep(1);
}

// ── Confirm Dialog ───────────────────────────────────────────────

function showBlogConfirmDialog(title, message, onConfirm) {
    const overlay = document.getElementById("blog-confirm-dialog-overlay");
    document.getElementById("blog-confirm-dialog-title").textContent = title;
    const msgEl = document.getElementById("blog-confirm-dialog-message");
    msgEl.innerHTML = message;
    const okBtn = document.getElementById("blog-confirm-dialog-ok");
    const newBtn = okBtn.cloneNode(true);
    okBtn.parentNode.replaceChild(newBtn, okBtn);
    newBtn.id = "blog-confirm-dialog-ok";
    newBtn.addEventListener("click", () => {
        closeBlogConfirmDialog();
        onConfirm();
    });
    overlay.classList.add("active");
}

function closeBlogConfirmDialog() {
    document.getElementById("blog-confirm-dialog-overlay").classList.remove("active");
}

// ── Landing Page Generator ─────────────────────────────────────

async function loadLandingPageStatus() {
    const chip = document.getElementById("chip-landing");
    if (!chip) return;

    const blogs = getUniqueBlogsFromArticles();
    const storeId = getSelectedStoreId();
    if (!storeId || blogs.length === 0) {
        chip.style.display = "none";
        return;
    }

    chip.style.display = "";
    const labelEl = document.getElementById("chip-landing-label");
    const blogId = blogs[0].id;

    try {
        const data = await apiRequest(`/api/blog/landing-status?store_id=${storeId}&blog_id=${blogId}`);
        if (data && data.page_handle) {
            labelEl.innerHTML = `Landing Page <span class="chip-count">${data.articles_count || 0}</span>`;
        } else {
            labelEl.textContent = "Landing Page";
        }

        const introEl = document.getElementById("landing-intro-text");
        if (introEl && data && data.intro_text) {
            introEl.value = data.intro_text;
        }
    } catch {
        labelEl.textContent = "Landing Page";
    }
}

async function refreshLandingStatus() {
    loadLandingPageStatus();
}

function getSelectedLandingBlogId() {
    const select = document.getElementById("landing-blog-select");
    if (select) return select.value;
    const blogs = getUniqueBlogsFromArticles();
    return blogs.length > 0 ? blogs[0].id : "";
}

function getUniqueBlogsFromArticles() {
    const seen = new Map();
    for (const a of currentArticles) {
        if (a.blog_id && !seen.has(a.blog_id)) {
            seen.set(a.blog_id, { id: a.blog_id, title: (a.blog_title || "Blog").replace(/\s+/g, ' ').trim() });
        }
    }
    return Array.from(seen.values());
}

async function generateLandingPage() {
    const storeId = getSelectedStoreId();
    const blogId = getSelectedLandingBlogId();
    if (!storeId || !blogId) {
        showToast("No blog available to generate landing page", "error");
        return;
    }

    const introEl = document.getElementById("landing-intro-text");
    const introText = introEl ? introEl.value.trim() : "";

    const chip = document.getElementById("chip-landing");
    const labelEl = document.getElementById("chip-landing-label");
    if (labelEl) labelEl.innerHTML = '<div class="spinner" style="width:14px;height:14px;border-width:2px;"></div> Generating...';

    try {
        const res = await apiRequest("/api/blog/generate-landing", {
            method: "POST",
            body: JSON.stringify({ store_id: parseInt(storeId), blog_id: parseInt(blogId), intro_text: introText }),
        });
        if (res.task_id) {
            pollLandingTask(res.task_id);
        }
    } catch (err) {
        showToast("Failed to start generation: " + err.message, "error");
        refreshLandingStatus();
    }
}

function pollLandingTask(taskId) {
    const interval = setInterval(async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            if (task.status === "completed") {
                clearInterval(interval);
                showToast("Landing page generated!", "success");
                refreshLandingStatus();
            } else if (task.status === "failed") {
                clearInterval(interval);
                showToast("Landing page generation failed: " + (task.error || "Unknown error"), "error");
                refreshLandingStatus();
            }
        } catch (err) {
            clearInterval(interval);
            refreshLandingStatus();
        }
    }, 2000);
}

function getStoreDomain() {
    if (currentArticles.length > 0 && currentArticles[0].store_domain) {
        return currentArticles[0].store_domain;
    }
    return "";
}

function getSelectedStoreId() {
    if (currentArticles.length > 0 && currentArticles[0].store_id) {
        return currentArticles[0].store_id;
    }
    const storeSelect = document.getElementById("store-select");
    if (storeSelect) return storeSelect.value;
    return "";
}

// ── Blog Sync ──────────────────────────────────────────────────

async function syncBlogs() {
    const storeId = await getFirstStoreId();
    if (!storeId) {
        showToast("No store found. Add a store first.", "error");
        return;
    }

    const btn = document.getElementById("sync-blogs-btn");
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<div class="spinner" style="width:18px;height:18px;border-width:2px;"></div> Syncing...`;
    }

    try {
        const res = await apiRequest("/api/blog/sync", {
            method: "POST",
            body: JSON.stringify({ store_id: storeId }),
        });
        if (res.task_id) {
            pollBlogSync(res.task_id);
        }
    } catch (err) {
        showToast("Sync failed: " + err.message, "error");
        resetSyncButton();
    }
}

function pollBlogSync(taskId) {
    const interval = setInterval(async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            if (task.status === "completed") {
                clearInterval(interval);
                const r = task.result || {};
                showToast(`Synced ${r.blogs_synced || 0} blogs, ${r.articles_synced || 0} articles`, "success");
                resetSyncButton();
                loadArticles();
            } else if (task.status === "failed") {
                clearInterval(interval);
                showToast("Sync failed: " + (task.error || "Unknown error"), "error");
                resetSyncButton();
            }
        } catch (err) {
            clearInterval(interval);
            resetSyncButton();
        }
    }, 2000);
}

function resetSyncButton() {
    const btn = document.getElementById("sync-blogs-btn");
    if (btn) {
        btn.disabled = false;
        btn.innerHTML = `<span class="material-symbols-rounded">sync</span> Refresh from Shopify`;
    }
}

// ── Empty Blog Cleanup ──────────────────────────────────────────

async function loadEmptyBlogsStatus() {
    const chip = document.getElementById("chip-empty-blogs");
    if (!chip) return;

    const storeId = getSelectedStoreId();
    if (!storeId) { chip.style.display = "none"; return; }

    try {
        const emptyBlogs = await apiRequest(`/api/blog/empty-blogs?store_id=${storeId}`);
        if (emptyBlogs.length > 0) {
            chip.style.display = "";
            document.getElementById("chip-empty-label").textContent = `${emptyBlogs.length} Empty`;
        } else {
            chip.style.display = "none";
        }
    } catch {
        chip.style.display = "none";
    }
}

async function cleanupEmptyBlogs() {
    const storeId = getSelectedStoreId();
    if (!storeId) return;

    try {
        const emptyBlogs = await apiRequest(`/api/blog/empty-blogs?store_id=${storeId}`);
        if (emptyBlogs.length === 0) {
            showToast("No empty blogs to clean up", "success");
            return;
        }
        const names = emptyBlogs.map((b) => b.title).join(", ");
        showBlogConfirmDialog(
            "Delete Empty Blogs",
            `Delete ${emptyBlogs.length} empty blog(s) from Shopify?<br><br><strong>${escapeHtml(names)}</strong><br><br>This removes them from your store and sitemap.`,
            async () => {
                try {
                    const res = await apiRequest("/api/blog/cleanup-empty-blogs", {
                        method: "POST",
                        body: { store_id: parseInt(storeId) },
                    });
                    showToast(`Deleted ${res.count} empty blog(s)`, "success");
                    document.getElementById("chip-empty-blogs").style.display = "none";
                } catch (err) {
                    showToast(err.message, "error");
                }
            }
        );
    } catch (err) {
        showToast(err.message, "error");
    }
}

// ── Blog SEO Settings Card ──────────────────────────────────────

async function loadBlogSeoCard() {
    const card = document.getElementById("blog-seo-card");
    const toolbar = document.getElementById("blog-toolbar");

    const blogs = getUniqueBlogsFromArticles();
    const newsBlog = blogs.find((b) => {
        const article = currentArticles.find((a) => a.blog_id === b.id);
        return article && article.blog_handle === "news";
    });

    if (!newsBlog) {
        if (toolbar) toolbar.style.display = "none";
        if (card) card.style.display = "none";
        return;
    }

    if (toolbar) toolbar.style.display = "";
    if (card) {
        card.style.display = "none";
        card.dataset.blogId = newsBlog.id;
    }

    const cleanTitle = newsBlog.title.replace(/\s+/g, ' ').trim();
    const blogNameEl = document.getElementById("toolbar-blog-name");
    if (blogNameEl) blogNameEl.textContent = cleanTitle;

    const input = document.getElementById("blog-rename-input");
    if (input) input.value = cleanTitle;

    const storeId = getSelectedStoreId();
    if (storeId) {
        try {
            const data = await apiRequest(`/api/blog/landing-status?store_id=${storeId}&blog_id=${newsBlog.id}`);
            const introEl = document.getElementById("landing-intro-text");
            if (introEl && data && data.intro_text) {
                introEl.value = data.intro_text;
            }
            renderHeroImagePreview(data);
        } catch {}
    }

    initMetaCharCounters();

    const savedOpen = loadFilter("blogSettingsOpen", false);
    const panel = document.getElementById("blog-settings-panel");
    const gearBtn = document.getElementById("settings-gear-btn");
    if (savedOpen && panel) {
        panel.classList.add("open");
        toolbar.classList.add("panel-open");
        if (gearBtn) gearBtn.classList.add("active");
    }
}

async function renameBlog() {
    const card = document.getElementById("blog-seo-card");
    const blogId = card ? card.dataset.blogId : null;
    const input = document.getElementById("blog-rename-input");
    const title = input ? input.value.replace(/\s+/g, ' ').trim() : "";

    if (!blogId || !title) {
        showToast("Please enter a blog title", "error");
        return;
    }

    try {
        await apiRequest(`/api/blog/blogs/${blogId}`, {
            method: "PUT",
            body: { title },
        });
        showToast("Blog title updated to: " + title, "success");
        loadArticles();
    } catch (err) {
        showToast(err.message, "error");
    }
}

async function saveIntroText() {
    const card = document.getElementById("blog-seo-card");
    const blogId = card ? card.dataset.blogId : null;
    const storeId = getSelectedStoreId();
    const introEl = document.getElementById("landing-intro-text");
    const introText = introEl ? introEl.value.trim() : "";

    if (!storeId || !blogId) {
        showToast("No blog available", "error");
        return;
    }

    try {
        await apiRequest("/api/blog/save-intro", {
            method: "POST",
            body: { store_id: parseInt(storeId), blog_id: parseInt(blogId), intro_text: introText },
        });
        showToast("Intro paragraph saved", "success");
    } catch (err) {
        showToast(err.message, "error");
    }
}

function renderHeroImagePreview(data) {
    const preview = document.getElementById("hero-image-preview");
    const generateBtn = document.getElementById("generate-hero-btn");
    const removeBtn = document.getElementById("remove-hero-btn");
    if (!preview) return;

    if (generateBtn) generateBtn.disabled = false;

    if (data && data.hero_image_url) {
        preview.innerHTML = `<img src="${data.hero_image_url}" alt="${data.hero_image_alt || 'Blog hero banner'}">`;
        if (generateBtn) generateBtn.innerHTML = '<span class="material-symbols-rounded">auto_awesome</span> Regenerate Hero';
        if (removeBtn) removeBtn.style.display = "";
    } else {
        preview.innerHTML = '<p style="font-size: 12px; color: var(--on-surface-secondary); margin: 0;">No hero image. Click Generate to create one.</p>';
        if (generateBtn) generateBtn.innerHTML = '<span class="material-symbols-rounded">auto_awesome</span> Generate Hero';
        if (removeBtn) removeBtn.style.display = "none";
    }
}

async function generateHeroImage() {
    const card = document.getElementById("blog-seo-card");
    const blogId = card ? card.dataset.blogId : null;
    const storeId = getSelectedStoreId();
    if (!storeId || !blogId) {
        showToast("No blog available", "error");
        return;
    }

    const generateBtn = document.getElementById("generate-hero-btn");
    if (generateBtn) {
        generateBtn.disabled = true;
        generateBtn.innerHTML = '<span class="material-symbols-rounded rotating">progress_activity</span> Generating...';
    }

    try {
        const resp = await apiRequest("/api/blog/generate-hero-image", {
            method: "POST",
            body: { store_id: parseInt(storeId), blog_id: parseInt(blogId) },
        });
        if (resp.task_id) {
            pollHeroImageTask(resp.task_id);
        }
    } catch (err) {
        showToast(err.message, "error");
        if (generateBtn) {
            generateBtn.disabled = false;
            generateBtn.innerHTML = '<span class="material-symbols-rounded">auto_awesome</span> Generate Hero Image';
        }
    }
}

function pollHeroImageTask(taskId) {
    const interval = setInterval(async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            if (task.status === "completed") {
                clearInterval(interval);
                showToast("Hero image generated!", "success");
                loadBlogSeoCard();
            } else if (task.status === "failed") {
                clearInterval(interval);
                showToast("Hero image generation failed: " + (task.error || "Unknown error"), "error");
                const generateBtn = document.getElementById("generate-hero-btn");
                if (generateBtn) {
                    generateBtn.disabled = false;
                    generateBtn.innerHTML = '<span class="material-symbols-rounded">auto_awesome</span> Generate Hero Image';
                }
            }
        } catch {
            clearInterval(interval);
        }
    }, 3000);
}

async function removeHeroImage() {
    const card = document.getElementById("blog-seo-card");
    const blogId = card ? card.dataset.blogId : null;
    const storeId = getSelectedStoreId();
    if (!storeId || !blogId) return;

    try {
        await apiRequest("/api/blog/remove-hero-image", {
            method: "POST",
            body: { store_id: parseInt(storeId), blog_id: parseInt(blogId) },
        });
        showToast("Hero image removed", "success");
        loadBlogSeoCard();
    } catch (err) {
        showToast(err.message, "error");
    }
}

async function getAISuggestions() {
    const card = document.getElementById("blog-seo-card");
    const blogId = card ? card.dataset.blogId : null;
    const storeId = getSelectedStoreId();

    if (!storeId || !blogId) {
        showToast("No blog available", "error");
        return;
    }

    const btn = document.getElementById("ai-suggest-btn");
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<div class="spinner" style="width:16px;height:16px;border-width:2px;"></div> Researching...';
    }

    const titleSugDiv = document.getElementById("ai-title-suggestion");
    const introSugDiv = document.getElementById("ai-intro-suggestion");
    const metaTitleSugDiv = document.getElementById("ai-meta-title-suggestion");
    const metaDescSugDiv = document.getElementById("ai-meta-desc-suggestion");
    if (titleSugDiv) { titleSugDiv.style.display = "none"; titleSugDiv.innerHTML = ""; }
    if (introSugDiv) { introSugDiv.style.display = "none"; introSugDiv.innerHTML = ""; }
    if (metaTitleSugDiv) { metaTitleSugDiv.style.display = "none"; metaTitleSugDiv.innerHTML = ""; }
    if (metaDescSugDiv) { metaDescSugDiv.style.display = "none"; metaDescSugDiv.innerHTML = ""; }

    try {
        const res = await apiRequest("/api/blog/ai-suggestions", {
            method: "POST",
            body: { store_id: parseInt(storeId), blog_id: parseInt(blogId) },
        });
        if (res.task_id) {
            pollAISuggestionsTask(res.task_id);
        }
    } catch (err) {
        showToast("Failed to start AI suggestions: " + err.message, "error");
        resetAISuggestBtn();
    }
}

function pollAISuggestionsTask(taskId) {
    const poll = async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            if (task.status === "completed" && task.result) {
                renderAISuggestions(task.result);
                resetAISuggestBtn();
            } else if (task.status === "failed") {
                showToast("AI suggestions failed: " + (task.error || "Unknown error"), "error");
                resetAISuggestBtn();
            } else {
                setTimeout(poll, 2000);
            }
        } catch (err) {
            showToast(err.message, "error");
            resetAISuggestBtn();
        }
    };
    poll();
}

function resetAISuggestBtn() {
    const btn = document.getElementById("ai-suggest-btn");
    if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded">auto_awesome</span> AI Suggest';
    }
}

function renderAISuggestions(result) {
    const titleSugDiv = document.getElementById("ai-title-suggestion");
    const introSugDiv = document.getElementById("ai-intro-suggestion");

    if (result.suggested_blog_title && titleSugDiv) {
        titleSugDiv.style.display = "";
        titleSugDiv.innerHTML = `
            <div style="background: var(--primary-light); border-radius: 8px; padding: 12px;">
                <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                    <span class="material-symbols-rounded" style="font-size: 18px; color: var(--primary);">lightbulb</span>
                    <strong style="font-size: 13px;">Suggested Title</strong>
                </div>
                <p style="margin: 0 0 8px; font-size: 14px;">${escapeHtml(result.suggested_blog_title)}</p>
                ${result.blog_title_reasoning ? `<p style="margin: 0 0 8px; font-size: 12px; color: var(--on-surface-secondary);">${escapeHtml(result.blog_title_reasoning)}</p>` : ""}
                ${result.keywords_targeted ? `<p style="margin: 0 0 8px; font-size: 12px; color: var(--on-surface-secondary);"><strong>Keywords:</strong> ${escapeHtml(result.keywords_targeted.join(", "))}</p>` : ""}
                <button class="btn btn-primary btn-sm" onclick="useAISuggestedTitle()">
                    <span class="material-symbols-rounded">check</span>
                    Use This Title
                </button>
            </div>`;
    }

    if (result.suggested_intro && introSugDiv) {
        introSugDiv.style.display = "";
        introSugDiv.innerHTML = `
            <div style="background: var(--primary-light); border-radius: 8px; padding: 12px;">
                <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                    <span class="material-symbols-rounded" style="font-size: 18px; color: var(--primary);">lightbulb</span>
                    <strong style="font-size: 13px;">Suggested Intro Paragraph</strong>
                </div>
                <p style="margin: 0 0 8px; font-size: 14px;">${escapeHtml(result.suggested_intro)}</p>
                ${result.intro_reasoning ? `<p style="margin: 0 0 8px; font-size: 12px; color: var(--on-surface-secondary);">${escapeHtml(result.intro_reasoning)}</p>` : ""}
                <button class="btn btn-primary btn-sm" onclick="useAISuggestedIntro()">
                    <span class="material-symbols-rounded">check</span>
                    Use This Intro
                </button>
            </div>`;
    }

    const metaTitleSugDiv = document.getElementById("ai-meta-title-suggestion");
    if (result.suggested_meta_title && metaTitleSugDiv) {
        metaTitleSugDiv.style.display = "";
        metaTitleSugDiv.innerHTML = `
            <div style="background: var(--primary-light); border-radius: 8px; padding: 12px;">
                <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                    <span class="material-symbols-rounded" style="font-size: 18px; color: var(--primary);">lightbulb</span>
                    <strong style="font-size: 13px;">Suggested Meta Title</strong>
                    <span style="font-size: 11px; color: var(--on-surface-secondary);">(${result.suggested_meta_title.length} chars)</span>
                </div>
                <p style="margin: 0 0 8px; font-size: 14px;">${escapeHtml(result.suggested_meta_title)}</p>
                <button class="btn btn-primary btn-sm" onclick="useAISuggestedMetaTitle()">
                    <span class="material-symbols-rounded">check</span>
                    Use This Meta Title
                </button>
            </div>`;
    }

    const metaDescSugDiv = document.getElementById("ai-meta-desc-suggestion");
    if (result.suggested_meta_description && metaDescSugDiv) {
        metaDescSugDiv.style.display = "";
        metaDescSugDiv.innerHTML = `
            <div style="background: var(--primary-light); border-radius: 8px; padding: 12px;">
                <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                    <span class="material-symbols-rounded" style="font-size: 18px; color: var(--primary);">lightbulb</span>
                    <strong style="font-size: 13px;">Suggested Meta Description</strong>
                    <span style="font-size: 11px; color: var(--on-surface-secondary);">(${result.suggested_meta_description.length} chars)</span>
                </div>
                <p style="margin: 0 0 8px; font-size: 14px;">${escapeHtml(result.suggested_meta_description)}</p>
                <button class="btn btn-primary btn-sm" onclick="useAISuggestedMetaDesc()">
                    <span class="material-symbols-rounded">check</span>
                    Use This Meta Description
                </button>
            </div>`;
    }

    window._aiSuggestedTitle = result.suggested_blog_title || "";
    window._aiSuggestedIntro = result.suggested_intro || "";
    window._aiSuggestedMetaTitle = result.suggested_meta_title || "";
    window._aiSuggestedMetaDesc = result.suggested_meta_description || "";
}

function useAISuggestedTitle() {
    const input = document.getElementById("blog-rename-input");
    if (input && window._aiSuggestedTitle) {
        input.value = window._aiSuggestedTitle;
        showToast("Title applied — click Save Title to push to Shopify", "success");
    }
}

function useAISuggestedIntro() {
    const textarea = document.getElementById("landing-intro-text");
    if (textarea && window._aiSuggestedIntro) {
        textarea.value = window._aiSuggestedIntro;
        showToast("Intro applied — click Save Intro to persist", "success");
    }
}

function useAISuggestedMetaTitle() {
    const input = document.getElementById("blog-meta-title");
    if (input && window._aiSuggestedMetaTitle) {
        input.value = window._aiSuggestedMetaTitle;
        updateMetaCharCount("blog-meta-title", "blog-meta-title-count", 60);
        showToast("Meta title applied — click Save Meta Tags to push to Shopify", "success");
    }
}

function useAISuggestedMetaDesc() {
    const textarea = document.getElementById("blog-meta-description");
    if (textarea && window._aiSuggestedMetaDesc) {
        textarea.value = window._aiSuggestedMetaDesc;
        updateMetaCharCount("blog-meta-description", "blog-meta-desc-count", 160);
        showToast("Meta description applied — click Save Meta Tags to push to Shopify", "success");
    }
}

function updateMetaCharCount(inputId, countId, target) {
    const input = document.getElementById(inputId);
    const counter = document.getElementById(countId);
    if (input && counter) {
        const len = input.value.length;
        counter.textContent = `${len}/${target}`;
        counter.style.color = len > target ? "var(--error)" : "var(--on-surface-secondary)";
    }
}

function initMetaCharCounters() {
    const metaTitle = document.getElementById("blog-meta-title");
    const metaDesc = document.getElementById("blog-meta-description");
    if (metaTitle) {
        metaTitle.addEventListener("input", () => updateMetaCharCount("blog-meta-title", "blog-meta-title-count", 60));
    }
    if (metaDesc) {
        metaDesc.addEventListener("input", () => updateMetaCharCount("blog-meta-description", "blog-meta-desc-count", 160));
    }
}

async function saveBlogSeoMeta() {
    const card = document.getElementById("blog-seo-card");
    const blogId = card ? card.dataset.blogId : null;
    const input = document.getElementById("blog-rename-input");
    const title = input ? input.value.replace(/\s+/g, ' ').trim() : "";
    const metaTitle = (document.getElementById("blog-meta-title")?.value || "").trim();
    const metaDesc = (document.getElementById("blog-meta-description")?.value || "").trim();

    if (!blogId || !title) {
        showToast("Blog title is required", "error");
        return;
    }

    try {
        await apiRequest(`/api/blog/blogs/${blogId}`, {
            method: "PUT",
            body: { title, meta_title: metaTitle, meta_description: metaDesc },
        });
        showToast("Blog title and meta tags saved", "success");
        loadArticles();
    } catch (err) {
        showToast(err.message, "error");
    }
}

async function refreshBlogImages() {
    const card = document.getElementById("blog-seo-card");
    const blogId = card ? card.dataset.blogId : null;
    const storeId = getSelectedStoreId();

    if (!storeId || !blogId) {
        showToast("No blog available", "error");
        return;
    }

    try {
        const res = await apiRequest("/api/blog/refresh-images", {
            method: "POST",
            body: { store_id: parseInt(storeId), blog_id: parseInt(blogId) },
        });
        showToast(`Images refreshed — ${res.updated || 0} updated`, "success");
    } catch (err) {
        showToast("Failed to refresh images: " + err.message, "error");
    }
}

// ── Theme SEO Snippet ──────────────────────────────────────────

async function loadSeoSnippetStatus() {
    const chip = document.getElementById("chip-theme-seo");
    if (!chip) return;

    const storeId = getSelectedStoreId();
    if (!storeId) { chip.style.display = "none"; return; }

    chip.style.display = "";
    const dot = document.getElementById("chip-seo-dot");

    try {
        const status = await apiRequest(`/api/blog/seo-snippet-status?store_id=${storeId}`);
        chip.classList.remove("chip-installed", "chip-warning");
        if (status.installed) {
            if (status.outdated) {
                chip.classList.add("chip-warning");
                chip.title = "Update available — click to update";
            } else {
                chip.classList.add("chip-installed");
                chip.title = "SEO snippet installed";
            }
            if (dot) dot.style.display = "";
        } else {
            chip.title = "SEO snippet not installed";
            if (dot) dot.style.display = "none";
        }
    } catch {
        chip.classList.remove("chip-installed", "chip-warning");
        chip.title = "";
        if (dot) dot.style.display = "none";
    }
}

async function pushSeoSnippet() {
    const storeId = getSelectedStoreId();
    if (!storeId) return;

    showBlogConfirmDialog(
        "Push Theme SEO Fixes",
        "This will add a Liquid snippet to your theme that fixes:<br><br>" +
        "&#8226; BlogPosting structured data (JSON-LD)<br>" +
        "&#8226; Duplicate meta description tags<br>" +
        "&#8226; font-display: swap for web fonts<br>" +
        "&#8226; Twitter meta tags<br>" +
        "&#8226; og:type for blog pages + Blog schema<br>" +
        "&#8226; WebSite schema with correct name<br><br>" +
        "A <code>snippets/seo-fixes.liquid</code> file will be created and rendered in <code>layout/theme.liquid</code>.",
        async () => {
            try {
                await apiRequest("/api/blog/push-seo-snippet", {
                    method: "POST",
                    body: { store_id: parseInt(storeId) },
                });
                showToast("SEO snippet pushed to theme", "success");
                loadSeoSnippetStatus();
            } catch (err) {
                showToast(err.message, "error");
            }
        }
    );
}

async function revertSeoSnippet() {
    const storeId = getSelectedStoreId();
    if (!storeId) return;

    showBlogConfirmDialog(
        "Revert Theme SEO Fixes",
        "This will remove the <code>{% render 'seo-fixes' %}</code> line from your theme layout. The snippet file will remain but won't be rendered.",
        async () => {
            try {
                await apiRequest("/api/blog/revert-seo-snippet", {
                    method: "POST",
                    body: { store_id: parseInt(storeId) },
                });
                showToast("SEO snippet reverted", "success");
                loadSeoSnippetStatus();
            } catch (err) {
                showToast(err.message, "error");
            }
        }
    );
}

// ── URL Redirects ─────────────────────────────────────────────

async function detectOldBlogs() {
    const storeId = getSelectedStoreId();
    if (!storeId) return;

    const container = document.getElementById("redirects-list");
    if (!container) return;

    container.innerHTML = '<div class="loading-spinner"><div class="spinner"></div><p>Detecting old URLs...</p></div>';

    try {
        const data = await apiRequest(`/api/blog/detect-old-blogs?store_id=${storeId}`);
        const redirects = data.redirects || [];

        if (redirects.length === 0) {
            container.innerHTML = '<p style="font-size: 13px; color: var(--on-surface-secondary);">No old blog URLs detected.</p>';
            return;
        }

        let html = '<div style="margin-top: 8px;">';
        redirects.forEach((r, i) => {
            html += `<label style="display: flex; align-items: center; gap: 8px; padding: 4px 0; font-size: 13px; cursor: pointer;">
                <input type="checkbox" class="redirect-check" data-path="${escapeHtml(r.path)}" data-target="${escapeHtml(r.target)}" checked>
                <code>${escapeHtml(r.path)}</code> <span style="color: var(--on-surface-secondary);">&rarr;</span> <code>${escapeHtml(r.target)}</code>
            </label>`;
        });
        html += '</div>';
        html += `<button class="btn btn-primary btn-sm" onclick="createRedirects()" style="margin-top: 8px;">
            <span class="material-symbols-rounded">redo</span>
            Create Redirects
        </button>`;
        container.innerHTML = html;
    } catch (err) {
        container.innerHTML = `<p style="font-size: 13px; color: var(--error);">${escapeHtml(err.message)}</p>`;
    }
}

async function createRedirects() {
    const storeId = getSelectedStoreId();
    if (!storeId) return;

    const checks = document.querySelectorAll(".redirect-check:checked");
    if (checks.length === 0) {
        showToast("No redirects selected", "error");
        return;
    }

    const redirects = [];
    checks.forEach((cb) => {
        redirects.push({ path: cb.dataset.path, target: cb.dataset.target });
    });

    try {
        const result = await apiRequest("/api/blog/create-redirects", {
            method: "POST",
            body: { store_id: parseInt(storeId), redirects },
        });
        showToast(`Created ${result.created} redirect(s)${result.failed ? `, ${result.failed} failed` : ""}`, result.failed ? "warning" : "success");
        detectOldBlogs();
    } catch (err) {
        showToast(err.message, "error");
    }
}

// ── Refresh Links ─────────────────────────────────────────────────

let refreshSelectedProducts = [];
let refreshSelectedCollections = [];
let refreshArticleId = null;
let refreshStoreId = null;
let refreshOriginalBody = null;
let refreshProductSearchTimer = null;
let refreshCollectionSearchTimer = null;
let refreshProductResults = [];
let refreshCollectionResults = [];

async function openRefreshLinksPanel(articleId, storeId) {
    refreshArticleId = articleId;
    refreshStoreId = storeId;
    refreshSelectedProducts = [];
    refreshSelectedCollections = [];
    refreshOriginalBody = document.getElementById("article-edit-body").value;

    const container = document.getElementById("refresh-links-panel-container");
    container.innerHTML = '<div class="refresh-links-panel"><div class="loading-spinner"><div class="spinner"></div><p>Loading current links...</p></div></div>';

    try {
        const data = await apiRequest(`/api/blog/articles/${articleId}/current-links`);
        renderRefreshLinksPanel(container, data);
    } catch (err) {
        container.innerHTML = `<div class="refresh-links-panel"><p style="color: var(--error);">${escapeHtml(err.message)}</p></div>`;
    }
}

function renderRefreshLinksPanel(container, linksData) {
    const links = linksData.links || [];
    const internalLinks = links.filter(l => l.type !== "external");

    let linksHtml = "";
    if (internalLinks.length > 0) {
        linksHtml = `<ul class="current-links-list">` +
            internalLinks.map(l => `<li>
                <span class="link-type-badge ${l.type}">${l.type}</span>
                <span style="flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(l.anchor_text)}</span>
                <span style="color: var(--on-surface-secondary); font-size: 12px;">${escapeHtml(l.url)}</span>
            </li>`).join("") +
            `</ul>`;
    } else {
        linksHtml = `<p style="color: var(--on-surface-secondary); font-size: 13px; margin-bottom: 16px;">No internal links found in this article.</p>`;
    }

    container.innerHTML = `
        <div class="refresh-links-panel">
            <h4>
                <span class="material-symbols-rounded">link</span>
                Refresh Internal Links
                <button class="btn btn-sm" style="margin-left: auto; padding: 4px 8px;" onclick="closeRefreshLinksPanel()">
                    <span class="material-symbols-rounded" style="font-size: 18px;">close</span>
                </button>
            </h4>

            <div style="margin-bottom: 16px;">
                <label style="font-weight: 600; font-size: 13px; margin-bottom: 6px; display: block;">Current Internal Links (${internalLinks.length})</label>
                ${linksHtml}
            </div>

            <div style="margin-bottom: 16px;">
                <label style="font-weight: 600; font-size: 13px; margin-bottom: 6px; display: block;">Add Products to Link</label>
                <div class="refresh-search-group">
                    <input type="text" id="refresh-product-search" class="form-control" placeholder="Search products..."
                        oninput="debounceRefreshProductSearch(this.value)" autocomplete="off">
                    <div id="refresh-product-dropdown" class="autocomplete-dropdown" style="display: none;"></div>
                </div>
                <div id="refresh-product-chips" class="refresh-chips"></div>
            </div>

            <div style="margin-bottom: 16px;">
                <label style="font-weight: 600; font-size: 13px; margin-bottom: 6px; display: block;">Add Collections to Link</label>
                <div class="refresh-search-group">
                    <input type="text" id="refresh-collection-search" class="form-control" placeholder="Search collections..."
                        oninput="debounceRefreshCollectionSearch(this.value)" autocomplete="off">
                    <div id="refresh-collection-dropdown" class="autocomplete-dropdown" style="display: none;"></div>
                </div>
                <div id="refresh-collection-chips" class="refresh-chips"></div>
            </div>

            <button class="btn btn-primary" id="refresh-links-btn" onclick="startRefreshLinks()">
                <span class="material-symbols-rounded">auto_awesome</span>
                Regenerate Body with Links
            </button>

            <div id="refresh-links-result"></div>
        </div>`;
}

function closeRefreshLinksPanel() {
    document.getElementById("refresh-links-panel-container").innerHTML = "";
    refreshSelectedProducts = [];
    refreshSelectedCollections = [];
}

function debounceRefreshProductSearch(query) {
    if (refreshProductSearchTimer) clearTimeout(refreshProductSearchTimer);
    const dropdown = document.getElementById("refresh-product-dropdown");
    if (!query.trim()) { dropdown.style.display = "none"; return; }
    refreshProductSearchTimer = setTimeout(() => searchRefreshProducts(query.trim()), 300);
}

function debounceRefreshCollectionSearch(query) {
    if (refreshCollectionSearchTimer) clearTimeout(refreshCollectionSearchTimer);
    const dropdown = document.getElementById("refresh-collection-dropdown");
    if (!query.trim()) { dropdown.style.display = "none"; return; }
    refreshCollectionSearchTimer = setTimeout(() => searchRefreshCollections(query.trim()), 300);
}

async function searchRefreshProducts(query) {
    const dropdown = document.getElementById("refresh-product-dropdown");
    try {
        const results = await apiRequest(`/api/blog/wizard/search/products?store_id=${refreshStoreId}&q=${encodeURIComponent(query)}`);
        refreshProductResults = results;
        if (!results.length) {
            dropdown.innerHTML = '<div class="autocomplete-item" style="color: var(--on-surface-secondary);">No products found</div>';
        } else {
            dropdown.innerHTML = results.map((p, i) => {
                const already = refreshSelectedProducts.some(s => s.id === p.id);
                return `<div class="autocomplete-item${already ? " disabled" : ""}" onclick="${already ? "" : `addRefreshProduct(${i})`}">
                    <div>${escapeHtml(p.title)}${already ? ' <span style="color: var(--success); font-size: 12px;">(added)</span>' : ""}</div>
                    <div class="autocomplete-item-meta">${escapeHtml(p.vendor || "")}${p.vendor && p.product_type ? " · " : ""}${escapeHtml(p.product_type || "")}</div>
                </div>`;
            }).join("");
        }
        dropdown.style.display = "block";
    } catch (err) {
        dropdown.style.display = "none";
    }
}

async function searchRefreshCollections(query) {
    const dropdown = document.getElementById("refresh-collection-dropdown");
    try {
        const results = await apiRequest(`/api/blog/wizard/search/collections?store_id=${refreshStoreId}&q=${encodeURIComponent(query)}`);
        refreshCollectionResults = results;
        if (!results.length) {
            dropdown.innerHTML = '<div class="autocomplete-item" style="color: var(--on-surface-secondary);">No collections found</div>';
        } else {
            dropdown.innerHTML = results.map((c, i) => {
                const already = refreshSelectedCollections.some(s => s.id === c.id);
                return `<div class="autocomplete-item${already ? " disabled" : ""}" onclick="${already ? "" : `addRefreshCollection(${i})`}">
                    <div>${escapeHtml(c.title)}${already ? ' <span style="color: var(--success); font-size: 12px;">(added)</span>' : ""}</div>
                    <div class="autocomplete-item-meta">${escapeHtml(c.collection_type || "")}${c.products_count != null ? ` · ${c.products_count} products` : ""}</div>
                </div>`;
            }).join("");
        }
        dropdown.style.display = "block";
    } catch (err) {
        dropdown.style.display = "none";
    }
}

function addRefreshProduct(idx) {
    const p = refreshProductResults[idx];
    if (!p) return;
    if (refreshSelectedProducts.some(s => s.id === p.id)) return;
    refreshSelectedProducts.push({ id: p.id, title: p.title, handle: p.handle });
    renderRefreshProductChips();
    document.getElementById("refresh-product-search").value = "";
    document.getElementById("refresh-product-dropdown").style.display = "none";
}

function addRefreshCollection(idx) {
    const c = refreshCollectionResults[idx];
    if (!c) return;
    if (refreshSelectedCollections.some(s => s.id === c.id)) return;
    refreshSelectedCollections.push({ id: c.id, title: c.title, handle: c.handle });
    renderRefreshCollectionChips();
    document.getElementById("refresh-collection-search").value = "";
    document.getElementById("refresh-collection-dropdown").style.display = "none";
}

function removeRefreshProduct(id) {
    refreshSelectedProducts = refreshSelectedProducts.filter(p => p.id !== id);
    renderRefreshProductChips();
}

function removeRefreshCollection(id) {
    refreshSelectedCollections = refreshSelectedCollections.filter(c => c.id !== id);
    renderRefreshCollectionChips();
}

function renderRefreshProductChips() {
    const container = document.getElementById("refresh-product-chips");
    if (!container) return;
    container.innerHTML = refreshSelectedProducts.map(p =>
        `<span class="refresh-chip">
            ${escapeHtml(p.title)}
            <span class="remove-chip" onclick="removeRefreshProduct(${p.id})">&times;</span>
        </span>`
    ).join("");
}

function renderRefreshCollectionChips() {
    const container = document.getElementById("refresh-collection-chips");
    if (!container) return;
    container.innerHTML = refreshSelectedCollections.map(c =>
        `<span class="refresh-chip">
            ${escapeHtml(c.title)}
            <span class="remove-chip" onclick="removeRefreshCollection(${c.id})">&times;</span>
        </span>`
    ).join("");
}

async function startRefreshLinks() {
    if (!refreshSelectedProducts.length && !refreshSelectedCollections.length) {
        showToast("Select at least one product or collection to link", "error");
        return;
    }

    const btn = document.getElementById("refresh-links-btn");
    const resultDiv = document.getElementById("refresh-links-result");
    btn.disabled = true;
    btn.innerHTML = '<div class="spinner" style="width: 16px; height: 16px;"></div> Generating...';
    resultDiv.innerHTML = '<div style="margin-top: 16px;"><div class="loading-spinner"><div class="spinner"></div><p>AI is adding internal links...</p></div></div>';

    try {
        const { task_id } = await apiRequest(`/api/blog/articles/${refreshArticleId}/refresh-links`, {
            method: "POST",
            body: {
                product_ids: refreshSelectedProducts.map(p => p.id),
                collection_ids: refreshSelectedCollections.map(c => c.id),
            },
        });
        pollRefreshLinksTask(task_id);
    } catch (err) {
        resultDiv.innerHTML = `<p style="color: var(--error); margin-top: 12px;">${escapeHtml(err.message)}</p>`;
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded">auto_awesome</span> Regenerate Body with Links';
    }
}

function pollRefreshLinksTask(taskId) {
    const resultDiv = document.getElementById("refresh-links-result");
    const btn = document.getElementById("refresh-links-btn");
    const poll = async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            if (task.status === "completed" && task.result) {
                btn.disabled = false;
                btn.innerHTML = '<span class="material-symbols-rounded">auto_awesome</span> Regenerate Body with Links';
                renderRefreshComparison(resultDiv, task.result);
            } else if (task.status === "failed") {
                btn.disabled = false;
                btn.innerHTML = '<span class="material-symbols-rounded">auto_awesome</span> Regenerate Body with Links';
                resultDiv.innerHTML = `<p style="color: var(--error); margin-top: 12px;">Failed: ${escapeHtml(task.error || "Unknown error")}</p>`;
            } else {
                setTimeout(poll, 2000);
            }
        } catch (err) {
            btn.disabled = false;
            btn.innerHTML = '<span class="material-symbols-rounded">auto_awesome</span> Regenerate Body with Links';
            resultDiv.innerHTML = `<p style="color: var(--error); margin-top: 12px;">${escapeHtml(err.message)}</p>`;
        }
    };
    poll();
}

function renderRefreshComparison(container, result) {
    const newBody = result.refreshed_body || "";
    const stats = result.stats || {};

    const origText = (refreshOriginalBody || "").replace(/<[^>]+>/g, "");
    const origWordCount = origText.split(/\s+/).filter(Boolean).length;
    const origLinksCount = ((refreshOriginalBody || "").match(/<a\s[^>]*href\s*=\s*["']\/[^"']*/gi) || []).length;

    container.innerHTML = `
        <div style="margin-top: 16px;">
            <div class="stats-diff-bar">
                <div class="stat-item">
                    <span>Internal Links:</span>
                    <strong>${origLinksCount}</strong>
                    <span class="stat-arrow">&rarr;</span>
                    <strong>${stats.internal_links_count || 0}</strong>
                </div>
                <div class="stat-item">
                    <span>Word Count:</span>
                    <strong>${origWordCount}</strong>
                    <span class="stat-arrow">&rarr;</span>
                    <strong>${stats.word_count || 0}</strong>
                </div>
            </div>
            <div class="comparison-grid">
                <div class="comparison-pane">
                    <h5>Original</h5>
                    ${refreshOriginalBody || "<p>Empty</p>"}
                </div>
                <div class="comparison-pane new-body">
                    <h5>With New Links</h5>
                    ${newBody || "<p>Empty</p>"}
                </div>
            </div>
            <div style="display: flex; gap: 8px; margin-top: 16px;">
                <button class="btn btn-primary" onclick="acceptRefreshedBody()">
                    <span class="material-symbols-rounded">check</span>
                    Accept
                </button>
                <button class="btn btn-secondary" onclick="discardRefreshedBody()">
                    <span class="material-symbols-rounded">close</span>
                    Discard
                </button>
            </div>
        </div>`;

    container._refreshedBody = newBody;
}

function acceptRefreshedBody() {
    const container = document.getElementById("refresh-links-result");
    const newBody = container._refreshedBody;
    if (newBody) {
        document.getElementById("article-edit-body").value = newBody;
        showToast("Body updated — click 'Apply to Shopify' to save", "success");
    }
    document.getElementById("refresh-links-panel-container").innerHTML = "";
}

function discardRefreshedBody() {
    document.getElementById("refresh-links-result").innerHTML = "";
    showToast("Discarded", "info");
}

// Close refresh dropdowns on outside click
document.addEventListener("click", (e) => {
    if (!e.target.closest("#refresh-product-search") && !e.target.closest("#refresh-product-dropdown")) {
        const d = document.getElementById("refresh-product-dropdown");
        if (d) d.style.display = "none";
    }
    if (!e.target.closest("#refresh-collection-search") && !e.target.closest("#refresh-collection-dropdown")) {
        const d = document.getElementById("refresh-collection-dropdown");
        if (d) d.style.display = "none";
    }
});
