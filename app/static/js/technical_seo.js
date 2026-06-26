let tseoProductSort = loadFilter("tseoProductSort", { by: "title", dir: "asc" });
let tseoCollectionSort = loadFilter("tseoCollectionSort", { by: "title", dir: "asc" });
let tseoStatusFilter = loadFilter("tseoStatusFilter", "");
let tseoScoreFilter = loadFilter("tseoScoreFilter", "");
let tseoCollectionScoreFilter = loadFilter("tseoCollectionScoreFilter", "");
let tseoNotAnalyzedFilter = loadFilter("tseoNotAnalyzedFilter", false);
let tseoCollectionNotAnalyzedFilter = loadFilter("tseoCollectionNotAnalyzedFilter", false);
let selectedTseoProductIds = new Set();
let selectedTseoCollectionIds = new Set();
let currentTseoProducts = [];
let currentTseoCollections = [];
let lastClickedTseoProductIndex = -1;
let lastClickedTseoCollectionIndex = -1;
let tseoProductSearchTerm = "";
let _tseoProductSearchTimer = null;
let tseoCollectionSearchTerm = "";
let _tseoCollectionSearchTimer = null;

const CHECK_LABELS = {
    title_has_keyword: "Title Contains Keyword",
    keyword_at_title_start: "Keyword Position in Title",
    meta_title_has_keyword: "Meta Title Contains Keyword",
    meta_title_length: "Meta Title Length",
    meta_desc_has_keyword: "Meta Description Contains Keyword",
    meta_desc_length: "Meta Description Length",
    url_has_keyword: "URL Contains Keyword",
    body_first_100_has_keyword: "Keyword in First 100 Words",
    keyword_density: "Keyword Density",
    description_word_count: "Description Word Count",
    heading_structure: "Heading Structure",
    image_alt_coverage: "Image Alt Coverage",
    image_alt_has_keyword: "Image Alt Contains Keyword",
};

const CHECK_GROUPS = [
    { label: "Title & URL", icon: "title", keys: ["title_has_keyword", "keyword_at_title_start", "url_has_keyword"] },
    { label: "Meta Tags", icon: "sell", keys: ["meta_title_has_keyword", "meta_title_length", "meta_desc_has_keyword", "meta_desc_length"] },
    { label: "Content", icon: "article", keys: ["body_first_100_has_keyword", "keyword_density", "description_word_count", "heading_structure"] },
    { label: "Images", icon: "image", keys: ["image_alt_coverage", "image_alt_has_keyword"] },
];

function setupTseoModalDismiss(overlayId, closeFn) {
    const overlay = document.getElementById(overlayId);
    if (!overlay) return;
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) closeFn();
    });
}

document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
        const modal = document.getElementById("tseo-detail-modal");
        if (modal && modal.classList.contains("active")) closeTseoModal();
    }
});

document.addEventListener("DOMContentLoaded", () => {
    initTseoTabs();
    setupTseoModalDismiss("tseo-detail-modal", closeTseoModal);
});

function initTseoTabs() {
    const savedTab = loadFilter("tseoTab", "products");

    document.querySelectorAll(".tab").forEach((tab) => {
        tab.addEventListener("click", () => {
            document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
            document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
            tab.classList.add("active");
            document.getElementById("tab-" + tab.dataset.tab).classList.add("active");
            saveFilter("tseoTab", tab.dataset.tab);

            if (tab.dataset.tab === "products") loadTseoProducts();
            if (tab.dataset.tab === "collections") loadTseoCollections();
        });
    });

    const tabBtn = document.querySelector(`.tab[data-tab="${savedTab}"]`);
    if (tabBtn && savedTab !== "products") {
        tabBtn.click();
    } else {
        loadTseoProducts();
    }
}

function updateTseoTabCounts() {
    const prodTab = document.querySelector('.tab[data-tab="products"]');
    const collTab = document.querySelector('.tab[data-tab="collections"]');
    if (prodTab && allTseoProducts.length > 0) prodTab.textContent = `Products (${allTseoProducts.length})`;
    if (collTab && allTseoCollections.length > 0) collTab.textContent = `Collections (${allTseoCollections.length})`;
}

// --- Products ---

let allTseoProducts = [];

function setTseoStatusFilter(status) {
    tseoStatusFilter = tseoStatusFilter === status ? "" : status;
    saveFilter("tseoStatusFilter", tseoStatusFilter);
    selectedTseoProductIds.clear();
    applyTseoProductFilters();
}

function setTseoScoreFilter(score) {
    if (score === "not_analyzed") {
        tseoNotAnalyzedFilter = !tseoNotAnalyzedFilter;
        tseoScoreFilter = "";
        saveFilter("tseoNotAnalyzedFilter", tseoNotAnalyzedFilter);
        saveFilter("tseoScoreFilter", "");
    } else {
        tseoNotAnalyzedFilter = false;
        tseoScoreFilter = tseoScoreFilter === score ? "" : score;
        saveFilter("tseoNotAnalyzedFilter", false);
        saveFilter("tseoScoreFilter", tseoScoreFilter);
    }
    selectedTseoProductIds.clear();
    applyTseoProductFilters();
}

function applyTseoProductFilters() {
    let filtered = allTseoProducts;
    if (tseoStatusFilter) {
        filtered = filtered.filter(p => p.status === tseoStatusFilter);
    }
    if (tseoNotAnalyzedFilter) {
        filtered = filtered.filter(p => p.tech_seo_score === null || p.tech_seo_score === undefined);
    } else if (tseoScoreFilter === "good") {
        filtered = filtered.filter(p => p.tech_seo_score !== null && p.tech_seo_score >= 80);
    } else if (tseoScoreFilter === "fair") {
        filtered = filtered.filter(p => p.tech_seo_score !== null && p.tech_seo_score >= 50 && p.tech_seo_score < 80);
    } else if (tseoScoreFilter === "poor") {
        filtered = filtered.filter(p => p.tech_seo_score !== null && p.tech_seo_score < 50);
    }
    if (tseoProductSearchTerm) {
        const term = tseoProductSearchTerm.toLowerCase();
        filtered = filtered.filter(p => (p.title || "").toLowerCase().includes(term) || (p.handle || "").toLowerCase().includes(term));
    }
    currentTseoProducts = filtered;
    const container = document.getElementById("tseo-products-container");
    if (filtered.length === 0 && !tseoStatusFilter && !tseoScoreFilter && !tseoNotAnalyzedFilter && !tseoProductSearchTerm && allTseoProducts.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <span class="material-symbols-rounded">inventory_2</span>
                <p>No products synced yet. Go to Stores and sync your store first.</p>
                <a href="/stores" class="btn btn-primary">Go to Stores</a>
            </div>`;
        return;
    }
    renderTseoProductsTable(container, filtered);
}

async function loadTseoProducts() {
    const container = document.getElementById("tseo-products-container");
    try {
        let url = `/api/technical-seo/products?sort_by=${tseoProductSort.by}&sort_dir=${tseoProductSort.dir}`;
        const products = await apiRequest(url);
        allTseoProducts = products;
        updateTseoTabCounts();
        applyTseoProductFilters();
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>${escapeHtml(err.message)}</p></div>`;
    }
}

