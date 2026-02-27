let currentSort = loadFilter("productSort", { by: "title", dir: "asc" });
let collectionSort = loadFilter("collectionSort", { by: "title", dir: "asc" });
let collectionFilters = loadFilter("collectionFilters", { type: "", published: "", seo: "" });
let currentStatusFilter = loadFilter("productStatus", "");
let statusCounts = {};
let selectedProductIds = new Set();
let selectedCollectionIds = new Set();
let currentProducts = [];
let currentCollections = [];
let currentProductId = null;
let lastClickedProductIndex = -1;
let lastClickedCollectionIndex = -1;
let schemaSort = loadFilter("schemaSort", { by: "title", dir: "asc" });
let schemaFilter = loadFilter("schemaFilter", { generated: "", valid: "", pushed: "" });
let currentSchemaProducts = [];
let cachedSchemaStats = null;
let cachedSchemaSettings = null;

function openLightbox(src) {
    const overlay = document.createElement("div");
    overlay.className = "image-lightbox";
    overlay.innerHTML = `<img src="${src}">`;
    overlay.addEventListener("click", () => overlay.remove());
    document.body.appendChild(overlay);
}

function closeLightbox() {
    const lb = document.querySelector(".image-lightbox");
    if (lb) { lb.remove(); return true; }
    return false;
}

function formatOptimizedDate(dateStr) {
    if (!dateStr) return '<span style="color: var(--on-surface-secondary);">Never</span>';
    const d = new Date(dateStr);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function setupModalDismiss(overlayId, closeFn) {
    const overlay = document.getElementById(overlayId);
    if (!overlay) return;
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) closeFn();
    });
}

document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
        if (closeLightbox()) return;
        const ka = document.getElementById("keyword-analysis-modal");
        const gen = document.getElementById("generate-collections-modal");
        const schema = document.getElementById("schema-modal");
        const product = document.getElementById("product-modal");
        if (ka && ka.classList.contains("active")) closeKeywordAnalysisModal();
        else if (gen && gen.classList.contains("active")) closeGenerateCollectionsModal();
        else if (schema && schema.classList.contains("active")) closeSchemaModal();
        else if (product && product.classList.contains("active")) closeProductModal();
    }
});

document.addEventListener("DOMContentLoaded", () => {
    loadStatusCounts();
    initTabs();
    setupModalDismiss("product-modal", closeProductModal);
    setupModalDismiss("schema-modal", closeSchemaModal);
    setupModalDismiss("keyword-analysis-modal", closeKeywordAnalysisModal);
});

function initTabs() {
    const savedTab = loadFilter("seoTab", "products");

    document.querySelectorAll(".tab").forEach((tab) => {
        tab.addEventListener("click", () => {
            document
                .querySelectorAll(".tab")
                .forEach((t) => t.classList.remove("active"));
            document
                .querySelectorAll(".tab-panel")
                .forEach((p) => p.classList.remove("active"));
            tab.classList.add("active");
            document
                .getElementById("tab-" + tab.dataset.tab)
                .classList.add("active");

            saveFilter("seoTab", tab.dataset.tab);

            if (tab.dataset.tab === "products") loadProducts();
            if (tab.dataset.tab === "collections") loadCollections();
            if (tab.dataset.tab === "schema") loadSchemaTab();
        });
    });

    const tabBtn = document.querySelector(`.tab[data-tab="${savedTab}"]`);
    if (tabBtn && savedTab !== "products") {
        tabBtn.click();
    } else {
        loadProducts();
    }
}

async function loadStatusCounts() {
    try {
        statusCounts = await apiRequest("/api/seo/products/counts");
    } catch (e) {
        statusCounts = {};
    }
}

function setStatusFilter(status) {
    currentStatusFilter = currentStatusFilter === status ? "" : status;
    saveFilter("productStatus", currentStatusFilter);
    selectedProductIds.clear();
    lastClickedProductIndex = -1;
    loadProducts();
}

function clearAllProductFilters() {
    currentStatusFilter = "";
    saveFilter("productStatus", "");
    selectedProductIds.clear();
    lastClickedProductIndex = -1;
    loadProducts();
}

async function loadProducts() {
    const container = document.getElementById("products-container");
    try {
        let url = `/api/seo/products?sort_by=${currentSort.by}&sort_dir=${currentSort.dir}`;
        if (currentStatusFilter) url += `&status=${currentStatusFilter}`;
        const products = await apiRequest(url);
        currentProducts = products;
        if (products.length === 0 && !currentStatusFilter) {
            container.innerHTML = `
                <div class="empty-state">
                    <span class="material-symbols-rounded">inventory_2</span>
                    <p>No products synced yet. Go to Stores and sync your store first.</p>
                    <a href="/stores" class="btn btn-primary">Go to Stores</a>
                </div>`;
            return;
        }
        renderProductsTable(container, products);
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>${escapeHtml(err.message)}</p></div>`;
    }
}

function renderProductsTable(container, products) {
    let html = "";

    const total = statusCounts.total || 0;
    const activeCount = statusCounts.ACTIVE || 0;
    const draftCount = statusCounts.DRAFT || 0;
    const archivedCount = statusCounts.ARCHIVED || 0;

    html += `<div class="status-filter-bar">
        <button class="filter-chip ${currentStatusFilter === "" ? "active" : ""}" onclick="setStatusFilter('')">All (${total})</button>
        <button class="filter-chip ${currentStatusFilter === "ACTIVE" ? "active" : ""}" onclick="setStatusFilter('ACTIVE')">Active (${activeCount})</button>
        <button class="filter-chip ${currentStatusFilter === "DRAFT" ? "active" : ""}" onclick="setStatusFilter('DRAFT')">Draft (${draftCount})</button>
        <button class="filter-chip ${currentStatusFilter === "ARCHIVED" ? "active" : ""}" onclick="setStatusFilter('ARCHIVED')">Archived (${archivedCount})</button>
    </div>`;

    if (products.length === 0) {
        html += `<div class="empty-state">
            <span class="material-symbols-rounded">filter_list_off</span>
            <p>No products match the current filter.</p>
            <button class="btn btn-secondary" onclick="clearAllProductFilters()">
                <span class="material-symbols-rounded">clear_all</span>
                Clear Filters
            </button>
        </div>`;
        container.innerHTML = html;
        return;
    }

    html += `<div style="display: flex; gap: 8px; align-items: center; margin-bottom: 12px;">
        <button class="btn btn-secondary" onclick="openKeywordAnalysisModal()">
            <span class="material-symbols-rounded">label</span>
            Keyword & Tag Analysis
        </button>
    </div>`;

    if (selectedProductIds.size > 0) {
        html += `<div class="bulk-toolbar">
            <span class="bulk-count">${selectedProductIds.size} product${selectedProductIds.size > 1 ? "s" : ""} selected</span>
            <button class="btn btn-sm btn-primary" onclick="startBulkOptimize()">
                <span class="material-symbols-rounded">bolt</span>
                Bulk Optimize & Apply
            </button>
        </div>`;
    }

    html += '<div class="table-container"><table><thead><tr>';
    html += '<th class="checkbox-col"><input type="checkbox" class="table-checkbox" id="select-all-checkbox" onchange="toggleSelectAll(this)"></th>';
    html += '<th class="row-num-col">#</th>';

    const columns = [
        { key: "title", label: "Product" },
        { key: "store", label: "Store" },
        { key: "status", label: "Status" },
        { key: "seo_score", label: "SEO Score" },
        { key: "last_optimized_at", label: "Last Optimized" },
    ];

    for (const col of columns) {
        const isActive = currentSort.by === col.key;
        const dirClass = isActive ? currentSort.dir : "";
        html += `<th class="sortable-header ${dirClass}" onclick="toggleSort('${col.key}')">${col.label}</th>`;
    }

    html += "<th>Actions</th>";
    html += "</tr></thead><tbody>";

    for (let idx = 0; idx < products.length; idx++) {
        const p = products[idx];
        const fillClass =
            p.seo_score >= 80 ? "high" : p.seo_score >= 50 ? "medium" : "low";
        const checked = selectedProductIds.has(p.id) ? "checked" : "";
        const rowClass = selectedProductIds.has(p.id) ? "row-selected" : "";
        html += `<tr class="${rowClass}">
            <td class="checkbox-col"><input type="checkbox" class="table-checkbox" data-product-id="${p.id}" data-index="${idx}" ${checked} onclick="toggleProductSelect(${p.id}, this, event)"></td>
            <td class="row-num-col">${idx + 1}</td>
            <td><strong>${escapeHtml(p.title)}</strong><br><small style="color: var(--on-surface-secondary);">${escapeHtml(p.handle)}</small></td>
            <td>${escapeHtml(p.store_name)}</td>
            <td><span class="badge badge-info">${escapeHtml(p.status)}</span></td>
            <td>
                <div style="display: flex; align-items: center; gap: 8px;">
                    ${getScoreBadge(p.seo_score)}
                    <div class="score-bar" style="width: 80px;">
                        <div class="score-bar-fill ${fillClass}" style="width: ${p.seo_score}%;"></div>
                    </div>
                </div>
            </td>
            <td>${formatOptimizedDate(p.last_optimized_at)}</td>
            <td>
                <button class="btn btn-sm btn-secondary" onclick="viewProduct(${p.id})">View</button>
                <button class="btn btn-sm btn-primary" onclick="analyzeProduct(${p.id})">AI Analyze</button>
            </td>
        </tr>`;
    }
    html += "</tbody></table></div>";
    container.innerHTML = html;

    const selectAllCb = document.getElementById("select-all-checkbox");
    if (selectAllCb) {
        selectAllCb.checked =
            products.length > 0 && products.every((p) => selectedProductIds.has(p.id));
    }
}

function toggleSort(key) {
    if (currentSort.by === key) {
        currentSort.dir = currentSort.dir === "asc" ? "desc" : "asc";
    } else {
        currentSort.by = key;
        currentSort.dir = "asc";
    }
    saveFilter("productSort", currentSort);
    lastClickedProductIndex = -1;
    loadProducts();
}

function toggleProductSelect(productId, checkbox, event) {
    const currentIndex = parseInt(checkbox.dataset.index, 10);
    const isChecked = checkbox.checked;

    if (event && event.shiftKey && lastClickedProductIndex >= 0) {
        const start = Math.min(lastClickedProductIndex, currentIndex);
        const end = Math.max(lastClickedProductIndex, currentIndex);
        for (let i = start; i <= end; i++) {
            const p = currentProducts[i];
            if (p) {
                if (isChecked) {
                    selectedProductIds.add(p.id);
                } else {
                    selectedProductIds.delete(p.id);
                }
            }
        }
    } else {
        if (isChecked) {
            selectedProductIds.add(productId);
        } else {
            selectedProductIds.delete(productId);
        }
    }

    lastClickedProductIndex = currentIndex;
    renderProductsTable(document.getElementById("products-container"), currentProducts);
}

function toggleSelectAll(checkbox) {
    if (checkbox.checked) {
        for (const p of currentProducts) {
            selectedProductIds.add(p.id);
        }
    } else {
        selectedProductIds.clear();
    }
    renderProductsTable(document.getElementById("products-container"), currentProducts);
}

function startBulkOptimize() {
    if (selectedProductIds.size === 0) return;

    document.getElementById("confirm-dialog-title").textContent = "Bulk Optimize & Apply";
    document.getElementById("confirm-dialog-message").innerHTML =
        `This will optimize ${selectedProductIds.size} product${selectedProductIds.size > 1 ? "s" : ""} using AI and push changes to Shopify.`
        + `<div style="margin-top: 12px;"><label style="display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: 14px;">`
        + `<input type="checkbox" id="bulk-include-tags-checkbox"> Include tag re-analysis (default: tags managed separately)</label></div>`;

    const okBtn = document.getElementById("confirm-dialog-ok");
    okBtn.onclick = () => {
        const includeTags = document.getElementById("bulk-include-tags-checkbox")?.checked || false;
        closeConfirmDialog();
        executeBulkOptimize(!includeTags);
    };
    _openConfirmDialog();
}

function closeConfirmDialog() {
    document.getElementById("confirm-dialog-overlay").classList.remove("active");
    document.removeEventListener("keydown", _confirmDialogKeyHandler);
}

function _confirmDialogKeyHandler(e) {
    if (e.key === "Enter") {
        e.preventDefault();
        document.getElementById("confirm-dialog-ok").click();
    }
}

function _openConfirmDialog() {
    document.addEventListener("keydown", _confirmDialogKeyHandler);
    document.getElementById("confirm-dialog-overlay").classList.add("active");
    document.getElementById("confirm-dialog-ok").focus();
}

async function executeBulkOptimize(skipTags = true) {
    const ids = Array.from(selectedProductIds);

    const overlay = document.getElementById("bulk-progress-overlay");
    const bar = document.getElementById("bulk-progress-bar");
    const stats = document.getElementById("bulk-progress-stats");
    const current = document.getElementById("bulk-progress-current");
    const subtitle = document.getElementById("bulk-progress-subtitle");

    bar.style.width = "0%";
    stats.textContent = `0 / ${ids.length} completed`;
    current.textContent = "";
    subtitle.textContent = "Starting optimization...";
    overlay.classList.add("active");

    try {
        const resp = await apiRequest("/api/tasks/bulk", {
            method: "POST",
            body: { product_ids: ids, auto_apply: true, skip_tags: skipTags },
        });

        const bulkTaskId = resp.bulk_task_id;
        pollBulkTask(bulkTaskId, ids.length);
    } catch (err) {
        overlay.classList.remove("active");
        showToast(`Bulk optimization failed: ${err.message}`, "error");
    }
}

function pollBulkTask(bulkTaskId, total) {
    const overlay = document.getElementById("bulk-progress-overlay");
    const bar = document.getElementById("bulk-progress-bar");
    const stats = document.getElementById("bulk-progress-stats");
    const current = document.getElementById("bulk-progress-current");
    const subtitle = document.getElementById("bulk-progress-subtitle");

    const interval = setInterval(async () => {
        try {
            const task = await apiRequest(`/api/tasks/bulk/${bulkTaskId}`);
            const done = task.completed + task.failed;
            const pct = total > 0 ? Math.round((done / total) * 100) : 0;

            bar.style.width = `${pct}%`;
            stats.textContent = `${done} / ${total} completed` + (task.failed > 0 ? ` (${task.failed} failed)` : "");
            subtitle.textContent = "Optimizing products...";

            if (task.current_product_title) {
                current.textContent = `Currently: ${task.current_product_title}`;
            }

            if (task.status === "completed") {
                clearInterval(interval);
                bar.style.width = "100%";
                current.textContent = "";
                subtitle.textContent = "All done!";

                setTimeout(() => {
                    overlay.classList.remove("active");
                    selectedProductIds.clear();
                    loadProducts();
                    showToast(
                        `Bulk optimization complete: ${task.completed} succeeded, ${task.failed} failed`,
                        task.failed > 0 ? "warning" : "success"
                    );
                }, 1500);
            }
        } catch {
            clearInterval(interval);
            overlay.classList.remove("active");
            showToast("Error polling bulk task status", "error");
        }
    }, 2000);
}

async function loadCollections() {
    const container = document.getElementById("collections-container");
    try {
        let url = `/api/seo/collections?sort_by=${collectionSort.by}&sort_dir=${collectionSort.dir}`;
        if (collectionFilters.type) url += `&collection_type=${collectionFilters.type}`;
        if (collectionFilters.published) url += `&published=${collectionFilters.published}`;
        let collections = await apiRequest(url);
        if (collectionFilters.seo) {
            collections = collections.filter(c => {
                if (collectionFilters.seo === "high") return c.seo_score >= 80;
                if (collectionFilters.seo === "medium") return c.seo_score >= 50 && c.seo_score < 80;
                if (collectionFilters.seo === "low") return c.seo_score < 50;
                return true;
            });
        }
        currentCollections = collections;
        const hasCollectionFilters = collectionFilters.type || collectionFilters.published || collectionFilters.seo;
        if (collections.length === 0 && !hasCollectionFilters) {
            container.innerHTML = `
                <div class="empty-state">
                    <span class="material-symbols-rounded">folder</span>
                    <p>No collections synced yet.</p>
                    <button class="btn btn-primary" style="margin-top: 12px;" onclick="openGenerateCollectionsModal()">
                        <span class="material-symbols-rounded">auto_awesome</span>
                        Generate Collections from Tags
                    </button>
                </div>`;
            return;
        }
        renderCollectionsTable(container, collections);
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>${escapeHtml(err.message)}</p></div>`;
    }
}

