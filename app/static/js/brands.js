// Brands page — action-first table: KPI strip, search + filter chips, hover
// row actions, floating bulk bar. Store context comes from the global topbar
// StoreSelector (ui.js); selection is identity-keyed so it survives re-renders.

let brandStoreId = null;
let brandModel = []; // joined rows: {vendor, product_count, created, gc, gcId, imgUrl, score}
let renderedBrandRows = []; // visible rows only: index -> {vendor, imgUrl, gcId}
let interlinkSearchResults = [];
let brandSearch = "";
let brandFilter = loadFilter("brandsFilter", "all"); // all | created | missing | lowseo | nologo
let brandSort = loadFilter("brandsSort", { col: "brand", dir: "asc" });
const selectedVendors = new Set();
const selectedForLogo = new Set();
const selectedForSeo = new Set();
let lastCheckedIndex = null;
let brandImgModal = null;
let brandSeoModalHandle = null;
let brandConfirmHandle = null;

document.addEventListener("DOMContentLoaded", () => {
    initBrandToolbarEvents();
    initBrandModals();
    // StoreSelector.init() (ui.js) resolves the store asynchronously — wait a
    // beat, then load; reload on store switches.
    setTimeout(initBrandStore, 350);
    document.addEventListener("store:changed", (e) => {
        brandStoreId = e.detail.storeId;
        clearBrandSelection();
        loadBrands();
    });
});

async function initBrandStore() {
    brandStoreId = StoreSelector.getStoreId();
    if (!brandStoreId) {
        await new Promise((r) => setTimeout(r, 600));
        brandStoreId = StoreSelector.getStoreId();
    }
    if (!brandStoreId) {
        try {
            const stores = await apiRequest("/api/stores");
            if (stores && stores.length) brandStoreId = stores[0].id;
        } catch (e) { /* fall through to the empty state */ }
    }
    if (!brandStoreId) {
        document.getElementById("brands-table-wrap").innerHTML =
            '<div class="empty-state"><span class="material-symbols-rounded">store</span><p>No stores configured. Add a store first.</p></div>';
        return;
    }
    loadBrands();
}

async function loadBrands() {
    const wrap = document.getElementById("brands-table-wrap");
    UISkeleton.show(wrap, { type: "table", rows: 8 });

    try {
        const [vendors, collections] = await Promise.all([
            apiRequest(`/api/brands/vendors?store_id=${brandStoreId}`),
            apiRequest(`/api/brands/collections?store_id=${brandStoreId}`),
        ]);

        const collectionsMap = {};
        for (const c of collections || []) {
            if (c.tag && c.tag.startsWith("__brand__")) {
                collectionsMap[c.tag.substring("__brand__".length).toLowerCase()] = c;
            } else {
                collectionsMap[c.title.toLowerCase()] = c;
            }
        }

        brandModel = (vendors || []).map((v) => {
            const gc = collectionsMap[v.vendor.toLowerCase()] || null;
            return {
                vendor: v.vendor,
                product_count: v.product_count || 0,
                created: !!v.created,
                gc,
                gcId: gc ? gc.id : 0,
                imgUrl: (gc && gc.image_url) || null,
                score: v.created && gc ? gc.seo_score || 0 : null,
            };
        });

        renderBrandKpis();
        renderBrandChips();
        document.getElementById("brands-kpis").hidden = false;
        document.getElementById("brands-toolbar").hidden = false;
        renderBrandTable();

        if (brandModel.some((r) => r.created)) loadLandingPageStatus(brandStoreId);
    } catch (err) {
        wrap.innerHTML = `<div class="empty-state"><p style="color: var(--error);">Error loading vendors: ${escapeHtml(err.message)}</p></div>`;
    }
}

const BRAND_FILTERS = [
    ["all", "All"],
    ["created", "Created"],
    ["missing", "Missing"],
    ["lowseo", "Low SEO"],
    ["nologo", "No Logo"],
];

function brandFilterCounts() {
    const created = brandModel.filter((r) => r.created);
    return {
        all: brandModel.length,
        created: created.length,
        missing: brandModel.length - created.length,
        lowseo: created.filter((r) => (r.score || 0) < 70).length,
        nologo: created.filter((r) => !r.imgUrl).length,
    };
}

function renderBrandKpis() {
    const created = brandModel.filter((r) => r.created);
    const missing = brandModel.length - created.length;
    const noLogo = created.filter((r) => !r.imgUrl).length;
    const lowSeo = created.filter((r) => (r.score || 0) < 70).length;
    const totalProducts = brandModel.reduce((sum, r) => sum + r.product_count, 0);
    const avgScore = created.length
        ? Math.round(created.reduce((sum, r) => sum + (r.score || 0), 0) / created.length)
        : 0;
    const avgScoreColor = scoreColor(avgScore);

    document.getElementById("brands-kpis").innerHTML = `
        <div class="stat-card">
            <div class="stat-value">${brandModel.length}</div>
            <div class="stat-label">Brands</div>
            <div class="stat-sub">${totalProducts.toLocaleString()} products</div>
        </div>
        <div class="stat-card stat-card-clickable" onclick="setBrandFilter('missing')">
            <div class="stat-value">${created.length}</div>
            <div class="stat-label">Collections Created</div>
            <div class="stat-sub">${missing ? `${missing} missing` : "all created"}</div>
        </div>
        <div class="stat-card stat-card-clickable" onclick="setBrandFilter('lowseo')">
            <div class="stat-value" style="color:${created.length ? avgScoreColor : "var(--on-surface-secondary)"};">${created.length ? avgScore : "\u2014"}</div>
            <div class="stat-label">Avg SEO Score</div>
            <div class="stat-sub">${lowSeo} below 70</div>
        </div>
        <div class="stat-card stat-card-clickable" onclick="setBrandFilter('nologo')">
            <div class="stat-value">${noLogo}</div>
            <div class="stat-label">Missing Logos</div>
            <div class="stat-sub">of ${created.length} created</div>
        </div>`;
}

function renderBrandChips() {
    const counts = brandFilterCounts();
    document.getElementById("brand-filter-chips").innerHTML = BRAND_FILTERS.map(
        ([key, label]) =>
            `<button class="filter-chip ${brandFilter === key ? "active" : ""}" data-filter="${key}">${label}<span class="chip-count">${counts[key]}</span></button>`
    ).join("");
}