function renderTseoProductsTable(container, products) {
    let html = "";
    const hasMultipleStores = new Set(allTseoProducts.map(p => p.store_name)).size > 1;

    // Filter groups
    html += '<div class="filter-groups">';
    html += '<div class="filter-group">';
    html += '<span class="filter-group-label">Status</span>';
    const statuses = ["ACTIVE", "DRAFT", "ARCHIVED"];
    for (const s of statuses) {
        const active = tseoStatusFilter === s ? " active" : "";
        html += `<button class="filter-chip${active}" onclick="setTseoStatusFilter('${s}')">${s}</button>`;
    }
    html += '</div>';
    html += '<div class="filter-group-divider"></div>';
    html += '<div class="filter-group">';
    html += '<span class="filter-group-label">Score</span>';
    const scores = [
        { key: "good", label: "Good (80+)" },
        { key: "fair", label: "Fair (50-79)" },
        { key: "poor", label: "Poor (<50)" },
    ];
    for (const s of scores) {
        const active = tseoScoreFilter === s.key ? " active" : "";
        html += `<button class="filter-chip${active}" onclick="setTseoScoreFilter('${s.key}')">${s.label}</button>`;
    }
    const naActive = tseoNotAnalyzedFilter ? " active" : "";
    html += `<button class="filter-chip${naActive}" onclick="setTseoScoreFilter('not_analyzed')" style="${tseoNotAnalyzedFilter ? '' : 'border-style: dashed;'}">Not Analyzed</button>`;
    html += '</div>';
    if (tseoStatusFilter || tseoScoreFilter || tseoNotAnalyzedFilter || tseoProductSearchTerm) {
        html += `<button class="filter-chip" onclick="clearTseoProductFilters()" style="border-style: dashed;">
            <span class="material-symbols-rounded" style="font-size: 16px;">clear_all</span> Clear
        </button>`;
    }
    html += '</div>';

    // Search bar
    html += `<div class="search-bar">
        <span class="material-symbols-rounded">search</span>
        <input class="search-input" type="text" placeholder="Search products by title or handle..." value="${escapeAttr(tseoProductSearchTerm)}" oninput="onTseoProductSearch(this.value)">
        ${tseoProductSearchTerm ? '<button class="search-clear" onclick="onTseoProductSearch(\'\', true)"><span class="material-symbols-rounded" style="font-size:18px;">close</span></button>' : ''}
    </div>`;

    // Empty state for filters
    if (products.length === 0 && (tseoStatusFilter || tseoScoreFilter || tseoNotAnalyzedFilter || tseoProductSearchTerm)) {
        html += `<div class="empty-state">
            <span class="material-symbols-rounded">filter_list_off</span>
            <p>No products match the current filters${tseoProductSearchTerm ? " or search" : ""}.</p>
            <button class="btn btn-secondary" onclick="clearTseoProductFilters()">
                <span class="material-symbols-rounded">clear_all</span> Clear Filters
            </button>
        </div>`;
        container.innerHTML = html;
        return;
    }

    // Summary strip
    const analyzed = products.filter(p => p.tech_seo_score !== null && p.tech_seo_score !== undefined);
    const avgScore = analyzed.length > 0 ? Math.round(analyzed.reduce((s, p) => s + p.tech_seo_score, 0) / analyzed.length) : 0;
    const goodCount = analyzed.filter(p => p.tech_seo_score >= 80).length;
    const fairCount = analyzed.filter(p => p.tech_seo_score >= 50 && p.tech_seo_score < 80).length;
    const poorCount = analyzed.filter(p => p.tech_seo_score < 50).length;

    html += `<div class="summary-strip">
        <span class="stat-item"><strong>${products.length}</strong> products</span>
        <span class="stat-divider"></span>
        <span class="stat-item">Avg score: <strong>${avgScore}</strong></span>
        <span class="stat-divider"></span>
        <span class="stat-item"><strong>${analyzed.length}</strong> analyzed</span>
        <span class="stat-divider"></span>
        <span class="stat-item" style="color: var(--success);"><strong>${goodCount}</strong> good</span>
        <span class="stat-item" style="color: var(--warning);"><strong>${fairCount}</strong> fair</span>
        <span class="stat-item" style="color: var(--error);"><strong>${poorCount}</strong> poor</span>
    </div>`;

    // Bulk toolbar
    const selCount = selectedTseoProductIds.size;
    if (selCount > 0) {
        html += `<div class="bulk-toolbar">
            <span class="bulk-count">${selCount} product${selCount > 1 ? "s" : ""} selected</span>
            <button class="btn btn-primary btn-sm" onclick="bulkAnalyzeTseoProducts()">
                <span class="material-symbols-rounded">analytics</span> Bulk Analyze
            </button>
            <button class="btn btn-secondary btn-sm" onclick="bulkFixApplyTseoProducts()">
                <span class="material-symbols-rounded">auto_fix_high</span> Bulk Analyze + Fix + Apply
            </button>
        </div>`;
    }

    // Table
    html += '<div class="table-container"><table><thead><tr>';
    html += `<th class="checkbox-col"><input type="checkbox" class="table-checkbox" onchange="toggleTseoSelectAllProducts(this)" ${selCount === products.length && products.length > 0 ? "checked" : ""}></th>`;
    html += `<th class="sortable-header ${tseoProductSort.by === 'title' ? tseoProductSort.dir : ''}" onclick="toggleTseoProductSort('title')">Title</th>`;
    if (hasMultipleStores) html += `<th>Store</th>`;
    html += `<th>Status</th>`;
    html += `<th class="sortable-header ${tseoProductSort.by === 'tech_seo_score' ? tseoProductSort.dir : ''}" onclick="toggleTseoProductSort('tech_seo_score')">Score</th>`;
    html += `<th>Issues</th>`;
    html += `<th>Last Analyzed</th>`;
    html += `<th>Actions</th>`;
    html += '</tr></thead><tbody>';

    products.forEach((p, idx) => {
        const checked = selectedTseoProductIds.has(p.id) ? "checked" : "";
        const rowClass = selectedTseoProductIds.has(p.id) ? "row-selected" : "";
        const statusBadge = p.status === "ACTIVE"
            ? '<span class="badge badge-success">Active</span>'
            : `<span class="badge badge-warning">${escapeHtml(p.status || "Unknown")}</span>`;

        let scoreCell;
        if (p.tech_seo_score !== null && p.tech_seo_score !== undefined) {
            scoreCell = renderScoreRing(p.tech_seo_score);
        } else {
            scoreCell = '<span class="badge badge-not-analyzed">Not analyzed</span>';
        }

        const issuesCell = p.issues_count !== null && p.issues_count !== undefined
            ? `<span class="badge ${p.issues_count === 0 ? 'badge-success' : 'badge-error'}">${p.issues_count}</span>`
            : "—";

        const analyzedAt = p.analyzed_at
            ? `<span title="${escapeAttr(formatDate(p.analyzed_at))}">${formatRelativeTime(p.analyzed_at)}</span>`
            : '<span style="color: var(--on-surface-secondary);">Never</span>';

        html += `<tr class="${rowClass}">
            <td class="checkbox-col"><input type="checkbox" class="table-checkbox" ${checked} onclick="toggleTseoProductSelect(${p.id}, this, event)" data-index="${idx}"></td>
            <td><strong title="/${escapeAttr(p.handle || "")}">${escapeHtml(p.title || "Untitled")}</strong></td>
            ${hasMultipleStores ? `<td>${escapeHtml(p.store_name || "")}</td>` : ''}
            <td>${statusBadge}</td>
            <td>${scoreCell}</td>
            <td>${issuesCell}</td>
            <td>${analyzedAt}</td>
            <td>
                <div class="actions-cell">
                    <button class="action-icon" onclick="analyzeTseoItem('product', ${p.id})" title="Analyze">
                        <span class="material-symbols-rounded">analytics</span>
                    </button>
                    ${p.tech_seo_score !== null && p.tech_seo_score !== undefined ? `<button class="action-icon" onclick="viewTseoDetail('product', ${p.id})" title="View"><span class="material-symbols-rounded">visibility</span></button>` : ""}
                </div>
            </td>
        </tr>`;
    });

    html += '</tbody></table></div>';
    container.innerHTML = html;
}