function renderCollectionsTable(container, collections) {
    let html = "";

    html += `<div style="display: flex; gap: 8px; align-items: center; margin-bottom: 16px;">
        <button class="btn btn-primary" onclick="openGenerateCollectionsModal()">
            <span class="material-symbols-rounded">auto_awesome</span>
            Generate Collections from Tags
        </button>
        <span style="margin-left: auto; font-size: 14px; color: var(--on-surface-secondary); font-weight: 500;">${collections.length} collection${collections.length !== 1 ? "s" : ""}</span>
    </div>`;

    html += `<div class="status-filter-bar" style="gap: 6px;">
        <span style="font-size: 13px; color: var(--on-surface-secondary); font-weight: 500; margin-right: 4px;">Type:</span>
        <button class="filter-chip ${collectionFilters.type === "" ? "active" : ""}" onclick="setCollectionFilter('type','')">All</button>
        <button class="filter-chip ${collectionFilters.type === "smart" ? "active" : ""}" onclick="setCollectionFilter('type','smart')">Smart</button>
        <button class="filter-chip ${collectionFilters.type === "manual" ? "active" : ""}" onclick="setCollectionFilter('type','manual')">Manual</button>

        <span style="font-size: 13px; color: var(--on-surface-secondary); font-weight: 500; margin-left: 12px; margin-right: 4px;">Status:</span>
        <button class="filter-chip ${collectionFilters.published === "" ? "active" : ""}" onclick="setCollectionFilter('published','')">All</button>
        <button class="filter-chip ${collectionFilters.published === "true" ? "active" : ""}" onclick="setCollectionFilter('published','true')">Published</button>
        <button class="filter-chip ${collectionFilters.published === "false" ? "active" : ""}" onclick="setCollectionFilter('published','false')">Draft</button>

        <span style="font-size: 13px; color: var(--on-surface-secondary); font-weight: 500; margin-left: 12px; margin-right: 4px;">SEO:</span>
        <button class="filter-chip ${collectionFilters.seo === "" ? "active" : ""}" onclick="setCollectionFilter('seo','')">All</button>
        <button class="filter-chip ${collectionFilters.seo === "high" ? "active" : ""}" onclick="setCollectionFilter('seo','high')">Good (80+)</button>
        <button class="filter-chip ${collectionFilters.seo === "medium" ? "active" : ""}" onclick="setCollectionFilter('seo','medium')">Fair (50-79)</button>
        <button class="filter-chip ${collectionFilters.seo === "low" ? "active" : ""}" onclick="setCollectionFilter('seo','low')">Poor (&lt;50)</button>
    </div>`;

    if (collections.length === 0) {
        html += `<div class="empty-state">
            <span class="material-symbols-rounded">filter_list_off</span>
            <p>No collections match the current filters.</p>
            <button class="btn btn-secondary" onclick="clearAllCollectionFilters()">
                <span class="material-symbols-rounded">clear_all</span>
                Clear Filters
            </button>
        </div>`;
        container.innerHTML = html;
        return;
    }

    if (selectedCollectionIds.size > 0) {
        html += `<div class="bulk-toolbar">
            <span class="bulk-count">${selectedCollectionIds.size} collection${selectedCollectionIds.size > 1 ? "s" : ""} selected</span>
            <button class="btn btn-sm btn-primary" onclick="startBulkOptimizeCollections()">
                <span class="material-symbols-rounded">bolt</span>
                Bulk Optimize & Apply
            </button>
            <button class="btn btn-sm btn-success" onclick="startBulkPublishCollections()">
                <span class="material-symbols-rounded">publish</span>
                Publish Selected
            </button>
            <button class="btn btn-sm btn-danger" onclick="startBulkDeleteCollections()">
                <span class="material-symbols-rounded">delete</span>
                Delete Selected
            </button>
        </div>`;
    }

    html += '<div class="table-container"><table><thead><tr>';
    html += '<th class="checkbox-col"><input type="checkbox" class="table-checkbox" id="select-all-collections-checkbox" onchange="toggleSelectAllCollections(this)"></th>';
    html += '<th class="row-num-col">#</th>';

    const columns = [
        { key: "title", label: "Collection" },
        { key: "store", label: "Store" },
        { key: "collection_type", label: "Type" },
        { key: "published", label: "Published" },
        { key: "products_count", label: "Products" },
        { key: "seo_score", label: "SEO Score" },
        { key: "last_optimized_at", label: "Last Optimized" },
    ];

    for (const col of columns) {
        const isActive = collectionSort.by === col.key;
        const dirClass = isActive ? collectionSort.dir : "";
        html += `<th class="sortable-header ${dirClass}" onclick="toggleCollectionSort('${col.key}')">${col.label}</th>`;
    }

    html += "<th>Actions</th>";
    html += "</tr></thead><tbody>";

    for (let idx = 0; idx < collections.length; idx++) {
        const c = collections[idx];
        const fillClass =
            c.seo_score >= 80
                ? "high"
                : c.seo_score >= 50
                  ? "medium"
                  : "low";
        const checked = selectedCollectionIds.has(c.id) ? "checked" : "";
        const rowClass = selectedCollectionIds.has(c.id) ? "row-selected" : "";
        html += `<tr class="${rowClass}">
            <td class="checkbox-col"><input type="checkbox" class="table-checkbox" data-collection-id="${c.id}" data-index="${idx}" ${checked} onclick="toggleCollectionSelect(${c.id}, this, event)"></td>
            <td class="row-num-col">${idx + 1}</td>
            <td><strong>${escapeHtml(c.title)}</strong><br><small style="color: var(--on-surface-secondary);">${escapeHtml(c.handle)}</small></td>
            <td>${escapeHtml(c.store_name)}</td>
            <td><span class="badge ${c.collection_type === "smart" ? "badge-info" : "badge-warning"}">${c.collection_type === "smart" ? "Smart" : "Manual"}</span></td>
            <td><span class="badge ${c.published ? "badge-success" : "badge-error"}">${c.published ? "Published" : "Draft"}</span></td>
            <td>${c.products_count}</td>
            <td>
                <div style="display: flex; align-items: center; gap: 8px;">
                    ${getScoreBadge(c.seo_score)}
                    <div class="score-bar" style="width: 80px;">
                        <div class="score-bar-fill ${fillClass}" style="width: ${c.seo_score}%;"></div>
                    </div>
                </div>
            </td>
            <td>${formatOptimizedDate(c.last_optimized_at)}</td>
            <td style="white-space: nowrap;">
                <button class="btn btn-sm btn-secondary" onclick="viewCollection(${c.id})">View</button>
                <button class="btn btn-sm btn-primary" onclick="analyzeCollection(${c.id})">AI Analyze</button>
                <button class="btn btn-sm btn-danger" onclick="confirmDeleteCollection(${c.id}, '${escapeAttr(c.title)}')">Delete</button>
            </td>
        </tr>`;
    }
    html += "</tbody></table></div>";
    container.innerHTML = html;

    const selectAllCb = document.getElementById("select-all-collections-checkbox");
    if (selectAllCb) {
        selectAllCb.checked =
            collections.length > 0 && collections.every((c) => selectedCollectionIds.has(c.id));
    }
}

function toggleCollectionSort(key) {
    if (collectionSort.by === key) {
        collectionSort.dir = collectionSort.dir === "asc" ? "desc" : "asc";
    } else {
        collectionSort.by = key;
        collectionSort.dir = "asc";
    }
    saveFilter("collectionSort", collectionSort);
    lastClickedCollectionIndex = -1;
    loadCollections();
}

function setCollectionFilter(filterKey, value) {
    collectionFilters[filterKey] = collectionFilters[filterKey] === value ? "" : value;
    saveFilter("collectionFilters", collectionFilters);
    selectedCollectionIds.clear();
    lastClickedCollectionIndex = -1;
    loadCollections();
}

function clearAllCollectionFilters() {
    collectionFilters = { type: "", published: "", seo: "" };
    saveFilter("collectionFilters", collectionFilters);
    selectedCollectionIds.clear();
    lastClickedCollectionIndex = -1;
    loadCollections();
}

function toggleCollectionSelect(collectionId, checkbox, event) {
    const currentIndex = parseInt(checkbox.dataset.index, 10);
    const isChecked = checkbox.checked;

    if (event && event.shiftKey && lastClickedCollectionIndex >= 0) {
        const start = Math.min(lastClickedCollectionIndex, currentIndex);
        const end = Math.max(lastClickedCollectionIndex, currentIndex);
        for (let i = start; i <= end; i++) {
            const c = currentCollections[i];
            if (c) {
                if (isChecked) {
                    selectedCollectionIds.add(c.id);
                } else {
                    selectedCollectionIds.delete(c.id);
                }
            }
        }
    } else {
        if (isChecked) {
            selectedCollectionIds.add(collectionId);
        } else {
            selectedCollectionIds.delete(collectionId);
        }
    }

    lastClickedCollectionIndex = currentIndex;
    renderCollectionsTable(document.getElementById("collections-container"), currentCollections);
}

function toggleSelectAllCollections(checkbox) {
    if (checkbox.checked) {
        for (const c of currentCollections) {
            selectedCollectionIds.add(c.id);
        }
    } else {
        selectedCollectionIds.clear();
    }
    renderCollectionsTable(document.getElementById("collections-container"), currentCollections);
}

function startBulkOptimizeCollections() {
    if (selectedCollectionIds.size === 0) return;

    const overlay = document.getElementById("confirm-dialog-overlay");
    document.getElementById("confirm-dialog-title").textContent = "Bulk Optimize & Apply";
    document.getElementById("confirm-dialog-message").textContent =
        `This will optimize ${selectedCollectionIds.size} collection${selectedCollectionIds.size > 1 ? "s" : ""} using AI and push changes to Shopify. Continue?`;

    const okBtn = document.getElementById("confirm-dialog-ok");
    okBtn.onclick = () => {
        closeConfirmDialog();
        executeBulkOptimizeCollections();
    };
    _openConfirmDialog();
}

async function executeBulkOptimizeCollections() {
    const ids = Array.from(selectedCollectionIds);

    const overlay = document.getElementById("bulk-progress-overlay");
    const bar = document.getElementById("bulk-progress-bar");
    const stats = document.getElementById("bulk-progress-stats");
    const current = document.getElementById("bulk-progress-current");
    const subtitle = document.getElementById("bulk-progress-subtitle");

    bar.style.width = "0%";
    stats.textContent = `0 / ${ids.length} completed`;
    current.textContent = "";
    subtitle.textContent = "Starting optimization...";
    overlay.classList.add("active");

    try {
        const resp = await apiRequest("/api/tasks/bulk-collections", {
            method: "POST",
            body: { collection_ids: ids, auto_apply: true },
        });

        const bulkTaskId = resp.bulk_task_id;
        pollBulkCollectionTask(bulkTaskId, ids.length);
    } catch (err) {
        overlay.classList.remove("active");
        showToast(`Bulk optimization failed: ${err.message}`, "error");
    }
}

function pollBulkCollectionTask(bulkTaskId, total) {
    const overlay = document.getElementById("bulk-progress-overlay");
    const bar = document.getElementById("bulk-progress-bar");
    const stats = document.getElementById("bulk-progress-stats");
    const current = document.getElementById("bulk-progress-current");
    const subtitle = document.getElementById("bulk-progress-subtitle");

    const interval = setInterval(async () => {
        try {
            const task = await apiRequest(`/api/tasks/bulk/${bulkTaskId}`);
            const done = task.completed + task.failed;
            const pct = total > 0 ? Math.round((done / total) * 100) : 0;

            bar.style.width = `${pct}%`;
            stats.textContent = `${done} / ${total} completed` + (task.failed > 0 ? ` (${task.failed} failed)` : "");
            subtitle.textContent = "Optimizing collections...";

            if (task.current_item_title) {
                current.textContent = `Currently: ${task.current_item_title}`;
            } else if (task.current_product_title) {
                current.textContent = `Currently: ${task.current_product_title}`;
            }

            if (task.status === "completed") {
                clearInterval(interval);
                bar.style.width = "100%";
                current.textContent = "";
                subtitle.textContent = "All done!";

                setTimeout(() => {
                    overlay.classList.remove("active");
                    selectedCollectionIds.clear();
                    loadCollections();
                    showToast(
                        `Bulk optimization complete: ${task.completed} succeeded, ${task.failed} failed`,
                        task.failed > 0 ? "warning" : "success"
                    );
                }, 1500);
            }
        } catch {
            clearInterval(interval);
            overlay.classList.remove("active");
            showToast("Error polling bulk task status", "error");
        }
    }, 2000);
}

function confirmDeleteCollection(collectionId, title) {
    const overlay = document.getElementById("confirm-dialog-overlay");
    document.getElementById("confirm-dialog-title").textContent = "Delete Collection";
    document.getElementById("confirm-dialog-message").textContent =
        `This will permanently delete "${title}" from Shopify and your local database. This cannot be undone. Continue?`;

    const okBtn = document.getElementById("confirm-dialog-ok");
    okBtn.onclick = () => {
        closeConfirmDialog();
        executeDeleteCollections([collectionId]);
    };
    _openConfirmDialog();
}

function startBulkDeleteCollections() {
    if (selectedCollectionIds.size === 0) return;

    const overlay = document.getElementById("confirm-dialog-overlay");
    document.getElementById("confirm-dialog-title").textContent = "Delete Collections";
    document.getElementById("confirm-dialog-message").textContent =
        `This will permanently delete ${selectedCollectionIds.size} collection${selectedCollectionIds.size > 1 ? "s" : ""} from Shopify and your local database. This cannot be undone. Continue?`;

    const okBtn = document.getElementById("confirm-dialog-ok");
    okBtn.onclick = () => {
        closeConfirmDialog();
        executeDeleteCollections(Array.from(selectedCollectionIds));
    };
    _openConfirmDialog();
}

async function executeDeleteCollections(ids) {
    try {
        const resp = await apiRequest("/api/seo/collections/delete", {
            method: "POST",
            body: { collection_ids: ids },
        });

        const deletedCount = resp.deleted ? resp.deleted.length : 0;
        const failedCount = resp.failed ? resp.failed.length : 0;

        if (failedCount > 0) {
            showToast(`Deleted ${deletedCount}, failed ${failedCount}: ${resp.failed[0].error}`, "warning");
        } else {
            showToast(`Deleted ${deletedCount} collection${deletedCount !== 1 ? "s" : ""}`, "success");
        }

        selectedCollectionIds.clear();
        loadCollections();
    } catch (err) {
        showToast(`Delete failed: ${err.message}`, "error");
    }
}