function setBrandFilter(filter) {
    brandFilter = brandFilter === filter ? "all" : filter;
    saveFilter("brandsFilter", brandFilter);
    renderBrandChips();
    renderBrandTable();
}

function clearBrandFilters() {
    brandFilter = "all";
    saveFilter("brandsFilter", brandFilter);
    brandSearch = "";
    const input = document.getElementById("brand-search");
    if (input) input.value = "";
    document.getElementById("brand-search-clear").hidden = true;
    renderBrandChips();
    renderBrandTable();
}

function getVisibleRows() {
    let rows = brandModel;
    if (brandSearch) {
        const q = brandSearch.toLowerCase();
        rows = rows.filter((r) => r.vendor.toLowerCase().includes(q));
    }
    if (brandFilter === "created") rows = rows.filter((r) => r.created);
    else if (brandFilter === "missing") rows = rows.filter((r) => !r.created);
    else if (brandFilter === "lowseo") rows = rows.filter((r) => r.created && (r.score || 0) < 70);
    else if (brandFilter === "nologo") rows = rows.filter((r) => r.created && !r.imgUrl);

    const dir = brandSort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
        let cmp = 0;
        switch (brandSort.col) {
            case "brand":
                cmp = a.vendor.localeCompare(b.vendor, undefined, { sensitivity: "base" });
                break;
            case "products":
                cmp = a.product_count - b.product_count;
                break;
            case "status":
                cmp = (a.created ? 1 : 0) - (b.created ? 1 : 0);
                break;
            case "seo":
                cmp = (a.score ?? -1) - (b.score ?? -1);
                break;
        }
        return cmp * dir;
    });
}

function brandInitials(vendor) {
    return vendor
        .split(/\s+/)
        .map((w) => w[0])
        .join("")
        .slice(0, 2)
        .toUpperCase();
}

function renderBrandTable() {
    const wrap = document.getElementById("brands-table-wrap");

    if (brandModel.length === 0) {
        renderedBrandRows = [];
        wrap.innerHTML = '<div class="empty-state"><span class="material-symbols-rounded">loyalty</span><p>No vendors found in synced products.</p></div>';
        updateBulkBar();
        return;
    }

    const rows = getVisibleRows();
    renderedBrandRows = rows.map((r) => ({ vendor: r.vendor, imgUrl: r.imgUrl, gcId: r.gcId }));
    lastCheckedIndex = null;

    if (rows.length === 0) {
        wrap.innerHTML = `<div class="empty-state">
            <span class="material-symbols-rounded">search_off</span>
            <p>No brands match the current search or filter.</p>
            <button class="btn btn-secondary btn-sm" onclick="clearBrandFilters()">Clear filters</button>
        </div>`;
        applySelectionToDom(wrap);
        return;
    }

    const arrow = (col) =>
        brandSort.col === col
            ? `<span class="material-symbols-rounded" style="font-size:14px;vertical-align:-3px;">${brandSort.dir === "asc" ? "arrow_upward" : "arrow_downward"}</span>`
            : "";
    const thSort = "cursor:pointer;user-select:none;";

    let html = `<div class="table-container"><table class="data-table">
        <thead><tr>
            <th style="width: 36px;"><input type="checkbox" id="brand-select-all" onchange="toggleAllBrands(this.checked)"></th>
            <th style="width: 56px;"></th>
            <th data-sort="brand" style="${thSort}">Brand ${arrow("brand")}</th>
            <th data-sort="products" style="width: 100px;${thSort}">Products ${arrow("products")}</th>
            <th data-sort="status" style="width: 100px;${thSort}">Status ${arrow("status")}</th>
            <th data-sort="seo" style="width: 130px;${thSort}">SEO ${arrow("seo")}</th>
            <th style="width: 120px;"></th>
        </tr></thead><tbody>`;

    rows.forEach((r, idx) => {
        html += "<tr>";

        if (r.created && r.gcId) {
            html += `<td><input type="checkbox" class="brand-cb logo-checkbox" data-idx="${idx}" value="${r.gcId}"></td>`;
        } else if (!r.created) {
            html += `<td><input type="checkbox" class="brand-cb vendor-checkbox" data-idx="${idx}" value="${escapeAttr(r.vendor)}"></td>`;
        } else {
            html += "<td></td>";
        }

        if (r.imgUrl) {
            html += `<td><img class="brand-avatar" src="${escapeAttr(r.imgUrl)}" alt="${escapeAttr(r.vendor)}" loading="lazy" onclick="previewBrandImage(${idx})"></td>`;
        } else {
            html += `<td><span class="brand-avatar-ph${r.created ? "" : " is-empty"}">${escapeHtml(brandInitials(r.vendor))}</span></td>`;
        }

        html += `<td><strong>${escapeHtml(r.vendor)}</strong>${
            r.created && r.gc && r.gc.handle
                ? `<div style="font-size:12px;color:var(--on-surface-secondary);">/collections/${escapeHtml(r.gc.handle)}</div>`
                : ""
        }</td>`;
        html += `<td>${r.product_count}</td>`;

        if (r.created) {
            html += `<td><span class="badge badge-success">Created</span></td>`;

            const s = r.score || 0;
            const color = scoreColor(s);
            html += `<td><span class="score-inline"><span style="font-weight:600;font-size:13px;color:${color};min-width:20px;">${s}</span><span class="score-bar-mini"><span class="score-bar-mini-fill" style="width:${s}%;background:${color};"></span></span></span></td>`;

            html += `<td><div class="row-actions">`;
            if (r.gcId) {
                html += `<button class="icon-btn" onclick="openBrandSeoModal(${r.gcId})" title="Optimize SEO">
                        <span class="material-symbols-rounded" style="color: var(--primary);">auto_awesome</span>
                    </button>
                    <button class="icon-btn" id="logo-btn-${r.gcId}" onclick="generateBrandLogo(${idx})" title="Generate Logo Image">
                        <span class="material-symbols-rounded">image</span>
                    </button>
                    <button class="icon-btn" onclick="deleteBrandCollection(${idx})" title="Delete">
                        <span class="material-symbols-rounded" style="color: var(--error);">delete</span>
                    </button>`;
            }
            html += `</div></td>`;
        } else {
            html += `<td><span style="color:var(--on-surface-secondary);">\u2014</span></td>`;
            html += `<td><span style="color:var(--on-surface-secondary);">\u2014</span></td>`;
            html += `<td></td>`;
        }

        html += "</tr>";
    });

    html += "</tbody></table></div>";
    wrap.innerHTML = html;
    applySelectionToDom(wrap);
}