function onTseoProductSearch(value, immediate) {
    clearTimeout(_tseoProductSearchTimer);
    if (immediate) {
        tseoProductSearchTerm = value;
        applyTseoProductFilters();
        return;
    }
    _tseoProductSearchTimer = setTimeout(() => {
        tseoProductSearchTerm = value;
        applyTseoProductFilters();
    }, 300);
}

function clearTseoProductFilters() {
    tseoStatusFilter = "";
    tseoScoreFilter = "";
    tseoNotAnalyzedFilter = false;
    tseoProductSearchTerm = "";
    saveFilter("tseoStatusFilter", "");
    saveFilter("tseoScoreFilter", "");
    saveFilter("tseoNotAnalyzedFilter", false);
    selectedTseoProductIds.clear();
    loadTseoProducts();
}

function toggleTseoProductSort(key) {
    if (tseoProductSort.by === key) {
        tseoProductSort.dir = tseoProductSort.dir === "asc" ? "desc" : "asc";
    } else {
        tseoProductSort = { by: key, dir: "asc" };
    }
    saveFilter("tseoProductSort", tseoProductSort);
    loadTseoProducts();
}

function toggleTseoProductSelect(id, checkbox, event) {
    const idx = parseInt(checkbox.dataset.index, 10);
    const isChecked = checkbox.checked;

    if (event && event.shiftKey && lastClickedTseoProductIndex >= 0) {
        const start = Math.min(lastClickedTseoProductIndex, idx);
        const end = Math.max(lastClickedTseoProductIndex, idx);
        for (let i = start; i <= end; i++) {
            const p = currentTseoProducts[i];
            if (p) {
                if (isChecked) {
                    selectedTseoProductIds.add(p.id);
                } else {
                    selectedTseoProductIds.delete(p.id);
                }
            }
        }
    } else {
        if (isChecked) selectedTseoProductIds.add(id);
        else selectedTseoProductIds.delete(id);
    }

    lastClickedTseoProductIndex = idx;
    renderTseoProductsTable(document.getElementById("tseo-products-container"), currentTseoProducts);
}

function toggleTseoSelectAllProducts(checkbox) {
    if (checkbox.checked) {
        currentTseoProducts.forEach(p => selectedTseoProductIds.add(p.id));
    } else {
        selectedTseoProductIds.clear();
    }
    renderTseoProductsTable(document.getElementById("tseo-products-container"), currentTseoProducts);
}

// --- Collections ---

let allTseoCollections = [];

function setTseoCollectionScoreFilter(score) {
    if (score === "not_analyzed") {
        tseoCollectionNotAnalyzedFilter = !tseoCollectionNotAnalyzedFilter;
        tseoCollectionScoreFilter = "";
        saveFilter("tseoCollectionNotAnalyzedFilter", tseoCollectionNotAnalyzedFilter);
        saveFilter("tseoCollectionScoreFilter", "");
    } else {
        tseoCollectionNotAnalyzedFilter = false;
        tseoCollectionScoreFilter = tseoCollectionScoreFilter === score ? "" : score;
        saveFilter("tseoCollectionNotAnalyzedFilter", false);
        saveFilter("tseoCollectionScoreFilter", tseoCollectionScoreFilter);
    }
    selectedTseoCollectionIds.clear();
    applyTseoCollectionFilters();
}