function startBulkPublishCollections() {
    if (selectedCollectionIds.size === 0) return;

    const overlay = document.getElementById("confirm-dialog-overlay");
    document.getElementById("confirm-dialog-title").textContent = "Publish Collections";
    document.getElementById("confirm-dialog-message").textContent =
        `This will publish ${selectedCollectionIds.size} collection${selectedCollectionIds.size > 1 ? "s" : ""} to all sales channels. Continue?`;

    const okBtn = document.getElementById("confirm-dialog-ok");
    okBtn.onclick = () => {
        closeConfirmDialog();
        executeBulkPublishCollections(Array.from(selectedCollectionIds));
    };
    _openConfirmDialog();
}

async function executeBulkPublishCollections(ids) {
    try {
        const resp = await apiRequest("/api/seo/collections/publish", {
            method: "POST",
            body: { collection_ids: ids },
        });

        const publishedCount = resp.published ? resp.published.length : 0;
        const failedCount = resp.failed ? resp.failed.length : 0;

        if (failedCount > 0) {
            showToast(`Published ${publishedCount}, failed ${failedCount}: ${resp.failed[0].error}`, "warning");
        } else {
            showToast(`Published ${publishedCount} collection${publishedCount !== 1 ? "s" : ""}`, "success");
        }

        selectedCollectionIds.clear();
        loadCollections();
    } catch (err) {
        showToast(`Publish failed: ${err.message}`, "error");
    }
}

async function viewProduct(id) {
    const modal = document.getElementById("product-modal");
    const content = document.getElementById("product-detail-content");
    content.innerHTML =
        '<div class="loading-spinner"><div class="spinner"></div><p>Loading...</p></div>';
    modal.classList.add("active");

    try {
        const p = await apiRequest(`/api/seo/products/${id}`);
        currentProductId = p.id;
        document.getElementById("product-modal-title").textContent = p.title;

        let html = "";

        const statusBadge = p.status === "ACTIVE"
            ? '<span class="badge badge-success">Active</span>'
            : `<span class="badge badge-warning">${escapeHtml(p.status || "Unknown")}</span>`;

        html += `<div class="preview-info-grid">
            <div class="info-item"><span class="info-label">Status</span><span class="info-value">${statusBadge}</span></div>
            <div class="info-item"><span class="info-label">Vendor</span><span class="info-value">${escapeHtml(p.vendor || "—")}</span></div>
            <div class="info-item"><span class="info-label">Type</span><span class="info-value">${escapeHtml(p.product_type || "—")}</span></div>
            <div class="info-item"><span class="info-label">Handle</span><span class="info-value mono">${escapeHtml(p.handle || "—")}</span></div>
            <div class="info-item"><span class="info-label">Store</span><span class="info-value">${escapeHtml(p.store_name || "—")}</span></div>
        </div>`;

        if (p.tags) {
            const tagList = p.tags.split(",").map(t => t.trim()).filter(Boolean);
            if (tagList.length > 0) {
                html += '<div class="preview-tags">';
                for (const tag of tagList) {
                    html += `<span class="chip-display">${escapeHtml(tag)}</span>`;
                }
                html += '</div>';
            }
        }

        html += '<div class="preview-section">';
        html += '<div class="preview-section-title"><span class="material-symbols-rounded">analytics</span> SEO Score Breakdown</div>';
        for (const [key, info] of Object.entries(p.seo_breakdown)) {
            const fillClass =
                info.score >= 80
                    ? "high"
                    : info.score >= 50
                      ? "medium"
                      : "low";
            html += `<div class="score-row">
                <span class="score-label">${escapeHtml(key.replace("_", " "))}</span>
                <div class="score-bar"><div class="score-bar-fill ${fillClass}" style="width: ${info.score}%;"></div></div>
                <span class="score-value">${info.score}</span>
            </div>`;
        }
        html += "</div>";

        html += `
            <div class="form-group">
                <label>Meta Title</label>
                <div class="form-control" style="background: var(--surface-dim);">${escapeHtml(p.meta_title) || "<em>Not set</em>"}</div>
            </div>
            <div class="form-group">
                <label>Meta Description</label>
                <div class="form-control" style="background: var(--surface-dim); min-height: 60px;">${escapeHtml(p.meta_description) || "<em>Not set</em>"}</div>
            </div>
        `;

        html += '<div class="preview-section">';
        html += '<div class="preview-section-title"><span class="material-symbols-rounded">description</span> Product Description</div>';
        html += `<div class="description-preview">${p.description || ""}</div>`;
        html += '</div>';

        if (p.images && p.images.length > 0) {
            html += '<div class="preview-section">';
            html += `<div class="preview-section-title"><span class="material-symbols-rounded">image</span> Images (${p.images.length})</div>`;
            html += '<div class="image-gallery">';
            for (const img of p.images) {
                const altBadgeClass = img.alt ? "" : " missing";
                const altText = img.alt || "No alt text";
                html += `<div class="image-gallery-item" data-src="${escapeAttr(img.url)}">
                    <img src="${escapeAttr(img.url)}" alt="${escapeAttr(altText)}" loading="lazy">
                    <div class="alt-badge${altBadgeClass}">${escapeHtml(altText)}</div>
                </div>`;
            }
            html += '</div></div>';
        }

        let variants = [];
        if (p.variants) {
            try {
                variants = typeof p.variants === "string" ? JSON.parse(p.variants) : p.variants;
            } catch { variants = []; }
        }
        if (Array.isArray(variants) && variants.length > 0) {
            html += '<div class="preview-section">';
            html += `<div class="preview-section-title"><span class="material-symbols-rounded">inventory_2</span> Variants (${variants.length})</div>`;
            html += '<div class="variants-summary"><table><thead><tr>';
            html += '<th>Title</th><th>Price</th><th>Compare At</th><th>SKU</th><th>Barcode</th><th>Available</th>';
            html += '</tr></thead><tbody>';
            for (const v of variants) {
                const available = v.inventoryQuantity != null
                    ? (v.inventoryQuantity > 0
                        ? `<span class="badge badge-success">${v.inventoryQuantity}</span>`
                        : `<span class="badge badge-error">0</span>`)
                    : "—";
                html += `<tr>
                    <td>${escapeHtml(v.title || "Default")}</td>
                    <td>${v.price ? "$" + escapeHtml(String(v.price)) : "—"}</td>
                    <td>${v.compareAtPrice ? "$" + escapeHtml(String(v.compareAtPrice)) : "—"}</td>
                    <td style="font-family: var(--font-mono); font-size: 11px;">${escapeHtml(v.sku || "—")}</td>
                    <td style="font-family: var(--font-mono); font-size: 11px;">${escapeHtml(v.barcode || "—")}</td>
                    <td>${available}</td>
                </tr>`;
            }
            html += '</tbody></table></div></div>';
        }

        html += `<div style="margin-top: 20px; display: flex; gap: 8px;">
            <button class="btn btn-primary" onclick="analyzeProduct(${p.id})">
                <span class="material-symbols-rounded">smart_toy</span>
                Get AI Recommendations
            </button>
        </div>`;

        html += '<div id="ai-results-container"></div>';

        html += '<div id="product-schema-preview" style="margin-top: 24px; border-top: 1px solid var(--outline); padding-top: 16px;">';
        html += '<h4 style="margin-bottom: 12px;"><span class="material-symbols-rounded" style="vertical-align: middle;">code</span> Schema Markup</h4>';
        html += '<div id="product-schema-content"><div class="loading-spinner"><div class="spinner"></div><p>Generating schema preview...</p></div></div>';
        html += '</div>';

        content.innerHTML = html;

        content.querySelectorAll(".image-gallery-item").forEach(item => {
            item.addEventListener("click", () => openLightbox(item.dataset.src));
        });

        loadProductSchemaPreview(p.id);
    } catch (err) {
        content.innerHTML = `<p>Error: ${escapeHtml(err.message)}</p>`;
    }
}

function closeProductModal() {
    document.getElementById("product-modal").classList.remove("active");
    currentProductId = null;
}

async function analyzeProduct(productId) {
    const modal = document.getElementById("product-modal");
    if (!modal.classList.contains("active")) {
        await viewProduct(productId);
    }

    const resultsContainer = document.getElementById("ai-results-container");
    if (!resultsContainer) return;

    resultsContainer.innerHTML = `
        <div class="ai-panel" style="margin-top: 20px;">
            <h4><span class="material-symbols-rounded">smart_toy</span> AI Analysis</h4>
            <div class="loading-spinner"><div class="spinner"></div><p>AI is analyzing this product...</p></div>
        </div>`;

    try {
        const taskResp = await apiRequest("/api/tasks", {
            method: "POST",
            body: {
                task_type: "product_seo",
                input: { product_id: productId },
            },
        });

        const taskId = taskResp.task_id;
        pollTaskResult(taskId, resultsContainer, productId);
    } catch (err) {
        resultsContainer.innerHTML = `
            <div class="ai-panel" style="margin-top: 20px;">
                <h4><span class="material-symbols-rounded">smart_toy</span> AI Analysis</h4>
                <p style="color: var(--error);">Error: ${escapeHtml(err.message)}</p>
            </div>`;
    }
}

function pollTaskResult(taskId, container, productId) {
    const interval = setInterval(async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            if (task.status === "completed") {
                clearInterval(interval);
                renderAIResults(container, task.result, productId);
            } else if (task.status === "failed") {
                clearInterval(interval);
                container.innerHTML = `
                    <div class="ai-panel" style="margin-top: 20px;">
                        <h4><span class="material-symbols-rounded">smart_toy</span> AI Analysis</h4>
                        <p style="color: var(--error);">Failed: ${escapeHtml(task.error || "Unknown error")}</p>
                    </div>`;
            }
        } catch {
            clearInterval(interval);
        }
    }, 2000);
}

function renderAIResults(container, result, productId) {
    if (!result) {
        container.innerHTML = "";
        return;
    }

    let html = `
        <div class="ai-panel" style="margin-top: 20px;">
            <div class="success-banner">
                <span class="material-symbols-rounded">check_circle</span>
                Optimization complete!
            </div>`;

    html += '<div class="side-by-side">';
    html += `<div class="form-group">
        <label>Optimized Title (H1 Tag)</label>
        <input type="text" class="ai-field-editable" id="ai-h1-tag"
            value="${escapeAttr(result.h1_tag || result.optimized_meta_title || "")}">
    </div>`;
    html += `<div class="form-group">
        <label>Meta Title</label>
        <input type="text" class="ai-field-editable" id="ai-meta-title"
            value="${escapeAttr(result.optimized_meta_title || "")}">
    </div>`;
    html += '</div>';

    html += `<div class="form-group">
        <label>Meta Description</label>
        <textarea class="ai-field-editable" id="ai-meta-description" rows="3">${escapeHtml(result.optimized_meta_description || "")}</textarea>
    </div>`;

    html += `<div class="form-group">
        <label>Product Description</label>
        <textarea class="ai-field-editable" id="ai-description" rows="5">${escapeHtml(result.optimized_description || "")}</textarea>
    </div>`;

    html += '<div class="side-by-side">';

    html += '<div class="form-group">';
    html += '<label>Tags</label>';
    const tags = result.suggested_tags || [];
    html += `<div class="chip-input-container" id="ai-tags-container">`;
    for (const tag of tags) {
        html += `<span class="chip">${escapeHtml(tag)}<button class="chip-remove" onclick="removeChip(this)">&times;</button></span>`;
    }
    html += `<input type="text" placeholder="Add tag..." onkeydown="handleChipKeydown(event, this)">`;
    html += `</div>`;
    html += '</div>';

    html += '<div class="form-group">';
    html += '<label>Keywords Used</label>';
    html += '<div style="display: flex; flex-wrap: wrap; gap: 6px;">';
    const keywords = result.keywords_used || [];
    for (const kw of keywords) {
        html += `<span class="chip-display">${escapeHtml(kw)}</span>`;
    }
    if (keywords.length === 0) {
        html += '<span style="font-size: 13px; color: var(--on-surface-secondary);">None</span>';
    }
    html += '</div></div>';

    html += '</div>';

    if (result.competitor_insights) {
        html += `<div class="form-group">
            <label>Competitor Insights</label>
            <div class="ai-field-value" style="font-size: 14px; line-height: 1.6;">${escapeHtml(result.competitor_insights)}</div>
        </div>`;
    }

    if (result.image_alt_suggestions && result.image_alt_suggestions.length > 0) {
        html += '<div class="form-group"><label>Image Alt Text Suggestions</label>';
        for (let i = 0; i < result.image_alt_suggestions.length; i++) {
            const alt = result.image_alt_suggestions[i];
            const mediaId = typeof alt === "object" ? (alt.media_id || "") : "";
            const altText = typeof alt === "string" ? alt : (alt.suggested_alt || alt.alt || "");
            html += `<div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
                <input type="text" class="ai-field-editable ai-image-alt"
                    data-media-id="${escapeAttr(mediaId)}"
                    value="${escapeAttr(altText)}"
                    style="flex: 1;">
                <span style="font-size: 11px; color: var(--on-surface-secondary); white-space: nowrap;">Image ${i + 1}</span>
            </div>`;
        }
        html += '</div>';
    }

    if (result.additional_recommendations && result.additional_recommendations.length > 0) {
        html += `<div class="form-group">
            <label>Additional Recommendations</label>
            <ul style="padding-left: 20px; margin: 8px 0;">`;
        for (const rec of result.additional_recommendations) {
            html += `<li style="margin-bottom: 6px; font-size: 14px;">${escapeHtml(rec)}</li>`;
        }
        html += `</ul></div>`;
    }

    if (result.summary) {
        html += `<div class="form-group">
            <label>Summary</label>
            <p style="font-size: 14px; line-height: 1.6;">${escapeHtml(result.summary)}</p>
        </div>`;
    }

    if (result.raw_response) {
        html += `<div class="form-group">
            <label>AI Response</label>
            <div class="ai-field-value" style="white-space: pre-wrap;"><code>${escapeHtml(result.raw_response)}</code></div>
        </div>`;
    }

    html += `<div class="modal-footer">
        <button class="btn btn-secondary" onclick="closeProductModal()">Cancel</button>
        <button class="btn btn-primary" onclick="applyChanges(${productId})">
            <span class="material-symbols-rounded">publish</span>
            Apply Changes
        </button>
    </div>`;

    html += "</div>";
    container.innerHTML = html;
}