async function fixBrandTitles() {
    if (!brandStoreId) return;
    const btn = document.getElementById("fix-titles-btn");
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="material-symbols-rounded spin">sync</span> Fixing...';
    }
    try {
        const resp = await apiRequest("/api/brands/fix-titles", {
            method: "POST",
            body: { store_id: brandStoreId },
        });
        if (resp.fixed > 0) {
            showToast(`Fixed ${resp.fixed} collection title${resp.fixed !== 1 ? "s" : ""} back to brand names${resp.errors > 0 ? ` (${resp.errors} failed)` : ""}`, "success");
            loadBrands();
        } else {
            showToast("All titles are already correct", "success");
        }
    } catch (err) {
        showToast(`Fix titles failed: ${err.message}`, "error");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<span class="material-symbols-rounded">restore</span> Fix Collection Titles';
        }
    }
}

function sortBrandsBy(col) {
    if (brandSort.col === col) {
        brandSort.dir = brandSort.dir === "asc" ? "desc" : "asc";
    } else {
        brandSort = { col, dir: col === "seo" || col === "products" ? "desc" : "asc" };
    }
    saveFilter("brandsSort", brandSort);
    renderBrandTable();
}

/* Selection is identity-keyed (vendor name / gcId). After each render, prune
 * entries for rows the current search/filter hides — the bulk bar count must
 * always equal what the user can see — then re-check the visible boxes. */
function applySelectionToDom(container) {
    const visibleVendors = new Set();
    const visibleGcIds = new Set();
    renderedBrandRows.forEach((r) => {
        visibleVendors.add(r.vendor);
        if (r.gcId) visibleGcIds.add(String(r.gcId));
    });
    for (const v of [...selectedVendors]) if (!visibleVendors.has(v)) selectedVendors.delete(v);
    for (const id of [...selectedForLogo]) {
        if (!visibleGcIds.has(id)) {
            selectedForLogo.delete(id);
            selectedForSeo.delete(id);
        }
    }
    container.querySelectorAll(".vendor-checkbox").forEach((cb) => { cb.checked = selectedVendors.has(cb.value); });
    container.querySelectorAll(".logo-checkbox").forEach((cb) => { cb.checked = selectedForLogo.has(cb.value); });
    syncSelectAllHeader();
    updateBulkBar();
}

function syncSelectAllHeader() {
    const selectAll = document.getElementById("brand-select-all");
    if (!selectAll) return;
    const cbs = document.querySelectorAll("#brands-table-wrap .brand-cb");
    const checked = document.querySelectorAll("#brands-table-wrap .brand-cb:checked");
    selectAll.checked = cbs.length > 0 && checked.length === cbs.length;
    selectAll.indeterminate = checked.length > 0 && checked.length < cbs.length;
}

function syncSelectionState() {
    selectedVendors.clear();
    selectedForLogo.clear();
    selectedForSeo.clear();
    document.querySelectorAll(".vendor-checkbox:checked").forEach(cb => selectedVendors.add(cb.value));
    document.querySelectorAll(".logo-checkbox:checked").forEach(cb => {
        selectedForLogo.add(cb.value);
        selectedForSeo.add(cb.value);
    });
    syncSelectAllHeader();
    updateBulkBar();
}

function toggleAllBrands(checked) {
    selectedVendors.clear();
    selectedForLogo.clear();
    selectedForSeo.clear();
    document.querySelectorAll(".vendor-checkbox").forEach(cb => {
        cb.checked = checked;
        if (checked) selectedVendors.add(cb.value);
    });
    document.querySelectorAll(".logo-checkbox").forEach(cb => {
        cb.checked = checked;
        if (checked) {
            selectedForLogo.add(cb.value);
            selectedForSeo.add(cb.value);
        }
    });
    syncSelectAllHeader();
    updateBulkBar();
}

function updateBulkBar() {
    const bar = document.getElementById("brands-bulk-bar");
    if (!bar) return;
    const total = selectedVendors.size + selectedForLogo.size;
    if (!total) {
        bar.classList.remove("active");
        return;
    }
    document.getElementById("bulk-bar-count").textContent = `${total} selected`;
    let html = "";
    if (selectedVendors.size) {
        html += `<button class="btn btn-primary btn-sm" onclick="createBrandCollections()">
            <span class="material-symbols-rounded">add</span> Create Collections (${selectedVendors.size})</button>`;
    }
    if (selectedForLogo.size) {
        html += `<button class="btn btn-secondary btn-sm" onclick="bulkGenerateLogos()">
            <span class="material-symbols-rounded">image</span> Generate Logos (${selectedForLogo.size})</button>
            <button class="btn btn-secondary btn-sm" onclick="startBulkBrandSeo()">
            <span class="material-symbols-rounded">auto_awesome</span> Optimize SEO (${selectedForSeo.size})</button>`;
    }
    document.getElementById("bulk-bar-actions").innerHTML = html;
    bar.classList.add("active");
}

function clearBrandSelection() {
    selectedVendors.clear();
    selectedForLogo.clear();
    selectedForSeo.clear();
    document.querySelectorAll("#brands-table-wrap .brand-cb").forEach((cb) => { cb.checked = false; });
    const selectAll = document.getElementById("brand-select-all");
    if (selectAll) {
        selectAll.checked = false;
        selectAll.indeterminate = false;
    }
    updateBulkBar();
}