function applyTseoCollectionFilters() {
    let filtered = allTseoCollections;
    if (tseoCollectionNotAnalyzedFilter) {
        filtered = filtered.filter(c => c.tech_seo_score === null || c.tech_seo_score === undefined);
    } else if (tseoCollectionScoreFilter === "good") {
        filtered = filtered.filter(c => c.tech_seo_score !== null && c.tech_seo_score >= 80);
    } else if (tseoCollectionScoreFilter === "fair") {
        filtered = filtered.filter(c => c.tech_seo_score !== null && c.tech_seo_score >= 50 && c.tech_seo_score < 80);
    } else if (tseoCollectionScoreFilter === "poor") {
        filtered = filtered.filter(c => c.tech_seo_score !== null && c.tech_seo_score < 50);
    }
    if (tseoCollectionSearchTerm) {
        const term = tseoCollectionSearchTerm.toLowerCase();
        filtered = filtered.filter(c => (c.title || "").toLowerCase().includes(term) || (c.handle || "").toLowerCase().includes(term));
    }
    currentTseoCollections = filtered;
    const container = document.getElementById("tseo-collections-container");
    if (filtered.length === 0 && !tseoCollectionScoreFilter && !tseoCollectionNotAnalyzedFilter && !tseoCollectionSearchTerm && allTseoCollections.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <span class="material-symbols-rounded">folder</span>
                <p>No collections synced yet.</p>
            </div>`;
        return;
    }
    renderTseoCollectionsTable(container, filtered);
}

async function loadTseoCollections() {
    const container = document.getElementById("tseo-collections-container");
    try {
        let url = `/api/technical-seo/collections?sort_by=${tseoCollectionSort.by}&sort_dir=${tseoCollectionSort.dir}`;
        const collections = await apiRequest(url);
        allTseoCollections = collections;
        updateTseoTabCounts();
        applyTseoCollectionFilters();
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>${escapeHtml(err.message)}</p></div>`;
    }
}

function renderTseoCollectionsTable(container, collections) {
    let html = "";
    const hasMultipleStores = new Set(allTseoCollections.map(c => c.store_name)).size > 1;

    // Filter groups
    html += '<div class="filter-groups">';
    html += '<div class="filter-group">';
    html += '<span class="filter-group-label">Score</span>';
    const scores = [
        { key: "good", label: "Good (80+)" },
        { key: "fair", label: "Fair (50-79)" },
        { key: "poor", label: "Poor (<50)" },
    ];
    for (const s of scores) {
        const active = tseoCollectionScoreFilter === s.key ? " active" : "";
        html += `<button class="filter-chip${active}" onclick="setTseoCollectionScoreFilter('${s.key}')">${s.label}</button>`;
    }
    const naActive = tseoCollectionNotAnalyzedFilter ? " active" : "";
    html += `<button class="filter-chip${naActive}" onclick="setTseoCollectionScoreFilter('not_analyzed')" style="${tseoCollectionNotAnalyzedFilter ? '' : 'border-style: dashed;'}">Not Analyzed</button>`;
    html += '</div>';
    if (tseoCollectionScoreFilter || tseoCollectionNotAnalyzedFilter || tseoCollectionSearchTerm) {
        html += `<button class="filter-chip" onclick="clearTseoCollectionFilters()" style="border-style: dashed;">
            <span class="material-symbols-rounded" style="font-size: 16px;">clear_all</span> Clear
        </button>`;
    }
    html += '</div>';

    // Search bar
    html += `<div class="search-bar">
        <span class="material-symbols-rounded">search</span>
        <input class="search-input" type="text" placeholder="Search collections by title or handle..." value="${escapeAttr(tseoCollectionSearchTerm)}" oninput="onTseoCollectionSearch(this.value)">
        ${tseoCollectionSearchTerm ? '<button class="search-clear" onclick="onTseoCollectionSearch(\'\', true)"><span class="material-symbols-rounded" style="font-size:18px;">close</span></button>' : ''}
    </div>`;

    // Empty state for filters
    if (collections.length === 0 && (tseoCollectionScoreFilter || tseoCollectionNotAnalyzedFilter || tseoCollectionSearchTerm)) {
        html += `<div class="empty-state">
            <span class="material-symbols-rounded">filter_list_off</span>
            <p>No collections match the current filters${tseoCollectionSearchTerm ? " or search" : ""}.</p>
            <button class="btn btn-secondary" onclick="clearTseoCollectionFilters()">
                <span class="material-symbols-rounded">clear_all</span> Clear Filters
            </button>
        </div>`;
        container.innerHTML = html;
        return;
    }

    // Summary strip
    const analyzed = collections.filter(c => c.tech_seo_score !== null && c.tech_seo_score !== undefined);
    const avgScore = analyzed.length > 0 ? Math.round(analyzed.reduce((s, c) => s + c.tech_seo_score, 0) / analyzed.length) : 0;
    const goodCount = analyzed.filter(c => c.tech_seo_score >= 80).length;
    const fairCount = analyzed.filter(c => c.tech_seo_score >= 50 && c.tech_seo_score < 80).length;
    const poorCount = analyzed.filter(c => c.tech_seo_score < 50).length;

    html += `<div class="summary-strip">
        <span class="stat-item"><strong>${collections.length}</strong> collections</span>
        <span class="stat-divider"></span>
        <span class="stat-item">Avg score: <strong>${avgScore}</strong></span>
        <span class="stat-divider"></span>
        <span class="stat-item"><strong>${analyzed.length}</strong> analyzed</span>
        <span class="stat-divider"></span>
        <span class="stat-item" style="color: var(--success);"><strong>${goodCount}</strong> good</span>
        <span class="stat-item" style="color: var(--warning);"><strong>${fairCount}</strong> fair</span>
        <span class="stat-item" style="color: var(--error);"><strong>${poorCount}</strong> poor</span>
    </div>`;

    // Bulk toolbar
    const selCount = selectedTseoCollectionIds.size;
    if (selCount > 0) {
        html += `<div class="bulk-toolbar">
            <span class="bulk-count">${selCount} collection${selCount > 1 ? "s" : ""} selected</span>
            <button class="btn btn-primary btn-sm" onclick="bulkAnalyzeTseoCollections()">
                <span class="material-symbols-rounded">analytics</span> Bulk Analyze
            </button>
            <button class="btn btn-secondary btn-sm" onclick="bulkFixApplyTseoCollections()">
                <span class="material-symbols-rounded">auto_fix_high</span> Bulk Analyze + Fix + Apply
            </button>
        </div>`;
    }

    // Table
    html += '<div class="table-container"><table><thead><tr>';
    html += `<th class="checkbox-col"><input type="checkbox" class="table-checkbox" onchange="toggleTseoSelectAllCollections(this)" ${selCount === collections.length && collections.length > 0 ? "checked" : ""}></th>`;
    html += `<th class="sortable-header ${tseoCollectionSort.by === 'title' ? tseoCollectionSort.dir : ''}" onclick="toggleTseoCollectionSort('title')">Title</th>`;
    if (hasMultipleStores) html += `<th>Store</th>`;
    html += `<th class="sortable-header ${tseoCollectionSort.by === 'products_count' ? tseoCollectionSort.dir : ''}" onclick="toggleTseoCollectionSort('products_count')">Products</th>`;
    html += `<th class="sortable-header ${tseoCollectionSort.by === 'tech_seo_score' ? tseoCollectionSort.dir : ''}" onclick="toggleTseoCollectionSort('tech_seo_score')">Score</th>`;
    html += `<th>Issues</th>`;
    html += `<th>Last Analyzed</th>`;
    html += `<th>Actions</th>`;
    html += '</tr></thead><tbody>';

    collections.forEach((c, idx) => {
        const checked = selectedTseoCollectionIds.has(c.id) ? "checked" : "";
        const rowClass = selectedTseoCollectionIds.has(c.id) ? "row-selected" : "";

        let scoreCell;
        if (c.tech_seo_score !== null && c.tech_seo_score !== undefined) {
            scoreCell = renderScoreRing(c.tech_seo_score);
        } else {
            scoreCell = '<span class="badge badge-not-analyzed">Not analyzed</span>';
        }

        const issuesCell = c.issues_count !== null && c.issues_count !== undefined
            ? `<span class="badge ${c.issues_count === 0 ? 'badge-success' : 'badge-error'}">${c.issues_count}</span>`
            : "—";

        const analyzedAt = c.analyzed_at
            ? `<span title="${escapeAttr(formatDate(c.analyzed_at))}">${formatRelativeTime(c.analyzed_at)}</span>`
            : '<span style="color: var(--on-surface-secondary);">Never</span>';

        html += `<tr class="${rowClass}">
            <td class="checkbox-col"><input type="checkbox" class="table-checkbox" ${checked} onclick="toggleTseoCollectionSelect(${c.id}, this, event)" data-index="${idx}"></td>
            <td><strong title="/${escapeAttr(c.handle || "")}">${escapeHtml(c.title || "Untitled")}</strong></td>
            ${hasMultipleStores ? `<td>${escapeHtml(c.store_name || "")}</td>` : ''}
            <td>${c.products_count || 0}</td>
            <td>${scoreCell}</td>
            <td>${issuesCell}</td>
            <td>${analyzedAt}</td>
            <td>
                <div class="actions-cell">
                    <button class="action-icon" onclick="analyzeTseoItem('collection', ${c.id})" title="Analyze">
                        <span class="material-symbols-rounded">analytics</span>
                    </button>
                    ${c.tech_seo_score !== null && c.tech_seo_score !== undefined ? `<button class="action-icon" onclick="viewTseoDetail('collection', ${c.id})" title="View"><span class="material-symbols-rounded">visibility</span></button>` : ""}
                </div>
            </td>
        </tr>`;
    });

    html += '</tbody></table></div>';
    container.innerHTML = html;
}

