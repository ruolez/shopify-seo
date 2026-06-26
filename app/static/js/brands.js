let brandStoreId = null;
let cachedVendors = [];
let cachedCollections = [];
const selectedVendors = new Set();
const selectedForLogo = new Set();
const selectedForSeo = new Set();
let lastCheckedIndex = null;
let brandSortCol = "brand";
let brandSortDir = "asc";

document.addEventListener("DOMContentLoaded", () => {
    loadVendors();
});

async function loadVendors() {
    const container = document.getElementById("brands-container");
    container.innerHTML = '<div class="loading-spinner"><div class="spinner"></div><p>Loading stores...</p></div>';

    try {
        const stores = await apiRequest("/api/stores");
        if (!stores || stores.length === 0) {
            container.innerHTML = '<div class="empty-state"><span class="material-symbols-rounded">store</span><p>No stores configured. Add a store first.</p></div>';
            return;
        }

        let storeSelectHtml = "";
        if (stores.length === 1) {
            brandStoreId = stores[0].id;
            storeSelectHtml = `<input type="hidden" id="brand-store-id" value="${stores[0].id}">
                <p style="margin-bottom: 16px;">Store: <strong>${escapeHtml(stores[0].name)}</strong></p>`;
        } else {
            storeSelectHtml = `<div class="form-group" style="margin-bottom: 16px;">
                <label>Select Store</label>
                <select class="form-control" id="brand-store-id" onchange="onBrandStoreChange(this.value)">
                    <option value="">-- Select a store --</option>
                    ${stores.map(s => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join("")}
                </select>
            </div>`;
        }

        container.innerHTML = storeSelectHtml + '<div id="vendors-table-container"></div>';

        if (brandStoreId) {
            fetchVendors(brandStoreId);
        }
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p style="color: var(--error);">Error: ${escapeHtml(err.message)}</p></div>`;
    }
}

async function onBrandStoreChange(storeId) {
    brandStoreId = storeId || null;
    selectedVendors.clear();
    const tableContainer = document.getElementById("vendors-table-container");
    if (!storeId) {
        tableContainer.innerHTML = "";
        return;
    }
    fetchVendors(storeId);
}

async function fetchVendors(storeId) {
    const tableContainer = document.getElementById("vendors-table-container");
    tableContainer.innerHTML = '<div class="loading-spinner"><div class="spinner"></div><p>Loading vendors...</p></div>';

    try {
        const [vendors, collections] = await Promise.all([
            apiRequest(`/api/brands/vendors?store_id=${storeId}`),
            apiRequest(`/api/brands/collections?store_id=${storeId}`),
        ]);
        cachedVendors = vendors || [];
        cachedCollections = collections || [];
        renderUnifiedTable();
    } catch (err) {
        tableContainer.innerHTML = `<p style="color: var(--error);">Error loading vendors: ${escapeHtml(err.message)}</p>`;
    }
}

function renderUnifiedTable() {
    const container = document.getElementById("vendors-table-container");
    selectedVendors.clear();

    if (cachedVendors.length === 0) {
        container.innerHTML = '<div class="empty-state"><span class="material-symbols-rounded">loyalty</span><p>No vendors found in synced products.</p></div>';
        return;
    }

    const collectionsMap = {};
    for (const c of cachedCollections) {
        if (c.tag && c.tag.startsWith("__brand__")) {
            collectionsMap[c.tag.substring("__brand__".length).toLowerCase()] = c;
        } else {
            collectionsMap[c.title.toLowerCase()] = c;
        }
    }

    const uncreatedCount = cachedVendors.filter(v => !v.created).length;
    const createdCount = cachedVendors.filter(v => v.created).length;
    selectedForLogo.clear();

    let html = `<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 8px;">
        <h3 style="margin: 0;">Brands (${cachedVendors.length})</h3>
        <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
            <span id="landing-page-status"></span>`;

    if (createdCount > 0) {
        html += `<button class="btn btn-secondary btn-sm" id="landing-page-btn" onclick="generateLandingPage()">
                <span class="material-symbols-rounded">web</span>
                Landing Page
            </button>
            <button class="btn btn-secondary btn-sm" id="bulk-seo-btn" onclick="startBulkBrandSeo()" disabled>
                <span class="material-symbols-rounded">auto_awesome</span>
                <span id="bulk-seo-label">Optimize SEO</span>
            </button>
            <button class="btn btn-secondary btn-sm" id="bulk-logo-btn" onclick="bulkGenerateLogos()" disabled>
                <span class="material-symbols-rounded">image</span>
                <span id="bulk-logo-label">Generate Logos</span>
            </button>
            <button class="btn btn-secondary btn-sm" id="fix-titles-btn" onclick="fixBrandTitles()" title="Revert AI-renamed collection titles back to original brand names">
                <span class="material-symbols-rounded">restore</span>
                Fix Titles
            </button>`;
    }

    if (uncreatedCount > 0) {
        html += `<span style="font-size: 13px; color: var(--on-surface-secondary);" id="brand-selected-count">0 selected</span>
            <button class="btn btn-primary btn-sm" id="create-brands-btn" onclick="createBrandCollections()" disabled>
                <span class="material-symbols-rounded">add</span>
                Create Collections
            </button>`;
    }

    html += `</div></div>`;

    const sortArrow = (col) => {
        if (brandSortCol !== col) return "";
        return `<span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;margin-left:2px;">${brandSortDir === "asc" ? "arrow_upward" : "arrow_downward"}</span>`;
    };
    const thStyle = "cursor:pointer;user-select:none;";

    html += `<div class="table-container"><table class="data-table">
        <thead><tr>
            <th style="width: 40px;"><input type="checkbox" id="brand-select-all" onchange="toggleAllBrands(this.checked)"></th>
            <th style="width: 60px;">Image</th>
            <th style="${thStyle}" onclick="sortBrandsBy('brand')">Brand ${sortArrow("brand")}</th>
            <th style="width: 100px;${thStyle}" onclick="sortBrandsBy('products')">Products ${sortArrow("products")}</th>
            <th style="width: 100px;${thStyle}" onclick="sortBrandsBy('status')">Status ${sortArrow("status")}</th>
            <th style="width: 90px;${thStyle}" onclick="sortBrandsBy('seo')">SEO ${sortArrow("seo")}</th>
            <th style="width: 100px;">Actions</th>
        </tr></thead><tbody>`;

    const sortedVendors = [...cachedVendors].sort((a, b) => {
        const gcA = collectionsMap[a.vendor.toLowerCase()];
        const gcB = collectionsMap[b.vendor.toLowerCase()];
        let cmp = 0;
        switch (brandSortCol) {
            case "brand":
                cmp = a.vendor.localeCompare(b.vendor, undefined, { sensitivity: "base" });
                break;
            case "products":
                cmp = (a.product_count || 0) - (b.product_count || 0);
                break;
            case "status":
                cmp = (a.created ? 1 : 0) - (b.created ? 1 : 0);
                break;
            case "seo":
                cmp = ((gcA ? gcA.seo_score : -1) || -1) - ((gcB ? gcB.seo_score : -1) || -1);
                break;
        }
        return brandSortDir === "asc" ? cmp : -cmp;
    });

    let rowIndex = 0;
    for (const v of sortedVendors) {
        const gc = collectionsMap[v.vendor.toLowerCase()];
        const imgUrl = gc && gc.image_url ? gc.image_url : null;
        const gcId = gc ? gc.id : 0;

        html += "<tr>";

        if (v.created && gcId) {
            html += `<td><input type="checkbox" class="brand-cb logo-checkbox" data-idx="${rowIndex}" value="${gcId}"></td>`;
        } else if (!v.created) {
            html += `<td><input type="checkbox" class="brand-cb vendor-checkbox" data-idx="${rowIndex}" value="${escapeHtml(v.vendor)}"></td>`;
        } else {
            html += `<td></td>`;
        }

        if (imgUrl) {
            html += `<td><img src="${escapeHtml(imgUrl)}" alt="${escapeHtml(v.vendor)}" style="width: 48px; height: 48px; object-fit: cover; border-radius: 6px; cursor: pointer; border: 1px solid var(--outline);" onclick="previewBrandImage('${escapeHtml(imgUrl)}', '${escapeHtml(v.vendor)}')"></td>`;
        } else if (v.created) {
            html += `<td><span style="display: inline-flex; width: 48px; height: 48px; align-items: center; justify-content: center; border-radius: 6px; background: var(--surface-variant, #f0f0f0); border: 1px dashed var(--outline);"><span class="material-symbols-rounded" style="font-size: 20px; color: var(--on-surface-secondary);">image</span></span></td>`;
        } else {
            html += `<td></td>`;
        }

        html += `<td><strong>${escapeHtml(v.vendor)}</strong></td>`;
        html += `<td>${v.product_count}</td>`;

        if (v.created) {
            html += `<td><span class="status-badge badge-success">Created</span></td>`;

            const score = gc ? (gc.seo_score || 0) : 0;
            const scoreColor = score >= 70 ? "var(--success, #16a34a)" : score >= 40 ? "var(--warning, #d97706)" : "var(--error, #dc2626)";
            html += `<td><span style="display:inline-flex;align-items:center;gap:4px;font-weight:600;font-size:13px;color:${scoreColor};">${score}<span style="font-weight:400;color:var(--on-surface-secondary);font-size:12px;">/ 100</span></span></td>`;

            html += `<td style="white-space: nowrap;">`;
            if (gcId) {
                html += `<button class="icon-btn" onclick="openBrandSeoModal(${gcId})" title="Optimize SEO">
                        <span class="material-symbols-rounded" style="color: var(--primary);">auto_awesome</span>
                    </button>
                    <button class="icon-btn" id="logo-btn-${gcId}" onclick="generateBrandLogo(${gcId}, '${escapeHtml(v.vendor)}')" title="Generate Logo Image">
                        <span class="material-symbols-rounded">image</span>
                    </button>
                    <button class="icon-btn" onclick="deleteBrandCollection(${gcId}, '${escapeHtml(v.vendor)}')" title="Delete">
                        <span class="material-symbols-rounded" style="color: var(--error);">delete</span>
                    </button>`;
            }
            html += `</td>`;
        } else {
            html += `<td><span class="status-badge badge-neutral">\u2014</span></td>`;
            html += `<td></td>`;
            html += `<td></td>`;
        }

        html += "</tr>";
        rowIndex++;
    }

    html += "</tbody></table></div>";

    if (uncreatedCount === 0) {
        html += '<p style="margin-top: 12px; color: var(--on-surface-secondary); font-size: 13px;">All vendors already have brand collections.</p>';
    }

    container.innerHTML = html;
    lastCheckedIndex = null;

    container.addEventListener("click", function (e) {
        const cb = e.target.closest(".brand-cb");
        if (!cb) return;

        const idx = parseInt(cb.dataset.idx, 10);
        const allCbs = Array.from(container.querySelectorAll(".brand-cb"));

        if (e.shiftKey && lastCheckedIndex !== null) {
            const start = Math.min(lastCheckedIndex, idx);
            const end = Math.max(lastCheckedIndex, idx);
            const state = cb.checked;
            for (let i = start; i <= end; i++) {
                const target = allCbs.find(c => parseInt(c.dataset.idx, 10) === i);
                if (target) target.checked = state;
            }
        }
        lastCheckedIndex = idx;

        syncSelectionState();
    });

    if (createdCount > 0) {
        loadLandingPageStatus(brandStoreId);
    }
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
            fetchVendors(brandStoreId);
        } else {
            showToast("All titles are already correct", "success");
        }
    } catch (err) {
        showToast(`Fix titles failed: ${err.message}`, "error");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<span class="material-symbols-rounded">restore</span> Fix Titles';
        }
    }
}