function initBrandToolbarEvents() {
    const wrap = document.getElementById("brands-table-wrap");

    // One delegated listener for sortable headers and checkbox shift-click
    // ranges — bound once so re-renders never stack duplicate handlers.
    wrap.addEventListener("click", (e) => {
        const th = e.target.closest("th[data-sort]");
        if (th) {
            sortBrandsBy(th.dataset.sort);
            return;
        }

        const cb = e.target.closest(".brand-cb");
        if (!cb) return;
        const idx = parseInt(cb.dataset.idx, 10);
        if (e.shiftKey && lastCheckedIndex !== null) {
            const start = Math.min(lastCheckedIndex, idx);
            const end = Math.max(lastCheckedIndex, idx);
            const state = cb.checked;
            wrap.querySelectorAll(".brand-cb").forEach((c) => {
                const i = parseInt(c.dataset.idx, 10);
                if (i >= start && i <= end) c.checked = state;
            });
        }
        lastCheckedIndex = idx;
        syncSelectionState();
    });

    const search = document.getElementById("brand-search");
    const clearBtn = document.getElementById("brand-search-clear");
    let searchDebounce = null;
    search.addEventListener("input", () => {
        clearTimeout(searchDebounce);
        searchDebounce = setTimeout(() => {
            brandSearch = search.value.trim();
            clearBtn.hidden = !brandSearch;
            renderBrandTable();
        }, 150);
    });
    clearBtn.addEventListener("click", () => {
        search.value = "";
        brandSearch = "";
        clearBtn.hidden = true;
        renderBrandTable();
        search.focus();
    });

    document.getElementById("brand-filter-chips").addEventListener("click", (e) => {
        const chip = e.target.closest(".filter-chip");
        if (!chip) return;
        brandFilter = chip.dataset.filter;
        saveFilter("brandsFilter", brandFilter);
        renderBrandChips();
        renderBrandTable();
    });

    const moreBtn = document.getElementById("brands-more-btn");
    const moreMenu = document.getElementById("brands-more-menu");
    moreBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        moreMenu.classList.toggle("open");
    });
    document.addEventListener("click", (e) => {
        if (!e.target.closest(".overflow-wrap")) moreMenu.classList.remove("open");
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") moreMenu.classList.remove("open");
    });
}

function initBrandModals() {
    brandImgModal = UIModal.adopt("brand-image-preview-overlay", {
        onClose: () => { document.getElementById("brand-preview-img").src = ""; },
    });
    brandSeoModalHandle = UIModal.adopt("brand-seo-modal-overlay", {
        onClose: () => {
            if (brandSeoPollInterval) {
                clearInterval(brandSeoPollInterval);
                brandSeoPollInterval = null;
            }
        },
    });
    brandConfirmHandle = UIModal.adopt("brand-confirm-dialog-overlay");
}

async function createBrandCollections() {
    if (selectedVendors.size === 0 || !brandStoreId) return;

    const vendors = Array.from(selectedVendors);
    const overlay = document.getElementById("brand-progress-overlay");
    const bar = document.getElementById("brand-progress-bar");
    const stats = document.getElementById("brand-progress-stats");
    const current = document.getElementById("brand-progress-current");
    const subtitle = document.getElementById("brand-progress-subtitle");

    bar.style.width = "0%";
    stats.textContent = `0 / ${vendors.length} completed`;
    current.textContent = "";
    subtitle.textContent = "Creating smart collections on Shopify...";
    overlay.classList.add("active");

    try {
        const resp = await apiRequest("/api/brands/create", {
            method: "POST",
            body: { store_id: brandStoreId, vendors },
        });

        pollBrandTask(resp.bulk_task_id, vendors.length);
    } catch (err) {
        overlay.classList.remove("active");
        showToast(`Creation failed: ${err.message}`, "error");
    }
}

function pollBrandTask(bulkTaskId, total) {
    const overlay = document.getElementById("brand-progress-overlay");
    const bar = document.getElementById("brand-progress-bar");
    const stats = document.getElementById("brand-progress-stats");
    const current = document.getElementById("brand-progress-current");
    const subtitle = document.getElementById("brand-progress-subtitle");

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
                    loadBrands();
                    showToast(
                        `${task.completed} brand collection${task.completed !== 1 ? "s" : ""} created${task.failed > 0 ? `, ${task.failed} failed` : ""}`,
                        task.failed > 0 ? "warning" : "success"
                    );
                }, 1500);
            }
        } catch {
            clearInterval(interval);
            overlay.classList.remove("active");
            showToast("Error polling creation status", "error");
        }
    }, 2000);
}

async function bulkGenerateLogos() {
    if (selectedForLogo.size === 0 || !brandStoreId) return;

    const gcIds = Array.from(selectedForLogo).map(Number);
    const overlay = document.getElementById("brand-progress-overlay");
    const bar = document.getElementById("brand-progress-bar");
    const stats = document.getElementById("brand-progress-stats");
    const current = document.getElementById("brand-progress-current");
    const subtitle = document.getElementById("brand-progress-subtitle");

    bar.style.width = "0%";
    stats.textContent = `0 / ${gcIds.length} completed`;
    current.textContent = "";
    subtitle.textContent = "Searching for logos and generating images...";
    overlay.classList.add("active");

    try {
        const resp = await apiRequest("/api/brands/collections/bulk-logo", {
            method: "POST",
            body: { store_id: brandStoreId, gc_ids: gcIds },
        });

        pollBrandLogoTask(resp.bulk_task_id, gcIds.length);
    } catch (err) {
        overlay.classList.remove("active");
        showToast(`Bulk logo generation failed: ${err.message}`, "error");
    }
}

function pollBrandLogoTask(bulkTaskId, total) {
    const overlay = document.getElementById("brand-progress-overlay");
    const bar = document.getElementById("brand-progress-bar");
    const stats = document.getElementById("brand-progress-stats");
    const current = document.getElementById("brand-progress-current");
    const subtitle = document.getElementById("brand-progress-subtitle");

    const interval = setInterval(async () => {
        try {
            const task = await apiRequest(`/api/tasks/bulk/${bulkTaskId}`);
            const done = task.completed + task.failed;
            const pct = total > 0 ? Math.round((done / total) * 100) : 0;

            bar.style.width = `${pct}%`;
            stats.textContent = `${done} / ${total} completed` + (task.failed > 0 ? ` (${task.failed} failed)` : "");
            subtitle.textContent = "Searching for logos and generating images...";

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
                    loadBrands();
                    showToast(
                        `${task.completed} logo${task.completed !== 1 ? "s" : ""} generated${task.failed > 0 ? `, ${task.failed} failed` : ""}`,
                        task.failed > 0 ? "warning" : "success"
                    );
                }, 1500);
            }
        } catch {
            clearInterval(interval);
            overlay.classList.remove("active");
            showToast("Error polling logo generation status", "error");
        }
    }, 3000);
}