function escapeAttr(str) {
    if (!str) return "";
    return str.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function removeChip(btn) {
    btn.parentElement.remove();
}

function handleChipKeydown(event, input) {
    if (event.key === "Enter" && input.value.trim()) {
        event.preventDefault();
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.innerHTML = `${escapeHtml(input.value.trim())}<button class="chip-remove" onclick="removeChip(this)">&times;</button>`;
        input.parentElement.insertBefore(chip, input);
        input.value = "";
    }
}

function getTagsFromContainer() {
    const container = document.getElementById("ai-tags-container");
    if (!container) return [];
    const chips = container.querySelectorAll(".chip");
    return Array.from(chips).map((chip) => {
        const clone = chip.cloneNode(true);
        const btn = clone.querySelector(".chip-remove");
        if (btn) btn.remove();
        return clone.textContent.trim();
    });
}

async function applyChanges(productId) {
    const title = document.getElementById("ai-h1-tag")?.value?.trim() || "";
    const metaTitle = document.getElementById("ai-meta-title")?.value?.trim() || "";
    const metaDescription = document.getElementById("ai-meta-description")?.value?.trim() || "";
    const description = document.getElementById("ai-description")?.value?.trim() || "";
    const tags = getTagsFromContainer();

    const imageAlts = [];
    document.querySelectorAll(".ai-image-alt").forEach((input) => {
        const mediaId = input.dataset.mediaId;
        const alt = input.value.trim();
        if (mediaId && alt) {
            imageAlts.push({ media_id: mediaId, alt: alt });
        }
    });

    const payload = {};
    if (title) payload.title = title;
    if (description) payload.description_html = description;
    if (metaTitle) payload.meta_title = metaTitle;
    if (metaDescription) payload.meta_description = metaDescription;
    if (tags.length > 0) payload.tags = tags;
    if (imageAlts.length > 0) payload.image_alts = imageAlts;

    if (Object.keys(payload).length === 0) {
        showToast("No changes to apply", "warning");
        return;
    }

    try {
        const applyBtn = document.querySelector('.modal-footer .btn-primary');
        if (applyBtn) {
            applyBtn.disabled = true;
            applyBtn.innerHTML = '<span class="material-symbols-rounded">sync</span> Applying...';
        }

        const resp = await apiRequest(`/api/seo/products/${productId}/apply`, {
            method: "POST",
            body: payload,
        });

        if (resp.ok) {
            showToast(`Changes applied! Updated: ${resp.updated_fields.join(", ")}`, "success");
            closeProductModal();
            loadProducts();
        } else {
            showToast(`Failed to apply: ${resp.error || "Unknown error"}`, "error");
            if (applyBtn) {
                applyBtn.disabled = false;
                applyBtn.innerHTML = '<span class="material-symbols-rounded">publish</span> Apply Changes';
            }
        }
    } catch (err) {
        showToast(`Error applying changes: ${err.message}`, "error");
        const applyBtn = document.querySelector('.modal-footer .btn-primary');
        if (applyBtn) {
            applyBtn.disabled = false;
            applyBtn.innerHTML = '<span class="material-symbols-rounded">publish</span> Apply Changes';
        }
    }
}

async function viewCollection(id) {
    try {
        const c = await apiRequest(`/api/seo/collections/${id}`);
        const modal = document.getElementById("product-modal");
        const content = document.getElementById("product-detail-content");
        document.getElementById("product-modal-title").textContent = c.title;

        let html = "";

        const typeBadge = c.collection_type === "smart"
            ? '<span class="badge badge-warning">Smart</span>'
            : '<span class="badge badge-success">Manual</span>';
        const publishedBadge = c.published
            ? '<span class="badge badge-success">Published</span>'
            : '<span class="badge badge-error">Unpublished</span>';

        html += `<div class="preview-info-grid">
            <div class="info-item"><span class="info-label">Type</span><span class="info-value">${typeBadge}</span></div>
            <div class="info-item"><span class="info-label">Published</span><span class="info-value">${publishedBadge}</span></div>
            <div class="info-item"><span class="info-label">Products</span><span class="info-value">${c.products_count != null ? c.products_count : "—"}</span></div>
            <div class="info-item"><span class="info-label">Handle</span><span class="info-value mono">${escapeHtml(c.handle || "—")}</span></div>
            <div class="info-item"><span class="info-label">Store</span><span class="info-value">${escapeHtml(c.store_name || "—")}</span></div>
        </div>`;

        html += '<div class="preview-section">';
        html += '<div class="preview-section-title"><span class="material-symbols-rounded">analytics</span> SEO Score Breakdown</div>';
        for (const [key, info] of Object.entries(c.seo_breakdown)) {
            const fillClass =
                info.score >= 80
                    ? "high"
                    : info.score >= 50
                      ? "medium"
                      : "low";
            html += `<div class="score-row">
                <span class="score-label">${escapeHtml(key.replace("_", " "))}</span>
                <div class="score-bar"><div class="score-bar-fill ${fillClass}" style="width: ${info.score}%;"></div></div>
                <span class="score-value">${info.score}</span>
            </div>`;
        }
        html += "</div>";

        html += `
            <div class="form-group">
                <label>Meta Title</label>
                <div class="form-control" style="background: var(--surface-dim);">${escapeHtml(c.meta_title) || "<em>Not set</em>"}</div>
            </div>
            <div class="form-group">
                <label>Meta Description</label>
                <div class="form-control" style="background: var(--surface-dim); min-height: 60px;">${escapeHtml(c.meta_description) || "<em>Not set</em>"}</div>
            </div>`;

        html += '<div class="preview-section">';
        html += '<div class="preview-section-title"><span class="material-symbols-rounded">description</span> Collection Description</div>';
        html += `<div class="description-preview">${c.description || ""}</div>`;
        html += '</div>';

        if (c.image_url) {
            html += '<div class="preview-section">';
            html += '<div class="preview-section-title"><span class="material-symbols-rounded">image</span> Collection Image</div>';
            html += `<div class="collection-image-preview" data-src="${escapeAttr(c.image_url)}">
                <img src="${escapeAttr(c.image_url)}" alt="${escapeAttr(c.title || "")}">
            </div>`;
            html += '</div>';
        }

        html += `<div style="margin-top: 20px;">
            <button class="btn btn-primary" onclick="analyzeCollection(${c.id})">
                <span class="material-symbols-rounded">smart_toy</span>
                Get AI Recommendations
            </button>
        </div>`;

        html += '<div id="ai-collection-results-container"></div>';

        content.innerHTML = html;

        const collImg = content.querySelector(".collection-image-preview");
        if (collImg) {
            collImg.addEventListener("click", () => openLightbox(collImg.dataset.src));
        }

        modal.classList.add("active");
    } catch (err) {
        showToast(`Failed to load collection: ${err.message}`, "error");
    }
}

async function analyzeCollection(collectionId) {
    const modal = document.getElementById("product-modal");
    if (!modal.classList.contains("active")) {
        await viewCollection(collectionId);
    }

    const resultsContainer = document.getElementById("ai-collection-results-container");
    if (!resultsContainer) return;

    resultsContainer.innerHTML = `
        <div class="ai-panel" style="margin-top: 20px;">
            <h4><span class="material-symbols-rounded">smart_toy</span> AI Analysis</h4>
            <div class="loading-spinner"><div class="spinner"></div><p>AI is analyzing this collection...</p></div>
        </div>`;

    try {
        const taskResp = await apiRequest("/api/tasks", {
            method: "POST",
            body: {
                task_type: "collection_seo",
                input: { collection_id: collectionId },
            },
        });

        const taskId = taskResp.task_id;
        pollCollectionTaskResult(taskId, resultsContainer, collectionId);
    } catch (err) {
        resultsContainer.innerHTML = `
            <div class="ai-panel" style="margin-top: 20px;">
                <h4><span class="material-symbols-rounded">smart_toy</span> AI Analysis</h4>
                <p style="color: var(--error);">Error: ${escapeHtml(err.message)}</p>
            </div>`;
    }
}

function pollCollectionTaskResult(taskId, container, collectionId) {
    const interval = setInterval(async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            if (task.status === "completed") {
                clearInterval(interval);
                renderCollectionAIResults(container, task.result, collectionId);
            } else if (task.status === "failed") {
                clearInterval(interval);
                container.innerHTML = `
                    <div class="ai-panel" style="margin-top: 20px;">
                        <h4><span class="material-symbols-rounded">smart_toy</span> AI Analysis</h4>
                        <p style="color: var(--error);">Failed: ${escapeHtml(task.error || "Unknown error")}</p>
                    </div>`;
            }
        } catch {
            clearInterval(interval);
        }
    }, 2000);
}

function renderCollectionAIResults(container, result, collectionId) {
    if (!result) {
        container.innerHTML = "";
        return;
    }

    let html = `
        <div class="ai-panel" style="margin-top: 20px;">
            <div class="success-banner">
                <span class="material-symbols-rounded">check_circle</span>
                Optimization complete!
            </div>`;

    html += '<div class="side-by-side">';
    html += `<div class="form-group">
        <label>Optimized Title (H1 Tag)</label>
        <input type="text" class="ai-field-editable" id="ai-collection-h1-tag"
            value="${escapeAttr(result.h1_tag || result.optimized_meta_title || "")}">
    </div>`;
    html += `<div class="form-group">
        <label>Meta Title</label>
        <input type="text" class="ai-field-editable" id="ai-collection-meta-title"
            value="${escapeAttr(result.optimized_meta_title || "")}">
    </div>`;
    html += '</div>';

    html += `<div class="form-group">
        <label>Meta Description</label>
        <textarea class="ai-field-editable" id="ai-collection-meta-description" rows="3">${escapeHtml(result.optimized_meta_description || "")}</textarea>
    </div>`;

    html += `<div class="form-group">
        <label>Collection Description</label>
        <textarea class="ai-field-editable" id="ai-collection-description" rows="8">${escapeHtml(result.optimized_description || "")}</textarea>
    </div>`;

    html += '<div class="form-group">';
    html += '<label>Keywords Used</label>';
    html += '<div style="display: flex; flex-wrap: wrap; gap: 6px;">';
    const keywords = result.keywords_used || [];
    for (const kw of keywords) {
        html += `<span class="chip-display">${escapeHtml(kw)}</span>`;
    }
    if (keywords.length === 0) {
        html += '<span style="font-size: 13px; color: var(--on-surface-secondary);">None</span>';
    }
    html += '</div></div>';

    if (result.competitor_insights) {
        html += `<div class="form-group">
            <label>Competitor Insights</label>
            <div class="ai-field-value" style="font-size: 14px; line-height: 1.6;">${escapeHtml(result.competitor_insights)}</div>
        </div>`;
    }

    if (result.additional_recommendations && result.additional_recommendations.length > 0) {
        html += `<div class="form-group">
            <label>Additional Recommendations</label>
            <ul style="padding-left: 20px; margin: 8px 0;">`;
        for (const rec of result.additional_recommendations) {
            html += `<li style="margin-bottom: 6px; font-size: 14px;">${escapeHtml(rec)}</li>`;
        }
        html += `</ul></div>`;
    }

    if (result.summary) {
        html += `<div class="form-group">
            <label>Summary</label>
            <p style="font-size: 14px; line-height: 1.6;">${escapeHtml(result.summary)}</p>
        </div>`;
    }

    if (result.raw_response) {
        html += `<div class="form-group">
            <label>AI Response</label>
            <div class="ai-field-value" style="white-space: pre-wrap;"><code>${escapeHtml(result.raw_response)}</code></div>
        </div>`;
    }

    html += `<div class="modal-footer">
        <button class="btn btn-secondary" onclick="closeProductModal()">Cancel</button>
        <button class="btn btn-primary" onclick="applyCollectionChanges(${collectionId})">
            <span class="material-symbols-rounded">publish</span>
            Apply Changes
        </button>
    </div>`;

    html += "</div>";
    container.innerHTML = html;
}

async function applyCollectionChanges(collectionId) {
    const title = document.getElementById("ai-collection-h1-tag")?.value?.trim() || "";
    const metaTitle = document.getElementById("ai-collection-meta-title")?.value?.trim() || "";
    const metaDescription = document.getElementById("ai-collection-meta-description")?.value?.trim() || "";
    const description = document.getElementById("ai-collection-description")?.value?.trim() || "";

    const payload = {};
    if (title) payload.title = title;
    if (description) payload.description_html = description;
    if (metaTitle) payload.meta_title = metaTitle;
    if (metaDescription) payload.meta_description = metaDescription;

    if (Object.keys(payload).length === 0) {
        showToast("No changes to apply", "warning");
        return;
    }

    try {
        const applyBtn = document.querySelector('.ai-panel .modal-footer .btn-primary');
        if (applyBtn) {
            applyBtn.disabled = true;
            applyBtn.innerHTML = '<span class="material-symbols-rounded">sync</span> Applying...';
        }

        const resp = await apiRequest(`/api/seo/collections/${collectionId}/apply`, {
            method: "POST",
            body: payload,
        });

        if (resp.ok) {
            showToast(`Changes applied! Updated: ${resp.updated_fields.join(", ")}`, "success");
            closeProductModal();
            loadCollections();
        } else {
            showToast(`Failed to apply: ${resp.error || "Unknown error"}`, "error");
            if (applyBtn) {
                applyBtn.disabled = false;
                applyBtn.innerHTML = '<span class="material-symbols-rounded">publish</span> Apply Changes';
            }
        }
    } catch (err) {
        showToast(`Error applying changes: ${err.message}`, "error");
        const applyBtn = document.querySelector('.ai-panel .modal-footer .btn-primary');
        if (applyBtn) {
            applyBtn.disabled = false;
            applyBtn.innerHTML = '<span class="material-symbols-rounded">publish</span> Apply Changes';
        }
    }
}


// ========== Schema Markup Tab ==========