function sortBrandsBy(col) {
    if (brandSortCol === col) {
        brandSortDir = brandSortDir === "asc" ? "desc" : "asc";
    } else {
        brandSortCol = col;
        brandSortDir = col === "seo" || col === "products" ? "desc" : "asc";
    }
    renderUnifiedTable();
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
    updateSelectedCount();
    updateLogoCount();
    updateSeoCount();
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
    updateSelectedCount();
    updateLogoCount();
    updateSeoCount();
}

function onVendorCheckChange() {
    syncSelectionState();
}

function onLogoCheckChange() {
    syncSelectionState();
}

function updateSelectedCount() {
    const countEl = document.getElementById("brand-selected-count");
    const btn = document.getElementById("create-brands-btn");
    if (countEl) countEl.textContent = `${selectedVendors.size} selected`;
    if (btn) btn.disabled = selectedVendors.size === 0;
}

function updateLogoCount() {
    const btn = document.getElementById("bulk-logo-btn");
    const label = document.getElementById("bulk-logo-label");
    if (btn) btn.disabled = selectedForLogo.size === 0;
    if (label) label.textContent = selectedForLogo.size > 0 ? `Generate Logos (${selectedForLogo.size})` : "Generate Logos";
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
                    fetchVendors(brandStoreId);
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
                    fetchVendors(brandStoreId);
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
        btn.innerHTML = '<span class="material-symbols-rounded">sync</span> Creating...';
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
            btn.innerHTML = '<span class="material-symbols-rounded">web</span> Landing Page';
        }
    }
}