async function generateLandingPage() {
    if (!brandStoreId) return;
    const btn = document.getElementById("landing-page-btn");
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="material-symbols-rounded spin">sync</span> Creating...';
    }

    try {
        const resp = await apiRequest("/api/brands/landing-page", {
            method: "POST",
            body: { store_id: brandStoreId },
        });
        showToast(`Brands landing page ${resp.brands_count ? "updated" : "created"} with ${resp.brands_count} brands`, "success");
        loadLandingPageStatus(brandStoreId);
    } catch (err) {
        showToast(`Landing page failed: ${err.message}`, "error");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<span class="material-symbols-rounded">web</span> Generate Landing Page';
        }
    }
}

async function loadLandingPageStatus(storeId) {
    const container = document.getElementById("landing-page-status");
    if (!container) return;

    try {
        const status = await apiRequest(`/api/brands/landing-page/status?store_id=${storeId}`);
        if (status.exists) {
            container.innerHTML = `<span class="badge badge-success">Live</span>
                <a href="${escapeAttr(status.url)}" target="_blank" style="font-size: 13px; color: var(--primary);">Landing page</a>`;
        } else {
            container.innerHTML = "";
        }
    } catch {
        container.innerHTML = "";
    }
}

function deleteBrandCollection(rowIndex) {
    const row = renderedBrandRows[rowIndex];
    if (!row || !row.gcId) return;
    const gcId = row.gcId;
    const title = row.vendor;
    const titleEl = document.getElementById("brand-confirm-dialog-title");
    const msgEl = document.getElementById("brand-confirm-dialog-message");
    const okBtn = document.getElementById("brand-confirm-dialog-ok");

    titleEl.textContent = "Delete Brand Collection";
    msgEl.textContent = `Delete the brand collection "${title}" from Shopify?`;
    okBtn.textContent = "Delete";
    okBtn.onclick = async () => {
        closeBrandConfirmDialog();
        try {
            await apiRequest(`/api/brands/collections/${gcId}`, { method: "DELETE" });
            showToast(`Deleted "${title}"`, "success");
            loadBrands();
        } catch (err) {
            showToast(`Delete failed: ${err.message}`, "error");
        }
    };
    brandConfirmHandle.open();
}

async function generateBrandLogo(rowIndex) {
    const row = renderedBrandRows[rowIndex];
    if (!row || !row.gcId) return;
    const gcId = row.gcId;
    const brandName = row.vendor;
    const titleEl = document.getElementById("brand-confirm-dialog-title");
    const msgEl = document.getElementById("brand-confirm-dialog-message");
    const okBtn = document.getElementById("brand-confirm-dialog-ok");

    titleEl.textContent = "Generate Brand Logo";
    msgEl.innerHTML = `<p>Generate a logo image for <strong>${escapeHtml(brandName)}</strong></p>
        <div class="form-group" style="margin-top:12px;">
            <label>Search keywords (optional)</label>
            <textarea id="brand-logo-search-keywords" class="form-control" rows="2"
                placeholder="e.g. vintage logo, original emblem, packaging label..."></textarea>
            <small style="color:var(--on-surface-secondary)">Added to the Brave image search query to help find the right logo image.</small>
        </div>
        <div class="form-group" style="margin-top:12px;">
            <label>Image generation instructions (optional)</label>
            <textarea id="brand-logo-image-prompt" class="form-control" rows="2"
                placeholder="e.g. Use a dark background, make the logo larger, minimalist style..."></textarea>
            <small style="color:var(--on-surface-secondary)">Appended to the AI image generation prompt as additional directions for the final banner.</small>
        </div>`;
    okBtn.textContent = "Generate";
    okBtn.onclick = async () => {
        closeBrandConfirmDialog();
        const searchKeywords = document.getElementById("brand-logo-search-keywords")?.value?.trim() || "";
        const imagePrompt = document.getElementById("brand-logo-image-prompt")?.value?.trim() || "";

        const btn = document.getElementById(`logo-btn-${gcId}`);
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<span class="material-symbols-rounded spin">sync</span>';
        }

        showToast(`Searching for ${brandName} logo and generating image...`, "info");

        try {
            const body = {};
            if (searchKeywords) body.search_keywords = searchKeywords;
            if (imagePrompt) body.custom_prompt = imagePrompt;
            await apiRequest(`/api/brands/collections/${gcId}/generate-logo`, {
                method: "POST",
                body,
            });
            showToast(`Brand logo image created for ${brandName}`, "success");
            loadBrands();
        } catch (err) {
            showToast(`Logo generation failed: ${err.message}`, "error");
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '<span class="material-symbols-rounded">image</span>';
            }
        }
    };
    brandConfirmHandle.open();
}

function previewBrandImage(rowIndex) {
    const row = renderedBrandRows[rowIndex];
    if (!row || !row.imgUrl) return;
    document.getElementById("brand-preview-img").src = row.imgUrl;
    document.getElementById("brand-preview-title").textContent = row.vendor;
    brandImgModal.open();
}

function closeBrandImagePreview() {
    brandImgModal.close();
}

function closeBrandConfirmDialog() {
    brandConfirmHandle.close();
}

// ==================== Brand SEO Optimization ====================

let brandSeoPollInterval = null;