async function loadSchemaTab() {
    const container = document.getElementById("schema-container");
    try {
        const [stats, products, settings] = await Promise.all([
            apiRequest("/api/schema/stats"),
            apiRequest("/api/schema/products"),
            apiRequest("/api/schema/settings"),
        ]);
        cachedSchemaStats = stats;
        cachedSchemaSettings = settings;
        renderSchemaTab(container, stats, products, settings);
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>${escapeHtml(err.message)}</p></div>`;
    }
}

function renderSchemaTab(container, stats, products, settings) {
    currentSchemaProducts = products;
    let html = "";

    html += '<div class="stats-grid" style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px;">';
    html += renderStatCard("Total Products", stats.total_products, "inventory_2");
    html += renderStatCard("Schemas Generated", stats.schemas_generated, "code");
    html += renderStatCard("Valid", stats.schemas_valid, "check_circle");
    html += renderStatCard("Pushed to Shopify", stats.schemas_pushed, "cloud_upload");
    html += "</div>";

    html += '<div class="card" style="padding: 20px; margin-bottom: 24px;">';
    html += '<h4 style="margin-bottom: 16px;">Organization Settings</h4>';
    html += '<div class="side-by-side">';
    html += `<div class="form-group">
        <label>Organization Name</label>
        <input type="text" class="form-control" id="schema-org-name" value="${escapeAttr(settings.organization_name)}">
    </div>`;
    html += `<div class="form-group">
        <label>Default Currency</label>
        <input type="text" class="form-control" id="schema-currency" value="${escapeAttr(settings.default_currency)}" placeholder="USD">
    </div>`;
    html += "</div>";
    html += '<div class="side-by-side">';
    html += `<div class="form-group">
        <label>Logo URL</label>
        <input type="text" class="form-control" id="schema-logo-url" value="${escapeAttr(settings.organization_logo_url)}" placeholder="https://...">
    </div>`;
    html += `<div class="form-group">
        <label>Organization URL</label>
        <input type="text" class="form-control" id="schema-org-url" value="${escapeAttr(settings.organization_url)}" placeholder="https://...">
    </div>`;
    html += "</div>";
    html += `<button class="btn btn-primary" onclick="saveSchemaSettings()">
        <span class="material-symbols-rounded">save</span> Save Settings
    </button>`;
    html += "</div>";

    html += '<div style="display: flex; gap: 8px; margin-bottom: 16px;">';
    html += `<button class="btn btn-primary" onclick="batchGenerateSchemas()">
        <span class="material-symbols-rounded">code</span> Generate All
    </button>`;
    html += `<button class="btn btn-secondary" onclick="batchPushSchemas()">
        <span class="material-symbols-rounded">cloud_upload</span> Push All to Shopify
    </button>`;
    html += "</div>";

    html += `<div class="status-filter-bar" style="gap: 6px;">
        <span style="font-size: 13px; color: var(--on-surface-secondary); font-weight: 500; margin-right: 4px;">Schema:</span>
        <button class="filter-chip ${schemaFilter.generated === "" ? "active" : ""}" onclick="setSchemaFilter('generated','')">All</button>
        <button class="filter-chip ${schemaFilter.generated === "yes" ? "active" : ""}" onclick="setSchemaFilter('generated','yes')">Generated</button>
        <button class="filter-chip ${schemaFilter.generated === "no" ? "active" : ""}" onclick="setSchemaFilter('generated','no')">Not Generated</button>

        <span style="font-size: 13px; color: var(--on-surface-secondary); font-weight: 500; margin-left: 12px; margin-right: 4px;">Valid:</span>
        <button class="filter-chip ${schemaFilter.valid === "" ? "active" : ""}" onclick="setSchemaFilter('valid','')">All</button>
        <button class="filter-chip ${schemaFilter.valid === "yes" ? "active" : ""}" onclick="setSchemaFilter('valid','yes')">Valid</button>
        <button class="filter-chip ${schemaFilter.valid === "no" ? "active" : ""}" onclick="setSchemaFilter('valid','no')">Invalid</button>

        <span style="font-size: 13px; color: var(--on-surface-secondary); font-weight: 500; margin-left: 12px; margin-right: 4px;">Pushed:</span>
        <button class="filter-chip ${schemaFilter.pushed === "" ? "active" : ""}" onclick="setSchemaFilter('pushed','')">All</button>
        <button class="filter-chip ${schemaFilter.pushed === "yes" ? "active" : ""}" onclick="setSchemaFilter('pushed','yes')">Pushed</button>
        <button class="filter-chip ${schemaFilter.pushed === "no" ? "active" : ""}" onclick="setSchemaFilter('pushed','no')">Not Pushed</button>
    </div>`;

    let filtered = products.slice();
    if (schemaFilter.generated === "yes") filtered = filtered.filter(p => p.schema_generated);
    else if (schemaFilter.generated === "no") filtered = filtered.filter(p => !p.schema_generated);
    if (schemaFilter.valid === "yes") filtered = filtered.filter(p => p.schema_generated && p.is_valid);
    else if (schemaFilter.valid === "no") filtered = filtered.filter(p => p.schema_generated && !p.is_valid);
    if (schemaFilter.pushed === "yes") filtered = filtered.filter(p => p.pushed_to_shopify);
    else if (schemaFilter.pushed === "no") filtered = filtered.filter(p => !p.pushed_to_shopify);

    filtered.sort((a, b) => {
        let valA, valB;
        switch (schemaSort.by) {
            case "title": valA = (a.title || "").toLowerCase(); valB = (b.title || "").toLowerCase(); break;
            case "status": valA = (a.status || "").toLowerCase(); valB = (b.status || "").toLowerCase(); break;
            case "schema": valA = a.schema_generated ? 1 : 0; valB = b.schema_generated ? 1 : 0; break;
            case "valid": valA = a.schema_generated ? (a.is_valid ? 2 : 1) : 0; valB = b.schema_generated ? (b.is_valid ? 2 : 1) : 0; break;
            case "pushed": valA = a.pushed_to_shopify ? 1 : 0; valB = b.pushed_to_shopify ? 1 : 0; break;
            default: valA = (a.title || "").toLowerCase(); valB = (b.title || "").toLowerCase();
        }
        if (valA < valB) return schemaSort.dir === "asc" ? -1 : 1;
        if (valA > valB) return schemaSort.dir === "asc" ? 1 : -1;
        return 0;
    });

    const schemaColumns = [
        { key: "title", label: "Product" },
        { key: "status", label: "Status" },
        { key: "schema", label: "Schema" },
        { key: "valid", label: "Valid" },
        { key: "pushed", label: "Pushed" },
    ];

    html += '<div class="table-container"><table><thead><tr>';
    html += '<th class="row-num-col">#</th>';
    for (const col of schemaColumns) {
        const isActive = schemaSort.by === col.key;
        const dirClass = isActive ? schemaSort.dir : "";
        html += `<th class="sortable-header ${dirClass}" onclick="toggleSchemaSort('${col.key}')">${col.label}</th>`;
    }
    html += "<th>Actions</th>";
    html += "</tr></thead><tbody>";

    const hasSchemaFilters = schemaFilter.generated || schemaFilter.valid || schemaFilter.pushed;
    if (filtered.length === 0 && hasSchemaFilters) {
        html += `<tr><td colspan="7" style="text-align: center; padding: 24px; color: var(--on-surface-secondary);">No products match the current filters.
            <br><button class="btn btn-sm btn-secondary" style="margin-top: 8px;" onclick="clearAllSchemaFilters()">
                <span class="material-symbols-rounded">clear_all</span> Clear Filters
            </button></td></tr>`;
    } else if (filtered.length === 0) {
        html += '<tr><td colspan="7" style="text-align: center; padding: 24px; color: var(--on-surface-secondary);">No products found</td></tr>';
    }

    for (let idx = 0; idx < filtered.length; idx++) {
        const p = filtered[idx];
        const schemaStatus = p.schema_generated
            ? '<span class="badge badge-success">Generated</span>'
            : '<span class="badge badge-info">Not Generated</span>';
        const validStatus = p.schema_generated
            ? (p.is_valid ? '<span class="badge badge-success">Valid</span>' : '<span class="badge badge-error">Invalid</span>')
            : '<span style="color: var(--on-surface-secondary);">\u2014</span>';
        const pushedStatus = p.pushed_to_shopify
            ? '<span class="badge badge-success">Yes</span>'
            : '<span style="color: var(--on-surface-secondary);">No</span>';

        html += `<tr>
            <td class="row-num-col">${idx + 1}</td>
            <td><strong>${escapeHtml(p.title)}</strong><br><small style="color: var(--on-surface-secondary);">${escapeHtml(p.handle)}</small></td>
            <td><span class="badge badge-info">${escapeHtml(p.status)}</span></td>
            <td>${schemaStatus}</td>
            <td>${validStatus}</td>
            <td>${pushedStatus}</td>
            <td style="white-space: nowrap;">
                <button class="btn btn-sm btn-primary" onclick="generateSchema(${p.id})">Generate</button>
                <button class="btn btn-sm btn-secondary" onclick="viewSchema(${p.id})" ${!p.schema_generated ? "disabled" : ""}>View</button>
                <button class="btn btn-sm btn-secondary" onclick="pushSchema(${p.id})" ${!p.schema_generated ? "disabled" : ""}>Push</button>
            </td>
        </tr>`;
    }

    html += "</tbody></table></div>";
    container.innerHTML = html;
}

function renderStatCard(label, value, icon) {
    return `<div class="card" style="padding: 16px; text-align: center;">
        <span class="material-symbols-rounded" style="font-size: 28px; color: var(--primary);">${icon}</span>
        <div style="font-size: 28px; font-weight: 600; margin: 4px 0;">${value}</div>
        <div style="font-size: 13px; color: var(--on-surface-secondary);">${label}</div>
    </div>`;
}

function toggleSchemaSort(key) {
    if (schemaSort.by === key) {
        schemaSort.dir = schemaSort.dir === "asc" ? "desc" : "asc";
    } else {
        schemaSort.by = key;
        schemaSort.dir = "asc";
    }
    saveFilter("schemaSort", schemaSort);
    rerenderSchemaTable();
}

function setSchemaFilter(filterKey, value) {
    schemaFilter[filterKey] = schemaFilter[filterKey] === value ? "" : value;
    saveFilter("schemaFilter", schemaFilter);
    rerenderSchemaTable();
}

function clearAllSchemaFilters() {
    schemaFilter = { generated: "", valid: "", pushed: "" };
    saveFilter("schemaFilter", schemaFilter);
    rerenderSchemaTable();
}

function rerenderSchemaTable() {
    const container = document.getElementById("schema-container");
    if (!container || currentSchemaProducts.length === 0 || !cachedSchemaStats || !cachedSchemaSettings) return;
    renderSchemaTab(container, cachedSchemaStats, currentSchemaProducts, cachedSchemaSettings);
}

async function generateSchema(productId) {
    try {
        const resp = await apiRequest(`/api/schema/products/${productId}/generate`, { method: "POST" });
        if (resp.ok) {
            showToast("Schema generated" + (resp.is_valid ? " (valid)" : " (has validation issues)"), resp.is_valid ? "success" : "warning");
            loadSchemaTab();
        }
    } catch (err) {
        showToast(`Generate failed: ${err.message}`, "error");
    }
}

async function viewSchema(productId) {
    const modal = document.getElementById("schema-modal");
    const content = document.getElementById("schema-modal-content");
    content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div><p>Loading...</p></div>';
    modal.classList.add("active");

    try {
        const resp = await apiRequest(`/api/schema/products/${productId}/view`);
        document.getElementById("schema-modal-title").textContent = "Schema Preview";

        let html = "";

        if (resp.is_valid) {
            html += '<div class="success-banner" style="margin-bottom: 16px;"><span class="material-symbols-rounded">check_circle</span> Valid Schema</div>';
        } else {
            html += '<div style="background: var(--error-light, #fce8e6); padding: 12px; border-radius: 8px; margin-bottom: 16px; color: var(--error);">';
            html += '<strong>Validation Issues:</strong><ul style="margin: 4px 0 0 16px;">';
            for (const err of (resp.validation_errors || [])) {
                html += `<li>${escapeHtml(err)}</li>`;
            }
            html += "</ul></div>";
        }

        html += '<div class="form-group"><label>JSON-LD</label>';
        html += `<pre style="background: var(--surface-dim); padding: 16px; border-radius: 8px; overflow: auto; max-height: 400px; font-size: 13px;"><code id="schema-json-display">${escapeHtml(JSON.stringify(resp.schema, null, 2))}</code></pre>`;
        html += `<button class="btn btn-sm btn-secondary" style="margin-top: 8px;" onclick="copySchemaJson()">
            <span class="material-symbols-rounded">content_copy</span> Copy JSON
        </button>`;
        html += "</div>";

        html += '<div class="form-group"><label>Script Tag</label>';
        html += `<pre style="background: var(--surface-dim); padding: 16px; border-radius: 8px; overflow: auto; max-height: 200px; font-size: 13px;"><code>${escapeHtml(resp.script_tag)}</code></pre>`;
        html += `<button class="btn btn-sm btn-secondary" style="margin-top: 8px;" onclick="copySchemaScriptTag()">
            <span class="material-symbols-rounded">content_copy</span> Copy Script Tag
        </button>`;
        html += "</div>";

        if (resp.pushed_to_shopify) {
            html += `<p style="font-size: 13px; color: var(--success);"><span class="material-symbols-rounded" style="vertical-align: middle; font-size: 16px;">check_circle</span> Pushed to Shopify${resp.pushed_at ? " at " + new Date(resp.pushed_at).toLocaleString() : ""}</p>`;
        }

        html += `<div class="modal-footer">
            <button class="btn btn-secondary" onclick="closeSchemaModal()">Close</button>
            <button class="btn btn-primary" onclick="pushSchema(${productId}); closeSchemaModal();">
                <span class="material-symbols-rounded">cloud_upload</span> Push to Shopify
            </button>
        </div>`;

        content.innerHTML = html;

        // Store data for copy functions
        content.dataset.schemaJson = JSON.stringify(resp.schema, null, 2);
        content.dataset.scriptTag = resp.script_tag;
    } catch (err) {
        content.innerHTML = `<p style="color: var(--error);">Error: ${escapeHtml(err.message)}</p>`;
    }
}

function closeSchemaModal() {
    document.getElementById("schema-modal").classList.remove("active");
}

function copySchemaJson() {
    const content = document.getElementById("schema-modal-content");
    const json = content.dataset.schemaJson;
    navigator.clipboard.writeText(json).then(() => showToast("JSON copied!", "success"));
}

function copySchemaScriptTag() {
    const content = document.getElementById("schema-modal-content");
    const tag = content.dataset.scriptTag;
    navigator.clipboard.writeText(tag).then(() => showToast("Script tag copied!", "success"));
}

async function pushSchema(productId) {
    try {
        const resp = await apiRequest(`/api/schema/products/${productId}/push`, { method: "POST" });
        if (resp.ok) {
            showToast("Schema pushed to Shopify!", "success");
            loadSchemaTab();
        }
    } catch (err) {
        showToast(`Push failed: ${err.message}`, "error");
    }
}

async function batchGenerateSchemas() {
    try {
        showToast("Generating schemas for all products...", "info");
        const resp = await apiRequest("/api/schema/batch/generate", { method: "POST", body: {} });
        if (resp.ok) {
            showToast(`Generated ${resp.generated}/${resp.total} schemas` + (resp.errors.length > 0 ? ` (${resp.errors.length} errors)` : ""), "success");
            loadSchemaTab();
        }
    } catch (err) {
        showToast(`Batch generate failed: ${err.message}`, "error");
    }
}

async function batchPushSchemas() {
    try {
        showToast("Pushing schemas to Shopify...", "info");
        const resp = await apiRequest("/api/schema/batch/push", { method: "POST", body: {} });
        if (resp.ok) {
            showToast(`Pushed ${resp.pushed}/${resp.total} schemas` + (resp.errors.length > 0 ? ` (${resp.errors.length} errors)` : ""), "success");
            loadSchemaTab();
        }
    } catch (err) {
        showToast(`Batch push failed: ${err.message}`, "error");
    }
}

async function saveSchemaSettings() {
    const payload = {
        organization_name: document.getElementById("schema-org-name")?.value?.trim() || "",
        organization_logo_url: document.getElementById("schema-logo-url")?.value?.trim() || "",
        organization_url: document.getElementById("schema-org-url")?.value?.trim() || "",
        default_currency: document.getElementById("schema-currency")?.value?.trim() || "USD",
    };

    try {
        const resp = await apiRequest("/api/schema/settings", { method: "POST", body: payload });
        if (resp.ok) {
            showToast("Schema settings saved!", "success");
        }
    } catch (err) {
        showToast(`Failed to save settings: ${err.message}`, "error");
    }
}

// ========== Generate Collections from Tags ==========

let generateTagsData = [];
let selectedTagKeys = new Set();
let tagTitleOverrides = {};
let aiFilterApplied = false;
let aiRejectedTags = {};
let minProductsFilter = 1;
let generateMode = "tags";
let taxonomyData = null;
let selectedTaxonomyCategories = {};
let taxonomyStoreId = null;
let tagsInlineMode = false;

function ensureGenerateCollectionsModal() {
    if (document.getElementById("generate-collections-modal")) return;

    const modalHtml = `
        <div class="modal-overlay" id="generate-collections-modal" style="padding: 0;">
            <div class="modal" style="width: 100vw; height: 100vh; max-width: 100vw; max-height: 100vh; border-radius: 0; display: flex; flex-direction: column;">
                <div class="modal-header">
                    <h3 id="generate-collections-title">Generate Collections from Tags</h3>
                    <button class="icon-btn" onclick="closeGenerateCollectionsModal()"><span class="material-symbols-rounded">close</span></button>
                </div>
                <div id="generate-collections-content" style="flex: 1; overflow-y: auto;"></div>
            </div>
        </div>`;

    const progressHtml = `
        <div class="progress-overlay" id="generate-progress-overlay">
            <div class="progress-card">
                <span class="material-symbols-rounded" style="font-size: 40px; color: var(--primary); margin-bottom: 12px;">sync</span>
                <h3>Generating Collections</h3>
                <p class="progress-subtitle" id="generate-progress-subtitle">Creating collections...</p>
                <div class="progress-bar-container">
                    <div class="progress-bar" id="generate-progress-bar"></div>
                </div>
                <div class="progress-stats" id="generate-progress-stats">0 / 0 completed</div>
                <div class="progress-current" id="generate-progress-current"></div>
            </div>
        </div>`;

    document.body.insertAdjacentHTML("beforeend", modalHtml);
    if (!document.getElementById("generate-progress-overlay")) {
        document.body.insertAdjacentHTML("beforeend", progressHtml);
    }
}

async function openGenerateCollectionsModal() {
    ensureGenerateCollectionsModal();
    const modal = document.getElementById("generate-collections-modal");
    const content = document.getElementById("generate-collections-content");
    content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div><p>Loading stores...</p></div>';
    modal.classList.add("active");

    try {
        const stores = await apiRequest("/api/stores");
        if (!stores || stores.length === 0) {
            content.innerHTML = '<div style="padding: 24px;"><p>No stores configured. Add a store first.</p></div>';
            return;
        }

        const storeId = stores.length === 1 ? stores[0].id : null;
        taxonomyStoreId = storeId;

        let hasTaxonomy = false;
        if (storeId) {
            try {
                const taxData = await apiRequest(`/api/keyword-taxonomy/${storeId}`);
                if (taxData.exists && taxData.status === "completed") {
                    hasTaxonomy = true;
                    taxonomyData = taxData;
                }
            } catch {}
        }

        generateMode = hasTaxonomy ? "taxonomy" : "tags";

        let storeSelectHtml = "";
        if (stores.length === 1) {
            storeSelectHtml = `<input type="hidden" id="generate-store-id" value="${stores[0].id}">
                <p style="margin-bottom: 0;">Store: <strong>${escapeHtml(stores[0].name)}</strong></p>`;
        } else {
            storeSelectHtml = `<div class="form-group">
                <label>Select Store</label>
                <select class="form-control" id="generate-store-id" onchange="onGenerateStoreChange(this.value)">
                    <option value="">-- Select a store --</option>
                    ${stores.map(s => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join("")}
                </select>
            </div>`;
        }

        let html = `<div style="padding: 24px;">${storeSelectHtml}`;

        html += `<div style="display: flex; gap: 0; margin: 16px 0; border-bottom: 2px solid var(--outline);">
            <button class="generate-mode-tab ${generateMode === "tags" ? "active" : ""}" onclick="switchGenerateMode('tags')" id="mode-tab-tags"
                style="padding: 10px 20px; border: none; background: none; cursor: pointer; font-size: 14px; font-weight: 500;
                color: ${generateMode === "tags" ? "var(--primary)" : "var(--on-surface-secondary)"};
                border-bottom: 2px solid ${generateMode === "tags" ? "var(--primary)" : "transparent"}; margin-bottom: -2px;">
                From Raw Tags
            </button>
            <button class="generate-mode-tab ${generateMode === "taxonomy" ? "active" : ""}" onclick="switchGenerateMode('taxonomy')" id="mode-tab-taxonomy"
                style="padding: 10px 20px; border: none; background: none; cursor: pointer; font-size: 14px; font-weight: 500;
                color: ${generateMode === "taxonomy" ? "var(--primary)" : "var(--on-surface-secondary)"};
                border-bottom: 2px solid ${generateMode === "taxonomy" ? "var(--primary)" : "transparent"}; margin-bottom: -2px;">
                From Taxonomy
            </button>
        </div>`;

        html += '<div id="generate-mode-content"></div>';
        html += '</div>';
        content.innerHTML = html;

        if (storeId) {
            if (generateMode === "taxonomy") {
                renderTaxonomyGenerateView(storeId);
            } else {
                loadTagsForStoreInline(storeId);
            }
        }
    } catch (err) {
        content.innerHTML = `<div style="padding: 24px;"><p style="color: var(--error);">Error: ${escapeHtml(err.message)}</p></div>`;
    }
}

async function onGenerateStoreChange(storeId) {
    if (!storeId) return;
    taxonomyStoreId = storeId;
    taxonomyData = null;

    try {
        const taxData = await apiRequest(`/api/keyword-taxonomy/${storeId}`);
        if (taxData.exists && taxData.status === "completed") {
            taxonomyData = taxData;
        }
    } catch {}

    const hasTaxonomy = !!taxonomyData;
    generateMode = hasTaxonomy ? "taxonomy" : "tags";
    updateModeTabStyles();

    if (generateMode === "taxonomy") {
        renderTaxonomyGenerateView(storeId);
    } else {
        loadTagsForStoreInline(storeId);
    }
}

function switchGenerateMode(mode) {
    generateMode = mode;
    updateModeTabStyles();

    const storeId = document.getElementById("generate-store-id")?.value;
    if (!storeId) {
        document.getElementById("generate-mode-content").innerHTML =
            '<p style="color: var(--on-surface-secondary); padding: 16px 0;">Select a store first.</p>';
        return;
    }

    if (mode === "taxonomy") {
        renderTaxonomyGenerateView(storeId);
    } else {
        loadTagsForStoreInline(storeId);
    }
}

function updateModeTabStyles() {
    const tagsTab = document.getElementById("mode-tab-tags");
    const taxTab = document.getElementById("mode-tab-taxonomy");
    if (tagsTab) {
        tagsTab.style.color = generateMode === "tags" ? "var(--primary)" : "var(--on-surface-secondary)";
        tagsTab.style.borderBottom = generateMode === "tags" ? "2px solid var(--primary)" : "2px solid transparent";
    }
    if (taxTab) {
        taxTab.style.color = generateMode === "taxonomy" ? "var(--primary)" : "var(--on-surface-secondary)";
        taxTab.style.borderBottom = generateMode === "taxonomy" ? "2px solid var(--primary)" : "2px solid transparent";
    }
}

async function loadTagsForStoreInline(storeId) {
    tagsInlineMode = true;
    const modeContent = document.getElementById("generate-mode-content");
    modeContent.innerHTML = '<div class="loading-spinner"><div class="spinner"></div><p>Extracting tags from products...</p></div>';

    try {
        const tags = await apiRequest(`/api/seo/collections/generate/tags?store_id=${storeId}`);
        generateTagsData = tags;
        selectedTagKeys = new Set();
        tagTitleOverrides = {};
        aiFilterApplied = false;
        aiRejectedTags = {};

        if (tags.length === 0) {
            modeContent.innerHTML = `<div class="empty-state">
                <span class="material-symbols-rounded">label_off</span>
                <p>No new tags found. Either products have no tags or all tags already have matching collections.</p>
            </div>`;
            return;
        }

        renderTagsTable(modeContent, storeId, true);
    } catch (err) {
        modeContent.innerHTML = `<p style="color: var(--error);">Error: ${escapeHtml(err.message)}</p>`;
    }
}

function closeGenerateCollectionsModal() {
    document.getElementById("generate-collections-modal").classList.remove("active");
    generateTagsData = [];
    selectedTagKeys = new Set();
    tagTitleOverrides = {};
    aiFilterApplied = false;
    aiRejectedTags = {};
    minProductsFilter = 1;
    generateMode = "tags";
    taxonomyData = null;
    selectedTaxonomyCategories = {};
    taxonomyStoreId = null;
    tagsInlineMode = false;
}

async function loadTagsForStore(storeId) {
    if (!storeId) return;

    const content = document.getElementById("generate-collections-content");
    const existingStore = document.getElementById("generate-store-id");
    const storeInfo = existingStore ? `<input type="hidden" id="generate-store-id" value="${storeId}">` : "";

    try {
        const tags = await apiRequest(`/api/seo/collections/generate/tags?store_id=${storeId}`);
        generateTagsData = tags;
        selectedTagKeys = new Set();
        tagTitleOverrides = {};
        aiFilterApplied = false;
        aiRejectedTags = {};

        if (tags.length === 0) {
            content.innerHTML = `<div style="padding: 24px;">
                ${storeInfo}
                <div class="empty-state">
                    <span class="material-symbols-rounded">label_off</span>
                    <p>No new tags found. Either products have no tags or all tags already have matching collections.</p>
                </div>
            </div>`;
            return;
        }

        renderTagsTable(content, storeId);
    } catch (err) {
        content.innerHTML = `<div style="padding: 24px;"><p style="color: var(--error);">Error: ${escapeHtml(err.message)}</p></div>`;
    }
}

function renderTagsTable(content, storeId, inline = false) {
    const tags = generateTagsData;
    let html = inline ? "" : `<input type="hidden" id="generate-store-id" value="${storeId}">`;

    html += '<div style="padding: 0 24px;">';
    html += `<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
        <div style="display: flex; align-items: center; gap: 16px;">
            <p style="margin: 0;">${tags.length} unique tag${tags.length !== 1 ? "s" : ""} found</p>
            <label style="display: flex; align-items: center; gap: 6px; font-size: 13px; white-space: nowrap;">
                Min products:
                <input type="number" class="form-control" id="min-products-input" value="${minProductsFilter}" min="1" style="width: 70px; padding: 4px 8px;"
                    onchange="minProductsFilter = Math.max(1, parseInt(this.value) || 1); reRenderTagsTable()">
            </label>
        </div>
        <div style="display: flex; gap: 8px;">
            <button class="btn btn-sm btn-secondary" id="ai-filter-btn" onclick="runAIFilter()" ${aiFilterApplied ? "disabled" : ""}>
                <span class="material-symbols-rounded">smart_toy</span>
                ${aiFilterApplied ? "AI Filtered" : "AI Filter (Optional)"}
            </button>
        </div>
    </div>`;

    const filteredTags = tags.filter(t => t.product_count >= minProductsFilter);

    html += '<div class="table-container" style="max-height: 60vh; overflow-y: auto;"><table><thead><tr>';
    html += '<th class="checkbox-col"><input type="checkbox" class="table-checkbox" id="select-all-tags-checkbox" onchange="toggleSelectAllTags(this)"></th>';
    html += "<th>Tag</th><th>Collection Title</th><th>Products</th><th>Sample Products</th><th>Status</th>";
    if (aiFilterApplied) html += "<th>AI Status</th>";
    html += "</tr></thead><tbody>";

    if (filteredTags.length === 0) {
        const colspan = aiFilterApplied ? 7 : 6;
        html += `<tr><td colspan="${colspan}" style="text-align: center; padding: 24px; color: var(--on-surface-secondary);">No tags with ${minProductsFilter}+ products</td></tr>`;
    }

    for (let idx = 0; idx < filteredTags.length; idx++) {
        const t = filteredTags[idx];
        const key = t.tag.toLowerCase();
        const isRejected = aiRejectedTags[key];
        const isExisting = !!t.existing_collection;
        const checked = selectedTagKeys.has(key) ? "checked" : "";
        const titleValue = tagTitleOverrides[key] || (isExisting ? t.existing_collection.title : t.tag.replace(/\b\w/g, c => c.toUpperCase()));
        const rowStyle = isRejected ? 'style="opacity: 0.5;"' : "";
        const tagRowClass = selectedTagKeys.has(key) ? "row-selected" : "";

        html += `<tr ${rowStyle} class="${tagRowClass}">
            <td class="checkbox-col"><input type="checkbox" class="table-checkbox" data-tag-key="${escapeAttr(key)}" data-index="${idx}" ${checked} onclick="toggleTagSelect('${escapeAttr(key)}', this)"></td>
            <td><strong>${escapeHtml(t.tag)}</strong></td>
            <td><input type="text" class="form-control" style="min-width: 180px;" value="${escapeAttr(titleValue)}" onchange="tagTitleOverrides['${escapeAttr(key)}'] = this.value" ${isExisting ? "disabled" : ""}></td>
            <td>${t.product_count}</td>
            <td style="font-size: 12px; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeAttr(t.sample_products.map(p => p.title).join(', '))}">${escapeHtml(t.sample_products.map(p => p.title).join(", "))}</td>
            <td>${isExisting ? '<span class="badge badge-warning" title="Collection exists — missing products will be added">Existing</span>' : '<span class="badge badge-info">New</span>'}</td>`;

        if (aiFilterApplied) {
            if (isRejected) {
                html += `<td><span class="badge badge-error" title="${escapeAttr(isRejected)}">Rejected</span></td>`;
            } else {
                html += '<td><span class="badge badge-success">Valid</span></td>';
            }
        }

        html += "</tr>";
    }

    html += "</tbody></table></div>";

    const showingText = filteredTags.length < tags.length ? `Showing ${filteredTags.length} of ${tags.length}` : `Showing all ${tags.length}`;

    let newCount = 0;
    let updateCount = 0;
    for (const t of filteredTags) {
        const key = t.tag.toLowerCase();
        if (selectedTagKeys.has(key)) {
            if (t.existing_collection) updateCount++;
            else newCount++;
        }
    }
    const generateBtnLabel = newCount > 0 && updateCount > 0
        ? `Generate ${newCount} / Update ${updateCount} Collections`
        : updateCount > 0
            ? `Update ${updateCount} Collection${updateCount !== 1 ? "s" : ""}`
            : `Generate ${newCount} Collection${newCount !== 1 ? "s" : ""}`;

    html += `<div style="display: flex; justify-content: space-between; align-items: center; margin-top: 16px; padding-bottom: 24px;">
        <div style="display: flex; flex-direction: column; gap: 8px;">
            <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                <input type="checkbox" id="auto-optimize-checkbox">
                Auto-optimize with AI after creation (SEO titles, descriptions, interlinking)
            </label>
            <span style="font-size: 12px; color: var(--on-surface-secondary);">${showingText} tags</span>
        </div>
        <div style="display: flex; gap: 8px;">
            <span id="selected-tags-count" style="line-height: 36px; margin-right: 8px;">${selectedTagKeys.size} selected</span>
            <button class="btn btn-secondary" onclick="closeGenerateCollectionsModal()">Cancel</button>
            <button class="btn btn-primary" onclick="executeGenerateCollections()" ${selectedTagKeys.size === 0 ? "disabled" : ""} id="generate-btn">
                <span class="material-symbols-rounded">add_circle</span>
                ${generateBtnLabel}
            </button>
        </div>
    </div>`;

    html += "</div>";
    content.innerHTML = html;

    const selectAllCb = document.getElementById("select-all-tags-checkbox");
    if (selectAllCb) {
        const nonRejected = filteredTags.filter(t => !aiRejectedTags[t.tag.toLowerCase()]);
        selectAllCb.checked = nonRejected.length > 0 && nonRejected.every(t => selectedTagKeys.has(t.tag.toLowerCase()));
    }
}

function toggleTagSelect(key, checkbox) {
    if (checkbox.checked) {
        selectedTagKeys.add(key);
    } else {
        selectedTagKeys.delete(key);
    }
    updateGenerateButton();
}

function toggleSelectAllTags(checkbox) {
    const filteredTags = generateTagsData.filter(t => t.product_count >= minProductsFilter);
    if (checkbox.checked) {
        for (const t of filteredTags) {
            const key = t.tag.toLowerCase();
            if (!aiRejectedTags[key]) {
                selectedTagKeys.add(key);
            }
        }
    } else {
        for (const t of filteredTags) {
            selectedTagKeys.delete(t.tag.toLowerCase());
        }
    }
    reRenderTagsTable();
}

function reRenderTagsTable() {
    const storeId = document.getElementById("generate-store-id")?.value;
    const target = tagsInlineMode
        ? document.getElementById("generate-mode-content")
        : document.getElementById("generate-collections-content");
    if (target && storeId) renderTagsTable(target, storeId, tagsInlineMode);
}

function updateGenerateButton() {
    const btn = document.getElementById("generate-btn");
    const count = document.getElementById("selected-tags-count");

    let newCount = 0;
    let updateCount = 0;
    const filteredTags = generateTagsData.filter(t => t.product_count >= minProductsFilter);
    for (const t of filteredTags) {
        const key = t.tag.toLowerCase();
        if (selectedTagKeys.has(key)) {
            if (t.existing_collection) updateCount++;
            else newCount++;
        }
    }

    if (btn) {
        btn.disabled = selectedTagKeys.size === 0;
        const label = newCount > 0 && updateCount > 0
            ? `Generate ${newCount} / Update ${updateCount} Collections`
            : updateCount > 0
                ? `Update ${updateCount} Collection${updateCount !== 1 ? "s" : ""}`
                : `Generate ${newCount} Collection${newCount !== 1 ? "s" : ""}`;
        btn.innerHTML = `<span class="material-symbols-rounded">add_circle</span> ${label}`;
    }
    if (count) {
        count.textContent = `${selectedTagKeys.size} selected`;
    }
}

function findClosestTagKey(aiKey, originalKeys) {
    if (originalKeys.has(aiKey)) return aiKey;
    for (const ok of originalKeys) {
        if (ok.includes(aiKey) || aiKey.includes(ok)) return ok;
    }
    return null;
}

async function runAIFilter() {
    const btn = document.getElementById("ai-filter-btn");
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="material-symbols-rounded">sync</span> Filtering...';
    }

    try {
        const filteredTags = generateTagsData.filter(t => t.product_count >= minProductsFilter);
        const tagsPayload = filteredTags.map(t => ({ tag: t.tag, product_count: t.product_count }));

        aiFilterApplied = true;
        aiRejectedTags = {};
        selectedTagKeys.clear();

        const originalKeys = new Set(filteredTags.map(t => t.tag.toLowerCase()));
        let totalValid = 0;
        let totalRejected = 0;

        const BATCH_SIZE = 50;
        for (let i = 0; i < tagsPayload.length; i += BATCH_SIZE) {
            const batch = tagsPayload.slice(i, i + BATCH_SIZE);
            const batchNum = Math.floor(i / BATCH_SIZE) + 1;
            const totalBatches = Math.ceil(tagsPayload.length / BATCH_SIZE);

            if (btn && totalBatches > 1) {
                btn.innerHTML = `<span class="material-symbols-rounded">sync</span> Filtering batch ${batchNum}/${totalBatches}...`;
            }

            const resp = await apiRequest("/api/seo/collections/generate/filter-tags", {
                method: "POST",
                body: { tags: batch },
            });

            if (resp.rejected_tags) {
                totalRejected += resp.rejected_tags.length;
                for (const r of resp.rejected_tags) {
                    const rKey = r.tag.toLowerCase();
                    const matched = findClosestTagKey(rKey, originalKeys);
                    if (matched) {
                        aiRejectedTags[matched] = r.reason || "Rejected by AI";
                    }
                }
            }

            if (resp.valid_tags) {
                totalValid += resp.valid_tags.length;
                for (const v of resp.valid_tags) {
                    const vKey = v.tag.toLowerCase();
                    const matched = findClosestTagKey(vKey, originalKeys);
                    if (matched) {
                        selectedTagKeys.add(matched);
                        if (v.collection_title) {
                            tagTitleOverrides[matched] = v.collection_title;
                        }
                    }
                }
            }
        }

        reRenderTagsTable();
        showToast(`AI filter complete: ${totalValid} valid, ${totalRejected} rejected`, "success");
    } catch (err) {
        showToast(`AI filter failed: ${err.message}`, "error");
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<span class="material-symbols-rounded">smart_toy</span> AI Filter (Optional)';
        }
    }
}

async function executeGenerateCollections() {
    if (selectedTagKeys.size === 0) return;

    const storeId = document.getElementById("generate-store-id")?.value;
    if (!storeId) {
        showToast("Please select a store", "warning");
        return;
    }

    const autoOptimize = document.getElementById("auto-optimize-checkbox")?.checked || false;

    const tagsToGenerate = [];
    for (const t of generateTagsData) {
        const key = t.tag.toLowerCase();
        if (selectedTagKeys.has(key)) {
            tagsToGenerate.push({
                tag: t.tag,
                collection_title: tagTitleOverrides[key] || t.tag.replace(/\b\w/g, c => c.toUpperCase()),
            });
        }
    }

    closeGenerateCollectionsModal();
    ensureGenerateCollectionsModal();

    const overlay = document.getElementById("generate-progress-overlay");
    const bar = document.getElementById("generate-progress-bar");
    const stats = document.getElementById("generate-progress-stats");
    const current = document.getElementById("generate-progress-current");
    const subtitle = document.getElementById("generate-progress-subtitle");

    bar.style.width = "0%";
    stats.textContent = `0 / ${tagsToGenerate.length} completed`;
    current.textContent = "";
    subtitle.textContent = "Creating collections on Shopify...";
    overlay.classList.add("active");

    try {
        const resp = await apiRequest("/api/seo/collections/generate", {
            method: "POST",
            body: {
                store_id: parseInt(storeId),
                tags: tagsToGenerate,
                auto_optimize: autoOptimize,
            },
        });

        const bulkTaskId = resp.bulk_task_id;
        pollGenerateTask(bulkTaskId, tagsToGenerate.length, autoOptimize);
    } catch (err) {
        overlay.classList.remove("active");
        showToast(`Generation failed: ${err.message}`, "error");
    }
}

function pollGenerateTask(bulkTaskId, total, autoOptimize) {
    const overlay = document.getElementById("generate-progress-overlay");
    const bar = document.getElementById("generate-progress-bar");
    const stats = document.getElementById("generate-progress-stats");
    const current = document.getElementById("generate-progress-current");
    const subtitle = document.getElementById("generate-progress-subtitle");

    const interval = setInterval(async () => {
        try {
            const task = await apiRequest(`/api/tasks/bulk/${bulkTaskId}`);
            const done = task.completed + task.failed;
            const pct = total > 0 ? Math.round((done / total) * 100) : 0;

            bar.style.width = `${pct}%`;
            stats.textContent = `${done} / ${total} completed` + (task.failed > 0 ? ` (${task.failed} failed)` : "");
            subtitle.textContent = "Creating collections on Shopify...";

            if (task.current_product_title) {
                current.textContent = `Currently: ${task.current_product_title}`;
            }

            if (task.status === "completed") {
                clearInterval(interval);
                bar.style.width = "100%";
                current.textContent = "";

                if (autoOptimize && task.completed > 0) {
                    subtitle.textContent = "Collections created! Now running SEO optimization...";
                    stats.textContent = `${task.completed} collections created. AI optimization running in background.`;
                    setTimeout(() => {
                        overlay.classList.remove("active");
                        loadCollections();
                        showToast(
                            `${task.completed} collection${task.completed !== 1 ? "s" : ""} created! SEO optimization running in background.`,
                            task.failed > 0 ? "warning" : "success"
                        );
                    }, 2000);
                } else {
                    subtitle.textContent = "All done!";
                    setTimeout(() => {
                        overlay.classList.remove("active");
                        loadCollections();
                        showToast(
                            `${task.completed} collection${task.completed !== 1 ? "s" : ""} created${task.failed > 0 ? `, ${task.failed} failed` : ""}`,
                            task.failed > 0 ? "warning" : "success"
                        );
                    }, 1500);
                }
            }
        } catch {
            clearInterval(interval);
            overlay.classList.remove("active");
            showToast("Error polling generation status", "error");
        }
    }, 2000);
}


async function renderTaxonomyGenerateView(storeId) {
    const modeContent = document.getElementById("generate-mode-content");

    if (!taxonomyData) {
        try {
            const data = await apiRequest(`/api/keyword-taxonomy/${storeId}`);
            if (!data.exists || data.status !== "completed") {
                modeContent.innerHTML = `<div class="empty-state">
                    <span class="material-symbols-rounded">label_off</span>
                    <p>No taxonomy found for this store. Run Keyword Analysis first, or use "From Raw Tags" mode.</p>
                </div>`;
                return;
            }
            taxonomyData = data;
        } catch (err) {
            modeContent.innerHTML = `<p style="color: var(--error);">Error loading taxonomy: ${escapeHtml(err.message)}</p>`;
            return;
        }
    }

    const taxonomy = taxonomyData.taxonomy || {};
    const assignments = taxonomyData.product_assignments || [];
    const categories = taxonomy.categories || [];

    if (categories.length === 0) {
        modeContent.innerHTML = `<div class="empty-state">
            <span class="material-symbols-rounded">category</span>
            <p>Taxonomy has no categories. Re-run Keyword Analysis.</p>
        </div>`;
        return;
    }

    const tagProductCounts = {};
    for (const a of assignments) {
        for (const tag of (a.assigned_tags || [])) {
            tagProductCounts[tag] = (tagProductCounts[tag] || 0) + 1;
        }
    }

    if (Object.keys(selectedTaxonomyCategories).length === 0) {
        for (const cat of categories) {
            selectedTaxonomyCategories[cat.name] = {
                selected: true,
                tags: {},
            };
            for (const tag of (cat.tags || [])) {
                selectedTaxonomyCategories[cat.name].tags[tag] = true;
            }
        }
    }

    let html = '';
    html += `<p style="font-size: 13px; color: var(--on-surface-secondary); margin-bottom: 16px;">
        Create hierarchical collections: each category becomes a parent collection, each tag becomes a child sub-collection with precise product assignment.
    </p>`;

    html += '<div class="table-container" style="max-height: 55vh; overflow-y: auto;">';

    for (const cat of categories) {
        const catState = selectedTaxonomyCategories[cat.name] || { selected: false, tags: {} };
        const catTags = cat.tags || [];

        const catProductIds = new Set();
        for (const tag of catTags) {
            for (const a of assignments) {
                if ((a.assigned_tags || []).includes(tag)) {
                    catProductIds.add(a.product_id);
                }
            }
        }

        const selectedTagCount = catTags.filter(t => catState.tags[t]).length;
        const allSelected = selectedTagCount === catTags.length && catState.selected;

        html += `<div style="border: 1px solid var(--outline); border-radius: 8px; margin-bottom: 12px; overflow: hidden;">`;
        html += `<div style="display: flex; align-items: center; gap: 12px; padding: 12px 16px; background: var(--surface-dim); cursor: pointer;"
            onclick="toggleTaxonomyCategoryExpand('${escapeAttr(cat.name)}')">
            <input type="checkbox" class="table-checkbox" id="tax-cat-checkbox-${escapeAttr(cat.name)}" ${allSelected ? "checked" : ""}
                onclick="event.stopPropagation(); toggleTaxonomyCategory('${escapeAttr(cat.name)}', this.checked)">
            <span style="font-weight: 600; flex: 1;">${escapeHtml(cat.name)}</span>
            <span style="font-size: 12px; color: var(--on-surface-secondary);">${selectedTagCount}/${catTags.length} tags &middot; ${catProductIds.size} products</span>
            <span class="material-symbols-rounded taxonomy-chevron" id="chevron-${escapeAttr(cat.name)}" style="transition: transform 0.2s;">expand_more</span>
        </div>`;

        html += `<div id="tax-cat-${escapeAttr(cat.name)}" style="padding: 0 16px 12px;">`;
        for (const tag of catTags) {
            const count = tagProductCounts[tag] || 0;
            const checked = catState.tags[tag] ? "checked" : "";
            html += `<label style="display: flex; align-items: center; gap: 10px; padding: 6px 0; cursor: pointer; border-bottom: 1px solid var(--outline-light, #f0f0f0);">
                <input type="checkbox" class="table-checkbox" ${checked}
                    onchange="toggleTaxonomyTag('${escapeAttr(cat.name)}', '${escapeAttr(tag)}', this.checked)">
                <span style="flex: 1;">${escapeHtml(tag)}</span>
                <span style="font-size: 12px; color: var(--on-surface-secondary);">${count} products</span>
            </label>`;
        }
        html += '</div></div>';
    }

    html += '</div>';

    let totalCategories = 0;
    let totalTags = 0;
    let totalProducts = new Set();
    for (const cat of categories) {
        const catState = selectedTaxonomyCategories[cat.name];
        if (!catState || !catState.selected) continue;
        const selectedTags = (cat.tags || []).filter(t => catState.tags[t]);
        if (selectedTags.length === 0) continue;
        totalCategories++;
        totalTags += selectedTags.length;
        for (const tag of selectedTags) {
            for (const a of assignments) {
                if ((a.assigned_tags || []).includes(tag)) {
                    totalProducts.add(a.product_id);
                }
            }
        }
    }

    html += `<div style="display: flex; justify-content: space-between; align-items: center; margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--outline);">
        <div style="display: flex; flex-direction: column; gap: 8px;">
            <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                <input type="checkbox" id="taxonomy-auto-optimize-checkbox">
                Auto-optimize with AI after creation (SEO titles, descriptions, interlinking)
            </label>
            <span id="taxonomy-summary-text" style="font-size: 12px; color: var(--on-surface-secondary);">
                ${totalCategories} parent collection${totalCategories !== 1 ? "s" : ""} + ${totalTags} sub-collection${totalTags !== 1 ? "s" : ""} &middot; ${totalProducts.size} unique products
            </span>
        </div>
        <div style="display: flex; gap: 8px;">
            <button class="btn btn-secondary" onclick="closeGenerateCollectionsModal()">Cancel</button>
            <button class="btn btn-primary" id="taxonomy-generate-btn" onclick="executeGenerateTaxonomyCollections()" ${totalTags === 0 ? "disabled" : ""}>
                <span class="material-symbols-rounded">account_tree</span>
                Generate ${totalCategories + totalTags} Collections
            </button>
        </div>
    </div>`;

    modeContent.innerHTML = html;
}

function toggleTaxonomyCategoryExpand(catName) {
    const el = document.getElementById(`tax-cat-${catName}`);
    const chevron = document.getElementById(`chevron-${catName}`);
    if (el) {
        const isHidden = el.style.display === "none";
        el.style.display = isHidden ? "" : "none";
        if (chevron) chevron.style.transform = isHidden ? "" : "rotate(-90deg)";
    }
}

function toggleTaxonomyCategory(catName, checked) {
    if (!selectedTaxonomyCategories[catName]) return;
    selectedTaxonomyCategories[catName].selected = checked;
    for (const tag of Object.keys(selectedTaxonomyCategories[catName].tags)) {
        selectedTaxonomyCategories[catName].tags[tag] = checked;
    }
    const container = document.getElementById(`tax-cat-${catName}`);
    if (container) {
        container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
            cb.checked = checked;
        });
    }
    updateTaxonomySummary();
}

function toggleTaxonomyTag(catName, tag, checked) {
    if (!selectedTaxonomyCategories[catName]) return;
    selectedTaxonomyCategories[catName].tags[tag] = checked;
    const allTags = selectedTaxonomyCategories[catName].tags;
    const anySelected = Object.values(allTags).some(v => v);
    const allSelected = Object.values(allTags).every(v => v);
    selectedTaxonomyCategories[catName].selected = anySelected;
    const catCheckbox = document.getElementById(`tax-cat-checkbox-${catName}`);
    if (catCheckbox) {
        catCheckbox.checked = allSelected;
        catCheckbox.indeterminate = anySelected && !allSelected;
    }
    updateTaxonomySummary();
}

function updateTaxonomySummary() {
    if (!taxonomyData) return;
    const taxonomy = taxonomyData.taxonomy || {};
    const assignments = taxonomyData.product_assignments || [];
    const categories = taxonomy.categories || [];

    let totalCategories = 0;
    let totalTags = 0;
    let totalProducts = new Set();
    for (const cat of categories) {
        const catState = selectedTaxonomyCategories[cat.name];
        if (!catState || !catState.selected) continue;
        const selectedTags = (cat.tags || []).filter(t => catState.tags[t]);
        if (selectedTags.length === 0) continue;
        totalCategories++;
        totalTags += selectedTags.length;
        for (const tag of selectedTags) {
            for (const a of assignments) {
                if ((a.assigned_tags || []).includes(tag)) {
                    totalProducts.add(a.product_id);
                }
            }
        }
    }

    const summaryEl = document.getElementById("taxonomy-summary-text");
    if (summaryEl) {
        summaryEl.textContent = `${totalCategories} parent collection${totalCategories !== 1 ? "s" : ""} + ${totalTags} sub-collection${totalTags !== 1 ? "s" : ""} \u00b7 ${totalProducts.size} unique products`;
    }

    const btn = document.getElementById("taxonomy-generate-btn");
    if (btn) {
        btn.disabled = totalTags === 0;
        btn.innerHTML = `<span class="material-symbols-rounded">account_tree</span> Generate ${totalCategories + totalTags} Collections`;
    }
}

async function executeGenerateTaxonomyCollections() {
    const storeId = document.getElementById("generate-store-id")?.value;
    if (!storeId) {
        showToast("Please select a store", "warning");
        return;
    }

    const autoOptimize = document.getElementById("taxonomy-auto-optimize-checkbox")?.checked || false;

    const categoriesToGenerate = [];
    for (const [catName, catState] of Object.entries(selectedTaxonomyCategories)) {
        if (!catState.selected) continue;
        const selectedTags = Object.entries(catState.tags)
            .filter(([, v]) => v)
            .map(([t]) => t);
        if (selectedTags.length === 0) continue;
        categoriesToGenerate.push({ name: catName, tags: selectedTags });
    }

    if (categoriesToGenerate.length === 0) {
        showToast("No categories selected", "warning");
        return;
    }

    let totalWork = 0;
    for (const cat of categoriesToGenerate) {
        totalWork += 1 + cat.tags.length;
    }

    closeGenerateCollectionsModal();
    ensureGenerateCollectionsModal();

    const overlay = document.getElementById("generate-progress-overlay");
    const bar = document.getElementById("generate-progress-bar");
    const stats = document.getElementById("generate-progress-stats");
    const current = document.getElementById("generate-progress-current");
    const subtitle = document.getElementById("generate-progress-subtitle");

    bar.style.width = "0%";
    stats.textContent = `0 / ${totalWork} completed`;
    current.textContent = "";
    subtitle.textContent = "Creating taxonomy collections on Shopify...";
    overlay.classList.add("active");

    try {
        const resp = await apiRequest("/api/seo/collections/generate/taxonomy", {
            method: "POST",
            body: {
                store_id: parseInt(storeId),
                categories: categoriesToGenerate,
                auto_optimize: autoOptimize,
            },
        });

        const bulkTaskId = resp.bulk_task_id;
        pollGenerateTask(bulkTaskId, totalWork, autoOptimize);
    } catch (err) {
        overlay.classList.remove("active");
        showToast(`Generation failed: ${err.message}`, "error");
    }
}

async function loadProductSchemaPreview(productId) {
    const container = document.getElementById("product-schema-content");
    if (!container) return;

    try {
        const resp = await apiRequest(`/api/schema/products/${productId}/generate`, { method: "POST" });
        let html = "";

        if (resp.is_valid) {
            html += '<div class="success-banner" style="margin-bottom: 12px;"><span class="material-symbols-rounded">check_circle</span> Valid Schema</div>';
        } else {
            html += '<div style="background: var(--error-light, #fce8e6); padding: 8px 12px; border-radius: 8px; margin-bottom: 12px; color: var(--error); font-size: 13px;">';
            html += "<strong>Validation Issues:</strong> " + (resp.validation_errors || []).join(", ");
            html += "</div>";
        }

        html += `<pre style="background: var(--surface-dim); padding: 12px; border-radius: 8px; overflow: auto; max-height: 300px; font-size: 12px;"><code>${escapeHtml(JSON.stringify(resp.schema, null, 2))}</code></pre>`;
        html += '<div style="display: flex; gap: 8px; margin-top: 8px;">';
        html += `<button class="btn btn-sm btn-secondary" onclick="navigator.clipboard.writeText(JSON.stringify(${escapeAttr(JSON.stringify(resp.schema))}, null, 2)).then(() => showToast('Copied!', 'success'))">
            <span class="material-symbols-rounded">content_copy</span> Copy Schema
        </button>`;
        html += `<button class="btn btn-sm btn-primary" onclick="pushSchema(${productId})">
            <span class="material-symbols-rounded">cloud_upload</span> Push to Shopify
        </button>`;
        html += "</div>";

        container.innerHTML = html;
    } catch (err) {
        container.innerHTML = `<p style="font-size: 13px; color: var(--on-surface-secondary);">Schema preview unavailable: ${escapeHtml(err.message)}</p>`;
    }
}

function openKeywordAnalysisModal() {
    const modal = document.getElementById("keyword-analysis-modal");
    const content = document.getElementById("keyword-analysis-content");
    content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div><p>Loading taxonomy data...</p></div>';
    modal.classList.add("active");

    loadKeywordTaxonomy();
}

function closeKeywordAnalysisModal() {
    document.getElementById("keyword-analysis-modal").classList.remove("active");
}

async function loadKeywordTaxonomy() {
    const content = document.getElementById("keyword-analysis-content");

    const storeIds = [...new Set(currentProducts.map(p => p.store_id))];
    if (storeIds.length === 0) {
        content.innerHTML = '<div class="empty-state"><p>No products loaded. Sync a store first.</p></div>';
        return;
    }
    const storeId = storeIds[0];

    try {
        const data = await apiRequest(`/api/keyword-taxonomy/${storeId}`);
        renderKeywordAnalysisContent(content, data, storeId);
    } catch (err) {
        content.innerHTML = `<div class="empty-state"><p>Error: ${escapeHtml(err.message)}</p></div>`;
    }
}

function renderKeywordAnalysisContent(content, data, storeId) {
    let html = '<div style="padding: 24px;">';

    if (data.exists && data.status === "completed") {
        const taxonomy = data.taxonomy || {};
        const assignments = data.product_assignments || [];
        const categories = taxonomy.categories || [];
        const globalTags = taxonomy.global_tags || [];

        html += `<div style="display: flex; gap: 8px; align-items: center; margin-bottom: 20px; flex-wrap: wrap;">
            <span style="font-size: 13px; color: var(--on-surface-secondary);">
                ${data.product_count} products &middot; Last run: ${data.completed_at ? new Date(data.completed_at).toLocaleString() : "N/A"}
            </span>
        </div>`;

        if (taxonomy.summary) {
            html += `<div class="form-group" style="margin-bottom: 20px;">
                <label>Analysis Summary</label>
                <p style="font-size: 14px; line-height: 1.6; color: var(--on-surface);">${escapeHtml(taxonomy.summary)}</p>
            </div>`;
        }

        if (globalTags.length > 0) {
            html += '<div class="form-group" style="margin-bottom: 20px;">';
            html += '<label>Global Tags</label>';
            html += '<div style="display: flex; flex-wrap: wrap; gap: 6px;">';
            for (const tag of globalTags) {
                html += `<span class="chip-display">${escapeHtml(tag)}</span>`;
            }
            html += '</div></div>';
        }

        if (categories.length > 0) {
            html += '<div class="form-group" style="margin-bottom: 20px;">';
            html += '<label>Tag Categories</label>';
            html += '<div class="table-container"><table><thead><tr><th>Category</th><th>Tags</th></tr></thead><tbody>';
            for (const cat of categories) {
                const catTags = (cat.tags || []).map(t => `<span class="chip-display">${escapeHtml(t)}</span>`).join(" ");
                html += `<tr><td><strong>${escapeHtml(cat.name || "")}</strong></td><td>${catTags}</td></tr>`;
            }
            html += '</tbody></table></div></div>';
        }

        if (assignments.length > 0) {
            html += '<div class="form-group">';
            html += `<label>Product Assignments (${assignments.length})</label>`;
            html += '<div class="table-container" style="max-height: 400px; overflow-y: auto;"><table><thead><tr>';
            html += '<th>#</th><th>Product</th><th>Assigned Tags</th>';
            html += '</tr></thead><tbody>';
            for (let i = 0; i < assignments.length; i++) {
                const a = assignments[i];
                const tags = (a.assigned_tags || []).map(t => `<span class="chip-display">${escapeHtml(t)}</span>`).join(" ");
                const productTitle = currentProducts.find(p => p.id === a.product_id)?.title || `#${a.product_id}`;
                html += `<tr><td>${i + 1}</td><td>${escapeHtml(productTitle)}</td><td>${tags}</td></tr>`;
            }
            html += '</tbody></table></div></div>';
        }

        html += `<div style="display: flex; gap: 8px; margin-top: 24px; padding-top: 20px; border-top: 1px solid var(--outline);">
            <button class="btn btn-primary" onclick="confirmBulkApplyTags(${storeId})" style="min-width: 220px;">
                <span class="material-symbols-rounded">publish</span>
                Apply Tags to Shopify (${assignments.length} products)
            </button>
            <button class="btn btn-secondary" onclick="runKeywordAnalysis(${storeId})">
                <span class="material-symbols-rounded">refresh</span>
                Re-run Analysis
            </button>
        </div>`;

    } else {
        html += `<div class="empty-state">
            <span class="material-symbols-rounded">label</span>
            <p>No keyword analysis has been run for this store yet.</p>
            <p style="font-size: 13px; color: var(--on-surface-secondary); margin-bottom: 16px;">
                Keyword analysis examines all products in the store and builds a consistent tag taxonomy,
                ensuring product variants share the same tags for better collection grouping.
            </p>
            <button class="btn btn-primary" onclick="runKeywordAnalysis(${storeId})">
                <span class="material-symbols-rounded">auto_awesome</span>
                Run Keyword Analysis
            </button>
        </div>`;
    }

    html += '</div>';
    content.innerHTML = html;
}

