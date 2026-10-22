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
    setupModalDismiss("scheduled-preview-modal", closeScheduledPreview);
});

document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
        const modal = document.getElementById("article-modal");
        if (modal && modal.classList.contains("active")) closeArticleModal();
        const preview = document.getElementById("scheduled-preview-modal");
        if (preview && preview.classList.contains("active")) closeScheduledPreview();
    }
});

function initBlogTabs() {
    const params = new URLSearchParams(window.location.search);
    const savedTab = params.get("tab") || loadFilter("blogTab", "articles");

    document.querySelectorAll(".tab").forEach((tab) => {
        tab.addEventListener("click", () => {
            document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
            document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
            tab.classList.add("active");
            document.getElementById("tab-" + tab.dataset.tab).classList.add("active");
            saveFilter("blogTab", tab.dataset.tab);

            if (tab.dataset.tab === "articles") loadArticles();
            if (tab.dataset.tab === "create") initWizard();
            if (tab.dataset.tab === "scheduled") initScheduledTab();
            if (tab.dataset.tab === "intro-links") initIntroLinksTab();
        });
    });

    const tabBtn = document.querySelector(`.tab[data-tab="${savedTab}"]`);
    if (tabBtn && savedTab !== "articles") {
        tabBtn.click();
    } else {
        loadArticles();
    }

    // /blog?article=<id> opens that article (Keyword Strategy, Home queue)
    const articleId = parseInt(params.get("article"), 10);
    if (articleId) setTimeout(() => openArticleDetail(articleId), 500);

    // /blog?tab=create&topic=<q>[&keywords=<newline-separated>] deep link
    // (AI Search visibility → Draft post; Keyword Strategy → Write an article)
    const topic = params.get("topic");
    const seedKeywords = params.get("keywords");
    if (topic || seedKeywords) {
        if (savedTab !== "create") document.querySelector('.tab[data-tab="create"]')?.click();
        setTimeout(() => {
            const el = document.getElementById("wizard-topic");
            if (el && topic && !el.value) el.value = topic;
            const kwEl = document.getElementById("wizard-keywords");
            if (kwEl && seedKeywords && !kwEl.value) kwEl.value = seedKeywords;
        }, 400);
    }
}

// ── Articles Tab ─────────────────────────────────────────────────

// Posts Google already shows but few people click — the cheapest traffic to win.
async function loadRefreshCandidates() {
    const card = document.getElementById("refresh-candidates-card");
    if (!card) return;
    try {
        const storeId = await getFirstStoreId();
        if (!storeId) return;
        const { candidates } = await apiRequest(`/api/blog/refresh-candidates?store_id=${storeId}`);
        if (!candidates || !candidates.length) { card.innerHTML = ""; return; }
        const total = candidates.reduce((sum, c) => sum + c.est_clicks_gain, 0);
        card.innerHTML = `<details class="card" style="margin-bottom:16px;" ${candidates.length <= 5 ? "open" : ""}>
            <summary style="cursor:pointer;font-weight:600;">Worth refreshing — ${candidates.length} post${candidates.length === 1 ? "" : "s"}, about +${total} clicks a month
                <span style="font-weight:400;font-size:13px;color:var(--on-surface-secondary);"> · Google shows these, but few people click. A refresh rewrites the post around those searches. Same URL.</span></summary>
            <div class="table-container" style="margin-top:12px;"><table class="data-table"><thead><tr><th>Post</th><th>Impressions (28d)</th><th>Clicks</th><th>Position</th><th>Could add</th><th></th></tr></thead><tbody>
            ${candidates.map((c) => `<tr>
                <td>${escapeHtml(c.title)}${c.keyword ? `<div style="font-size:12px;color:var(--on-surface-secondary);">${escapeHtml(c.keyword)}</div>` : ""}</td>
                <td>${c.impressions.toLocaleString()}</td><td>${c.clicks}</td><td>${c.position}</td>
                <td style="color:var(--success);font-weight:600;">+${c.est_clicks_gain}/mo</td>
                <td style="text-align:right;"><button class="btn btn-secondary btn-sm" onclick="openArticleDetail(${c.id})"><span class="material-symbols-rounded">autorenew</span> Open</button></td>
            </tr>`).join("")}</tbody></table></div></details>`;
    } catch (e) {
        card.innerHTML = "";
    }
}