async function openBrandSeoModal(gcId) {
    if (brandSeoPollInterval) {
        clearInterval(brandSeoPollInterval);
        brandSeoPollInterval = null;
    }
    const body = document.getElementById("brand-seo-modal-body");
    const title = document.getElementById("brand-seo-modal-title");

    body.innerHTML = '<div class="loading-spinner"><div class="spinner"></div><p>Loading SEO data...</p></div>';
    brandSeoModalHandle.open();

    try {
        const data = await apiRequest(`/api/brands/collections/${gcId}/seo`);
        title.textContent = data.title || "Brand SEO";

        let html = "";

        // Info grid — same pattern as viewCollection
        const scoreBadgeColor = scoreColor(data.seo_score);
        html += `<div class="preview-info-grid">
            <div class="info-item"><span class="info-label">SEO Score</span><span class="info-value" style="font-weight:700;color:${scoreBadgeColor}">${data.seo_score} / 100</span></div>
            <div class="info-item"><span class="info-label">Products</span><span class="info-value">${data.products_count || 0}</span></div>
            <div class="info-item"><span class="info-label">Handle</span><span class="info-value mono">${escapeHtml(data.handle || "—")}</span></div>
            <div class="info-item"><span class="info-label">Store</span><span class="info-value">${escapeHtml(data.store_name || "—")}</span></div>
        </div>`;

        // SEO Score Breakdown — visual bars
        if (data.seo_breakdown) {
            html += '<div class="preview-section">';
            html += '<div class="preview-section-title"><span class="material-symbols-rounded">analytics</span> SEO Score Breakdown</div>';
            for (const [key, info] of Object.entries(data.seo_breakdown)) {
                const score = info.score !== undefined ? info.score : (typeof info === "number" ? info : 0);
                const fillClass = score >= 80 ? "high" : score >= 50 ? "medium" : "low";
                html += `<div class="score-row">
                    <span class="score-label">${escapeHtml(key.replace(/_/g, " "))}</span>
                    <div class="score-bar"><div class="score-bar-fill ${fillClass}" style="width: ${score}%;"></div></div>
                    <span class="score-value">${Math.round(score)}</span>
                </div>`;
            }
            html += '</div>';
        }

        // Current meta fields
        html += `<div class="form-group">
            <label>Meta Title</label>
            <div class="form-control" style="background: var(--surface-dim);">${escapeHtml(data.meta_title) || "<em>Not set</em>"}</div>
        </div>
        <div class="form-group">
            <label>Meta Description</label>
            <div class="form-control" style="background: var(--surface-dim); min-height: 48px;">${escapeHtml(data.meta_description) || "<em>Not set</em>"}</div>
        </div>`;

        // Keywords Section
        html += '<div class="preview-section">';
        html += '<div class="preview-section-title"><span class="material-symbols-rounded">key</span> Target Keywords</div>';
        html += '<div style="display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px;" id="brand-keyword-checkboxes">';
        const keywords = data.suggested_keywords || [];
        for (let i = 0; i < keywords.length; i++) {
            const kw = keywords[i];
            const kwText = typeof kw === "string" ? kw : kw.keyword || "";
            const checked = i < 5 ? "checked" : "";
            html += `<label class="chip-display" style="cursor:pointer;gap:4px;">
                <input type="checkbox" class="brand-kw-cb" value="${escapeAttr(kwText)}" ${checked}> ${escapeHtml(kwText)}
            </label>`;
        }
        html += '</div>';
        html += `<div style="display:flex;gap:8px;align-items:center;">
            <input type="text" id="brand-custom-keyword" placeholder="Add custom keyword..." class="form-control" style="max-width:260px;">
            <button class="btn btn-secondary btn-sm" onclick="addBrandCustomKeyword()">Add</button>
        </div>`;
        html += '</div>';

        // Interlinking Section
        html += '<div class="preview-section">';
        html += '<div class="preview-section-title"><span class="material-symbols-rounded">link</span> Internal Links (include in description)</div>';
        html += '<div style="display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px;" id="brand-interlink-checkboxes">';
        const relatedColls = data.related_collections || [];
        for (let i = 0; i < relatedColls.length; i++) {
            const c = relatedColls[i];
            const checked = i < 3 ? "checked" : "";
            html += `<label class="chip-display" style="cursor:pointer;gap:4px;">
                <input type="checkbox" class="brand-interlink-cb" value="${c.id}" data-title="${escapeAttr(c.title)}" data-handle="${escapeAttr(c.handle)}" ${checked}> ${escapeHtml(c.title)}
            </label>`;
        }
        if (relatedColls.length === 0) {
            html += '<span style="font-size:13px;color:var(--on-surface-secondary);">No related collections found</span>';
        }
        html += '</div>';
        html += `<div style="position:relative;">
            <input type="text" id="brand-interlink-search" placeholder="Search collections to add..." class="form-control" style="max-width:320px;" autocomplete="off"
                data-store-id="${data.store_id}" data-exclude-id="${data.collection_id}">
            <div id="brand-interlink-dropdown" style="display:none;position:absolute;top:100%;left:0;width:320px;max-height:220px;overflow-y:auto;background:var(--surface);border:1px solid var(--outline);border-radius:8px;box-shadow:0 4px 12px rgba(0,0,0,.15);z-index:100;margin-top:4px;"></div>
        </div>`;
        html += '</div>';

        // Optimize Button
        html += `<div style="margin-top: 20px; display: flex; gap: 8px;">
            <button class="btn btn-primary" id="brand-seo-optimize-btn" onclick="optimizeBrandSeo(${gcId})">
                <span class="material-symbols-rounded">auto_awesome</span>
                Optimize with AI
            </button>
        </div>`;

        // AI Results Container
        html += `<div id="brand-seo-results-container"></div>`;

        body.innerHTML = html;

        initBrandInterlinkSearch();

    } catch (err) {
        body.innerHTML = `<p style="color: var(--error);">Error loading SEO data: ${escapeHtml(err.message)}</p>`;
    }
}

function closeBrandSeoModal() {
    // poll cleanup happens in the modal's onClose (covers Esc/overlay-click too)
    brandSeoModalHandle.close();
}

function addBrandCustomKeyword() {
    const input = document.getElementById("brand-custom-keyword");
    const val = (input.value || "").trim();
    if (!val) return;

    const container = document.getElementById("brand-keyword-checkboxes");
    const label = document.createElement("label");
    label.style.cssText = "display: inline-flex; align-items: center; gap: 4px; font-size: 13px; padding: 4px 10px; border-radius: 16px; background: var(--surface, #fff); border: 1px solid var(--outline, #ddd); cursor: pointer;";
    label.innerHTML = `<input type="checkbox" class="brand-kw-cb" value="${escapeAttr(val)}" checked> ${escapeHtml(val)} <span style="font-size: 11px; color: var(--on-surface-secondary);">(custom)</span>`;
    container.appendChild(label);
    input.value = "";
}

function initBrandInterlinkSearch() {
    const input = document.getElementById("brand-interlink-search");
    const dropdown = document.getElementById("brand-interlink-dropdown");
    if (!input || !dropdown) return;

    let debounceTimer = null;

    input.addEventListener("input", () => {
        clearTimeout(debounceTimer);
        const q = input.value.trim();
        if (q.length < 2) {
            dropdown.style.display = "none";
            return;
        }
        debounceTimer = setTimeout(() => searchBrandInterlinks(q, input, dropdown), 300);
    });

    input.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            dropdown.style.display = "none";
        }
    });

    document.addEventListener("click", (e) => {
        if (!input.contains(e.target) && !dropdown.contains(e.target)) {
            dropdown.style.display = "none";
        }
    });
}