async function loadLandingPageStatus(storeId) {
    const container = document.getElementById("landing-page-status");
    if (!container) return;

    try {
        const status = await apiRequest(`/api/brands/landing-page/status?store_id=${storeId}`);
        if (status.exists) {
            container.innerHTML = `<span class="status-badge badge-success" style="margin-right: 4px;">Live</span>
                <a href="${escapeHtml(status.url)}" target="_blank" style="font-size: 13px; color: var(--primary);">${escapeHtml(status.url)}</a>`;
        } else {
            container.innerHTML = "";
        }
    } catch {
        container.innerHTML = "";
    }
}

function deleteBrandCollection(gcId, title) {
    const overlay = document.getElementById("brand-confirm-dialog-overlay");
    const msgEl = document.getElementById("brand-confirm-dialog-message");
    const okBtn = document.getElementById("brand-confirm-dialog-ok");

    msgEl.textContent = `Delete the brand collection "${title}" from Shopify?`;
    okBtn.onclick = async () => {
        closeBrandConfirmDialog();
        try {
            await apiRequest(`/api/brands/collections/${gcId}`, { method: "DELETE" });
            showToast(`Deleted "${title}"`, "success");
            fetchVendors(brandStoreId);
        } catch (err) {
            showToast(`Delete failed: ${err.message}`, "error");
        }
    };
    overlay.classList.add("active");
}