function onTseoCollectionSearch(value, immediate) {
    clearTimeout(_tseoCollectionSearchTimer);
    if (immediate) {
        tseoCollectionSearchTerm = value;
        applyTseoCollectionFilters();
        return;
    }
    _tseoCollectionSearchTimer = setTimeout(() => {
        tseoCollectionSearchTerm = value;
        applyTseoCollectionFilters();
    }, 300);
}

function clearTseoCollectionFilters() {
    tseoCollectionScoreFilter = "";
    tseoCollectionNotAnalyzedFilter = false;
    tseoCollectionSearchTerm = "";
    saveFilter("tseoCollectionScoreFilter", "");
    saveFilter("tseoCollectionNotAnalyzedFilter", false);
    selectedTseoCollectionIds.clear();
    loadTseoCollections();
}

function toggleTseoCollectionSort(key) {
    if (tseoCollectionSort.by === key) {
        tseoCollectionSort.dir = tseoCollectionSort.dir === "asc" ? "desc" : "asc";
    } else {
        tseoCollectionSort = { by: key, dir: "asc" };
    }
    saveFilter("tseoCollectionSort", tseoCollectionSort);
    loadTseoCollections();
}

function toggleTseoCollectionSelect(id, checkbox, event) {
    const idx = parseInt(checkbox.dataset.index, 10);
    const isChecked = checkbox.checked;

    if (event && event.shiftKey && lastClickedTseoCollectionIndex >= 0) {
        const start = Math.min(lastClickedTseoCollectionIndex, idx);
        const end = Math.max(lastClickedTseoCollectionIndex, idx);
        for (let i = start; i <= end; i++) {
            const c = currentTseoCollections[i];
            if (c) {
                if (isChecked) {
                    selectedTseoCollectionIds.add(c.id);
                } else {
                    selectedTseoCollectionIds.delete(c.id);
                }
            }
        }
    } else {
        if (isChecked) selectedTseoCollectionIds.add(id);
        else selectedTseoCollectionIds.delete(id);
    }

    lastClickedTseoCollectionIndex = idx;
    renderTseoCollectionsTable(document.getElementById("tseo-collections-container"), currentTseoCollections);
}

function toggleTseoSelectAllCollections(checkbox) {
    if (checkbox.checked) {
        currentTseoCollections.forEach(c => selectedTseoCollectionIds.add(c.id));
    } else {
        selectedTseoCollectionIds.clear();
    }
    renderTseoCollectionsTable(document.getElementById("tseo-collections-container"), currentTseoCollections);
}

// --- Analyze ---

async function analyzeTseoItem(itemType, itemId) {
    try {
        showToast("Analyzing...", "info");
        const result = await apiRequest("/api/technical-seo/analyze", {
            method: "POST",
            body: { item_type: itemType, item_id: itemId },
        });
        showToast(`Score: ${result.score} | ${result.issues_count} issues found`, "success");
        if (itemType === "product") loadTseoProducts();
        else loadTseoCollections();
    } catch (err) {
        showToast("Analysis failed: " + err.message, "error");
    }
}