async function searchBrandInterlinks(query, input, dropdown) {
    const storeId = input.dataset.storeId;
    const excludeId = input.dataset.excludeId;
    try {
        const results = await apiRequest(`/api/brands/search-collections?store_id=${storeId}&q=${encodeURIComponent(query)}&exclude_id=${excludeId}`);
        if (!results || results.length === 0) {
            dropdown.innerHTML = '<div style="padding:10px 14px;font-size:13px;color:var(--on-surface-secondary);">No collections found</div>';
            dropdown.style.display = "block";
            return;
        }

        const existing = new Set();
        document.querySelectorAll(".brand-interlink-cb").forEach(cb => existing.add(String(cb.value)));

        interlinkSearchResults = results;
        let html = "";
        results.forEach((c, i) => {
            const alreadyAdded = existing.has(String(c.id));
            html += `<div class="interlink-search-item" style="padding:8px 14px;cursor:${alreadyAdded ? "default" : "pointer"};display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--outline-variant, var(--outline));${alreadyAdded ? "opacity:0.5;" : ""}"
                ${alreadyAdded ? "" : `onclick="addBrandInterlink(${i})"`}
                onmouseenter="if(!${alreadyAdded})this.style.background='var(--surface-variant, var(--surface-dim, #f0f0f0))'"
                onmouseleave="this.style.background='transparent'">
                <div>
                    <div style="font-size:14px;font-weight:500;">${escapeHtml(c.title)}</div>
                    <div style="font-size:12px;color:var(--on-surface-secondary);">/collections/${escapeHtml(c.handle)}${c.products_count ? ` · ${c.products_count} products` : ""}</div>
                </div>
                ${alreadyAdded ? '<span style="font-size:12px;color:var(--on-surface-secondary);">Added</span>' : '<span class="material-symbols-rounded" style="font-size:18px;color:var(--primary);">add_circle</span>'}
            </div>`;
        });
        dropdown.innerHTML = html;
        dropdown.style.display = "block";
    } catch {
        dropdown.style.display = "none";
    }
}

function addBrandInterlink(idx) {
    const c = interlinkSearchResults[idx];
    if (!c) return;
    const id = c.id;
    const title = c.title;
    const handle = c.handle;
    const container = document.getElementById("brand-interlink-checkboxes");
    const existing = container.querySelector(`.brand-interlink-cb[value="${id}"]`);
    if (existing) return;

    const noResultsSpan = container.querySelector("span");
    if (noResultsSpan && noResultsSpan.textContent.includes("No related")) {
        noResultsSpan.remove();
    }

    const label = document.createElement("label");
    label.className = "chip-display";
    label.style.cssText = "cursor:pointer;gap:4px;";
    label.innerHTML = `<input type="checkbox" class="brand-interlink-cb" value="${id}" data-title="${escapeAttr(title)}" data-handle="${escapeAttr(handle)}" checked> ${escapeHtml(title)}`;
    container.appendChild(label);

    const input = document.getElementById("brand-interlink-search");
    const dropdown = document.getElementById("brand-interlink-dropdown");
    if (input) input.value = "";
    if (dropdown) dropdown.style.display = "none";

    showToast(`Added "${title}" to interlinks`, "success");
}

async function optimizeBrandSeo(gcId) {
    const btn = document.getElementById("brand-seo-optimize-btn");
    const resultsContainer = document.getElementById("brand-seo-results-container");

    const keywords = [];
    document.querySelectorAll(".brand-kw-cb:checked").forEach(cb => keywords.push(cb.value));

    const interlinkCollections = [];
    document.querySelectorAll(".brand-interlink-cb:checked").forEach(cb => {
        interlinkCollections.push({
            id: parseInt(cb.value),
            title: cb.dataset.title || "",
            handle: cb.dataset.handle || "",
        });
    });

    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded spin">sync</span> Optimizing...';

    resultsContainer.innerHTML = `
        <div class="ai-panel" style="margin-top: 20px;">
            <h4><span class="material-symbols-rounded">smart_toy</span> AI Analysis</h4>
            <div class="loading-spinner"><div class="spinner"></div><p>AI is analyzing this brand collection...</p></div>
        </div>`;

    try {
        const resp = await apiRequest(`/api/brands/collections/${gcId}/optimize`, {
            method: "POST",
            body: { keywords, interlink_collections: interlinkCollections },
        });

        pollBrandSeoTask(resp.task_id, gcId);
    } catch (err) {
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded">auto_awesome</span> Optimize with AI';
        resultsContainer.innerHTML = `
            <div class="ai-panel" style="margin-top: 20px;">
                <p style="color: var(--error);">Error: ${escapeHtml(err.message)}</p>
            </div>`;
    }
}

function pollBrandSeoTask(taskId, gcId) {
    if (brandSeoPollInterval) clearInterval(brandSeoPollInterval);
    let consecutiveErrors = 0;
    const interval = setInterval(async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            consecutiveErrors = 0;
            if (task.status === "completed") {
                clearInterval(interval);
                if (brandSeoPollInterval === interval) brandSeoPollInterval = null;
                renderBrandSeoResults(task.result, gcId);
            } else if (task.status === "failed") {
                clearInterval(interval);
                if (brandSeoPollInterval === interval) brandSeoPollInterval = null;
                const btn = document.getElementById("brand-seo-optimize-btn");
                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = '<span class="material-symbols-rounded">auto_awesome</span> Optimize with AI';
                }
                const container = document.getElementById("brand-seo-results-container");
                container.innerHTML = `
                    <div class="ai-panel" style="margin-top: 20px;">
                        <h4><span class="material-symbols-rounded">smart_toy</span> AI Analysis</h4>
                        <p style="color: var(--error);">Failed: ${escapeHtml(task.error || "Unknown error")}</p>
                    </div>`;
                showToast(task.error || "Brand SEO optimization failed", "error");
            }
        } catch (err) {
            consecutiveErrors++;
            if (consecutiveErrors >= 5) {
                clearInterval(interval);
                if (brandSeoPollInterval === interval) brandSeoPollInterval = null;
                const btn = document.getElementById("brand-seo-optimize-btn");
                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = '<span class="material-symbols-rounded">auto_awesome</span> Optimize with AI';
                }
                showToast(`Lost contact with the optimization task (${err.message}). It may still be running — reload to check.`, "error");
            }
        }
    }, 2000);
    brandSeoPollInterval = interval;
}