async function generateBrandLogo(gcId, brandName) {
    const overlay = document.getElementById("brand-confirm-dialog-overlay");
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
        } catch (err) {
            showToast(`Logo generation failed: ${err.message}`, "error");
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '<span class="material-symbols-rounded">auto_awesome</span>';
            }
        }
    };
    overlay.classList.add("active");
}

function previewBrandImage(imageUrl, brandName) {
    const overlay = document.getElementById("brand-image-preview-overlay");
    const img = document.getElementById("brand-preview-img");
    const title = document.getElementById("brand-preview-title");
    img.src = imageUrl;
    title.textContent = brandName;
    overlay.classList.add("active");
}

function closeBrandImagePreview() {
    const overlay = document.getElementById("brand-image-preview-overlay");
    overlay.classList.remove("active");
    document.getElementById("brand-preview-img").src = "";
}

function closeBrandConfirmDialog() {
    document.getElementById("brand-confirm-dialog-overlay").classList.remove("active");
}

function updateSeoCount() {
    const btn = document.getElementById("bulk-seo-btn");
    const label = document.getElementById("bulk-seo-label");
    if (btn) btn.disabled = selectedForSeo.size === 0;
    if (label) label.textContent = selectedForSeo.size > 0 ? `Optimize SEO (${selectedForSeo.size})` : "Optimize SEO";
}

function escapeAttr(str) {
    if (!str) return "";
    return str.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/'/g, "&#39;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}


// ==================== Brand SEO Optimization ====================

async function openBrandSeoModal(gcId) {
    const overlay = document.getElementById("brand-seo-modal-overlay");
    const body = document.getElementById("brand-seo-modal-body");
    const title = document.getElementById("brand-seo-modal-title");

    body.innerHTML = '<div class="loading-spinner"><div class="spinner"></div><p>Loading SEO data...</p></div>';
    overlay.classList.add("active");

    try {
        const data = await apiRequest(`/api/brands/collections/${gcId}/seo`);
        title.textContent = data.title || "Brand SEO";

        let html = "";

        // Info grid — same pattern as viewCollection
        const scoreBadgeColor = data.seo_score >= 70 ? "#16a34a" : data.seo_score >= 40 ? "#d97706" : "#dc2626";
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
    document.getElementById("brand-seo-modal-overlay").classList.remove("active");
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

        let html = "";
        for (const c of results) {
            const alreadyAdded = existing.has(String(c.id));
            html += `<div class="interlink-search-item" style="padding:8px 14px;cursor:${alreadyAdded ? "default" : "pointer"};display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--outline-variant, var(--outline));${alreadyAdded ? "opacity:0.5;" : ""}"
                ${alreadyAdded ? "" : `onclick="addBrandInterlink(${c.id}, '${escapeAttr(c.title)}', '${escapeAttr(c.handle)}')"`}
                onmouseenter="if(!${alreadyAdded})this.style.background='var(--surface-variant, var(--surface-dim, #f0f0f0))'"
                onmouseleave="this.style.background='transparent'">
                <div>
                    <div style="font-size:14px;font-weight:500;">${escapeHtml(c.title)}</div>
                    <div style="font-size:12px;color:var(--on-surface-secondary);">/collections/${escapeHtml(c.handle)}${c.products_count ? ` · ${c.products_count} products` : ""}</div>
                </div>
                ${alreadyAdded ? '<span style="font-size:12px;color:var(--on-surface-secondary);">Added</span>' : '<span class="material-symbols-rounded" style="font-size:18px;color:var(--primary);">add_circle</span>'}
            </div>`;
        }
        dropdown.innerHTML = html;
        dropdown.style.display = "block";
    } catch {
        dropdown.style.display = "none";
    }
}

function addBrandInterlink(id, title, handle) {
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
    const interval = setInterval(async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            if (task.status === "completed") {
                clearInterval(interval);
                renderBrandSeoResults(task.result, gcId);
            } else if (task.status === "failed") {
                clearInterval(interval);
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
            }
        } catch {
            clearInterval(interval);
        }
    }, 2000);
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
            fetchVendors(brandStoreId);
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

    const overlay = document.getElementById("brand-confirm-dialog-overlay");
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
    overlay.classList.add("active");
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
                    fetchVendors(brandStoreId);
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