// --- Detail Modal ---

let _tseoExpandedGroups = {};

async function viewTseoDetail(itemType, itemId) {
    const modal = document.getElementById("tseo-detail-modal");
    const content = document.getElementById("tseo-detail-content");
    content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div><p>Loading analysis...</p></div>';
    modal.classList.add("active");

    try {
        const analysis = await apiRequest(`/api/technical-seo/analysis/${itemType}/${itemId}`);
        document.getElementById("tseo-detail-title").textContent = `Technical SEO — ${itemType === "product" ? "Product" : "Collection"}`;

        let html = "";

        // Hero section
        html += `<div class="tseo-modal-hero">`;
        html += renderScoreRing(analysis.score, 80);
        html += `<div class="tseo-modal-stats">
            <div class="tseo-modal-stat">
                <span class="tseo-stat-label">Issues</span>
                <span class="tseo-stat-value"><span class="badge ${analysis.issues_count === 0 ? 'badge-success' : 'badge-error'}">${analysis.issues_count}</span></span>
            </div>
            <div class="tseo-modal-stat">
                <span class="tseo-stat-label">Fixable</span>
                <span class="tseo-stat-value"><span class="badge badge-fixable">${analysis.fixable_count}</span></span>
            </div>
            <div class="tseo-modal-stat">
                <span class="tseo-stat-label">Analyzed</span>
                <span class="tseo-stat-value" title="${escapeAttr(analysis.updated_at ? formatDate(analysis.updated_at) : '')}">${analysis.updated_at ? formatRelativeTime(analysis.updated_at) : "—"}</span>
            </div>
        </div>`;
        html += `<div class="tseo-hero-actions">
            <button class="btn btn-primary" onclick="getAiFixes('${itemType}', ${itemId})">
                <span class="material-symbols-rounded">auto_fix_high</span> Get AI Fixes
            </button>
            <button class="btn btn-secondary" onclick="analyzeTseoItemInModal('${itemType}', ${itemId})">
                <span class="material-symbols-rounded">refresh</span> Re-analyze
            </button>
        </div>`;
        html += `</div>`;

        // Detected keywords
        if (analysis.detected_keywords && analysis.detected_keywords.length > 0) {
            html += '<div class="preview-section">';
            html += '<div class="preview-section-title"><span class="material-symbols-rounded">key</span> Detected Keywords</div>';
            html += '<div class="preview-tags">';
            for (const kw of analysis.detected_keywords) {
                if (kw.packaging_only) {
                    html += `<span class="chip-display" style="border: 1px dashed var(--primary);"><strong>${escapeHtml(kw.keyword)}</strong> <span class="badge badge-info" style="font-size: 10px; margin-left: 4px;">pkg</span></span>`;
                } else {
                    const conf = kw.confidence >= 15 ? "badge-success" : kw.confidence >= 8 ? "badge-warning" : "badge-info";
                    html += `<span class="chip-display"><strong>${escapeHtml(kw.keyword)}</strong> <span class="badge ${conf}" style="font-size: 10px; margin-left: 4px;">${kw.confidence}</span></span>`;
                }
            }
            html += '</div></div>';
        }

        // Grouped checks breakdown
        html += '<div class="preview-section">';
        html += '<div class="preview-section-title"><span class="material-symbols-rounded">checklist</span> Check-by-Check Breakdown</div>';

        CHECK_GROUPS.forEach((group, gIdx) => {
            const groupChecks = group.keys.filter(k => analysis.checks[k]).map(k => ({ key: k, ...analysis.checks[k] }));
            if (groupChecks.length === 0) return;

            const groupAvg = Math.round(groupChecks.reduce((s, c) => s + c.score, 0) / groupChecks.length);
            const hasIssues = groupChecks.some(c => c.issue);
            const collapsed = !hasIssues;
            _tseoExpandedGroups[gIdx] = !collapsed;

            const avgCls = groupAvg >= 80 ? "color: var(--success);" : groupAvg >= 50 ? "color: var(--warning);" : "color: var(--error);";

            html += `<div class="tseo-check-group${collapsed ? ' collapsed' : ''}" id="tseo-group-${gIdx}">
                <div class="tseo-check-group-header" onclick="toggleTseoCheckGroup(${gIdx})">
                    <span class="material-symbols-rounded">${group.icon}</span>
                    <span class="tseo-group-label">${group.label}</span>
                    <span class="tseo-group-score" style="${avgCls}">${groupAvg}</span>
                    <span class="material-symbols-rounded tseo-chevron">expand_more</span>
                </div>
                <div class="tseo-check-group-body">`;

            for (const check of groupChecks) {
                const checkFill = check.score >= 80 ? "high" : check.score >= 50 ? "medium" : "low";
                const label = CHECK_LABELS[check.key] || check.key.replace(/_/g, " ");
                html += `<div class="tseo-check-item">
                    <div class="score-row">
                        <span class="score-label">${escapeHtml(label)}</span>
                        <div class="score-bar"><div class="score-bar-fill ${checkFill}" style="width: ${check.score}%;"></div></div>
                        <span class="score-value">${check.score}</span>
                    </div>
                    <div style="padding: 2px 0 4px 0; font-size: 12px; color: var(--on-surface-secondary);">${escapeHtml(check.detail)}</div>
                    ${check.issue ? `<div style="padding: 0 0 4px 0;"><span class="badge badge-warning" style="font-size: 11px;">${escapeHtml(check.issue)}</span></div>` : ""}
                </div>`;
            }

            html += `</div></div>`;
        });
        html += '</div>';

        // AI Fixes container
        html += '<div id="tseo-ai-fixes-container"></div>';

        content.innerHTML = html;

        // Show existing AI fixes if available
        if (analysis.ai_fixes && analysis.ai_fixes.fixes) {
            const fixContainer = document.getElementById("tseo-ai-fixes-container");
            if (fixContainer) fixContainer.innerHTML = renderAiFixes(analysis.ai_fixes, itemType, itemId);
        }

    } catch (err) {
        content.innerHTML = `<p>Error: ${escapeHtml(err.message)}</p>`;
    }
}

function toggleTseoCheckGroup(idx) {
    const el = document.getElementById(`tseo-group-${idx}`);
    if (!el) return;
    el.classList.toggle("collapsed");
    _tseoExpandedGroups[idx] = !el.classList.contains("collapsed");
}

