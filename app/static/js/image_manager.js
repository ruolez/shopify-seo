let imgSelectedIds = new Set();
let imgProducts = [];
let imgLastClickedIndex = -1;
let imgStoreFilter = loadFilter("imgStoreFilter", "");
let imgStatusFilter = loadFilter("imgStatusFilter", "");
let imgLastStats = null;

document.addEventListener("DOMContentLoaded", () => {
    loadImgStores();
    setupImgModalDismiss("img-preview-modal", closeImgPreview);
});

document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
        const modal = document.getElementById("img-preview-modal");
        if (modal && modal.classList.contains("active")) closeImgPreview();
    }
});

function setupImgModalDismiss(overlayId, closeFn) {
    const overlay = document.getElementById(overlayId);
    if (!overlay) return;
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) closeFn();
    });
}

async function loadImgStores() {
    const container = document.getElementById("img-manager-container");
    try {
        const stores = await apiRequest("/api/stores");
        if (!stores || stores.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <span class="material-symbols-rounded">store</span>
                    <p>No stores configured. Add a store first.</p>
                    <a href="/stores" class="btn btn-primary">Go to Stores</a>
                </div>`;
            return;
        }

        if (!imgStoreFilter && stores.length > 0) {
            imgStoreFilter = String(stores[0].id);
            saveFilter("imgStoreFilter", imgStoreFilter);
        }

        renderImgStoreSelector(container, stores);
        scanMissingImages();
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>${escapeHtml(err.message)}</p></div>`;
    }
}

function renderImgStoreSelector(container, stores) {
    let html = '<div class="filter-bar" style="display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 16px;">';
    html += '<span style="font-weight: 500; margin-right: 4px;">Store:</span>';
    for (const s of stores) {
        const active = imgStoreFilter === String(s.id) ? " active" : "";
        html += `<button class="filter-chip${active}" onclick="selectImgStore('${s.id}')">${escapeHtml(s.name)}</button>`;
    }
    html += '</div>';
    html += '<div id="img-scan-results"><div class="loading-spinner"><div class="spinner"></div><p>Scanning for products missing images...</p></div></div>';
    container.innerHTML = html;
}

function selectImgStore(storeId) {
    imgStoreFilter = String(storeId);
    saveFilter("imgStoreFilter", imgStoreFilter);
    imgSelectedIds.clear();
    imgLastClickedIndex = -1;
    loadImgStores();
}

async function scanMissingImages(liveRefresh) {
    const resultsContainer = document.getElementById("img-scan-results");
    if (!resultsContainer) return;
    resultsContainer.innerHTML = `<div class="loading-spinner"><div class="spinner"></div><p>${liveRefresh ? "Checking Shopify for image changes..." : "Scanning for products missing images..."}</p></div>`;

    try {
        const data = await apiRequest(`/api/images/scan?store_id=${imgStoreFilter}${liveRefresh ? "&refresh=1" : ""}`);
        imgProducts = data.products;
        imgLastStats = data.stats;
        renderImgResults(resultsContainer, data);
        if (liveRefresh) {
            showToast(data.refreshed_from_shopify
                ? `Updated ${data.refreshed_from_shopify} product(s) from Shopify`
                : "Checked Shopify — no image changes found", "success");
        }
    } catch (err) {
        resultsContainer.innerHTML = `<div class="empty-state"><p>${escapeHtml(err.message)}</p></div>`;
    }
}

function renderImgResults(container, data) {
    const stats = data.stats;
    let filtered = imgProducts;

    if (imgStatusFilter === "missing") {
        filtered = filtered.filter(p => !p.has_placeholder && p.image_count === 0);
    } else if (imgStatusFilter === "placeholder") {
        filtered = filtered.filter(p => p.has_placeholder);
    } else if (imgStatusFilter === "needs_seo_fix") {
        filtered = filtered.filter(p => p.has_placeholder && !p.seo_optimized);
    }

    let html = "";

    // Stats bar
    html += `<div class="preview-info-grid" style="margin-bottom: 16px;">
        <div class="info-item">
            <span class="info-label">Total Active</span>
            <span class="info-value"><span class="badge badge-info">${stats.total_active}</span></span>
        </div>
        <div class="info-item">
            <span class="info-label">With Images</span>
            <span class="info-value"><span class="badge badge-success">${stats.with_images}</span></span>
        </div>
        <div class="info-item">
            <span class="info-label">Missing Images</span>
            <span class="info-value"><span class="badge badge-error">${stats.missing_images}</span></span>
        </div>
        <div class="info-item">
            <span class="info-label">Has Placeholder</span>
            <span class="info-value"><span class="badge badge-info">${stats.has_placeholder}</span></span>
        </div>
        <div class="info-item">
            <span class="info-label">Needs SEO Fix</span>
            <span class="info-value"><span class="badge ${stats.needs_seo_fix > 0 ? 'badge-warning' : 'badge-success'}">${stats.needs_seo_fix}</span></span>
        </div>
    </div>`;

    // Filter chips
    html += '<div class="filter-bar" style="display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 12px;">';
    const filters = [
        { key: "missing", label: "No Image" },
        { key: "placeholder", label: "Has Placeholder" },
        { key: "needs_seo_fix", label: "Needs SEO Fix" },
    ];
    for (const f of filters) {
        const active = imgStatusFilter === f.key ? " active" : "";
        html += `<button class="filter-chip${active}" onclick="setImgStatusFilter('${f.key}')">${f.label}</button>`;
    }
    if (imgStatusFilter) {
        html += `<button class="filter-chip" onclick="clearImgStatusFilter()" style="border-style: dashed;">Clear filter</button>`;
    }

    html += `<button class="btn btn-secondary btn-sm" onclick="scanMissingImages(true)" style="margin-left: auto;" title="Re-checks Shopify for image changes on imageless/placeholder products">
        <span class="material-symbols-rounded">refresh</span> Re-scan
    </button>`;
    html += '</div>';

    if (filtered.length === 0) {
        html += `<div class="empty-state">
            <span class="material-symbols-rounded">check_circle</span>
            <p>${imgStatusFilter ? "No products match this filter." : "All active products have images!"}</p>
        </div>`;
        container.innerHTML = html;
        return;
    }

    // Bulk toolbar
    const selCount = imgSelectedIds.size;
    if (selCount > 0) {
        const missingSelected = filtered.filter(p => imgSelectedIds.has(p.id) && !p.has_placeholder).length;
        const needsSeoSelected = filtered.filter(p => imgSelectedIds.has(p.id) && p.has_placeholder && !p.seo_optimized).length;
        html += `<div class="bulk-toolbar">
            <span>${selCount} selected</span>
            <button class="btn btn-primary btn-sm" onclick="bulkUploadPlaceholders()" ${missingSelected === 0 ? 'disabled title="All selected already have placeholders"' : ''}>
                <span class="material-symbols-rounded">cloud_upload</span> Upload Placeholders${missingSelected > 0 ? ` (${missingSelected})` : ''}
            </button>
            <button class="btn btn-secondary btn-sm" onclick="bulkReplacePlaceholders()" ${needsSeoSelected === 0 ? 'disabled title="No selected placeholders need SEO fix"' : ''}>
                <span class="material-symbols-rounded">autorenew</span> Re-upload with SEO Names${needsSeoSelected > 0 ? ` (${needsSeoSelected})` : ''}
            </button>
        </div>`;
    }

    // Table
    html += '<table class="data-table"><thead><tr>';
    html += `<th style="width:40px"><input type="checkbox" onchange="toggleImgSelectAll(this)" ${selCount === filtered.length && filtered.length > 0 ? "checked" : ""}></th>`;
    html += '<th style="width:40px">#</th>';
    html += '<th>Product</th>';
    html += '<th>Status</th>';
    html += '<th>Images</th>';
    html += '<th>Actions</th>';
    html += '</tr></thead><tbody>';

    filtered.forEach((p, idx) => {
        const checked = imgSelectedIds.has(p.id) ? "checked" : "";
        const rowClass = imgSelectedIds.has(p.id) ? ' class="selected"' : "";

        let statusCell;
        if (p.has_placeholder && p.seo_optimized) {
            statusCell = `<span class="badge badge-success">SEO Optimized</span>`;
        } else if (p.has_placeholder) {
            statusCell = `<span class="badge badge-warning">Needs SEO Fix</span>`;
        } else {
            statusCell = `<span class="badge badge-error">No Image</span>`;
        }

        let actions = `<button class="btn btn-secondary btn-sm" onclick="previewPlaceholder(${p.id})" title="Preview">
                        <span class="material-symbols-rounded">visibility</span>
                    </button>`;

        if (!p.has_placeholder && p.image_count === 0) {
            actions += `<button class="btn btn-primary btn-sm" onclick="uploadSinglePlaceholder(${p.id})" title="Upload Placeholder">
                        <span class="material-symbols-rounded">cloud_upload</span>
                    </button>`;
        }

        if (p.has_placeholder && !p.seo_optimized) {
            actions += `<button class="btn btn-secondary btn-sm" onclick="replaceSinglePlaceholder(${p.id})" title="Replace with SEO Name">
                        <span class="material-symbols-rounded">autorenew</span>
                    </button>`;
        }

        html += `<tr${rowClass}>
            <td><input type="checkbox" ${checked} onclick="toggleImgSelect(${p.id}, this, event)" data-index="${idx}"></td>
            <td>${idx + 1}</td>
            <td>
                <div style="font-weight: 500;">${escapeHtml(p.title || "Untitled")}</div>
                <div style="font-size: 11px; color: var(--on-surface-secondary); font-family: var(--font-mono);">/${escapeHtml(p.handle || "")}</div>
                ${p.placeholder_filename ? `<div style="font-size: 10px; color: var(--on-surface-secondary);">${escapeHtml(p.placeholder_filename)}</div>` : ''}
            </td>
            <td>${statusCell}</td>
            <td>${p.image_count || 0}</td>
            <td>
                <div style="display: flex; gap: 4px;">
                    ${actions}
                </div>
            </td>
        </tr>`;
    });

    html += '</tbody></table>';
    container.innerHTML = html;
}

function setImgStatusFilter(key) {
    imgStatusFilter = imgStatusFilter === key ? "" : key;
    saveFilter("imgStatusFilter", imgStatusFilter);
    imgSelectedIds.clear();
    const container = document.getElementById("img-scan-results");
    if (container && imgProducts.length > 0) {
        renderImgResults(container, { products: imgProducts, stats: imgLastStats });
    }
}

function clearImgStatusFilter() {
    imgStatusFilter = "";
    saveFilter("imgStatusFilter", "");
    imgSelectedIds.clear();
    const container = document.getElementById("img-scan-results");
    if (container && imgProducts.length > 0) {
        renderImgResults(container, { products: imgProducts, stats: imgLastStats });
    }
}

function toggleImgSelect(id, checkbox, event) {
    const idx = parseInt(checkbox.dataset.index, 10);
    const isChecked = checkbox.checked;

    let filtered = getFilteredImgProducts();

    if (event && event.shiftKey && imgLastClickedIndex >= 0) {
        const start = Math.min(imgLastClickedIndex, idx);
        const end = Math.max(imgLastClickedIndex, idx);
        for (let i = start; i <= end; i++) {
            const p = filtered[i];
            if (p) {
                if (isChecked) imgSelectedIds.add(p.id);
                else imgSelectedIds.delete(p.id);
            }
        }
    } else {
        if (isChecked) imgSelectedIds.add(id);
        else imgSelectedIds.delete(id);
    }

    imgLastClickedIndex = idx;
    const container = document.getElementById("img-scan-results");
    if (container) renderImgResults(container, { products: imgProducts, stats: imgLastStats });
}

function toggleImgSelectAll(checkbox) {
    const filtered = getFilteredImgProducts();
    if (checkbox.checked) {
        filtered.forEach(p => imgSelectedIds.add(p.id));
    } else {
        imgSelectedIds.clear();
    }
    const container = document.getElementById("img-scan-results");
    if (container) renderImgResults(container, { products: imgProducts, stats: imgLastStats });
}

function getFilteredImgProducts() {
    let filtered = imgProducts;
    if (imgStatusFilter === "missing") {
        filtered = filtered.filter(p => !p.has_placeholder && p.image_count === 0);
    } else if (imgStatusFilter === "placeholder") {
        filtered = filtered.filter(p => p.has_placeholder);
    } else if (imgStatusFilter === "needs_seo_fix") {
        filtered = filtered.filter(p => p.has_placeholder && !p.seo_optimized);
    }
    return filtered;
}

// Preview
async function previewPlaceholder(productId) {
    const product = imgProducts.find((x) => x.id === productId);
    if (!product) return;
    const title = product.title;
    const handle = product.handle || "";
    const modal = document.getElementById("img-preview-modal");
    const content = document.getElementById("img-preview-content");
    content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div><p>Generating preview...</p></div>';
    modal.classList.add("active");

    try {
        const body = { title: title };
        if (handle) body.handle = handle;
        const result = await apiRequest("/api/images/generate-preview", {
            method: "POST",
            body: body,
        });

        content.innerHTML = `
            <div style="text-align: center;">
                <img src="data:image/png;base64,${result.image_base64}"
                     alt="${escapeAttr(result.alt_text)}"
                     style="max-width: 100%; border-radius: 12px; border: 1px solid var(--outline);">
                <div style="margin-top: 12px; font-size: 13px; color: var(--on-surface-secondary);">
                    <strong>Filename:</strong> ${escapeHtml(result.filename)}<br>
                    <strong>Alt text:</strong> ${escapeHtml(result.alt_text)}
                </div>
            </div>`;
    } catch (err) {
        content.innerHTML = `<p>Error: ${escapeHtml(err.message)}</p>`;
    }
}

function closeImgPreview() {
    document.getElementById("img-preview-modal").classList.remove("active");
}

// Single upload
async function uploadSinglePlaceholder(productId) {
    openImgConfirm(
        "Upload Placeholder",
        "Generate and upload a placeholder image for this product?",
        async () => {
            closeImgConfirm();
            try {
                showToast("Uploading placeholder...", "info");
                const result = await apiRequest("/api/images/bulk-upload", {
                    method: "POST",
                    body: { product_ids: [productId] },
                });
                pollImgBulkTask(result.bulk_task_id, 1);
            } catch (err) {
                showToast("Upload failed: " + err.message, "error");
            }
        }
    );
}

// Single replace
async function replaceSinglePlaceholder(productId) {
    openImgConfirm(
        "Replace Placeholder",
        "Delete the old image and re-upload with an SEO-friendly filename?",
        async () => {
            closeImgConfirm();
            try {
                showToast("Replacing placeholder...", "info");
                const result = await apiRequest("/api/images/bulk-replace", {
                    method: "POST",
                    body: { product_ids: [productId] },
                });
                pollImgBulkTask(result.bulk_task_id, 1);
            } catch (err) {
                showToast("Replace failed: " + err.message, "error");
            }
        }
    );
}

// Bulk upload
function bulkUploadPlaceholders() {
    const filtered = getFilteredImgProducts();
    const missingIds = filtered
        .filter(p => imgSelectedIds.has(p.id) && !p.has_placeholder)
        .map(p => p.id);

    if (missingIds.length === 0) {
        showToast("No products need placeholders", "warning");
        return;
    }

    openImgConfirm(
        "Upload Placeholders",
        `Generate and upload placeholder images for ${missingIds.length} products?`,
        async () => {
            closeImgConfirm();
            try {
                const result = await apiRequest("/api/images/bulk-upload", {
                    method: "POST",
                    body: { product_ids: missingIds },
                });
                pollImgBulkTask(result.bulk_task_id, result.total);
            } catch (err) {
                showToast("Failed to start upload: " + err.message, "error");
            }
        }
    );
}

// Bulk replace
function bulkReplacePlaceholders() {
    const filtered = getFilteredImgProducts();
    const needsSeoIds = filtered
        .filter(p => imgSelectedIds.has(p.id) && p.has_placeholder && !p.seo_optimized)
        .map(p => p.id);

    if (needsSeoIds.length === 0) {
        showToast("No selected placeholders need SEO fix", "warning");
        return;
    }

    openImgConfirm(
        "Re-upload with SEO Names",
        `Delete and re-upload ${needsSeoIds.length} placeholder images with SEO-friendly filenames?`,
        async () => {
            closeImgConfirm();
            try {
                const result = await apiRequest("/api/images/bulk-replace", {
                    method: "POST",
                    body: { product_ids: needsSeoIds },
                });
                pollImgBulkTask(result.bulk_task_id, result.total);
            } catch (err) {
                showToast("Failed to start replace: " + err.message, "error");
            }
        }
    );
}

function pollImgBulkTask(bulkTaskId, total) {
    const overlay = document.getElementById("img-progress-overlay");
    const bar = document.getElementById("img-progress-bar");
    const stats = document.getElementById("img-progress-stats");
    const current = document.getElementById("img-progress-current");
    const subtitle = document.getElementById("img-progress-subtitle");

    overlay.classList.add("active");
    subtitle.textContent = "Generating and uploading placeholders...";
    bar.style.width = "0%";
    stats.textContent = `0 / ${total} completed`;
    current.textContent = "";

    const poll = async () => {
        try {
            const task = await apiRequest(`/api/images/bulk-status/${bulkTaskId}`);
            const completed = task.completed + task.failed;
            const pct = total > 0 ? Math.round((completed / total) * 100) : 0;

            bar.style.width = `${pct}%`;
            stats.textContent = `${completed} / ${total} completed${task.failed > 0 ? ` (${task.failed} failed)` : ""}`;

            if (task.current_product_title) {
                current.textContent = task.current_product_title;
            }

            if (task.status === "completed") {
                setTimeout(() => {
                    overlay.classList.remove("active");
                    showToast(
                        `Complete: ${task.completed} succeeded${task.failed > 0 ? `, ${task.failed} failed` : ""}`,
                        task.failed > 0 ? "warning" : "success"
                    );
                    imgSelectedIds.clear();
                    scanMissingImages();
                }, 1000);
            } else {
                setTimeout(poll, 2000);
            }
        } catch (err) {
            overlay.classList.remove("active");
            showToast("Error polling task: " + err.message, "error");
        }
    };

    poll();
}

// Confirm dialog
let imgConfirmCallback = null;

function openImgConfirm(title, message, onOk) {
    document.getElementById("img-confirm-title").textContent = title;
    document.getElementById("img-confirm-message").textContent = message;
    document.getElementById("img-confirm-overlay").classList.add("active");
    imgConfirmCallback = onOk;

    const okBtn = document.getElementById("img-confirm-ok");
    okBtn.onclick = () => {
        if (imgConfirmCallback) imgConfirmCallback();
    };
}

function closeImgConfirm() {
    document.getElementById("img-confirm-overlay").classList.remove("active");
    imgConfirmCallback = null;
}