async function loadArticles() {
    loadRefreshCandidates();
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
        loadBlogTemplateStatus();
        loadVisualSettings();
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

    const gscWindow = (currentArticles.find((a) => a.gsc_window) || {}).gsc_window;
    const gscLabel = gscWindow ? `${gscWindow.days}d` : "GSC";
    const columns = [
        { key: "title", label: "Title", sortable: true },
        { key: "status", label: "Status", sortable: true },
        { key: "created_at", label: "Created", sortable: true },
        { key: "seo_score", label: "SEO Score", sortable: true },
        { key: "gsc_clicks", label: `Clicks (${gscLabel})`, sortable: true, title: gscWindow ? `Google Search Console, ${gscWindow.from} → ${gscWindow.to}` : "Connect Google Search Console to see clicks" },
        { key: "gsc_impressions", label: `Impressions (${gscLabel})`, sortable: true, title: gscWindow ? `Google Search Console, ${gscWindow.from} → ${gscWindow.to}` : "Connect Google Search Console to see impressions" },
        { key: "last_optimized_at", label: "Last Optimized", sortable: true },
    ];

    columns.forEach((col) => {
        if (col.sortable) {
            const arrow = articleSort.by === col.key ? (articleSort.dir === "asc" ? " ↑" : " ↓") : "";
            html += `<th class="sortable" onclick="sortArticles('${col.key}')" ${col.title ? `title="${escapeHtml(col.title)}"` : ""}>${col.label}${arrow}</th>`;
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
            <td style="white-space:nowrap;">${formatOptimizedDate(a.created_at_display)}</td>
            <td><span class="score-inline">${getScoreBadge(a.seo_score)}${getScoreBarMini(a.seo_score)}</span></td>
            <td style="text-align:right;">${formatGscMetric(a.gsc_clicks)}</td>
            <td style="text-align:right;">${formatGscMetric(a.gsc_impressions)}${a.gsc_position ? ` <span style="font-size:11px;color:var(--on-surface-secondary);" title="Average position">#${a.gsc_position}</span>` : ""}</td>
            <td>${formatOptimizedDate(a.last_optimized_at)}</td>
        </tr>`;
    });

    html += "</tbody></table></div>";
    container.innerHTML = html;
}

function formatGscMetric(value) {
    if (value === null || value === undefined) return '<span style="color: var(--on-surface-secondary);">—</span>';
    return Number(value).toLocaleString();
}

function formatOptimizedDate(dateStr) {
    if (!dateStr) return '<span style="color: var(--on-surface-secondary);">Never</span>';
    const d = new Date(dateStr);
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function getScoreBarMini(score) {
    const s = score || 0;
    const color = scoreColor(s);
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
        loadPendingRestyle(articleId);
        loadArticleMedia(articleId);
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
        image: "Images",
        structure: "Structure (table / FAQ)",
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
                        <button class="btn btn-secondary" onclick="refreshArticle(${article.id}, this)" title="Rewrites this post in place around the searches Google already shows it for and the collections you sell. Same URL, same images, existing links kept. You review a before/after before anything changes.">
                            <span class="material-symbols-rounded">autorenew</span>
                            Refresh for search
                        </button>
                        <button class="btn btn-secondary" onclick="openRefreshLinksPanel(${article.id}, ${article.store_id})">
                            <span class="material-symbols-rounded">link</span>
                            Refresh Links
                        </button>
                        <button class="btn btn-secondary" onclick="restyleArticle(${article.id}, this)" title="Upgrade this post to the visual format: takeaways, tables/steps, FAQ, inline AI images. Facts and links are preserved. You review before anything is applied.">
                            <span class="material-symbols-rounded">auto_fix_high</span>
                            Regenerate with new style
                        </button>
                    </div>
                    <div id="article-refresh-container"></div>
                    <details style="margin-top:16px;" ontoggle="if (this.open) UIHistory.render(this.querySelector('.article-history'), 'article', ${article.id})">
                        <summary style="cursor:pointer;font-weight:500;">History — what the app changed on this post, with undo</summary>
                        <div class="article-history"></div>
                    </details>
                    <div id="refresh-links-panel-container"></div>
                    <div id="restyle-panel-container"></div>
                    <div id="article-media-container"></div>
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
                    <p><strong>Layout:</strong> ${article.template_suffix === "blog"
                        ? '<span class="badge badge-success">Styled template</span>'
                        : '<span class="badge">Theme default</span>'}</p>
                    ${article.template_suffix !== "blog" ? `
                    <button class="btn btn-secondary btn-sm" onclick="applyTemplateToArticle(${article.id}, this)" title="Switch this post to the styled article template (colored links, layout). Content is not changed.">
                        <span class="material-symbols-rounded">palette</span>
                        Apply styled template
                    </button>` : ""}
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

// ── In-article images (regenerate one at a time after publish) ───

const articleMediaPolls = {};

async function loadArticleMedia(articleId) {
    const panel = document.getElementById("article-media-container");
    if (!panel) return;
    try {
        const data = await apiRequest(`/api/blog/articles/${articleId}/media`);
        renderArticleMedia(articleId, data);
        if (data.task_id) watchArticleMediaTask(articleId, data.task_id);
    } catch (err) {
        console.warn("article media unavailable", err);
    }
}

function _articleMediaStatus(img) {
    if (img.cdn_url) return '<span class="badge badge-success">Ready</span>';
    if (img.status === "failed") return `<span class="badge badge-error" title="${escapeHtml(img.error || "")}">Failed</span>`;
    if (img.status === "skipped") return `<span class="badge badge-warning" title="${escapeHtml(img.error || "")}">Skipped</span>`;
    return '<span class="badge">Not generated</span>';
}

function renderArticleMedia(articleId, data) {
    const panel = document.getElementById("article-media-container");
    if (!panel) return;
    const inline = data.inline || [];
    if (!data.session_id || !inline.length) { panel.innerHTML = ""; return; }
    const busy = data.status === "generating_media";
    const cards = inline.map((img, i) => {
        const idx = i + 1;
        const canGenerate = !busy && (img.cdn_url || img.placeable);
        return `
        <div style="display: grid; grid-template-columns: 160px 1fr; gap: 12px; margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--outline); align-items: start;">
            <div>
                ${img.cdn_url
                    ? `<img src="${escapeHtml(img.cdn_url)}" alt="${escapeHtml(img.alt || "")}" style="width: 100%; border-radius: 8px; border: 1px solid var(--outline);">`
                    : `<div style="border: 2px dashed var(--outline); border-radius: 8px; height: 110px; display: flex; align-items: center; justify-content: center; color: var(--on-surface-secondary);"><span class="material-symbols-rounded" style="font-size: 32px;">image</span></div>`}
            </div>
            <div>
                <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap;">
                    <div>
                        <strong>Image ${idx}</strong>
                        <span class="badge" style="margin-left: 6px;">${escapeHtml(img.kind || "scene")}</span>
                        <div style="font-size: 12px; color: var(--on-surface-secondary); margin-top: 2px;">Section: ${escapeHtml(img.section || "—")}</div>
                    </div>
                    ${_articleMediaStatus(img)}
                </div>
                <div class="form-group" style="margin: 8px 0;">
                    <label style="font-size: 12px;">Image brief (what to depict)</label>
                    <textarea class="form-control" rows="2" style="font-size: 13px;" data-article-media-field="prompt" data-article-media-idx="${idx}" ${busy ? "disabled" : ""}>${escapeHtml(img.prompt || "")}</textarea>
                </div>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                    <div class="form-group" style="margin-bottom: 8px;">
                        <label style="font-size: 12px;">Alt text</label>
                        <input type="text" class="form-control" style="font-size: 13px;" maxlength="125" data-article-media-field="alt" data-article-media-idx="${idx}" value="${escapeHtml(img.alt || "")}" ${busy ? "disabled" : ""}>
                    </div>
                    <div class="form-group" style="margin-bottom: 8px;">
                        <label style="font-size: 12px;">Caption</label>
                        <input type="text" class="form-control" style="font-size: 13px;" data-article-media-field="caption" data-article-media-idx="${idx}" value="${escapeHtml(img.caption || "")}" ${busy ? "disabled" : ""}>
                    </div>
                </div>
                <button class="btn btn-secondary btn-sm" onclick="regenerateArticleInlineImage(${articleId}, ${data.session_id}, ${idx}, this)" ${canGenerate ? "" : "disabled"}
                        title="${!img.cdn_url && !img.placeable ? "The body has no placeholder for this image any more — run Regenerate with new style to plan it again" : "Regenerates only this image and updates the live article"}">
                    <span class="material-symbols-rounded">${img.cdn_url ? "refresh" : "auto_awesome"}</span>
                    ${img.cdn_url ? "Regenerate" : "Generate"}
                </button>
            </div>
        </div>`;
    }).join("");
    panel.innerHTML = `
        <div class="card" style="margin-top: 12px;">
            <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap;">
                <h4 style="margin: 0;">In-article images</h4>
                ${busy ? '<span class="badge badge-warning">Generating…</span>' : `<span style="font-size: 12px; color: var(--on-surface-secondary);">${inline.filter((i) => i.cdn_url).length}/${inline.length} ready · ~$0.05 per image</span>`}
            </div>
            <p style="font-size: 12px; color: var(--on-surface-secondary); margin: 6px 0 0;">Tweak the brief, alt or caption, then regenerate a single image. Only that figure is swapped in the live article; the rest of the body is left as is.</p>
            ${cards}
        </div>`;
}

async function regenerateArticleInlineImage(articleId, sessionId, index, btn) {
    const edits = {};
    document.querySelectorAll(`[data-article-media-idx="${index}"]`).forEach((el) => { edits[el.dataset.articleMediaField] = el.value; });
    if (btn) btn.disabled = true;
    try {
        const res = await apiRequest(`/api/blog/wizard/${sessionId}/media/${index}/regenerate`, { method: "POST", body: edits });
        showToast(`Generating image ${index}…`, "info");
        await loadArticleMedia(articleId);
        watchArticleMediaTask(articleId, res.task_id);
    } catch (err) {
        showToast(err.message, "error");
        if (btn) btn.disabled = false;
    }
}

function watchArticleMediaTask(articleId, taskId) {
    if (articleMediaPolls[articleId]) clearInterval(articleMediaPolls[articleId]);
    articleMediaPolls[articleId] = setInterval(async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            if (task.status !== "completed" && task.status !== "failed") return;
            clearInterval(articleMediaPolls[articleId]);
            delete articleMediaPolls[articleId];
            if (task.status === "failed") {
                showToast("Image generation failed: " + (task.error || "Unknown error"), "error");
            } else {
                const r = task.result || {};
                showToast(`Image updated in the live article (${r.ready || 0}/${r.total || 0} ready)`, "success");
                try {
                    const article = await apiRequest(`/api/blog/articles/${articleId}`);
                    const bodyEl = document.getElementById("article-edit-body");
                    if (bodyEl) bodyEl.value = article.body || "";
                } catch (e) { console.warn("article refresh failed", e); }
            }
            loadArticleMedia(articleId);
        } catch (err) {
            console.warn("article media poll error", err);
        }
    }, 3000);
}

// ── Restyle existing article (visual format) ─────────────────────

const restylePolls = {};

function _restyleSpinnerHtml(startedAt) {
    const started = startedAt ? ` (started ${new Date(startedAt).toLocaleTimeString()})` : "";
    return `<div class="card" style="margin-top: 12px; border-left: 3px solid var(--primary);" id="restyle-progress-card">
        <div class="loading-spinner"><div class="spinner"></div>
        <p><strong>Restyling…</strong> restructuring the article, then generating inline images. Usually 1–3 minutes${started}. You can close this window — the preview will be waiting when you reopen the article.</p></div>
    </div>`;
}

function watchRestyleTask(articleId, taskId, btn) {
    if (restylePolls[articleId]) clearInterval(restylePolls[articleId]);
    restylePolls[articleId] = setInterval(async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            if (task.status === "completed") {
                clearInterval(restylePolls[articleId]);
                delete restylePolls[articleId];
                if (btn) btn.disabled = false;
                showToast("Restyle preview is ready — review it and click Apply", "success");
                renderRestylePreview(articleId, taskId, task.result || {});
            } else if (task.status === "failed") {
                clearInterval(restylePolls[articleId]);
                delete restylePolls[articleId];
                if (btn) btn.disabled = false;
                showToast("Restyle failed: " + (task.error || "Unknown error"), "error");
                const panel = document.getElementById("restyle-panel-container");
                if (panel) panel.innerHTML = `<div class="card" style="margin-top: 12px; border-left: 3px solid var(--error);"><p style="margin:0;">Restyle failed: ${escapeHtml(task.error || "Unknown error")}</p></div>`;
            }
        } catch (err) {
            console.warn("restyle poll error", err);
        }
    }, 3000);
}

async function restyleArticle(articleId, btn) {
    const panel = document.getElementById("restyle-panel-container");
    if (!panel) { showToast("Open the article first", "error"); return; }
    if (btn) btn.disabled = true;
    panel.innerHTML = _restyleSpinnerHtml(Date.now());
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
    try {
        const res = await apiRequest(`/api/blog/articles/${articleId}/restyle`, { method: "POST", body: {} });
        showToast("Restyling started — this takes 1–3 minutes", "info");
        watchRestyleTask(articleId, res.task_id, btn);
    } catch (err) {
        showToast(err.message, "error");
        if (btn) btn.disabled = false;
        panel.innerHTML = "";
    }
}

async function loadPendingRestyle(articleId) {
    // Reopening an article: surface an in-flight or finished-but-unapplied restyle.
    try {
        const { task } = await apiRequest(`/api/blog/articles/${articleId}/restyle/latest`);
        const panel = document.getElementById("restyle-panel-container");
        if (!task || !panel) return;
        if (task.status === "completed" && task.result) {
            renderRestylePreview(articleId, task.id, task.result);
        } else if (task.status === "running" || task.status === "pending") {
            panel.innerHTML = _restyleSpinnerHtml(task.created_at);
            watchRestyleTask(articleId, task.id, null);
        }
    } catch (err) {
        console.warn("pending restyle check failed", err);
    }
}

async function renderRestylePreview(articleId, taskId, result) {
    await ensureArticlePreviewCss();
    const panel = document.getElementById("restyle-panel-container");
    if (!panel) return;
    const st = result.stats || {};
    const qa = (result._qa && result._qa.anchors) ? result._qa.anchors : null;
    panel.innerHTML = `
        <div class="card" style="margin-top: 12px;">
            <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap;">
                <div>
                    <h4 style="margin: 0;">Restyled preview</h4>
                    <p style="margin: 4px 0 0; font-size: 13px; color: var(--on-surface-secondary);">
                        ${st.before_words || 0} → ${st.after_words || 0} words · ${st.before_figures || 0} → ${st.after_figures || 0} in-body images
                        ${result.hero ? " · new hero image" : ""}
                        ${qa ? ` · ${(qa.violations || []).length} anchor issue(s) fixed` : ""}
                    </p>
                </div>
                <div style="display: flex; gap: 8px;">
                    <button class="btn btn-secondary btn-sm" onclick="document.getElementById('restyle-panel-container').innerHTML=''">Discard</button>
                    <button class="btn btn-primary btn-sm" onclick="applyRestyle(${articleId}, ${taskId}, this)">
                        <span class="material-symbols-rounded">publish</span>
                        Apply to Shopify
                    </button>
                </div>
            </div>
            <p style="font-size: 12px; color: var(--on-surface-secondary); margin: 8px 0;">Applying updates the body, switches the post to the styled template and writes FAQ schema. Facts and links were preserved; images that failed to generate are simply omitted.</p>
            ${result.hero && result.hero.image_url ? `<img src="${escapeHtml(result.hero.image_url)}" alt="${escapeHtml(result.hero.image_alt || "")}" style="max-width: 320px; border-radius: 8px; margin-bottom: 8px;">` : ""}
            <div class="a2z-blog" style="max-height: 520px; overflow-y: auto; border: 1px solid var(--outline); border-radius: 8px; padding: 16px; background: #fff; color: #1f2937;">
                <div class="a2z-blog__body" id="restyle-preview-body"></div>
            </div>
        </div>`;
    document.getElementById("restyle-preview-body").innerHTML = result.body_html || "";
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function applyRestyle(articleId, taskId, btn) {
    if (btn) btn.disabled = true;
    try {
        const res = await apiRequest(`/api/blog/articles/${articleId}/restyle/apply`, { method: "POST", body: { task_id: taskId } });
        showToast(`Restyled article applied (${res.figures || 0} in-body images)`, "success");
        openArticleDetail(articleId);
        loadArticles();
    } catch (err) {
        showToast(err.message, "error");
        if (btn) btn.disabled = false;
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

// ── Refresh for search ───────────────────────────────────────────
let lastArticleRefresh = null;

async function refreshArticle(articleId, btn) {
    const box = document.getElementById("article-refresh-container");
    if (btn) btn.disabled = true;
    box.innerHTML = '<div class="card" style="margin-top:16px;"><div class="loading-spinner"><div class="spinner"></div><p>Reading the searches this post appears for and drafting a refresh… (about a minute)</p></div></div>';
    try {
        const { task_id } = await apiRequest(`/api/blog/articles/${articleId}/refresh`, { method: "POST" });
        const task = await pollTaskUntilDone(`/api/tasks/${task_id}`, { intervalMs: 2500 });
        lastArticleRefresh = task.result;
        renderArticleRefresh(box, task.result);
    } catch (e) {
        box.innerHTML = `<div class="card" style="margin-top:16px;"><p style="color:var(--error);">Refresh failed: ${escapeHtml(e.message)}</p></div>`;
    } finally {
        if (btn) btn.disabled = false;
    }
}

function renderArticleRefresh(box, r) {
    const metaRow = (label, before, after) => after && after !== before
        ? `<tr><td style="white-space:nowrap;font-weight:500;">${label}</td><td style="color:var(--on-surface-secondary);">${escapeHtml(before || "(none)")}</td><td>${escapeHtml(after)}</td></tr>`
        : "";
    const queries = (r.queries || []).slice(0, 8).map((q) =>
        `<span class="badge" style="margin:2px;" title="${q.impressions} impressions, ${q.clicks} clicks in 90 days">${escapeHtml(q.query)}</span>`).join("");
    const removed = (r.would_remove || []).length
        ? `<p style="color:var(--error);font-size:13px;"><strong>Heads up:</strong> this draft drops ${escapeHtml(r.would_remove.join(", "))}. Applying will ask you to confirm.</p>`
        : "";
    box.innerHTML = `<div class="card" style="margin-top:16px;">
        <h3 style="margin:0 0 4px;">Refresh preview</h3>
        <p style="font-size:13px;color:var(--on-surface-secondary);margin:0 0 12px;">Nothing has changed yet. The URL and images stay the same.</p>
        ${queries ? `<div style="margin-bottom:12px;"><div style="font-size:12px;font-weight:500;margin-bottom:4px;">Written around these searches</div>${queries}</div>`
            : '<p style="font-size:13px;">Google has not shown this post for any searches yet, so the refresh tightens it around its topic and what you sell.</p>'}
        <div style="font-size:12px;font-weight:500;margin-bottom:4px;">What changes</div>
        <ul style="font-size:13px;margin:0 0 12px 18px;">${(r.changes || []).map((c) => `<li>${escapeHtml(c)}</li>`).join("") || "<li>Wording tightened.</li>"}</ul>
        <div class="table-container"><table class="data-table" style="font-size:13px;"><thead><tr><th></th><th>Now</th><th>After refresh</th></tr></thead><tbody>
            ${metaRow("Meta title", r.before.meta_title, r.meta_title)}
            ${metaRow("Meta description", r.before.meta_description, r.meta_description)}
            <tr><td style="font-weight:500;">Words</td><td>${r.before.words}</td><td>${r.after.words}</td></tr>
            <tr><td style="font-weight:500;">Links to your store</td><td>${r.before.internal_links}</td><td>${r.after.internal_links}</td></tr>
        </tbody></table></div>
        ${removed}
        <details style="margin:12px 0;"><summary style="cursor:pointer;font-size:13px;">Read the refreshed article</summary>
            <div style="margin-top:8px;padding:12px;border:1px solid var(--outline);border-radius:8px;max-height:420px;overflow:auto;">${r.body}</div></details>
        <div style="display:flex;gap:8px;">
            <button class="btn btn-primary" onclick="applyArticleRefresh(this)"><span class="material-symbols-rounded">cloud_upload</span> Apply to Shopify</button>
            <button class="btn btn-secondary" onclick="document.getElementById('article-refresh-container').innerHTML=''">Discard</button>
        </div>
    </div>`;
}

async function applyArticleRefresh(btn) {
    const r = lastArticleRefresh;
    if (!r) return;
    btn.disabled = true;
    const body = { body: r.body, source: "blog", change_type: "article_refresh" };
    if (r.meta_title) body.meta_title = r.meta_title;
    if (r.meta_description) body.meta_description = r.meta_description;
    if (r.summary) body.summary = r.summary;
    try {
        await apiRequest(`/api/blog/articles/${r.article_id}/apply`, { method: "POST", body });
        showToast("Refresh applied. You can undo it from the article's history.", "success");
        lastArticleRefresh = null;
        openArticleDetail(r.article_id);
        loadArticles();
    } catch (e) {
        showToast(`Could not apply: ${e.message}`, "error");
        btn.disabled = false;
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

// ── Topic Discovery ──────────────────────────────────────────────

let topicRunsCache = [];
let topicPipelineCache = [];

const TOPIC_INTENT_COLORS = {
    Informational: "#1a73e8",
    Commercial: "#1e8e3e",
    Transactional: "#f9ab00",
    Navigational: "#9334e6",
};

async function loadTopicSuggestions() {
    const storeId = await getFirstStoreId();
    if (!storeId) return;
    try {
        const { runs, pipeline } = await apiRequest(`/api/blog/topics?store_id=${storeId}`);
        topicRunsCache = runs || [];
        topicPipelineCache = pipeline || [];
        renderTopicRuns();
        if (topicPipelineCache.some((s) => s.enriching)) pollTopicEnrichment();
    } catch (err) {
        console.error("Failed to load topic suggestions", err);
    }
}

function renderTopicRuns() {
    const container = document.getElementById("topic-suggestions-container");
    const history = document.getElementById("topic-history");
    const historyList = document.getElementById("topic-history-list");
    if (!container) return;

    const completed = topicRunsCache.filter((r) => r.status === "completed");
    const latest = completed[0];

    // Ideas sent from AI Search (gap analysis → "Send to blog pipeline") arrive
    // outside a research run — show them as their own always-visible group.
    const pipelineVisible = topicPipelineCache.filter((s) => s.status !== "scheduled");
    const pipelineScheduled = topicPipelineCache.length - pipelineVisible.length;
    let pipelineHtml = "";
    if (pipelineVisible.length || pipelineScheduled) {
        pipelineHtml = `<div class="topic-run" style="margin-bottom:16px;">
            <p style="font-size:12px;color:var(--on-surface-secondary);margin:0 0 8px;display:flex;align-items:center;gap:6px;">
                <span class="material-symbols-rounded" style="font-size:16px;">smart_toy</span>
                Sent from <a href="/ai-seo?tab=visibility&sub=actions">AI Search — gap analysis</a>
            </p>
            ${pipelineVisible.map((s) => renderTopicSuggestion(s)).join("")}
            ${pipelineScheduled ? `<p style="font-size:13px;color:var(--on-surface-secondary);margin:4px 0 8px;">${pipelineScheduled} idea${pipelineScheduled > 1 ? "s" : ""} moved to the <a href="#" onclick="document.querySelector('.tab[data-tab=\\'scheduled\\']').click(); return false;">Scheduled tab</a>.</p>` : ""}
        </div>`;
    }

    if (!latest && !pipelineHtml) {
        container.innerHTML = "";
        history.style.display = "none";
        return;
    }

    container.innerHTML = pipelineHtml + (latest ? renderTopicRun(latest, false) : "");
    if (!latest) {
        history.style.display = "none";
        return;
    }

    const older = completed.slice(1);
    if (older.length) {
        history.style.display = "";
        historyList.innerHTML = older.map((r) => renderTopicRun(r, true)).join("");
    } else {
        history.style.display = "none";
    }
}

function renderTopicRun(run, isHistory) {
    const date = run.created_at ? new Date(run.created_at).toLocaleString() : "";
    let html = `<div class="topic-run" style="${isHistory ? "margin-bottom: 16px; padding-top: 12px; border-top: 1px solid var(--outline);" : ""}">`;
    html += `<p style="font-size: 12px; color: var(--on-surface-secondary); margin: 0 0 8px;">Run from ${escapeHtml(date)}</p>`;
    if (run.market_summary) {
        html += `<p style="font-size: 13px; margin: 0 0 12px; padding: 10px 12px; background: var(--primary-light); border-radius: 8px;">${escapeHtml(run.market_summary)}</p>`;
    }
    // Scheduled suggestions leave the research area — they live in the
    // Scheduled tab now (and reappear here if the scheduled post is cancelled).
    const visible = (run.suggestions || []).filter((s) => s.status !== "scheduled");
    const scheduledCount = (run.suggestions || []).length - visible.length;
    html += visible.map((s) => renderTopicSuggestion(s)).join("");
    if (scheduledCount) {
        html += `<p style="font-size: 13px; color: var(--on-surface-secondary); margin: 4px 0 8px;">${scheduledCount} suggestion${scheduledCount > 1 ? "s" : ""} moved to the <a href="#" onclick="document.querySelector('.tab[data-tab=\\'scheduled\\']').click(); return false;">Scheduled tab</a>.</p>`;
    }
    const rejected = run.rejected_topics || [];
    if (rejected.length) {
        html += `<details style="margin-top: 8px;"><summary style="cursor: pointer; font-size: 13px; color: var(--on-surface-secondary);">Ruled out (${rejected.length})</summary><ul style="font-size: 13px; color: var(--on-surface-secondary); margin: 8px 0 0;">`;
        html += rejected.map((r) => `<li><strong>${escapeHtml(r.topic)}</strong> — ${escapeHtml(r.reason)}</li>`).join("");
        html += `</ul></details>`;
    }
    html += `</div>`;
    return html;
}

function renderTopicSuggestion(s) {
    const kw = s.keywords || {};
    const intentColor = TOPIC_INTENT_COLORS[s.search_intent] || "#5f6368";
    const muted = s.status !== "suggested";

    const kwGroup = (label, words) => {
        if (!words || !words.length) return "";
        return `<div style="margin-top: 4px; font-size: 12px;"><span style="color: var(--on-surface-secondary); text-transform: uppercase; font-size: 10px; letter-spacing: 0.5px;">${label}</span> ${words.map((w) => `<span class="badge badge-info" style="margin: 2px;">${escapeHtml(w)}</span>`).join("")}</div>`;
    };

    let html = `<div class="card" style="margin-bottom: 12px; ${muted ? "opacity: 0.55;" : ""}" data-suggestion-id="${s.id}">`;
    html += `<div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap;">`;
    html += `<div style="flex: 1; min-width: 260px;">`;
    html += `<div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">`;
    html += `<h4 style="margin: 0;">${escapeHtml(s.h1 || s.topic)}</h4>`;
    html += `<span class="badge" style="background: ${intentColor}; color: #fff;">${escapeHtml(s.search_intent || "")}</span>`;
    if (s.opportunity_type === "foundation") {
        html += `<span class="badge" style="background: transparent; border: 1px solid var(--primary); color: var(--primary);">Foundation</span>`;
    } else {
        html += `<span class="badge" style="background: transparent; border: 1px solid var(--success); color: var(--success);">Quick Win</span>`;
    }
    if (s.status === "used") html += `<span class="badge" style="background: var(--success); color: #fff;">Used</span>`;
    if (s.status === "dismissed") html += `<span class="badge" style="background: var(--on-surface-secondary); color: #fff;">Dismissed</span>`;
    if (s.status === "scheduled") html += `<span class="badge" style="background: var(--primary); color: #fff;">Scheduled</span>`;
    if (s.enriching) html += `<span class="badge badge-warning" style="display:inline-flex;align-items:center;gap:5px;"><span class="spinner-sm" style="width:11px;height:11px;border-width:2px;"></span> Researching…</span>`;
    html += `</div>`;
    if (s.meta_title) {
        html += `<p style="margin: 8px 0 0; font-size: 13px;"><strong>Meta title</strong> <span style="color: var(--on-surface-secondary);">(${s.meta_title.length}/60)</span>: ${escapeHtml(s.meta_title)}</p>`;
    }
    if (s.meta_description) {
        html += `<p style="margin: 4px 0 0; font-size: 13px;"><strong>Meta description</strong> <span style="color: var(--on-surface-secondary);">(${s.meta_description.length}/155)</span>: ${escapeHtml(s.meta_description)}</p>`;
    }
    if (s.url_slug) {
        html += `<p style="margin: 4px 0 0; font-size: 13px;"><strong>Slug</strong>: /blogs/news/${escapeHtml(s.url_slug)}</p>`;
    }
    if (kw.primary) {
        html += `<div style="margin-top: 8px; font-size: 12px;"><span style="color: var(--on-surface-secondary); text-transform: uppercase; font-size: 10px; letter-spacing: 0.5px;">Primary</span> <span class="badge badge-info" style="margin: 2px; font-weight: 700;">${escapeHtml(kw.primary)}</span></div>`;
    }
    html += kwGroup("Informational", kw.informational);
    html += kwGroup("Commercial", kw.commercial);
    html += kwGroup("Transactional", kw.transactional);
    html += kwGroup("Navigational", kw.navigational);
    if (s.rationale) {
        html += `<p style="margin: 10px 0 0; font-size: 13px; color: var(--on-surface-secondary);"><strong>Why:</strong> ${escapeHtml(s.rationale)}</p>`;
    }
    if (s.cannibalization_note) {
        html += `<p style="margin: 4px 0 0; font-size: 13px; color: var(--on-surface-secondary);"><strong>Overlap guard:</strong> ${escapeHtml(s.cannibalization_note)}</p>`;
    }
    const links = [
        ...(s.target_articles || []).map((t) => ({ t, icon: "article" })),
        ...(s.target_collections || []).map((t) => ({ t, icon: "category" })),
        ...(s.target_products || []).map((t) => ({ t, icon: "sell" })),
    ];
    if (links.length) {
        html += `<div style="margin-top: 8px; font-size: 12px;"><span style="color: var(--on-surface-secondary); text-transform: uppercase; font-size: 10px; letter-spacing: 0.5px;">Link to</span> `;
        html += links.map((l) => `<span class="badge badge-info" style="margin: 2px;"><span class="material-symbols-rounded" style="font-size: 12px; vertical-align: middle;">${l.icon}</span> ${escapeHtml(l.t)}</span>`).join("");
        html += `</div>`;
    }
    html += `</div>`;
    html += `<div style="display: flex; flex-direction: column; gap: 8px;">`;
    if (s.status === "suggested") {
        // Pipeline-sent ideas that haven't been researched yet get a Research
        // button (fills meta, keyword groups, overlap guard, link targets).
        if (!s.meta_title && !s.enriching) {
            html += `<button class="btn btn-sm btn-primary" onclick="researchTopicSuggestion(${s.id}, this)"><span class="material-symbols-rounded">science</span> Research</button>`;
        }
        html += `<button class="btn btn-sm ${!s.meta_title ? "btn-secondary" : "btn-primary"}" onclick="scheduleTopicSuggestion(${s.id})"><span class="material-symbols-rounded">event</span> Schedule</button>`;
        html += `<button class="btn btn-sm btn-secondary" onclick="useTopicSuggestion(${s.id})"><span class="material-symbols-rounded">edit_note</span> Use this topic</button>`;
        html += `<button class="btn btn-sm btn-secondary" onclick="dismissTopicSuggestion(${s.id})">Dismiss</button>`;
    }
    html += `<button class="btn btn-sm btn-secondary" onclick="copyTopicKeywords(${s.id})"><span class="material-symbols-rounded">content_copy</span> Copy keywords</button>`;
    html += `</div></div></div>`;
    return html;
}

let topicEnrichPollTimer = null;

function pollTopicEnrichment() {
    if (topicEnrichPollTimer) return;
    topicEnrichPollTimer = setInterval(async () => {
        await loadTopicSuggestions();
        if (!topicPipelineCache.some((s) => s.enriching)) {
            clearInterval(topicEnrichPollTimer);
            topicEnrichPollTimer = null;
        }
    }, 5000);
}

async function researchTopicSuggestion(id, btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded">hourglass_empty</span> Starting…';
    try {
        await apiRequest(`/api/blog/topics/suggestions/${id}/enrich`, { method: "POST", body: {} });
        showToast("Researching this topic — meta, keywords and link targets will fill in shortly", "success");
        await loadTopicSuggestions();
        pollTopicEnrichment();
    } catch (err) {
        showToast(`Research failed to start: ${err.message}`, "error");
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded">science</span> Research';
    }
}

async function startTopicResearch() {
    const btn = document.getElementById("topic-research-btn");
    const storeId = await getFirstStoreId();
    if (!storeId) { showToast("No store found", "error"); return; }

    btn.disabled = true;
    document.getElementById("topic-research-progress").style.display = "";
    try {
        const { task_id } = await apiRequest("/api/blog/topics/research", {
            method: "POST",
            body: { store_id: storeId },
        });
        pollTopicResearchTask(task_id);
    } catch (err) {
        btn.disabled = false;
        document.getElementById("topic-research-progress").style.display = "none";
        showToast(err.message, "error");
    }
}

function pollTopicResearchTask(taskId) {
    let errors = 0;
    const finish = () => {
        document.getElementById("topic-research-btn").disabled = false;
        document.getElementById("topic-research-progress").style.display = "none";
    };
    const poll = async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            if (task.status === "completed") {
                finish();
                showToast("Topic research complete", "success");
                loadTopicSuggestions();
            } else if (task.status === "failed") {
                finish();
                showToast(`Topic research failed: ${task.error || "Unknown error"}`, "error");
            } else {
                setTimeout(poll, 3000);
            }
        } catch (err) {
            errors += 1;
            if (errors >= 5) {
                finish();
                showToast(err.message, "error");
            } else {
                setTimeout(poll, 5000);
            }
        }
    };
    poll();
}

function findTopicSuggestion(id) {
    // Pipeline-sent ideas (AI Search → blog) live outside any research run.
    const pipelineHit = topicPipelineCache.find((s) => s.id === id);
    if (pipelineHit) return pipelineHit;
    for (const run of topicRunsCache) {
        for (const s of run.suggestions || []) {
            if (s.id === id) return s;
        }
    }
    return null;
}

function flattenTopicKeywords(kw) {
    const out = [];
    if (kw.primary) out.push(kw.primary);
    for (const group of ["informational", "commercial", "transactional", "navigational"]) {
        for (const w of kw[group] || []) {
            if (!out.includes(w)) out.push(w);
        }
    }
    return out;
}

async function useTopicSuggestion(id) {
    const s = findTopicSuggestion(id);
    if (!s) return;
    document.getElementById("wizard-topic").value = s.topic || s.h1 || "";
    document.getElementById("wizard-keywords").value = flattenTopicKeywords(s.keywords || {}).join("\n");
    try {
        await apiRequest(`/api/blog/topics/suggestions/${id}`, { method: "PATCH", body: { status: "used" } });
        s.status = "used";
        renderTopicRuns();
    } catch (err) {
        showToast(err.message, "error");
    }
    showWizardStep(1);
    showToast("Topic loaded into the wizard — review keywords and start research", "success");
    document.getElementById("wizard-container").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function dismissTopicSuggestion(id) {
    try {
        await apiRequest(`/api/blog/topics/suggestions/${id}`, { method: "PATCH", body: { status: "dismissed" } });
        const s = findTopicSuggestion(id);
        if (s) s.status = "dismissed";
        renderTopicRuns();
    } catch (err) {
        showToast(err.message, "error");
    }
}

function copyTopicKeywords(id) {
    const s = findTopicSuggestion(id);
    if (!s) return;
    const kw = s.keywords || {};
    const lines = [];
    if (kw.primary) lines.push(`PRIMARY: ${kw.primary}`);
    const groups = [
        ["INFORMATIONAL", kw.informational],
        ["COMMERCIAL", kw.commercial],
        ["TRANSACTIONAL", kw.transactional],
        ["NAVIGATIONAL", kw.navigational],
    ];
    for (const [label, words] of groups) {
        if (words && words.length) lines.push(`${label}: ${words.join(", ")}`);
    }
    navigator.clipboard.writeText(lines.join("\n")).then(
        () => showToast("Keywords copied", "success"),
        () => showToast("Copy failed", "error")
    );
}

// ── Wizard ───────────────────────────────────────────────────────

async function initWizard() {
    showWizardStep(1);
    loadTopicSuggestions();
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
        const styleEl = document.querySelector('input[name="wizard-style"]:checked');
        const style = styleEl ? styleEl.value : undefined;
        const { session_id, keyword_warning } = await apiRequest("/api/blog/wizard/start", {
            method: "POST",
            body: { store_id: storeId, topic, keywords, style },
        });
        currentWizardSession = session_id;
        if (keyword_warning) showToast(keyword_warning, "warning");

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

    if (session.style === "classic") {
        showWizardStep(6);
        renderReview(sessionId);
    } else {
        showWizardStep(5);
        renderVisuals(sessionId);
    }
}

async function wizardReview() {
    showWizardStep(6);
    if (currentWizardSession) renderReview(currentWizardSession);
}

async function wizardVisuals() {
    showWizardStep(5);
    if (currentWizardSession) renderVisuals(currentWizardSession);
}

// ── Visuals step (hero + inline images) ──────────────────────────

function _heroCardHtml(featuredImageUrl, featuredAlt) {
    return featuredImageUrl
        ? `<img src="${escapeHtml(featuredImageUrl)}" alt="${escapeHtml(featuredAlt || "Featured image")}" style="width: 100%; border-radius: 8px; margin-bottom: 8px;">
           ${featuredAlt ? `<small style="display:block; color: var(--on-surface-secondary); margin-bottom: 8px;">Alt: ${escapeHtml(featuredAlt)}</small>` : ""}
           <button class="btn btn-secondary" onclick="wizardGenerateImage()" style="width: 100%;">
               <span class="material-symbols-rounded">refresh</span>
               Regenerate Hero
           </button>`
        : `<div style="border: 2px dashed var(--outline); border-radius: 8px; padding: 24px; text-align: center;">
               <span class="material-symbols-rounded" style="font-size: 40px; color: var(--on-surface-secondary);">image</span>
               <p style="color: var(--on-surface-secondary); font-size: 13px; margin: 8px 0 0;">AI hero image from the media plan — no text baked in</p>
           </div>
           <button class="btn btn-primary" onclick="wizardGenerateImage()" style="width: 100%; margin-top: 12px;">
               <span class="material-symbols-rounded">auto_awesome</span>
               Generate Hero Image
           </button>`;
}

function _inlineStatusBadge(entry) {
    if (!entry) return '<span class="badge">Not generated</span>';
    if (entry.status === "ready") return '<span class="badge badge-success">Ready</span>';
    if (entry.status === "failed") return `<span class="badge badge-error" title="${escapeHtml(entry.error || "")}">Failed</span>`;
    if (entry.status === "skipped") return `<span class="badge badge-warning" title="${escapeHtml(entry.error || "")}">Skipped</span>`;
    return '<span class="badge">Pending</span>';
}

async function renderVisuals(sessionId) {
    const session = await apiRequest(`/api/blog/wizard/${sessionId}`);
    const container = document.getElementById("visuals-content");
    const plan = session.media_plan || {};
    const hero = plan.hero || {};
    const inline = plan.inline || [];
    const images = session.inline_images || [];
    const byToken = {};
    images.forEach((e) => { if (e && e.token) byToken[e.token] = e; });
    const readyCount = images.filter((e) => e.status === "ready").length;
    const isBusy = session.status === "generating_media";
    const hasHero = !!session.featured_image_url;
    const totalCount = inline.length + 1;  // hero + inline
    const doneCount = readyCount + (hasHero ? 1 : 0);
    const nothingToGenerate = !inline.length && hasHero;

    let cards = "";
    if (!inline.length) {
        cards = `<div class="card"><p style="margin: 0; color: var(--on-surface-secondary); font-size: 13px;">No inline images were planned for this article (check "Inline images per article" in Article Design settings, then regenerate the outline).</p></div>`;
    }
    inline.forEach((spec, i) => {
        const entry = byToken[spec.token];
        const idx = i + 1;
        cards += `
        <div class="card" style="margin-top: 12px;" data-inline-idx="${idx}">
            <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap;">
                <div>
                    <strong>Image ${idx}</strong>
                    <span class="badge" style="margin-left: 6px;">${escapeHtml(spec.kind || "scene")}</span>
                    ${(spec.product_handles || []).length ? `<span class="badge" style="margin-left: 4px;" title="Real product photos used as reference">${(spec.product_handles || []).length} product ref</span>` : ""}
                    <div style="font-size: 12px; color: var(--on-surface-secondary); margin-top: 2px;">Section: ${escapeHtml(spec.section || "—")}</div>
                </div>
                ${_inlineStatusBadge(entry)}
            </div>
            <div style="display: grid; grid-template-columns: 220px 1fr; gap: 14px; margin-top: 12px; align-items: start;">
                <div>
                    ${entry && entry.cdn_url
                        ? `<img src="${escapeHtml(entry.cdn_url)}" alt="${escapeHtml(entry.alt || "")}" style="width: 100%; border-radius: 8px; border: 1px solid var(--outline);">`
                        : `<div style="border: 2px dashed var(--outline); border-radius: 8px; height: 150px; display: flex; align-items: center; justify-content: center; color: var(--on-surface-secondary);"><span class="material-symbols-rounded" style="font-size: 36px;">image</span></div>`}
                </div>
                <div>
                    <div class="form-group" style="margin-bottom: 8px;">
                        <label style="font-size: 12px;">Image brief (what to depict)</label>
                        <textarea class="form-control" rows="2" style="font-size: 13px;" data-media-field="prompt" data-media-idx="${idx}">${escapeHtml(spec.prompt || "")}</textarea>
                    </div>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                        <div class="form-group" style="margin-bottom: 8px;">
                            <label style="font-size: 12px;">Alt text</label>
                            <input type="text" class="form-control" style="font-size: 13px;" maxlength="125" data-media-field="alt" data-media-idx="${idx}" value="${escapeHtml(spec.alt || "")}">
                        </div>
                        <div class="form-group" style="margin-bottom: 8px;">
                            <label style="font-size: 12px;">Caption</label>
                            <input type="text" class="form-control" style="font-size: 13px;" data-media-field="caption" data-media-idx="${idx}" value="${escapeHtml(spec.caption || "")}">
                        </div>
                    </div>
                    <button class="btn btn-secondary btn-sm" onclick="regenerateInlineImage(${idx}, this)" ${isBusy ? "disabled" : ""}>
                        <span class="material-symbols-rounded">${entry && entry.cdn_url ? "refresh" : "auto_awesome"}</span>
                        ${entry && entry.cdn_url ? "Regenerate" : "Generate"}
                    </button>
                </div>
            </div>
        </div>`;
    });

    const estimate = (inline.length + (hasHero ? 0 : 1)) * 0.05;
    container.innerHTML = `
    <div style="display: grid; grid-template-columns: 1fr 340px; gap: 24px; align-items: start;">
        <div>
            <div class="card">
                <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap;">
                    <div>
                        <h4 style="margin: 0;">Images</h4>
                        <p style="margin: 4px 0 0; font-size: 13px; color: var(--on-surface-secondary);">
                            ${doneCount}/${totalCount} ready (hero + ${inline.length} inline) · ~$${estimate.toFixed(2)} for the missing ones
                        </p>
                    </div>
                    <div style="display: flex; gap: 8px;">
                        <button class="btn btn-secondary btn-sm" onclick="saveMediaPlanEdits()" title="Save edited briefs, alts and captions">
                            <span class="material-symbols-rounded">save</span>
                            Save plan
                        </button>
                        <button class="btn btn-primary btn-sm" id="generate-all-media-btn" onclick="generateAllInlineImages()" title="Generates the hero and every inline image that is not ready yet" ${isBusy || nothingToGenerate ? "disabled" : ""}>
                            <span class="material-symbols-rounded">auto_awesome</span>
                            ${doneCount ? "Generate missing" : "Generate all"}
                        </button>
                    </div>
                </div>
                ${isBusy ? '<div class="loading-spinner" style="margin-top: 12px;"><div class="spinner"></div><p>Generating images… this takes about 30s per image.</p></div>' : ""}
            </div>
            ${cards}
        </div>
        <div>
            <div class="card">
                <h4>Hero image</h4>
                ${hero.prompt ? `<p style="font-size: 12px; color: var(--on-surface-secondary); margin: 0 0 10px;">Brief: ${escapeHtml(hero.prompt)}</p>` : ""}
                ${hero.kind === "product" && (hero.product_handles || []).length ? `<p style="font-size: 12px; color: var(--on-surface-secondary); margin: 0 0 10px;"><span class="material-symbols-rounded" style="font-size: 14px; vertical-align: -2px;">photo_camera</span> Uses real product photos: ${escapeHtml(hero.product_handles.join(", "))}</p>` : ""}
                <div id="featured-image-container">${isBusy && !hasHero
                    ? '<div style="border: 2px dashed var(--outline); border-radius: 8px; padding: 24px; text-align: center;"><div class="spinner" style="margin: 0 auto 12px;"></div><p style="color: var(--on-surface-secondary); font-size: 13px; margin: 0;">Generating hero image…</p></div>'
                    : _heroCardHtml(session.featured_image_url, session.featured_image_alt)}</div>
            </div>
            <div class="card" style="margin-top: 16px;">
                <h4>Next</h4>
                <p style="font-size: 13px; color: var(--on-surface-secondary);">Images that fail are simply left out — the article never ships a broken marker. You can also come back here after review.</p>
                <button class="btn btn-primary" onclick="wizardReview()" style="width: 100%;">
                    <span class="material-symbols-rounded">rate_review</span>
                    Continue to Review
                </button>
            </div>
        </div>
    </div>`;

    if (isBusy) {
        pollWizardSession(sessionId, session.shopify_article_id ? "published" : "generated", () => renderVisuals(sessionId));
    }
}

function _collectMediaPlanEdits() {
    const plan = {};
    document.querySelectorAll("[data-media-field]").forEach((el) => {
        const idx = el.dataset.mediaIdx;
        plan[idx] = plan[idx] || {};
        plan[idx][el.dataset.mediaField] = el.value;
    });
    return plan;
}

async function saveMediaPlanEdits(silent = false) {
    if (!currentWizardSession) return null;
    const session = await apiRequest(`/api/blog/wizard/${currentWizardSession}`);
    const plan = session.media_plan || { hero: {}, inline: [] };
    const edits = _collectMediaPlanEdits();
    (plan.inline || []).forEach((spec, i) => {
        const e = edits[String(i + 1)];
        if (e) Object.assign(spec, e);
    });
    try {
        const res = await apiRequest(`/api/blog/wizard/${currentWizardSession}/media-plan`, { method: "PUT", body: { media_plan: plan } });
        if (!silent) showToast("Media plan saved", "success");
        return res.media_plan;
    } catch (err) {
        showToast(err.message, "error");
        return null;
    }
}

async function generateAllInlineImages() {
    if (!currentWizardSession) return;
    const sessionId = currentWizardSession;
    await saveMediaPlanEdits(true);
    const btn = document.getElementById("generate-all-media-btn");
    if (btn) btn.disabled = true;
    try {
        const res = await apiRequest(`/api/blog/wizard/${sessionId}/generate-media`, { method: "POST" });
        showToast(res.includes_hero ? "Generating hero and inline images…" : "Generating inline images…", "info");
        renderVisuals(sessionId);
    } catch (err) {
        showToast(err.message, "error");
        if (btn) btn.disabled = false;
    }
}

async function regenerateInlineImage(index, btn) {
    if (!currentWizardSession) return;
    const sessionId = currentWizardSession;
    const edits = _collectMediaPlanEdits()[String(index)] || {};
    if (btn) btn.disabled = true;
    try {
        await apiRequest(`/api/blog/wizard/${sessionId}/media/${index}/regenerate`, { method: "POST", body: edits });
        showToast(`Generating image ${index}…`, "info");
        renderVisuals(sessionId);
    } catch (err) {
        showToast(err.message, "error");
        if (btn) btn.disabled = false;
    }
}

// ── Styled preview (same CSS the theme template ships) ───────────

let articleCssCache = null;
async function ensureArticlePreviewCss() {
    if (document.getElementById("a2z-preview-css")) return;
    try {
        const storeId = getSelectedStoreId() || (await getFirstStoreId());
        const resp = await fetch(`/api/blog/article-css?store_id=${storeId}`);
        articleCssCache = await resp.text();
        const style = document.createElement("style");
        style.id = "a2z-preview-css";
        // Scope to the preview containers only.
        style.textContent = articleCssCache;
        document.head.appendChild(style);
    } catch (e) {
        console.warn("preview css unavailable", e);
    }
}

async function renderComposedPreview(sessionId, targetEl, bodyHtml) {
    if (!targetEl) return;
    try {
        const res = await apiRequest(`/api/blog/wizard/${sessionId}/compose-preview`, {
            method: "POST",
            body: bodyHtml === undefined ? {} : { body_html: bodyHtml },
        });
        targetEl.innerHTML = res.body_html || "";
    } catch (err) {
        targetEl.innerHTML = bodyHtml || "";
    }
}

async function renderReview(sessionId) {
    const session = await apiRequest(`/api/blog/wizard/${sessionId}`);
    const article = session.generated_article || {};
    const container = document.getElementById("review-content");
    const featuredImageUrl = session.featured_image_url || "";
    const isVisual = session.style !== "classic";
    if (isVisual) await ensureArticlePreviewCss();

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
                <h4>Preview ${isVisual ? '<span class="badge badge-success" style="margin-left:6px;">styled</span>' : ""}</h4>
                ${isVisual ? `<p style="font-size: 12px; color: var(--on-surface-secondary); margin: 0 0 8px;">Rendered exactly as it will publish — inline figures, table of contents and blocks included. Keep the <code>[[INLINE_IMAGE_n]]</code> markers where images should appear. <a href="#" onclick="wizardVisuals(); return false;">Edit visuals</a></p>` : ""}
                <div class="${isVisual ? "a2z-blog" : ""}" style="max-height: 480px; overflow-y: auto; border: 1px solid var(--outline); border-radius: 8px; padding: 16px; background: #fff; color: #1f2937;">
                    <div id="review-preview" class="${isVisual ? "a2z-blog__body" : ""}">${isVisual ? "" : (article.body_html || "")}</div>
                </div>
            </div>
        </div>
    </div>
    <div style="margin-top: 16px; display: flex; gap: 8px; align-items: center;">
        <p style="font-size: 13px; color: var(--on-surface-secondary); margin: 0; flex: 1;">Publishing to <strong>News</strong> blog${isVisual ? " · styled article template" : " · classic layout"}</p>
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

    // Live preview (visual: composed server-side so figures/TOC match what ships)
    const bodyTextarea = document.getElementById("review-body");
    if (bodyTextarea) {
        let previewTimer = null;
        if (isVisual) {
            renderComposedPreview(sessionId, document.getElementById("review-preview"), bodyTextarea.value);
        }
        bodyTextarea.addEventListener("input", () => {
            const target = document.getElementById("review-preview");
            if (!isVisual) { target.innerHTML = bodyTextarea.value; return; }
            clearTimeout(previewTimer);
            previewTimer = setTimeout(() => renderComposedPreview(sessionId, target, bodyTextarea.value), 500);
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

                showWizardStep(7);
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
            const fill = (id, value) => {
                const el = document.getElementById(id);
                if (el && value) el.value = value;
            };
            fill("landing-intro-text", data && data.intro_text);
            fill("hub-h1-input", data && data.hub_h1);
            fill("blog-meta-title", data && data.hub_meta_title);
            fill("blog-meta-description", data && data.hub_meta_description);
            updateMetaCharCount("blog-meta-title", "blog-meta-title-count", 60);
            updateMetaCharCount("blog-meta-description", "blog-meta-desc-count", 160);
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

// Saves any of the hub's intro / H1 / meta fields. The server refuses an H1 or
// meta title that names another page's keyword; apiRequest offers "apply anyway".
async function saveHubSettings(fields, savedMessage) {
    const card = document.getElementById("blog-seo-card");
    const blogId = card ? card.dataset.blogId : null;
    const storeId = getSelectedStoreId();
    if (!storeId || !blogId) {
        showToast("No blog available", "error");
        return;
    }
    try {
        await apiRequest("/api/blog/hub-settings", {
            method: "POST",
            body: { store_id: parseInt(storeId), blog_id: parseInt(blogId), ...fields },
        });
        showToast(savedMessage + " — the hub page is rebuilding", "success");
    } catch (err) {
        showToast(err.message, "error");
    }
}

function saveIntroText() {
    const introEl = document.getElementById("landing-intro-text");
    return saveHubSettings({ intro_text: introEl ? introEl.value.trim() : "" }, "Intro saved");
}

function saveHubH1() {
    const input = document.getElementById("hub-h1-input");
    return saveHubSettings({ hub_h1: input ? input.value.replace(/\s+/g, " ").trim() : "" }, "Hub heading saved");
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

    const conflicts = result.conflicts || [];
    const conflictNote = conflicts.length
        ? `<p style="margin: 0 0 8px; font-size: 12px; color: var(--error);"><strong>Competes with:</strong> ${escapeHtml(
              conflicts.map((c) => `"${c.phrase}" (${c.item_type} keyword)`).join(", "))}</p>`
        : "";
    if (result.suggested_hub_h1 && titleSugDiv) {
        titleSugDiv.style.display = "";
        titleSugDiv.innerHTML = `
            <div style="background: var(--primary-light); border-radius: 8px; padding: 12px;">
                <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                    <span class="material-symbols-rounded" style="font-size: 18px; color: var(--primary);">lightbulb</span>
                    <strong style="font-size: 13px;">Suggested Hub Heading</strong>
                </div>
                <p style="margin: 0 0 8px; font-size: 14px;">${escapeHtml(result.suggested_hub_h1)}</p>
                ${result.hub_h1_reasoning ? `<p style="margin: 0 0 8px; font-size: 12px; color: var(--on-surface-secondary);">${escapeHtml(result.hub_h1_reasoning)}</p>` : ""}
                ${result.keywords_targeted ? `<p style="margin: 0 0 8px; font-size: 12px; color: var(--on-surface-secondary);"><strong>Keywords:</strong> ${escapeHtml(result.keywords_targeted.join(", "))}</p>` : ""}
                ${conflictNote}
                <button class="btn btn-primary btn-sm" onclick="useAISuggestedTitle()">
                    <span class="material-symbols-rounded">check</span>
                    Use This Heading
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

    window._aiSuggestedTitle = result.suggested_hub_h1 || "";
    window._aiSuggestedIntro = result.suggested_intro || "";
    window._aiSuggestedMetaTitle = result.suggested_meta_title || "";
    window._aiSuggestedMetaDesc = result.suggested_meta_description || "";
}

function useAISuggestedTitle() {
    const input = document.getElementById("hub-h1-input");
    if (input && window._aiSuggestedTitle) {
        input.value = window._aiSuggestedTitle;
        showToast("Heading applied — click Save to push to Shopify", "success");
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

function saveBlogSeoMeta() {
    const metaTitle = (document.getElementById("blog-meta-title")?.value || "").trim();
    const metaDesc = (document.getElementById("blog-meta-description")?.value || "").trim();
    if (!metaTitle && !metaDesc) {
        showToast("Enter a meta title or description", "error");
        return;
    }
    return saveHubSettings({ hub_meta_title: metaTitle, hub_meta_description: metaDesc }, "Meta tags saved");
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

// ── Article Design (visual settings + blog template) ──────────────

let blogTemplateState = { installed: false, outdated: false, pending_retrofit: 0 };

async function loadBlogTemplateStatus() {
    const chip = document.getElementById("chip-blog-template");
    const storeId = getSelectedStoreId();
    if (!chip) return;
    if (!storeId) { chip.style.display = "none"; return; }
    chip.style.display = "";
    const dot = document.getElementById("chip-blog-template-dot");
    const statusEl = document.getElementById("blog-template-status");
    const installBtn = document.getElementById("install-blog-template-btn");
    const installLabel = document.getElementById("install-blog-template-label");
    const retrofitBtn = document.getElementById("retrofit-blog-template-btn");
    const retrofitLabel = document.getElementById("retrofit-blog-template-label");
    const revertBtn = document.getElementById("revert-blog-template-btn");

    try {
        const status = await apiRequest(`/api/blog/template-status?store_id=${storeId}`);
        blogTemplateState = status;
        chip.classList.remove("chip-installed", "chip-warning");
        if (status.installed) {
            chip.classList.add(status.outdated ? "chip-warning" : "chip-installed");
            chip.title = status.outdated ? "Article template update available" : "Styled article template installed";
            if (dot) dot.style.display = status.pending_retrofit > 0 || status.outdated ? "" : "none";
            if (statusEl) statusEl.innerHTML = `<span class="badge badge-success">Installed ${escapeHtml(status.version || "")}</span>`
                + (status.outdated ? ' <span class="badge badge-warning">Update available</span>' : "")
                + ` &nbsp;${status.applied || 0} post${status.applied === 1 ? "" : "s"} styled`
                + (status.pending_retrofit > 0 ? `, <strong>${status.pending_retrofit}</strong> on theme default` : "");
            if (installLabel) installLabel.textContent = status.outdated ? "Update Template" : "Re-push Template";
            if (installBtn) installBtn.classList.toggle("btn-primary", !!status.outdated), installBtn.classList.toggle("btn-secondary", !status.outdated);
            if (retrofitBtn) {
                retrofitBtn.style.display = status.pending_retrofit > 0 ? "" : "none";
                if (retrofitLabel) retrofitLabel.textContent = `Apply to ${status.pending_retrofit} existing post${status.pending_retrofit === 1 ? "" : "s"}`;
            }
            if (revertBtn) revertBtn.style.display = "";
        } else {
            chip.title = "Styled article template not installed — click to install";
            if (dot) dot.style.display = "none";
            if (statusEl) statusEl.innerHTML = '<span class="badge">Not installed</span> — new Visual posts will fall back to the theme layout until installed.';
            if (installLabel) installLabel.textContent = "Install Template";
            if (installBtn) { installBtn.classList.add("btn-primary"); installBtn.classList.remove("btn-secondary"); }
            if (retrofitBtn) retrofitBtn.style.display = "none";
            if (revertBtn) revertBtn.style.display = "none";
        }
    } catch (err) {
        chip.classList.remove("chip-installed", "chip-warning");
        chip.title = "";
        if (statusEl) statusEl.textContent = `Could not read theme: ${err.message}`;
    }
}

function onBlogTemplateChipClick() {
    if (!blogTemplateState.installed) { installBlogTemplate(); return; }
    const panel = document.getElementById("blog-settings-panel");
    if (panel && !panel.classList.contains("open")) toggleBlogSettings();
    document.getElementById("blog-template-status")?.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function installBlogTemplate() {
    const storeId = getSelectedStoreId();
    if (!storeId) return;
    showBlogConfirmDialog(
        blogTemplateState.installed ? "Update Article Template" : "Install Article Template",
        "This adds <code>templates/article.blog.liquid</code> and <code>sections/seo-blog-article.liquid</code> to your live theme.<br><br>" +
        "Posts using it get: brand-colored underlined links, styled takeaways / tables / callouts / FAQ, a hero image layout, related posts and BlogPosting + FAQPage structured data.<br><br>" +
        "Existing posts are not changed until you apply the template to them.",
        async () => {
            try {
                const res = await apiRequest("/api/blog/push-template", { method: "POST", body: { store_id: parseInt(storeId) } });
                showToast("Article template pushed to theme", "success");
                (res.warnings || []).forEach((w) => showToast(w, "warning"));
                loadBlogTemplateStatus();
            } catch (err) {
                showToast(err.message, "error");
            }
        }
    );
}

async function revertBlogTemplate() {
    const storeId = getSelectedStoreId();
    if (!storeId) return;
    showBlogConfirmDialog(
        "Remove Article Template",
        "All posts using the styled template are switched back to the theme's default article layout first, then the two theme files are deleted. Content and images are not touched.",
        async () => {
            try {
                const res = await apiRequest("/api/blog/revert-template", { method: "POST", body: { store_id: parseInt(storeId) } });
                showToast(`Template removed (${res.cleared || 0} posts reverted)`, "success");
                loadBlogTemplateStatus();
                loadArticles();
            } catch (err) {
                showToast(err.message, "error");
            }
        }
    );
}

async function retrofitBlogTemplate() {
    const storeId = getSelectedStoreId();
    if (!storeId) return;
    const n = blogTemplateState.pending_retrofit || 0;
    showBlogConfirmDialog(
        "Apply Template to Existing Posts",
        `Switch <strong>${n}</strong> published post${n === 1 ? "" : "s"} to the styled article template.<br><br>Only the layout changes — text, links and images stay exactly as they are. Reversible with Revert.`,
        async () => {
            const btn = document.getElementById("retrofit-blog-template-btn");
            if (btn) btn.disabled = true;
            try {
                const res = await apiRequest("/api/blog/retrofit-template", { method: "POST", body: { store_id: parseInt(storeId) } });
                showToast(`Applying template to ${res.total} posts…`, "info");
                const interval = setInterval(async () => {
                    try {
                        const task = await apiRequest(`/api/tasks/${res.task_id}`);
                        if (task.status === "completed") {
                            clearInterval(interval);
                            const r = task.result || {};
                            const failed = (r.failed || []).length;
                            showToast(`Template applied to ${r.updated || 0} posts${failed ? `, ${failed} failed` : ""}`, failed ? "warning" : "success");
                            if (btn) btn.disabled = false;
                            loadBlogTemplateStatus();
                            loadArticles();
                        } else if (task.status === "failed") {
                            clearInterval(interval);
                            showToast("Retrofit failed: " + (task.error || "Unknown error"), "error");
                            if (btn) btn.disabled = false;
                        }
                    } catch { clearInterval(interval); if (btn) btn.disabled = false; }
                }, 3000);
            } catch (err) {
                showToast(err.message, "error");
                if (btn) btn.disabled = false;
            }
        }
    );
}

async function applyTemplateToArticle(articleId, btn) {
    if (btn) btn.disabled = true;
    try {
        await apiRequest(`/api/blog/articles/${articleId}/apply-template`, { method: "POST", body: {} });
        showToast("Styled template applied", "success");
        openArticleDetail(articleId);
        loadBlogTemplateStatus();
    } catch (err) {
        showToast(err.message, "error");
        if (btn) btn.disabled = false;
    }
}

function _syncBrandColorInputs(fromHex) {
    const picker = document.getElementById("visual-brand-color");
    const hex = document.getElementById("visual-brand-color-hex");
    if (!picker || !hex) return;
    if (fromHex) {
        const v = hex.value.trim();
        if (/^#[0-9a-fA-F]{6}$/.test(v)) picker.value = v.toLowerCase();
    } else {
        hex.value = picker.value;
    }
}

let visualSettingsBound = false;
async function loadVisualSettings() {
    const storeId = getSelectedStoreId();
    const picker = document.getElementById("visual-brand-color");
    if (!storeId || !picker) return;
    if (!visualSettingsBound) {
        visualSettingsBound = true;
        picker.addEventListener("input", () => _syncBrandColorInputs(false));
        document.getElementById("visual-brand-color-hex").addEventListener("input", () => _syncBrandColorInputs(true));
        const styleSel = document.getElementById("visual-default-style");
        styleSel.addEventListener("change", () => {
            const radio = document.querySelector(`input[name="wizard-style"][value="${styleSel.value}"]`);
            if (radio) radio.checked = true;
        });
    }
    try {
        const data = await apiRequest(`/api/blog/visual-settings?store_id=${storeId}`);
        const v = data.effective || {};
        picker.value = v.brand_color || "#1a73e8";
        document.getElementById("visual-brand-color-hex").value = v.brand_color || "#1a73e8";
        document.getElementById("visual-default-style").value = v.default_style || "visual";
        document.getElementById("visual-inline-count").value = String(v.inline_image_count ?? 2);
        document.getElementById("visual-product-composite").checked = !!v.product_composite;
        document.getElementById("visual-blocks").checked = !!v.visual_blocks;
        document.getElementById("visual-toc").checked = !!v.toc;
        const radio = document.querySelector(`input[name="wizard-style"][value="${v.default_style || "visual"}"]`);
        if (radio) radio.checked = true;
        _renderColorWarnings(v.link_color_warnings || []);
    } catch (err) {
        console.warn("visual settings load failed", err);
    }
}

function _renderColorWarnings(warnings) {
    const el = document.getElementById("visual-color-warning");
    if (!el) return;
    if (!warnings.length) { el.style.display = "none"; el.textContent = ""; return; }
    el.style.display = "";
    el.innerHTML = warnings.map((w) => `<div>⚠ ${escapeHtml(w)}</div>`).join("");
}

async function saveVisualSettings() {
    const storeId = getSelectedStoreId();
    if (!storeId) return;
    const hexInput = document.getElementById("visual-brand-color-hex").value.trim();
    const body = {
        store_id: parseInt(storeId),
        brand_color: /^#[0-9a-fA-F]{3,6}$/.test(hexInput) ? hexInput : document.getElementById("visual-brand-color").value,
        default_style: document.getElementById("visual-default-style").value,
        inline_image_count: parseInt(document.getElementById("visual-inline-count").value),
        product_composite: document.getElementById("visual-product-composite").checked,
        visual_blocks: document.getElementById("visual-blocks").checked,
        toc: document.getElementById("visual-toc").checked,
    };
    try {
        const res = await apiRequest("/api/blog/visual-settings", { method: "POST", body });
        const v = res.effective || {};
        _renderColorWarnings(v.link_color_warnings || []);
        showToast(blogTemplateState.installed
            ? "Design settings saved — re-push the template to apply the new color to the storefront"
            : "Design settings saved", "success");
        loadVisualSettings();
    } catch (err) {
        showToast(err.message, "error");
    }
}

async function pushSeoSnippet() {
    const storeId = getSelectedStoreId();
    if (!storeId) return;

    showBlogConfirmDialog(
        "Push Theme SEO Fixes",
        "This will add a Liquid snippet to your theme that fixes:<br><br>" +
        "&#8226; Duplicate meta description tags<br>" +
        "&#8226; font-display: swap for web fonts<br>" +
        "&#8226; Twitter meta tags<br>" +
        "&#8226; og:type, Blog and breadcrumb schema on the blog hub<br>" +
        "&#8226; noindex on tag archives, paginated blog pages and the landing article's own URL<br><br>" +
        "A <code>snippets/seo-fixes.liquid</code> file will be created and rendered in <code>layout/theme.liquid</code>. " +
        "The blog section is also edited so the hub prints its own heading and drops the full tag list (Revert restores it).",
        async () => {
            try {
                const res = await apiRequest("/api/blog/push-seo-snippet", {
                    method: "POST",
                    body: { store_id: parseInt(storeId) },
                });
                const parts = (res && res.blog_section_patched) || [];
                showToast("SEO snippet pushed to theme" + (parts.length ? " (blog section: " + parts.join(", ") + ")" : ""), "success");
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

// ── Scheduled Posts ──────────────────────────────────────────────

let scheduledPosts = [];
let scheduledPostsTimer = null;

const SCHEDULED_STAGES = {
    auto_media: "Generating inline images",
    auto_queued: "Queued",
    auto_researching: "Researching…",
    auto_outlining: "Outlining…",
    auto_generating: "Writing…",
    auto_image: "Image…",
    auto_publishing: "Publishing…",
};

// datetime-local value ("YYYY-MM-DDTHH:MM") from local date parts — toISOString
// would shift to UTC and show the wrong wall-clock time in the input.
function toLocalInputValue(date) {
    const p = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())}T${p(date.getHours())}:${p(date.getMinutes())}`;
}

// Cadence HH:MM is stored in UTC; the inputs show local wall-clock time.
function utcTimeToLocal(hhmm) {
    const [h, m] = String(hhmm || "09:00").split(":").map(Number);
    const d = new Date();
    d.setUTCHours(h, m, 0, 0);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

function localTimeToUtc(hhmm) {
    const [h, m] = String(hhmm || "09:00").split(":").map(Number);
    const d = new Date();
    d.setHours(h, m, 0, 0);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

function initScheduledTab() {
    loadScheduleSettings();
    loadScheduledPosts();
}

function renderNextSlot(iso) {
    const el = document.getElementById("schedule-next-slot");
    if (el && iso) el.textContent = `Next open slot: ${new Date(iso).toLocaleString()}`;
}

async function loadScheduleSettings() {
    const storeId = await getFirstStoreId();
    if (!storeId) return;
    try {
        const s = await apiRequest(`/api/blog/schedule-settings?store_id=${storeId}`);
        document.getElementById("schedule-interval-days").value = s.interval_days;
        document.getElementById("schedule-publish-time").value = utcTimeToLocal(s.publish_time);
        renderNextSlot(s.next_slot);
    } catch (err) {
        console.error("Failed to load schedule settings", err);
    }
}

async function saveScheduleSettings() {
    const storeId = await getFirstStoreId();
    if (!storeId) return showToast("No store found", "error");
    const intervalDays = parseInt(document.getElementById("schedule-interval-days").value, 10);
    if (!intervalDays || intervalDays < 1) return showToast("Interval must be at least 1 day", "error");
    const localTime = document.getElementById("schedule-publish-time").value || "09:00";
    try {
        const res = await apiRequest("/api/blog/schedule-settings", {
            method: "PUT",
            body: { store_id: storeId, interval_days: intervalDays, publish_time: localTimeToUtc(localTime) },
        });
        renderNextSlot(res.next_slot);
        showToast("Cadence saved", "success");
    } catch (err) {
        showToast(err.message, "error");
    }
}

async function scheduleTopicSuggestion(id) {
    const s = findTopicSuggestion(id);
    if (!s) return;
    const storeId = await getFirstStoreId();
    if (!storeId) return showToast("No store found", "error");
    try {
        const res = await apiRequest("/api/blog/schedule-from-suggestion", {
            method: "POST",
            body: { store_id: storeId, suggestion_id: id },
        });
        s.status = "scheduled";
        renderTopicRuns();
        showToast(`Queued for ${new Date(res.scheduled_for).toLocaleString()} — writing the draft now. Approve it in the Scheduled tab before it publishes.`, "success");
        // Show the work happening: the Scheduled tab live-refreshes the row
        // through Researching… → Outlining… → Writing… → Image… → Ready.
        const schedTab = document.querySelector('.tab[data-tab="scheduled"]');
        if (schedTab) schedTab.click();
    } catch (err) {
        showToast(err.message, "error");
    }
}

function scheduledPostChip(row) {
    const s = row.status;
    if (s === "scheduled" && !row.reviewed_at) return `<span class="badge" style="background: var(--warning); color: #000;" title="Preview it, then approve. It will not publish until you do.">Needs review</span>`;
    if (s === "scheduled") return `<span class="badge" style="background: var(--success); color: #fff;">Approved</span>`;
    if (s === "failed") return `<span class="badge" style="background: var(--error); color: #fff;" title="${escapeAttr(row.error || "")}">Failed</span> <span style="font-size:12px;color:var(--error);">${escapeHtml(row.error || "")}</span>`;
    return `<span class="badge" style="background: var(--primary); color: #fff;">${escapeHtml(SCHEDULED_STAGES[s] || "Working…")}</span>`;
}

async function loadScheduledPosts() {
    const container = document.getElementById("blog-scheduled-container");
    const storeId = await getFirstStoreId();
    if (!storeId) { container.innerHTML = `<div class="card"><p>No store found.</p></div>`; return; }
    let rows;
    try {
        const res = await apiRequest(`/api/blog/scheduled?store_id=${storeId}`);
        rows = res.sessions || [];
    } catch (e) {
        container.innerHTML = `<div class="card"><p style="color:var(--error);">${escapeHtml(e.message)}</p></div>`;
        return;
    }
    scheduledPosts = rows;
    if (!rows.length) {
        container.innerHTML = `<div class="card"><p style="color:var(--on-surface-secondary);">Nothing queued. Use “Schedule” on a topic in the Create Post tab — the draft is written and lands here for your review before it publishes.</p></div>`;
        return;
    }
    const body = rows.map((row, i) => {
        const canAct = row.status === "scheduled" || row.status === "failed";
        const busy = row.status === "auto_publishing";
        const whenInput = `<span style="display:inline-flex; gap:6px; align-items:center;">
            <input type="datetime-local" id="blog-sched-when-${i}" class="form-control" style="max-width:200px;" value="${toLocalInputValue(new Date(row.scheduled_for))}" ${busy ? "disabled" : ""}>
            <button class="icon-btn" title="Save new time" onclick="rescheduleBlogPost(${i})" ${busy ? "disabled" : ""}><span class="material-symbols-rounded">save</span></button>
        </span>`;
        return `<tr>
            <td>${escapeHtml(row.title || row.topic || "(processing…)")} ${row.style === "classic" ? '<span class="badge" title="Legacy text article">Classic</span>' : '<span class="badge badge-success" title="Images, blocks, styled template">Visual</span>'}</td>
            <td style="white-space:nowrap;">${whenInput}</td>
            <td>${scheduledPostChip(row)}</td>
            <td style="white-space:nowrap;">
                <button class="btn btn-secondary btn-sm" onclick="previewScheduledPost(${i})" ${canAct ? "" : "disabled"}>
                    <span class="material-symbols-rounded">visibility</span> Preview
                </button>
                ${row.status === "scheduled" ? (row.reviewed_at
                    ? `<button class="btn btn-secondary btn-sm" style="margin-left:6px;" title="Put back on hold" onclick="approveScheduledPost(${i}, false, this)"><span class="material-symbols-rounded">undo</span> Unapprove</button>`
                    : `<button class="btn btn-primary btn-sm" style="margin-left:6px;" title="Allow this post to publish at its scheduled time" onclick="approveScheduledPost(${i}, true, this)"><span class="material-symbols-rounded">check</span> Approve</button>`) : ""}
                <button class="btn btn-secondary btn-sm" style="margin-left:6px;" onclick="publishBlogNow(${i}, this)" ${canAct ? "" : "disabled"}>
                    <span class="material-symbols-rounded">publish</span> ${row.status === "failed" ? "Retry now" : "Publish now"}
                </button>
                <button class="btn btn-secondary btn-sm" style="margin-left:6px;color:var(--error);" title="Cancel scheduled post" onclick="cancelScheduledPost(${i}, this)" ${canAct || row.status === "auto_queued" ? "" : "disabled"}>
                    <span class="material-symbols-rounded">delete</span>
                </button>
            </td>
        </tr>`;
    }).join("");
    container.innerHTML = `<div class="table-container"><table class="data-table">
        <thead><tr><th>Title</th><th>Publish at</th><th>Status</th><th></th></tr></thead>
        <tbody>${body}</tbody></table></div>`;

    // Keep refreshing while any session is still being prepared or publishing.
    clearTimeout(scheduledPostsTimer);
    const transient = rows.some((r) => r.status !== "scheduled" && r.status !== "failed");
    const panel = document.getElementById("tab-scheduled");
    if (transient && panel && panel.classList.contains("active")) {
        scheduledPostsTimer = setTimeout(loadScheduledPosts, 5000);
    }
}

async function approveScheduledPost(index, approved, btn) {
    const row = scheduledPosts[index];
    if (!row) return;
    if (btn) btn.disabled = true;
    try {
        const res = await apiRequest(`/api/blog/scheduled/${row.id}/approve`, { method: "POST", body: { approved } });
        showToast(approved
            ? `Approved — publishes ${new Date(res.scheduled_for).toLocaleString()}.`
            : "Back on hold — it will not publish until approved.", "success");
    } catch (e) {
        showToast(`Could not update approval: ${e.message}`, "error");
    }
    loadScheduledPosts();
}

async function rescheduleBlogPost(index) {
    const row = scheduledPosts[index];
    const input = document.getElementById(`blog-sched-when-${index}`);
    if (!row || !input) return;
    const when = input.value ? new Date(input.value) : null;
    if (!when || isNaN(when.getTime())) return showToast("Pick a valid date/time.", "error");
    try {
        await apiRequest(`/api/blog/scheduled/${row.id}`, {
            method: "PATCH",
            body: { scheduled_at: when.toISOString() },
        });
        // A saved time is not an approval — say so, or the post silently
        // sits in the queue while the scheduler skips it.
        if (row.status === "scheduled" && !row.reviewed_at) {
            showToast("Publish time updated — still needs your approval before it publishes.", "warning");
        } else {
            showToast("Publish time updated.", "success");
        }
        loadScheduledPosts();
    } catch (e) {
        showToast(`Reschedule failed: ${e.message}`, "error");
    }
}

async function publishBlogNow(index, btn) {
    const row = scheduledPosts[index];
    if (!row) return;
    if (btn) btn.disabled = true;
    let res;
    try {
        res = await apiRequest(`/api/blog/scheduled/${row.id}/publish-now`, { method: "POST" });
    } catch (e) {
        if (btn) btn.disabled = false;
        return showToast(`Publish failed: ${e.message}`, "error");
    }
    if (res.resumed) {
        showToast("Resuming preparation — the post will park as Ready, then publish.", "info");
        return loadScheduledPosts();
    }
    const poll = async () => {
        let s;
        try {
            s = await apiRequest(`/api/blog/wizard/${row.id}`);
        } catch { /* transient — keep polling */ }
        if (s && s.status === "published") {
            showToast("Post published.", "success");
            return loadScheduledPosts();
        }
        if (s && s.status === "failed") {
            showToast(`Publish failed: ${s.error || "unknown error"}`, "error");
            return loadScheduledPosts();
        }
        setTimeout(poll, 2000);
    };
    poll();
}

async function cancelScheduledPost(index, btn) {
    const row = scheduledPosts[index];
    if (!row) return;
    if (!confirm(`Cancel “${row.title || row.topic}”? The prepared post is discarded and the idea returns to the suggestion pool.`)) return;
    if (btn) btn.disabled = true;
    try {
        await apiRequest(`/api/blog/scheduled/${row.id}`, { method: "DELETE" });
        showToast("Scheduled post cancelled.", "success");
        loadScheduledPosts();
        loadTopicSuggestions();
    } catch (e) {
        if (btn) btn.disabled = false;
        showToast(`Cancel failed: ${e.message}`, "error");
    }
}

async function previewScheduledPost(index) {
    const row = scheduledPosts[index];
    if (!row) return;
    let s;
    try {
        s = await apiRequest(`/api/blog/wizard/${row.id}`);
    } catch (e) {
        return showToast(e.message, "error");
    }
    const art = s.generated_article || {};
    document.getElementById("scheduled-preview-title").textContent = art.title || s.topic || "Scheduled Post";
    let html = "";
    if (art.meta_title) html += `<p style="font-size:13px;"><strong>Meta title:</strong> ${escapeHtml(art.meta_title)}</p>`;
    if (art.meta_description) html += `<p style="font-size:13px;"><strong>Meta description:</strong> ${escapeHtml(art.meta_description)}</p>`;
    if (s.featured_image_url) html += `<img src="${escapeAttr(s.featured_image_url)}" alt="" style="max-width:320px;border-radius:8px;margin:8px 0;">`;
    const isVisual = s.style !== "classic";
    if (art.body_html) {
        html += isVisual
            ? `<div class="a2z-blog" style="background:#fff;color:#1f2937;border-radius:8px;padding:12px;"><div class="a2z-blog__body" id="scheduled-preview-body"></div></div>`
            : `<div style="margin-top:12px;">${art.body_html}</div>`;
    } else {
        html += `<p style="color:var(--on-surface-secondary);">Article not generated yet.</p>`;
    }
    document.getElementById("scheduled-preview-content").innerHTML = html;
    document.getElementById("scheduled-preview-modal").classList.add("active");
    if (art.body_html && isVisual) {
        await ensureArticlePreviewCss();
        renderComposedPreview(row.id, document.getElementById("scheduled-preview-body"));
    }
}

function closeScheduledPreview() {
    document.getElementById("scheduled-preview-modal").classList.remove("active");
}