function closeTseoModal() {
    document.getElementById("tseo-detail-modal").classList.remove("active");
}

async function analyzeTseoItemInModal(itemType, itemId) {
    try {
        showToast("Re-analyzing...", "info");
        await apiRequest("/api/technical-seo/analyze", {
            method: "POST",
            body: { item_type: itemType, item_id: itemId },
        });
        showToast("Analysis updated", "success");
        viewTseoDetail(itemType, itemId);
        if (itemType === "product") loadTseoProducts();
        else loadTseoCollections();
    } catch (err) {
        showToast("Analysis failed: " + err.message, "error");
    }
}

// --- AI Fixes ---

async function getAiFixes(itemType, itemId) {
    const container = document.getElementById("tseo-ai-fixes-container");
    if (!container) return;

    container.innerHTML = '<div class="loading-spinner"><div class="spinner"></div><p>AI is generating fixes...</p></div>';

    try {
        const taskResp = await apiRequest("/api/technical-seo/fix", {
            method: "POST",
            body: { item_type: itemType, item_id: itemId },
        });

        const taskId = taskResp.task_id;
        await pollTseoFixTask(taskId, itemType, itemId);
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>Failed to get AI fixes: ${escapeHtml(err.message)}</p></div>`;
    }
}

async function pollTseoFixTask(taskId, itemType, itemId) {
    const container = document.getElementById("tseo-ai-fixes-container");

    const poll = async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            if (task.status === "completed") {
                if (task.result && task.result.fixes) {
                    container.innerHTML = renderAiFixes(task.result, itemType, itemId);
                } else if (task.result && task.result.raw_response) {
                    container.innerHTML = `<div class="preview-section"><pre style="white-space: pre-wrap;">${escapeHtml(task.result.raw_response)}</pre></div>`;
                } else {
                    container.innerHTML = '<div class="empty-state"><p>AI returned no fixes. All checks may already be passing.</p></div>';
                }
            } else if (task.status === "failed") {
                container.innerHTML = `<div class="empty-state"><p>AI task failed: ${escapeHtml(task.error || "Unknown error")}</p></div>`;
            } else {
                setTimeout(poll, 2000);
            }
        } catch (err) {
            container.innerHTML = `<div class="empty-state"><p>Error polling task: ${escapeHtml(err.message)}</p></div>`;
        }
    };

    await poll();
}

function renderAiFixes(aiResult, itemType, itemId) {
    const fixes = aiResult.fixes || [];
    if (fixes.length === 0) return '<div class="empty-state"><p>No fixes proposed.</p></div>';

    let html = '<div class="preview-section" style="margin-top: 24px;">';
    html += '<div class="preview-section-title"><span class="material-symbols-rounded">auto_fix_high</span> AI Fix Proposals</div>';

    if (aiResult.summary) {
        html += `<p style="margin-bottom: 16px; color: var(--on-surface-secondary);">${escapeHtml(aiResult.summary)}</p>`;
    }

    if (aiResult.target_keywords && aiResult.target_keywords.length > 0) {
        html += '<div style="margin-bottom: 16px;"><strong>Target Keywords:</strong> ';
        html += aiResult.target_keywords.map(kw => `<span class="chip-display">${escapeHtml(kw)}</span>`).join(" ");
        html += '</div>';
    }

    fixes.forEach((fix, idx) => {
        const fieldLabel = {
            meta_title: "Meta Title",
            meta_description: "Meta Description",
            description: "Description",
            tags: "Tags",
            image_alt: "Image Alt Text",
            title: "Title",
        }[fix.field] || fix.field;

        const currentVal = typeof fix.current_value === "object" ? JSON.stringify(fix.current_value) : (fix.current_value || "");
        const proposedVal = typeof fix.proposed_value === "object" ? JSON.stringify(fix.proposed_value) : (fix.proposed_value || "");

        html += `<div class="fix-card" style="border: 1px solid var(--outline); border-radius: 12px; padding: 16px; margin-bottom: 12px;">
            <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
                <input type="checkbox" id="fix-check-${idx}" checked data-fix-index="${idx}">
                <label for="fix-check-${idx}" style="font-weight: 600; font-size: 14px;">${escapeHtml(fieldLabel)}</label>
            </div>
            <div style="margin-bottom: 8px;">
                <div style="font-size: 12px; color: var(--on-surface-secondary); margin-bottom: 2px;">Current</div>
                <div style="padding: 8px; background: var(--surface-dim); border-radius: 8px; font-size: 13px; opacity: 0.7;">${escapeHtml(currentVal) || "<em>Empty</em>"}</div>
            </div>
            <div style="margin-bottom: 8px;">
                <div style="font-size: 12px; color: var(--on-surface-secondary); margin-bottom: 2px;">Proposed</div>
                <textarea id="fix-value-${idx}" class="form-control" style="min-height: ${fix.field === 'description' ? '200px' : '40px'}; font-size: 13px;">${escapeHtml(proposedVal)}</textarea>
            </div>
            <div style="font-size: 12px; color: var(--on-surface-secondary);">${escapeHtml(fix.reason || "")}</div>
        </div>`;
    });

    html += `<button class="btn btn-primary" onclick="applySelectedFixes('${itemType}', ${itemId})" style="margin-top: 8px;">
        <span class="material-symbols-rounded">check</span>
        Apply Selected Fixes
    </button>`;
    html += '</div>';

    return html;
}

async function applySelectedFixes(itemType, itemId) {
    const container = document.getElementById("tseo-ai-fixes-container");
    const checkboxes = container.querySelectorAll('input[type="checkbox"][data-fix-index]');
    const fixes = [];

    checkboxes.forEach(cb => {
        if (!cb.checked) return;
        const idx = cb.dataset.fixIndex;
        const textarea = document.getElementById(`fix-value-${idx}`);
        if (!textarea || !textarea.value.trim()) return;

        const fixCards = container.querySelectorAll(".fix-card");
        const card = fixCards[parseInt(idx)];
        const label = card.querySelector("label").textContent.trim();

        const fieldMap = {
            "Meta Title": "meta_title",
            "Meta Description": "meta_description",
            "Description": "description",
            "Tags": "tags",
            "Image Alt Text": "image_alt",
            "Title": "title",
        };
        const field = fieldMap[label] || label.toLowerCase().replace(/\s+/g, "_");

        let value = textarea.value.trim();
        if (field === "tags") {
            try { value = JSON.parse(value); } catch { value = value.split(",").map(t => t.trim()); }
        }

        fixes.push({ field, value });
    });

    if (fixes.length === 0) {
        showToast("No fixes selected", "warning");
        return;
    }

    try {
        showToast("Applying fixes to Shopify...", "info");
        const result = await apiRequest("/api/technical-seo/apply", {
            method: "POST",
            body: { item_type: itemType, item_id: itemId, fixes },
        });

        if (result.ok) {
            showToast(`Fixes applied! New score: ${result.new_score}`, "success");
            viewTseoDetail(itemType, itemId);
            if (itemType === "product") loadTseoProducts();
            else loadTseoCollections();
        }
    } catch (err) {
        showToast("Failed to apply fixes: " + err.message, "error");
    }
}

// --- Bulk Operations ---

async function bulkAnalyzeTseoProducts() {
    const ids = Array.from(selectedTseoProductIds);
    if (ids.length === 0) return;

    try {
        showToast(`Analyzing ${ids.length} products...`, "info");
        const result = await apiRequest("/api/technical-seo/bulk-analyze", {
            method: "POST",
            body: { item_type: "product", item_ids: ids },
        });
        showToast(`${result.analyzed} products analyzed`, "success");
        selectedTseoProductIds.clear();
        loadTseoProducts();
    } catch (err) {
        showToast("Bulk analysis failed: " + err.message, "error");
    }
}

async function bulkAnalyzeTseoCollections() {
    const ids = Array.from(selectedTseoCollectionIds);
    if (ids.length === 0) return;

    try {
        showToast(`Analyzing ${ids.length} collections...`, "info");
        const result = await apiRequest("/api/technical-seo/bulk-analyze", {
            method: "POST",
            body: { item_type: "collection", item_ids: ids },
        });
        showToast(`${result.analyzed} collections analyzed`, "success");
        selectedTseoCollectionIds.clear();
        loadTseoCollections();
    } catch (err) {
        showToast("Bulk analysis failed: " + err.message, "error");
    }
}

async function bulkFixApplyTseoProducts() {
    const ids = Array.from(selectedTseoProductIds);
    if (ids.length === 0) return;

    openTseoConfirm(
        "Bulk Analyze + Fix + Apply",
        `This will analyze ${ids.length} products, get AI fixes, and auto-apply them to Shopify. Continue?`,
        async () => {
            closeTseoConfirm();
            try {
                const result = await apiRequest("/api/technical-seo/bulk-fix-apply", {
                    method: "POST",
                    body: { item_type: "product", item_ids: ids },
                });
                pollTseoBulkTask(result.bulk_task_id, result.total, "Analyzing + fixing products...");
            } catch (err) {
                showToast("Failed to start bulk operation: " + err.message, "error");
            }
        }
    );
}

async function bulkFixApplyTseoCollections() {
    const ids = Array.from(selectedTseoCollectionIds);
    if (ids.length === 0) return;

    openTseoConfirm(
        "Bulk Analyze + Fix + Apply",
        `This will analyze ${ids.length} collections, get AI fixes, and auto-apply them to Shopify. Continue?`,
        async () => {
            closeTseoConfirm();
            try {
                const result = await apiRequest("/api/technical-seo/bulk-fix-apply", {
                    method: "POST",
                    body: { item_type: "collection", item_ids: ids },
                });
                pollTseoBulkTask(result.bulk_task_id, result.total, "Analyzing + fixing collections...");
            } catch (err) {
                showToast("Failed to start bulk operation: " + err.message, "error");
            }
        }
    );
}

function pollTseoBulkTask(bulkTaskId, total, subtitleText) {
    const overlay = document.getElementById("tseo-progress-overlay");
    const bar = document.getElementById("tseo-progress-bar");
    const stats = document.getElementById("tseo-progress-stats");
    const current = document.getElementById("tseo-progress-current");
    const subtitle = document.getElementById("tseo-progress-subtitle");

    overlay.classList.add("active");
    subtitle.textContent = subtitleText;
    bar.style.width = "0%";
    stats.textContent = `0 / ${total} completed`;
    current.textContent = "";

    const poll = async () => {
        try {
            const task = await apiRequest(`/api/tasks/bulk/${bulkTaskId}`);
            const completed = task.completed + task.failed;
            const pct = total > 0 ? Math.round((completed / total) * 100) : 0;

            bar.style.width = `${pct}%`;
            stats.textContent = `${completed} / ${total} completed${task.failed > 0 ? ` (${task.failed} failed)` : ""}`;

            if (task.current_item_title) {
                current.textContent = task.current_item_title;
            }

            if (task.status === "completed") {
                setTimeout(() => {
                    overlay.classList.remove("active");
                    showToast(`Bulk operation complete: ${task.completed} succeeded, ${task.failed} failed`, task.failed > 0 ? "warning" : "success");
                    selectedTseoProductIds.clear();
                    selectedTseoCollectionIds.clear();
                    loadTseoProducts();
                    loadTseoCollections();
                }, 1000);
            } else {
                setTimeout(poll, 2000);
            }
        } catch (err) {
            overlay.classList.remove("active");
            showToast("Error polling bulk task: " + err.message, "error");
        }
    };

    poll();
}

// --- Confirm Dialog ---

let tseoConfirmCallback = null;

function openTseoConfirm(title, message, onOk) {
    document.getElementById("tseo-confirm-title").textContent = title;
    document.getElementById("tseo-confirm-message").textContent = message;
    document.getElementById("tseo-confirm-overlay").classList.add("active");
    tseoConfirmCallback = onOk;

    const okBtn = document.getElementById("tseo-confirm-ok");
    okBtn.onclick = () => {
        if (tseoConfirmCallback) tseoConfirmCallback();
    };
}

function closeTseoConfirm() {
    document.getElementById("tseo-confirm-overlay").classList.remove("active");
    tseoConfirmCallback = null;
}

// --- Helpers ---

function tseoSortIcon(key, sortState) {
    if (sortState.by !== key) return "";
    return sortState.dir === "asc"
        ? '<span class="material-symbols-rounded" style="font-size: 16px;">arrow_upward</span>'
        : '<span class="material-symbols-rounded" style="font-size: 16px;">arrow_downward</span>';
}

function escapeAttr(str) {
    if (!str) return "";
    return str.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/'/g, "&#39;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