function renderBrandSeoResults(result, gcId) {
    const container = document.getElementById("brand-seo-results-container");
    const btn = document.getElementById("brand-seo-optimize-btn");
    if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded">auto_awesome</span> Optimize with AI';
    }

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

    html += `<div class="form-group">
        <label>Meta Title</label>
        <input type="text" class="ai-field-editable" id="brand-meta-title"
            value="${escapeAttr(result.optimized_meta_title || "")}">
    </div>`;

    html += `<div class="form-group">
        <label>Meta Description</label>
        <textarea class="ai-field-editable" id="brand-meta-description" rows="3">${escapeHtml(result.optimized_meta_description || "")}</textarea>
    </div>`;

    html += `<div class="form-group">
        <label>Brand Description</label>
        <textarea class="ai-field-editable" id="brand-description" rows="10">${escapeHtml(result.optimized_description || "")}</textarea>
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

    html += `<div class="modal-footer">
        <button class="btn btn-secondary" onclick="closeBrandSeoModal()">Cancel</button>
        <button class="btn btn-primary" id="brand-apply-btn" onclick="applyBrandSeo(${gcId})">
            <span class="material-symbols-rounded">publish</span>
            Apply Changes
        </button>
    </div>`;

    html += "</div>";
    container.innerHTML = html;
}

async function applyBrandSeo(gcId) {
    const metaTitle = document.getElementById("brand-meta-title")?.value?.trim() || "";
    const metaDescription = document.getElementById("brand-meta-description")?.value?.trim() || "";
    const description = document.getElementById("brand-description")?.value?.trim() || "";

    const payload = {};
    if (description) payload.description_html = description;
    if (metaTitle) payload.meta_title = metaTitle;
    if (metaDescription) payload.meta_description = metaDescription;

    if (Object.keys(payload).length === 0) {
        showToast("No changes to apply", "warning");
        return;
    }

    const applyBtn = document.getElementById("brand-apply-btn");
    if (applyBtn) {
        applyBtn.disabled = true;
        applyBtn.innerHTML = '<span class="material-symbols-rounded spin">sync</span> Applying...';
    }

    try {
        const resp = await apiRequest(`/api/brands/collections/${gcId}/apply`, {
            method: "POST",
            body: payload,
        });

        if (resp.ok) {
            showToast(`Changes applied! Updated: ${resp.updated_fields.join(", ")}`, "success");
            closeBrandSeoModal();
            loadBrands();
        } else {
            showToast(`Failed to apply: ${resp.error || "Unknown error"}`, "error");
            if (applyBtn) {
                applyBtn.disabled = false;
                applyBtn.innerHTML = '<span class="material-symbols-rounded">publish</span> Apply Changes';
            }
        }
    } catch (err) {
        showToast(`Error applying changes: ${err.message}`, "error");
        if (applyBtn) {
            applyBtn.disabled = false;
            applyBtn.innerHTML = '<span class="material-symbols-rounded">publish</span> Apply Changes';
        }
    }
}


// ==================== Bulk Brand SEO ====================

async function startBulkBrandSeo() {
    if (selectedForSeo.size === 0 || !brandStoreId) return;

    const gcIds = Array.from(selectedForSeo).map(Number);

    const msgEl = document.getElementById("brand-confirm-dialog-message");
    const titleEl = document.getElementById("brand-confirm-dialog-title");
    const okBtn = document.getElementById("brand-confirm-dialog-ok");

    titleEl.textContent = "Bulk Optimize SEO";
    msgEl.innerHTML = `This will run AI SEO optimization on <strong>${gcIds.length}</strong> brand collection${gcIds.length !== 1 ? "s" : ""} and automatically apply the results to Shopify.`;
    okBtn.textContent = "Start Optimization";
    okBtn.onclick = async () => {
        closeBrandConfirmDialog();

        const progressOverlay = document.getElementById("brand-seo-progress-overlay");
        const bar = document.getElementById("brand-seo-progress-bar");
        const stats = document.getElementById("brand-seo-progress-stats");
        const current = document.getElementById("brand-seo-progress-current");
        const subtitle = document.getElementById("brand-seo-progress-subtitle");

        bar.style.width = "0%";
        stats.textContent = `0 / ${gcIds.length} completed`;
        current.textContent = "";
        subtitle.textContent = "Running AI optimization...";
        progressOverlay.classList.add("active");

        try {
            const resp = await apiRequest("/api/brands/bulk-optimize", {
                method: "POST",
                body: { store_id: brandStoreId, gc_ids: gcIds, auto_apply: true },
            });

            pollBulkBrandSeo(resp.bulk_task_id, gcIds.length);
        } catch (err) {
            progressOverlay.classList.remove("active");
            showToast(`Bulk SEO optimization failed: ${err.message}`, "error");
        }
    };
    brandConfirmHandle.open();
}

function pollBulkBrandSeo(bulkTaskId, total) {
    const overlay = document.getElementById("brand-seo-progress-overlay");
    const bar = document.getElementById("brand-seo-progress-bar");
    const stats = document.getElementById("brand-seo-progress-stats");
    const current = document.getElementById("brand-seo-progress-current");
    const subtitle = document.getElementById("brand-seo-progress-subtitle");

    const interval = setInterval(async () => {
        try {
            const task = await apiRequest(`/api/tasks/bulk/${bulkTaskId}`);
            const done = task.completed + task.failed;
            const pct = total > 0 ? Math.round((done / total) * 100) : 0;

            bar.style.width = `${pct}%`;
            stats.textContent = `${done} / ${total} completed` + (task.failed > 0 ? ` (${task.failed} failed)` : "");
            subtitle.textContent = "Running AI optimization...";

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
                    loadBrands();
                    showToast(
                        `${task.completed} brand${task.completed !== 1 ? "s" : ""} optimized${task.failed > 0 ? `, ${task.failed} failed` : ""}`,
                        task.failed > 0 ? "warning" : "success"
                    );
                }, 1500);
            }
        } catch {
            clearInterval(interval);
            overlay.classList.remove("active");
            showToast("Error polling SEO optimization status", "error");
        }
    }, 3000);
}