async function runKeywordAnalysis(storeId) {
    const content = document.getElementById("keyword-analysis-content");
    content.innerHTML = `<div style="padding: 24px;">
        <div class="loading-spinner">
            <div class="spinner"></div>
            <p>AI is analyzing all products and building taxonomy...</p>
            <p style="font-size: 13px; color: var(--on-surface-secondary);">This may take a minute for large catalogs.</p>
        </div>
    </div>`;

    try {
        const resp = await apiRequest("/api/tasks/keyword-analysis", {
            method: "POST",
            body: { store_id: storeId },
        });

        pollKeywordAnalysisTask(resp.task_id, storeId);
    } catch (err) {
        content.innerHTML = `<div style="padding: 24px;"><div class="empty-state"><p style="color: var(--error);">Error: ${escapeHtml(err.message)}</p></div></div>`;
    }
}

function pollKeywordAnalysisTask(taskId, storeId) {
    const content = document.getElementById("keyword-analysis-content");

    const interval = setInterval(async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            if (task.status === "completed") {
                clearInterval(interval);
                showToast("Keyword analysis complete!", "success");
                const data = await apiRequest(`/api/keyword-taxonomy/${storeId}`);
                renderKeywordAnalysisContent(content, data, storeId);
            } else if (task.status === "failed") {
                clearInterval(interval);
                content.innerHTML = `<div style="padding: 24px;"><div class="empty-state">
                    <p style="color: var(--error);">Analysis failed: ${escapeHtml(task.error || "Unknown error")}</p>
                    <button class="btn btn-primary" style="margin-top: 12px;" onclick="runKeywordAnalysis(${storeId})">Retry</button>
                </div></div>`;
            }
        } catch {
            clearInterval(interval);
            content.innerHTML = '<div style="padding: 24px;"><div class="empty-state"><p>Error polling task status.</p></div></div>';
        }
    }, 3000);
}

function confirmBulkApplyTags(storeId) {
    document.getElementById("confirm-dialog-title").textContent = "Apply Tags to Shopify";
    document.getElementById("confirm-dialog-message").textContent =
        "This will replace tags on all assigned products in Shopify with the taxonomy-based tags. Continue?";

    const okBtn = document.getElementById("confirm-dialog-ok");
    okBtn.onclick = () => {
        closeConfirmDialog();
        executeBulkApplyTags(storeId);
    };
    _openConfirmDialog();
}

async function executeBulkApplyTags(storeId) {
    const overlay = document.getElementById("bulk-progress-overlay");
    const bar = document.getElementById("bulk-progress-bar");
    const stats = document.getElementById("bulk-progress-stats");
    const current = document.getElementById("bulk-progress-current");
    const subtitle = document.getElementById("bulk-progress-subtitle");

    bar.style.width = "0%";
    stats.textContent = "0 / ? completed";
    current.textContent = "";
    subtitle.textContent = "Applying tags...";
    overlay.classList.add("active");

    try {
        const resp = await apiRequest(`/api/seo/bulk-apply-tags/${storeId}`, {
            method: "POST",
            body: {},
        });

        const bulkTaskId = resp.bulk_task_id;
        const total = resp.total;
        stats.textContent = `0 / ${total} completed`;

        const interval = setInterval(async () => {
            try {
                const task = await apiRequest(`/api/tasks/bulk/${bulkTaskId}`);
                const done = task.completed + task.failed;
                const pct = total > 0 ? Math.round((done / total) * 100) : 0;

                bar.style.width = `${pct}%`;
                stats.textContent = `${done} / ${total} completed` + (task.failed > 0 ? ` (${task.failed} failed)` : "");

                if (task.current_product_title) {
                    current.textContent = `Currently: ${task.current_product_title}`;
                }

                if (task.status === "completed") {
                    clearInterval(interval);
                    bar.style.width = "100%";
                    current.textContent = "";
                    subtitle.textContent = "All done!";

                    setTimeout(() => {
                        overlay.classList.remove("active");
                        loadProducts();
                        showToast(
                            `Tags applied: ${task.completed} succeeded, ${task.failed} failed`,
                            task.failed > 0 ? "warning" : "success"
                        );
                    }, 1500);
                }
            } catch {
                clearInterval(interval);
                overlay.classList.remove("active");
                showToast("Error polling bulk apply status", "error");
            }
        }, 2000);
    } catch (err) {
        overlay.classList.remove("active");
        showToast(`Bulk apply failed: ${err.message}`, "error");
    }
}
