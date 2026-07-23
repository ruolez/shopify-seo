let currentSort = loadFilter("productSort", { by: "title", dir: "asc" });
let collectionSort = loadFilter("collectionSort", { by: "title", dir: "asc" });
let collectionFilters = loadFilter("collectionFilters", { type: "", published: "", seo: "" });
let currentStatusFilter = loadFilter("productStatus", "");
let statusCounts = {};
let selectedProductIds = new Set();
let selectedCollectionIds = new Set();
let currentProducts = [];
let currentCollections = [];
// The product array actually rendered in the table (after search/status
// filters — applied inside renderProductsTable). Select-all and shift-range
// MUST operate on this, not currentProducts — otherwise a bulk apply hits
// products the user never saw. (Collections filter before render, so
// currentCollections is already the visible list.)
let visibleProducts = [];
let currentProductId = null;
let currentItemStoreId = null;
let currentItemType = "product";
let lastClickedProductIndex = -1;
let lastClickedCollectionIndex = -1;
let schemaSort = loadFilter("schemaSort", { by: "title", dir: "asc" });
let schemaFilter = loadFilter("schemaFilter", { generated: "", valid: "", pushed: "" });
let currentSchemaProducts = [];
let cachedSchemaStats = null;
let cachedSchemaSettings = null;
let productSearchTerm = "";
let _productSearchTimer = null;

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

function renderPositionBadge(pos) {
    if (pos == null || pos <= 0) return '—';
    const rounded = pos.toFixed(1);
    if (pos <= 3) return `<span class="badge badge-success">${rounded}</span>`;
    if (pos <= 10) return `<span class="badge badge-warning">${rounded}</span>`;
    return `<span class="badge badge-error">${rounded}</span>`;
}

function formatOptimizedDate(dateStr) {
    if (!dateStr) return '<span style="color: var(--on-surface-secondary);">Never</span>';
    const d = new Date(dateStr);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function renderScoreRing(score, size = 44, item = null) {
    const cls = score >= 80 ? "high" : score >= 50 ? "medium" : "low";
    const r = (size - 8) / 2;
    const circ = 2 * Math.PI * r;
    const offset = circ - (score / 100) * circ;
    const isLg = size > 60;
    let tooltip = "";
    if (item && item.content_score != null) {
        const parts = [`Content: ${item.content_score}`];
        if (item.keyword_score != null) parts.push(`Keywords: ${item.keyword_score}`);
        if (item.search_score != null) parts.push(`Search: ${item.search_score}`);
        parts.push(`Unified: ${score}`);
        tooltip = `title="${parts.join(' | ')}"`;
    } else if (item && item.on_page_score != null) {
        const parts = [`On-Page: ${item.on_page_score}`];
        if (item.search_score != null) parts.push(`Search: ${item.search_score}`);
        parts.push(`Composite: ${score}`);
        tooltip = `title="${parts.join(' | ')}"`;
    }
    return `<div class="${isLg ? "score-ring-lg" : "score-ring"}" style="${isLg ? "" : "width:" + size + "px;height:" + size + "px;"}" ${tooltip}>
        <svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
            <circle class="score-ring-bg" cx="${size/2}" cy="${size/2}" r="${r}"/>
            <circle class="score-ring-fill ${cls}" cx="${size/2}" cy="${size/2}" r="${r}"
                stroke-dasharray="${circ}" stroke-dashoffset="${offset}"/>
        </svg>
        <span class="score-ring-value">${score}</span>
    </div>`;
}

function itemInitials(title) {
    return (title || "").split(/\s+/).filter(Boolean).slice(0, 2)
        .map(w => w[0]).join("").toUpperCase() || "?";
}

function renderItemThumb(url, title) {
    if (url) return `<img class="item-thumb" src="${escapeAttr(url)}" alt="" loading="lazy">`;
    return `<span class="item-thumb item-thumb-ph">${escapeHtml(itemInitials(title))}</span>`;
}

function renderTrafficCell(item) {
    const hasGsc = item.gsc_clicks != null || item.gsc_impressions != null;
    const hasSr = item.semrush_volume != null || item.semrush_position != null;
    if (!hasGsc && !hasSr) return '<span class="traffic-none">—</span>';
    const l1 = hasGsc
        ? `${Math.round(item.gsc_clicks || 0).toLocaleString()} clk · ${Math.round(item.gsc_impressions || 0).toLocaleString()} imp`
          + (item.gsc_position != null ? ` · pos ${item.gsc_position.toFixed(1)}` : "")
        : '<span class="traffic-none">—</span>';
    const l2 = [];
    if (item.semrush_volume != null) l2.push(`vol ${item.semrush_volume.toLocaleString()}`);
    if (item.semrush_position != null) l2.push(`SR pos ${item.semrush_position.toFixed(1)}`);
    return `<div class="traffic-cell"><span>${l1}</span>${l2.length ? `<span class="traffic-sub">${l2.join(" · ")}</span>` : ""}</div>`;
}

function onItemRowClick(event, type, id) {
    if (event.target.closest("button, input, a, .row-actions, .checkbox-col")) return;
    const sel = window.getSelection();
    if (sel && sel.type === "Range") return;
    if (type === "product") viewProduct(id);
    else viewCollection(id);
}

function renderPriorityBadge(score) {
    if (score == null || score <= 0) return '';
    if (score >= 80) return `<span class="badge badge-error" title="Priority: ${score}">${score}</span>`;
    if (score >= 60) return `<span class="badge badge-warning" title="Priority: ${score}">${score}</span>`;
    if (score >= 30) return `<span class="badge badge-info" title="Priority: ${score}">${score}</span>`;
    return `<span class="badge" title="Priority: ${score}" style="background:var(--surface-variant);color:var(--on-surface-secondary)">${score}</span>`;
}

function toggleDescription() {
    const el = document.getElementById("desc-preview");
    if (!el) return;
    const btn = document.getElementById("desc-toggle-btn");
    if (el.classList.contains("collapsed")) {
        el.classList.remove("collapsed");
        el.style.maxHeight = "none";
        if (btn) btn.textContent = "Show less";
    } else {
        el.classList.add("collapsed");
        el.style.maxHeight = "";
        if (btn) btn.textContent = "Show more";
    }
}

function renderActionableRecommendations(result) {
    const actionableRecs = result.actionable_recommendations || [];
    const legacyRecs = result.additional_recommendations || [];
    let html = '';

    if (actionableRecs.length > 0) {
        html += `<div class="form-group">
            <label><span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">task_alt</span> Recommendations</label>
            <div id="actionable-recs-list">`;
        const typeIcons = { internal_linking: 'link', image_alt_text: 'image', faq_schema: 'quiz', advisory: 'info' };
        const typeLabels = { internal_linking: 'Internal Linking', image_alt_text: 'Image Alt Text', faq_schema: 'FAQ Schema', advisory: 'Advisory' };
        const typeColors = { internal_linking: 'var(--primary)', image_alt_text: 'var(--success)', faq_schema: '#e65100', advisory: 'var(--on-surface-secondary)' };

        for (let i = 0; i < actionableRecs.length; i++) {
            const rec = actionableRecs[i];
            const recType = rec.type || 'advisory';
            const isActionable = recType !== 'advisory';
            const color = typeColors[recType] || 'var(--on-surface-secondary)';

            html += `<div class="rec-item" id="rec-item-${i}" style="display:flex;align-items:flex-start;gap:10px;padding:10px;margin-bottom:8px;border-radius:8px;border:1px solid var(--outline);background:var(--surface-dim);">
                <span class="material-symbols-rounded" style="font-size:20px;color:${color};margin-top:1px;">${typeIcons[recType] || 'info'}</span>
                <div style="flex:1;min-width:0;">
                    <div style="font-size:13px;line-height:1.5;">${escapeHtml(rec.text || String(rec))}</div>
                    <span style="display:inline-block;font-size:10px;padding:2px 6px;margin-top:4px;border-radius:4px;background:${color}15;color:${color};font-weight:600;">${typeLabels[recType] || recType}</span>
                </div>`;
            if (isActionable) {
                html += `<button class="btn btn-sm btn-primary" style="white-space:nowrap;flex-shrink:0;" onclick='applyRecommendation(${i}, ${JSON.stringify(rec).replace(/'/g, "&#39;")})' id="rec-apply-btn-${i}">
                    <span class="material-symbols-rounded" style="font-size:14px;">preview</span> Preview
                </button>`;
            }
            html += `</div>`;
        }
        html += `</div></div>`;
    } else if (legacyRecs.length > 0) {
        html += `<div class="form-group">
            <label>Additional Recommendations</label>
            <ul style="padding-left: 20px; margin: 8px 0;">`;
        for (const rec of legacyRecs) {
            html += `<li style="margin-bottom: 6px; font-size: 14px;">${escapeHtml(typeof rec === 'string' ? rec : rec.text || JSON.stringify(rec))}</li>`;
        }
        html += `</ul></div>`;
    }
    return html;
}

let _pendingPreviewData = null;

async function applyRecommendation(index, rec) {
    const btn = document.getElementById(`rec-apply-btn-${index}`);
    if (!btn) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:14px;">hourglass_empty</span> Loading...';

    const storeId = currentItemStoreId || _getSeoStoreId();
    const itemId = currentProductId;

    if (!storeId) { showToast('Could not determine store', 'error'); btn.disabled = false; btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:14px;">preview</span> Preview'; return; }

    const body = { type: rec.type, store_id: parseInt(storeId), item_id: itemId };
    if (rec.type === 'internal_linking') {
        body.anchor_text = rec.anchor_text || '';
        body.target_handle = rec.target_handle || '';
        body.target_type = rec.target_type || currentItemType;
    } else if (rec.type === 'image_alt_text') {
        body.scope = rec.scope || (currentItemType === 'collection' ? 'collection_products' : 'single');
        body.item_type = currentItemType;
        body.text = rec.text || '';
    } else if (rec.type === 'faq_schema') {
        body.questions = rec.questions || [];
        body.item_type = currentItemType;
    }

    try {
        const preview = await apiRequest('/api/seo/actions/preview', { method: 'POST', body });
        btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:14px;">play_arrow</span> Preview';
        btn.disabled = false;
        showActionPreviewModal(rec.type, preview, body, index);
    } catch (err) {
        btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:14px;">play_arrow</span> Preview';
        btn.disabled = false;
        showToast(`Preview failed: ${err.message}`, 'error');
    }
}

function showActionPreviewModal(actionType, preview, applyBody, recIndex) {
    const modal = document.getElementById("impact-modal");
    const content = document.getElementById("impact-modal-content");
    document.getElementById("impact-modal-title").textContent =
        actionType === 'internal_linking' ? 'Internal Linking Preview' :
        actionType === 'image_alt_text' ? 'Image Alt Text Preview' :
        actionType === 'faq_schema' ? 'FAQ Schema Preview' : 'Action Preview';

    let html = '';

    if (actionType === 'internal_linking') {
        const aiSelected = preview.preview || [];
        const remaining = preview.remaining || [];
        const alreadyLinked = preview.already_linked || [];
        const totalAvailable = aiSelected.length + remaining.length;

        if (totalAvailable === 0 && alreadyLinked.length > 0) {
            html += `<div class="empty-state" style="padding:20px;"><span class="material-symbols-rounded">check_circle</span><p>All ${alreadyLinked.length} article(s) already link to this page.</p></div>`;
        } else if (totalAvailable === 0) {
            html += '<div class="empty-state" style="padding:20px;"><span class="material-symbols-rounded">info</span><p>No blog articles found. Sync blog articles first.</p></div>';
        } else {
            html += `<p style="margin-bottom:12px;">Add link to <code>${escapeHtml(preview.target_url)}</code> with anchor text "<strong>${escapeHtml(preview.anchor_text)}</strong>". Select which articles to update:</p>`;

            if (alreadyLinked.length > 0) {
                html += `<div style="font-size:12px;color:var(--on-surface-secondary);margin-bottom:10px;padding:6px 10px;background:var(--surface-dim);border-radius:6px;">${alreadyLinked.length} article(s) already linked: ${alreadyLinked.map(t => escapeHtml(t)).join(', ')}</div>`;
            }

            html += '<div style="margin-bottom:16px;max-height:400px;overflow-y:auto;">';

            if (aiSelected.length > 0) {
                html += '<div style="font-size:12px;font-weight:600;color:var(--primary);margin-bottom:6px;">AI Recommended</div>';
                for (const item of aiSelected) {
                    html += `<label style="display:flex;align-items:flex-start;gap:8px;padding:8px;border:1px solid var(--primary);border-radius:6px;margin-bottom:6px;cursor:pointer;background:var(--primary-light,#e8f0fe);">
                        <input type="checkbox" class="action-preview-check" data-id="${item.article_id}" data-shopify-id="${item.article_shopify_id}" data-target-url="${escapeAttr(item.target_url)}" data-anchor="${escapeAttr(item.anchor_text)}" checked>
                        <div><strong>${escapeHtml(item.article_title)}</strong></div>
                    </label>`;
                }
            }

            if (remaining.length > 0) {
                html += `<div style="font-size:12px;font-weight:600;color:var(--on-surface-secondary);margin:${aiSelected.length > 0 ? '12px' : '0'} 0 6px;">Other Articles (select manually)</div>`;
                for (const item of remaining) {
                    html += `<label style="display:flex;align-items:flex-start;gap:8px;padding:8px;border:1px solid var(--outline);border-radius:6px;margin-bottom:6px;cursor:pointer;">
                        <input type="checkbox" class="action-preview-check" data-id="${item.article_id}" data-shopify-id="${item.article_shopify_id}" data-target-url="${escapeAttr(item.target_url)}" data-anchor="${escapeAttr(item.anchor_text)}">
                        <div><strong>${escapeHtml(item.article_title)}</strong>${item.snippet ? `<div style="font-size:11px;color:var(--on-surface-secondary);max-height:36px;overflow:hidden;">${escapeHtml(item.snippet)}</div>` : ''}</div>
                    </label>`;
                }
            }

            html += '</div>';
            html += `<button class="btn btn-primary" onclick="confirmApplyAction('internal_linking', ${recIndex})"><span class="material-symbols-rounded" style="font-size:16px;">check</span> Confirm & Apply Selected</button>`;
        }
    } else if (actionType === 'image_alt_text') {
        const items = preview.preview || [];
        if (items.length === 0) {
            html += '<div class="empty-state" style="padding:20px;"><span class="material-symbols-rounded">info</span><p>No product images found to update.</p></div>';
        } else {
            let totalImgs = items.reduce((s, p) => s + p.suggestions.length, 0);
            html += `<p style="margin-bottom:12px;">Suggested alt text for <strong>${totalImgs}</strong> image(s) across <strong>${items.length}</strong> product(s). Review and edit:</p>`;
            for (const prod of items) {
                html += `<div style="margin-bottom:14px;padding:12px;border:1px solid var(--outline);border-radius:8px;">
                    <strong>${escapeHtml(prod.product_title)}</strong>
                    <div style="margin-top:8px;">`;
                for (let i = 0; i < prod.suggestions.length; i++) {
                    const s = prod.suggestions[i];
                    const hasCurrentAlt = s.current_alt && s.current_alt.trim();
                    const changed = hasCurrentAlt && s.current_alt.trim() !== s.suggested_alt.trim();
                    html += `<div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:8px;padding:6px;border-radius:6px;${changed ? 'background:var(--primary-light,#e8f0fe);' : ''}">
                        ${s.image_url ? `<img src="${escapeAttr(s.image_url)}" style="width:48px;height:48px;object-fit:cover;border-radius:4px;flex-shrink:0;">` : ''}
                        <div style="flex:1;min-width:0;">
                            ${hasCurrentAlt ? `<div style="font-size:11px;color:var(--on-surface-secondary);margin-bottom:3px;">Current: <span style="text-decoration:line-through;">${escapeHtml(s.current_alt)}</span></div>` : '<div style="font-size:11px;color:var(--error);margin-bottom:3px;">No alt text</div>'}
                            <input type="text" class="ai-field-editable alt-preview-input" data-product-id="${prod.product_id}" data-product-shopify-id="${prod.product_shopify_id}" data-media-id="${escapeAttr(s.media_id)}" value="${escapeAttr(s.suggested_alt)}" style="width:100%;font-size:13px;">
                        </div>
                    </div>`;
                }
                html += '</div></div>';
            }
            html += `<button class="btn btn-primary" onclick="confirmApplyAction('image_alt_text', ${recIndex})"><span class="material-symbols-rounded" style="font-size:16px;">check</span> Confirm & Apply All</button>`;
        }
    } else if (actionType === 'faq_schema') {
        const faqs = preview.preview || [];
        if (faqs.length === 0) {
            html += '<div class="empty-state" style="padding:20px;"><span class="material-symbols-rounded">error</span><p>Failed to generate FAQ answers.</p></div>';
        } else {
            html += `<p style="margin-bottom:12px;">Generated <strong>${faqs.length}</strong> FAQ(s) for "${escapeHtml(preview.item_title)}". Edit as needed:</p>`;
            for (let i = 0; i < faqs.length; i++) {
                html += `<div style="margin-bottom:12px;padding:10px;border:1px solid var(--outline);border-radius:8px;">
                    <div style="font-size:12px;color:var(--on-surface-secondary);margin-bottom:4px;">Question ${i+1}</div>
                    <input type="text" class="ai-field-editable faq-q-input" data-index="${i}" value="${escapeAttr(faqs[i].question)}" style="width:100%;margin-bottom:6px;font-weight:600;">
                    <div style="font-size:12px;color:var(--on-surface-secondary);margin-bottom:4px;">Answer</div>
                    <textarea class="ai-field-editable faq-a-input" data-index="${i}" rows="2" style="width:100%;">${escapeHtml(faqs[i].answer)}</textarea>
                </div>`;
            }
            _pendingPreviewData = { item_shopify_id: preview.item_shopify_id, item_type: preview.item_type };
            html += `<button class="btn btn-primary" onclick="confirmApplyAction('faq_schema', ${recIndex})"><span class="material-symbols-rounded" style="font-size:16px;">check</span> Confirm & Push to Shopify</button>`;
        }
    }

    content.innerHTML = html;
    _pendingPreviewData = _pendingPreviewData || {};
    _pendingPreviewData._applyBody = applyBody;
    modal.classList.add("active");
}

async function confirmApplyAction(actionType, recIndex) {
    const storeId = currentItemStoreId || _getSeoStoreId();
    const body = { type: actionType, store_id: storeId };
    let confirmBtn;

    try {
        if (actionType === 'internal_linking') {
            const checks = document.querySelectorAll('.action-preview-check:checked');
            const items = [];
            checks.forEach(cb => {
                items.push({
                    article_id: parseInt(cb.dataset.id),
                    article_shopify_id: cb.dataset.shopifyId,
                    target_url: cb.dataset.targetUrl,
                    anchor_text: cb.dataset.anchor,
                });
            });
            if (!items.length) { showToast('Select at least one article', 'error'); return; }
            body.items = items;
            confirmBtn = document.querySelector('#impact-modal-content .btn-primary');
        } else if (actionType === 'image_alt_text') {
            const inputs = document.querySelectorAll('.alt-preview-input');
            const byProduct = {};
            inputs.forEach(inp => {
                const pid = inp.dataset.productId;
                if (!byProduct[pid]) byProduct[pid] = { product_id: parseInt(pid), product_shopify_id: inp.dataset.productShopifyId, updates: [] };
                byProduct[pid].updates.push({ media_id: inp.dataset.mediaId, alt: inp.value });
            });
            body.items = Object.values(byProduct);
            confirmBtn = document.querySelector('#impact-modal-content .btn-primary');
        } else if (actionType === 'faq_schema') {
            const qs = document.querySelectorAll('.faq-q-input');
            const as_ = document.querySelectorAll('.faq-a-input');
            const faqs = [];
            qs.forEach((q, i) => { faqs.push({ question: q.value, answer: as_[i].value }); });
            body.faqs = faqs;
            body.item_id = (_pendingPreviewData._applyBody || {}).item_id;
            body.item_shopify_id = _pendingPreviewData.item_shopify_id;
            body.item_type = _pendingPreviewData.item_type;
            confirmBtn = document.querySelector('#impact-modal-content .btn-primary');
        }

        if (confirmBtn) {
            confirmBtn.disabled = true;
            confirmBtn.innerHTML = '<span class="material-symbols-rounded" style="font-size:14px;">hourglass_empty</span> Applying...';
        }

        const result = await apiRequest('/api/seo/actions/apply', { method: 'POST', body });
        showToast(result.message || 'Applied successfully', 'success');
        closeImpactModal();

        const recBtn = document.getElementById(`rec-apply-btn-${recIndex}`);
        const recItem = document.getElementById(`rec-item-${recIndex}`);
        if (recBtn) { recBtn.innerHTML = '<span class="material-symbols-rounded" style="font-size:14px;">check</span> Done'; recBtn.disabled = true; recBtn.classList.remove('btn-primary'); recBtn.classList.add('btn-secondary'); }
        if (recItem) recItem.style.borderColor = 'var(--success)';
    } catch (err) {
        showToast(`Apply failed: ${err.message}`, 'error');
        if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.innerHTML = '<span class="material-symbols-rounded" style="font-size:16px;">check</span> Retry'; }
    }
}

function toggleSchemaSettings() {
    const body = document.getElementById("schema-settings-body");
    const trigger = document.getElementById("schema-settings-trigger");
    if (!body || !trigger) return;
    body.classList.toggle("open");
    trigger.classList.toggle("open");
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
    handleDeepLink();
});

function handleDeepLink() {
    const params = new URLSearchParams(window.location.search);

    // /seo?tab=<name> deep link (dashboard insight cards link here)
    const tabParam = params.get("tab");
    if (tabParam) {
        const btn = document.querySelector(`.tab[data-tab="${CSS.escape(tabParam)}"]`);
        if (btn) btn.click();
    }

    const optimizeType = params.get("optimize");
    const itemId = params.get("id");
    const priorityAction = params.get("action");
    if (!optimizeType || !itemId) return;

    window.history.replaceState({}, "", "/seo");

    setTimeout(async () => {
        try {
            if (optimizeType === "product") {
                await viewProduct(parseInt(itemId));
                analyzeProduct(parseInt(itemId), priorityAction);
            } else if (optimizeType === "collection") {
                await viewCollection(parseInt(itemId));
                analyzeCollection(parseInt(itemId), priorityAction);
            }
        } catch (err) {
            showToast(`Failed to open item: ${err.message}`, "error");
        }
    }, 500);
}

function initTabs() {
    const savedTab = loadFilter("seoTab", "priorities");

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
            if (tab.dataset.tab === "priorities") loadPriorities();
            if (tab.dataset.tab === "issues") loadIssues();
            if (tab.dataset.tab === "schema") loadSchemaTab();
        });
    });

    const tabBtn = document.querySelector(`.tab[data-tab="${savedTab}"]`) ||
        document.querySelector('.tab[data-tab="priorities"]');
    tabBtn.click();
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
    productSearchTerm = "";
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

// Re-rendering rebuilds the search input, killing focus mid-typing. Swap the
// HTML while preserving focus + caret if the user was typing in the search box.
function setHtmlPreservingSearchFocus(container, html) {
    const active = document.activeElement;
    const caret = active && active.classList.contains("search-input") && container.contains(active)
        ? active.selectionStart
        : null;
    container.innerHTML = html;
    if (caret === null) return;
    const input = container.querySelector(".search-input");
    if (!input) return;
    input.focus();
    const pos = Math.min(caret, input.value.length);
    input.setSelectionRange(pos, pos);
}

function renderProductsTable(container, products) {
    let html = "";

    const total = statusCounts.total || 0;
    const activeCount = statusCounts.ACTIVE || 0;
    const draftCount = statusCounts.DRAFT || 0;
    const archivedCount = statusCounts.ARCHIVED || 0;

    html += `<div class="toolbar-row">
        <div class="status-filter-bar">
            <button class="filter-chip ${currentStatusFilter === "" ? "active" : ""}" onclick="setStatusFilter('')">All (${total})</button>
            <button class="filter-chip ${currentStatusFilter === "ACTIVE" ? "active" : ""}" onclick="setStatusFilter('ACTIVE')">Active (${activeCount})</button>
            <button class="filter-chip ${currentStatusFilter === "DRAFT" ? "active" : ""}" onclick="setStatusFilter('DRAFT')">Draft (${draftCount})</button>
            <button class="filter-chip ${currentStatusFilter === "ARCHIVED" ? "active" : ""}" onclick="setStatusFilter('ARCHIVED')">Archived (${archivedCount})</button>
        </div>
        <button class="btn btn-secondary btn-sm" onclick="openKeywordAnalysisModal()" style="margin-left: auto; white-space: nowrap;">
            <span class="material-symbols-rounded">label</span>
            Keyword & Tag Analysis
        </button>
    </div>`;

    html += `<div class="search-bar">
        <span class="material-symbols-rounded">search</span>
        <input class="search-input" type="text" placeholder="Search products by title..." value="${escapeAttr(productSearchTerm)}" oninput="onProductSearch(this.value)">
        ${productSearchTerm ? '<button class="search-clear" onclick="onProductSearch(\'\')"><span class="material-symbols-rounded" style="font-size:18px;">close</span></button>' : ''}
    </div>`;

    let filtered = products;
    if (productSearchTerm) {
        const term = productSearchTerm.toLowerCase();
        filtered = products.filter(p => (p.title || "").toLowerCase().includes(term));
    }
    visibleProducts = filtered;

    if (filtered.length === 0 && (currentStatusFilter || productSearchTerm)) {
        html += `<div class="empty-state">
            <span class="material-symbols-rounded">filter_list_off</span>
            <p>No products match the current filter${productSearchTerm ? " or search" : ""}.</p>
            <button class="btn btn-secondary" onclick="clearAllProductFilters()">
                <span class="material-symbols-rounded">clear_all</span>
                Clear Filters
            </button>
        </div>`;
        setHtmlPreservingSearchFocus(container, html);
        return;
    }

    const avgScore = filtered.length > 0 ? Math.round(filtered.reduce((s, p) => s + (p.seo_score || 0), 0) / filtered.length) : 0;
    const goodCount = filtered.filter(p => p.seo_score >= 80).length;
    const fairCount = filtered.filter(p => p.seo_score >= 50 && p.seo_score < 80).length;
    const poorCount = filtered.filter(p => p.seo_score < 50).length;

    html += `<div class="summary-strip">
        <span class="stat-item"><strong>${filtered.length}</strong> products</span>
        <span class="stat-divider"></span>
        <span class="stat-item">Avg score: <strong>${avgScore}</strong></span>
        <span class="stat-divider"></span>
        <span class="stat-item" style="color: var(--success);"><strong>${goodCount}</strong> good</span>
        <span class="stat-item" style="color: var(--warning);"><strong>${fairCount}</strong> fair</span>
        <span class="stat-item" style="color: var(--error);"><strong>${poorCount}</strong> poor</span>
    </div>`;

    if (selectedProductIds.size > 0) {
        html += `<div class="bulk-toolbar">
            <span class="bulk-count">${selectedProductIds.size} product${selectedProductIds.size > 1 ? "s" : ""} selected</span>
            <button class="btn btn-sm btn-primary" onclick="startBulkOptimize()">
                <span class="material-symbols-rounded">bolt</span>
                Bulk Optimize & Apply
            </button>
            <button class="btn btn-sm btn-secondary" onclick="startBulkOptimizeTitles()">
                <span class="material-symbols-rounded">title</span>
                Optimize Titles Only
            </button>
        </div>`;
    }

    const singleStore = new Set(filtered.map(p => p.store_name)).size <= 1;

    html += '<div class="table-container"><table class="items-table"><thead><tr>';
    html += '<th class="checkbox-col"><input type="checkbox" class="table-checkbox" id="select-all-checkbox" onchange="toggleSelectAll(this)"></th>';

    const columns = [
        { key: "title", label: "Product", cls: "item-col" },
        ...(singleStore ? [] : [{ key: "store", label: "Store" }]),
        { key: "seo_score", label: "Score" },
        { key: "issues_count", label: "Issues" },
        { key: "gsc_clicks", label: "Traffic (28d)" },
        { key: "last_optimized_at", label: "Last Optimized" },
    ];

    for (const col of columns) {
        const isActive = currentSort.by === col.key;
        const dirClass = isActive ? currentSort.dir : "";
        html += `<th class="sortable-header ${dirClass} ${col.cls || ""}" onclick="toggleSort('${col.key}')">${col.label}</th>`;
    }

    html += '<th style="width: 88px;"></th>';
    html += "</tr></thead><tbody>";

    for (let idx = 0; idx < filtered.length; idx++) {
        const p = filtered[idx];
        const checked = selectedProductIds.has(p.id) ? "checked" : "";
        const rowClass = selectedProductIds.has(p.id) ? "row-selected" : "";
        html += `<tr class="${rowClass} item-row" onclick="onItemRowClick(event, 'product', ${p.id})">
            <td class="checkbox-col"><input type="checkbox" class="table-checkbox" data-product-id="${p.id}" data-index="${idx}" ${checked} onclick="toggleProductSelect(${p.id}, this, event)"></td>
            <td class="item-col"><div class="item-cell">
                ${renderItemThumb(p.images && p.images[0] && p.images[0].url, p.title)}
                <div class="item-cell-text">
                    <div class="item-title"><span>${escapeHtml(p.title)}</span>${p.status !== "ACTIVE" ? `<span class="badge badge-warning">${escapeHtml(p.status)}</span>` : ""}</div>
                    <div class="item-sub">/${escapeHtml(p.handle || "")}</div>
                </div>
            </div></td>
            ${singleStore ? "" : `<td>${escapeHtml(p.store_name)}</td>`}
            <td>${renderScoreRing(p.seo_score, 36, p)}</td>
            <td>${renderIssuesBadge(p)}</td>
            <td>${renderTrafficCell(p)}</td>
            <td>${formatOptimizedDate(p.last_optimized_at)}</td>
            <td><div class="row-actions">
                <button class="action-icon" title="View product details" onclick="viewProduct(${p.id})"><span class="material-symbols-rounded">visibility</span></button>
                <button class="action-icon action-icon-primary" title="AI Analyze" onclick="analyzeProduct(${p.id})"><span class="material-symbols-rounded">smart_toy</span></button>
            </div></td>
        </tr>`;
    }
    html += "</tbody></table></div>";
    setHtmlPreservingSearchFocus(container, html);

    const selectAllCb = document.getElementById("select-all-checkbox");
    if (selectAllCb) {
        selectAllCb.checked =
            filtered.length > 0 && filtered.every((p) => selectedProductIds.has(p.id));
    }
}

function onProductSearch(value) {
    clearTimeout(_productSearchTimer);
    _productSearchTimer = setTimeout(() => {
        productSearchTerm = value;
        renderProductsTable(document.getElementById("products-container"), currentProducts);
    }, 250);
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
            const p = visibleProducts[i];
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
        for (const p of visibleProducts) {
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
        + `<div style="margin-top: 12px; display: flex; flex-direction: column; gap: 8px;">`
        + `<label style="display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: 14px;">`
        + `<input type="checkbox" id="bulk-skip-title-checkbox" checked> Keep original product titles (don't rename)</label>`
        + `<label style="display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: 14px;">`
        + `<input type="checkbox" id="bulk-include-tags-checkbox"> Include tag re-analysis (default: tags managed separately)</label></div>`;

    const okBtn = document.getElementById("confirm-dialog-ok");
    okBtn.onclick = () => {
        const includeTags = document.getElementById("bulk-include-tags-checkbox")?.checked || false;
        const skipTitle = document.getElementById("bulk-skip-title-checkbox")?.checked || false;
        closeConfirmDialog();
        executeBulkOptimize(!includeTags, skipTitle);
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

async function executeBulkOptimize(skipTags = true, skipTitle = false) {
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
            body: { product_ids: ids, auto_apply: true, skip_tags: skipTags, skip_title: skipTitle },
        });

        const bulkTaskId = resp.bulk_task_id;
        pollBulkTask(bulkTaskId, ids.length);
    } catch (err) {
        overlay.classList.remove("active");
        showToast(`Bulk optimization failed: ${err.message}`, "error");
    }
}

async function pollBulkTask(bulkTaskId, total, subtitleText = "Optimizing products...") {
    const overlay = document.getElementById("bulk-progress-overlay");
    const bar = document.getElementById("bulk-progress-bar");
    const stats = document.getElementById("bulk-progress-stats");
    const current = document.getElementById("bulk-progress-current");
    const subtitle = document.getElementById("bulk-progress-subtitle");

    try {
        const task = await pollTaskUntilDone(`/api/tasks/bulk/${bulkTaskId}`, {
            timeoutMs: Infinity,
            onProgress: (t) => {
                const done = t.completed + t.failed;
                const pct = total > 0 ? Math.round((done / total) * 100) : 0;

                bar.style.width = `${pct}%`;
                stats.textContent = `${done} / ${total} completed` + (t.failed > 0 ? ` (${t.failed} failed)` : "");
                subtitle.textContent = subtitleText;

                if (t.current_product_title) {
                    current.textContent = `Currently: ${t.current_product_title}`;
                }
            },
        });

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
    } catch (err) {
        overlay.classList.remove("active");
        selectedProductIds.clear();
        loadProducts();
        showToast(err.message, "error");
    }
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

let priorityTypeFilter = "product";
let priorityViewMode = "needs_work"; // "needs_work" or "optimized"
let optimizedSort = { by: "days_since", dir: "asc" };
let _cachedOptimizedItems = [];

let _cachedStoreId = null;

function _getSeoStoreId() {
    if (_cachedStoreId) return _cachedStoreId;
    if (currentProducts.length > 0 && currentProducts[0].store_id) return currentProducts[0].store_id;
    if (currentCollections.length > 0 && currentCollections[0].store_id) return currentCollections[0].store_id;
    const storeSelect = document.getElementById("store-select");
    if (storeSelect) return storeSelect.value;
    return "";
}

async function _ensureStoreId() {
    let sid = _getSeoStoreId();
    if (sid) return sid;
    try {
        const stores = await apiRequest("/api/stores");
        if (stores.length > 0) {
            _cachedStoreId = stores[0].id;
            return _cachedStoreId;
        }
    } catch {}
    return "";
}

function _priorityToolbar() {
    return `<div class="toolbar-row" style="gap:12px;flex-wrap:wrap;">
        <div class="status-filter-bar">
            <button class="filter-chip ${priorityViewMode === "needs_work" ? "active" : ""}" onclick="setPriorityView('needs_work')">Needs Work</button>
            <button class="filter-chip ${priorityViewMode === "optimized" ? "active" : ""}" onclick="setPriorityView('optimized')">Recently Optimized</button>
        </div>
        <div class="status-filter-bar" style="margin-left:auto;">
            <button class="filter-chip ${priorityTypeFilter === "product" ? "active" : ""}" onclick="setPriorityType('product')">Products</button>
            <button class="filter-chip ${priorityTypeFilter === "collection" ? "active" : ""}" onclick="setPriorityType('collection')">Collections</button>
        </div>
    </div>`;
}

async function loadPriorities() {
    const container = document.getElementById("priorities-container");

    const storeId = await _ensureStoreId();
    if (!storeId) {
        container.innerHTML = '<div class="empty-state"><span class="material-symbols-rounded">store</span><p>No stores found. Add a store first.</p></div>';
        return;
    }

    const typeLabel = priorityTypeFilter === "product" ? "products" : "collections";
    container.innerHTML = _priorityToolbar() + `
        <div class="loading-spinner" style="padding: 60px 0;">
            <div class="spinner"></div>
            <p>${priorityViewMode === "optimized" ? "Loading recently optimized items..." : `Analyzing ${typeLabel} — scoring SEO, search performance & opportunities...`}</p>
            <p style="font-size: 12px; color: var(--on-surface-secondary); margin-top: 4px;">This may take a moment for large catalogs</p>
        </div>`;

    try {
        if (priorityViewMode === "optimized") {
            const items = await apiRequest(`/api/seo/optimized?store_id=${storeId}&limit=30`);
            renderOptimizedTable(container, items);
        } else {
            const items = await apiRequest(`/api/seo/priorities?store_id=${storeId}&type=${priorityTypeFilter}&limit=50`);
            renderPrioritiesTable(container, items);
        }
    } catch (err) {
        container.innerHTML = _priorityToolbar() +
            `<div class="empty-state"><span class="material-symbols-rounded">error</span><p>${escapeHtml(err.message)}</p></div>`;
    }
}

function setPriorityView(mode) {
    priorityViewMode = mode;
    loadPriorities();
}

function setPriorityType(type) {
    priorityTypeFilter = type;
    loadPriorities();
}

function toggleOptimizedSort(key) {
    if (optimizedSort.by === key) {
        optimizedSort.dir = optimizedSort.dir === "asc" ? "desc" : "asc";
    } else {
        optimizedSort.by = key;
        optimizedSort.dir = key === "title" ? "asc" : "desc";
    }
    const sorted = _sortOptimizedItems([..._cachedOptimizedItems]);
    renderOptimizedTable(document.getElementById("priorities-container"), sorted, true);
}

function _sortOptimizedItems(items) {
    const key = optimizedSort.by;
    const dir = optimizedSort.dir === "asc" ? 1 : -1;
    return items.sort((a, b) => {
        let va = a[key], vb = b[key];
        if (va == null) va = key === "title" ? "" : -Infinity;
        if (vb == null) vb = key === "title" ? "" : -Infinity;
        if (typeof va === "string") return dir * va.localeCompare(vb);
        return dir * (va - vb);
    });
}

function renderOptimizedTable(container, items, skipCache) {
    if (!skipCache) _cachedOptimizedItems = items;
    let html = _priorityToolbar();

    if (items.length === 0) {
        html += '<div class="empty-state" style="padding:40px 0;"><span class="material-symbols-rounded">check_circle</span><p>No optimizations recorded yet. Optimize items from the "Needs Work" tab to track their progress here.</p></div>';
        container.innerHTML = html;
        return;
    }

    const mature = items.filter(i => i.days_since >= 28);
    const pending = items.filter(i => i.days_since < 28);
    const onPageImproved = items.filter(i => i.score_change > 0).length;
    const matureImproved = mature.filter(i => i.score_change > 5).length;
    const matureDeclined = mature.filter(i => i.score_change < -5).length;

    html += `<div class="summary-strip">
        <span class="stat-item"><strong>${items.length}</strong> optimized items</span>
        <span class="stat-divider"></span>
        <span class="stat-item" style="color:var(--success);"><strong>${onPageImproved}</strong> on-page improved</span>
        ${mature.length > 0 ? `<span class="stat-divider"></span>
        <span class="stat-item"><strong>${mature.length}</strong> mature (28d+)</span>
        <span class="stat-item" style="color:var(--success);"><strong>${matureImproved}</strong> search improved</span>
        ${matureDeclined > 0 ? `<span class="stat-item" style="color:var(--error);"><strong>${matureDeclined}</strong> needs review</span>` : ''}` : ''}
        ${pending.length > 0 ? `<span class="stat-divider"></span><span class="stat-item" style="color:var(--on-surface-secondary);"><strong>${pending.length}</strong> awaiting Google</span>` : ''}
    </div>`;

    html += `<div style="padding:10px 14px;margin-bottom:12px;background:var(--surface-dim);border-radius:8px;border:1px solid var(--outline);font-size:12px;line-height:1.6;color:var(--on-surface-secondary);">
        <strong style="color:var(--on-surface);">How to read this table:</strong>
        <strong>On-Page Δ</strong> reflects immediate content quality changes (meta title, description, alt text).
        <strong>Search traffic impact</strong> (clicks, impressions, position) takes <strong>2-4 weeks</strong> for Google to reflect.
        Click the <strong>trend icon</strong> on any row to see the full before/after GSC comparison once enough data is available.
    </div>`;

    const oCols = [
        { key: "title", label: "Title" },
        { key: "item_type", label: "Type" },
        { key: "days_since", label: "Optimized" },
        { key: "score_before", label: "Before", title: "Composite score at time of optimization" },
        { key: "score_now", label: "Now", title: "Composite score now" },
        { key: "score_change", label: "On-Page Δ", title: "On-page score change (instant)" },
        { key: null, label: "Status" },
        { key: "gsc_clicks", label: "Clicks", title: "Current 28-day GSC" },
        { key: "gsc_impressions", label: "Impr." },
        { key: "gsc_position", label: "Position" },
    ];
    html += '<div class="table-container"><table><thead><tr>';
    html += '<th class="row-num-col">#</th>';
    for (const col of oCols) {
        if (col.key) {
            const isActive = optimizedSort.by === col.key;
            const dirClass = isActive ? optimizedSort.dir : "";
            html += `<th class="sortable-header ${dirClass}" onclick="toggleOptimizedSort('${col.key}')"${col.title ? ` title="${col.title}"` : ''}>${col.label}</th>`;
        } else {
            html += `<th>${col.label}</th>`;
        }
    }
    html += '<th>Actions</th>';
    html += '</tr></thead><tbody>';

    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        const changeColor = item.score_change > 0 ? 'var(--success)' : item.score_change < 0 ? 'var(--error)' : 'var(--on-surface-secondary)';
        const changeIcon = item.score_change > 0 ? 'trending_up' : item.score_change < 0 ? 'trending_down' : 'trending_flat';
        const changeStr = item.score_change > 0 ? `+${item.score_change}` : String(item.score_change);
        const daysAgo = item.days_since === 0 ? 'Today' : item.days_since === 1 ? 'Yesterday' : `${item.days_since}d ago`;
        const typeBadge = item.item_type === 'product' ? '<span class="badge badge-info" style="font-size:10px;">Product</span>' : '<span class="badge badge-warning" style="font-size:10px;">Collection</span>';

        let statusLabel, statusColor;
        if (item.days_since < 14) {
            statusLabel = 'Awaiting Google';
            statusColor = 'var(--on-surface-secondary)';
        } else if (item.days_since < 28) {
            statusLabel = 'Measuring...';
            statusColor = 'var(--warning)';
        } else {
            if (item.score_change > 5) { statusLabel = 'Improved'; statusColor = 'var(--success)'; }
            else if (item.score_change < -5) { statusLabel = 'Check needed'; statusColor = 'var(--error)'; }
            else { statusLabel = 'Stable'; statusColor = 'var(--on-surface-secondary)'; }
        }

        html += `<tr>
            <td class="row-num-col">${i + 1}</td>
            <td><strong>${escapeHtml(item.title)}</strong></td>
            <td>${typeBadge}</td>
            <td style="white-space:nowrap;font-size:12px;">${daysAgo}</td>
            <td>${renderScoreRing(item.score_before, 32)}</td>
            <td>${renderScoreRing(item.score_now, 32)}</td>
            <td style="color:${changeColor};font-weight:700;font-size:13px;">
                <span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">${changeIcon}</span>
                ${changeStr}
            </td>
            <td><span style="font-size:11px;color:${statusColor};font-weight:600;">${statusLabel}</span></td>
            <td>${item.gsc_clicks != null ? Math.round(item.gsc_clicks).toLocaleString() : '—'}</td>
            <td>${item.gsc_impressions != null ? Math.round(item.gsc_impressions).toLocaleString() : '—'}</td>
            <td>${item.gsc_position != null ? renderPositionBadge(item.gsc_position) : '—'}</td>
            <td>
                <div class="actions-cell">
                    <button class="action-icon" title="View details" onclick="${item.item_type === 'product' ? 'viewProduct' : 'viewCollection'}(${item.id})"><span class="material-symbols-rounded">visibility</span></button>
                    <button class="action-icon" title="View before/after impact" onclick="viewImpact(${item.id}, '${item.item_type}')"><span class="material-symbols-rounded">trending_up</span></button>
                    <button class="action-icon action-icon-primary" title="Re-optimize" onclick="${item.item_type === 'product' ? 'analyzeProduct' : 'analyzeCollection'}(${item.id})"><span class="material-symbols-rounded">smart_toy</span></button>
                </div>
            </td>
        </tr>`;
    }
    html += '</tbody></table></div>';
    container.innerHTML = html;
}

// ── Work session: walk the priority queue in the detail modal ──────────
let _prioritySession = [];
let _sessionIndex = -1;

function startWorkSession(index) {
    _sessionIndex = index;
    openSessionItem(index);
}

async function openSessionItem(index) {
    if (!_prioritySession.length) return;
    _sessionIndex = Math.max(0, Math.min(index, _prioritySession.length - 1));
    const it = _prioritySession[_sessionIndex];
    if (it.type === "product") analyzeProduct(it.id, it.action);
    else analyzeCollection(it.id, it.action);
    renderSessionNav();
}

function renderSessionNav() {
    const header = document.querySelector("#product-modal .modal-header");
    if (!header) return;
    let nav = document.getElementById("session-nav");
    if (_sessionIndex < 0 || !_prioritySession.length) {
        if (nav) nav.remove();
        return;
    }
    if (!nav) {
        nav = document.createElement("div");
        nav.id = "session-nav";
        nav.style.cssText = "display:flex;align-items:center;gap:8px;margin-left:auto;margin-right:10px;font-size:12px;color:var(--on-surface-secondary);";
        header.insertBefore(nav, header.querySelector(".icon-btn"));
    }
    nav.innerHTML = `
        <button class="btn btn-secondary btn-sm" title="Previous (K)" ${_sessionIndex <= 0 ? "disabled" : ""} onclick="openSessionItem(${_sessionIndex - 1})"><span class="material-symbols-rounded">chevron_left</span></button>
        <span style="white-space:nowrap;">${_sessionIndex + 1} of ${_prioritySession.length}</span>
        <button class="btn btn-secondary btn-sm" title="Next (J)" ${_sessionIndex >= _prioritySession.length - 1 ? "disabled" : ""} onclick="openSessionItem(${_sessionIndex + 1})"><span class="material-symbols-rounded">chevron_right</span></button>`;
}

document.addEventListener("keydown", (e) => {
    const modal = document.getElementById("product-modal");
    if (!modal || !modal.classList.contains("active") || _sessionIndex < 0 || !_prioritySession.length) return;
    if (e.target && e.target.matches && e.target.matches("input, textarea, select, [contenteditable]")) return;
    if (e.key === "j" || e.key === "ArrowRight") openSessionItem(_sessionIndex + 1);
    else if (e.key === "k" || e.key === "ArrowLeft") openSessionItem(_sessionIndex - 1);
});

function renderPrioritiesTable(container, items) {
    let html = _priorityToolbar();

    // The ranked list doubles as the work-session queue.
    _prioritySession = items.map((i) => ({ id: i.id, type: i.item_type, action: i.recommended_action || null }));
    _sessionIndex = -1;
    renderSessionNav();

    if (items.length === 0) {
        html += '<div class="empty-state"><span class="material-symbols-rounded">check_circle</span><p>No priority items found. Connect GSC or import Semrush data to see SEO priorities.</p></div>';
        container.innerHTML = html;
        return;
    }

    const cannibalCount = items.filter(i => i.cannibalization_warning).length;
    html += `<div class="summary-strip">
        <span class="stat-item"><strong>${items.length}</strong> items with opportunities</span>
        <span class="stat-divider"></span>
        <span class="stat-item" style="color:var(--error);"><strong>${items.filter(i => i.priority_score >= 80).length}</strong> high priority</span>
        <span class="stat-item" style="color:var(--warning);"><strong>${items.filter(i => i.priority_score >= 60 && i.priority_score < 80).length}</strong> medium</span>
        <span class="stat-item" style="color:var(--on-surface-secondary);"><strong>${items.filter(i => i.priority_score < 60).length}</strong> low</span>
        ${cannibalCount > 0 ? `<span class="stat-divider"></span><span class="stat-item" style="color:var(--error);"><span class="material-symbols-rounded" style="font-size:14px;vertical-align:middle;">warning</span> <strong>${cannibalCount}</strong> cannibalization risk${cannibalCount > 1 ? 's' : ''}</span>` : ''}
        <span style="flex:1;"></span>
        <button class="btn btn-primary btn-sm" onclick="startWorkSession(0)" title="Open the top item and walk the queue with Prev/Next (J/K)">
            <span class="material-symbols-rounded">play_arrow</span> Start work session</button>
    </div>`;

    html += '<div class="table-container"><table><thead><tr>';
    html += '<th class="row-num-col">#</th>';
    html += '<th>Title</th>';
    html += '<th>Priority</th>';
    html += '<th>SEO Score</th>';
    html += '<th>Impressions</th>';
    html += '<th>CTR</th>';
    html += '<th>Position</th>';
    html += '<th>Top Opportunity</th>';
    html += '<th>Potential Clicks</th>';
    html += '<th>Actions</th>';
    html += '</tr></thead><tbody>';

    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        const optimizedLabel = item.last_optimized_at ? `<div style="font-size:10px;color:var(--on-surface-secondary);margin-top:2px;">Optimized ${new Date(item.last_optimized_at).toLocaleDateString('en-US', {month:'short',day:'numeric'})}</div>` : '';
        const recommendedAction = item.recommended_action ? escapeAttr(item.recommended_action) : 'Optimize';
        html += `<tr>
            <td class="row-num-col">${i + 1}</td>
            <td><strong>${escapeHtml(item.title)}</strong>${optimizedLabel}</td>
            <td>${renderPriorityBadge(item.priority_score)}</td>
            <td>${renderScoreRing(item.seo_score, 36, item)}</td>
            <td>${item.gsc_impressions != null ? Math.round(item.gsc_impressions).toLocaleString() : '—'}</td>
            <td>${item.gsc_ctr != null ? (item.gsc_ctr * 100).toFixed(1) + '%' : '—'}</td>
            <td>${item.gsc_position != null ? renderPositionBadge(item.gsc_position) : '—'}</td>
            <td style="max-width:220px;font-size:12px;">${item.top_opportunity ? escapeHtml(item.top_opportunity) : '<span style="color:var(--on-surface-secondary)">—</span>'}${item.cannibalization_warning ? `<div style="margin-top:3px;font-size:11px;color:var(--error);display:flex;align-items:center;gap:3px;" title="${escapeAttr(item.cannibalization_warning)}"><span class="material-symbols-rounded" style="font-size:14px;">warning</span>Cannibalization risk</div>` : ''}</td>
            <td>${item.potential_clicks_gain > 0 ? '+' + item.potential_clicks_gain.toLocaleString() : '—'}</td>
            <td>
                <div class="actions-cell">
                    <button class="btn btn-sm btn-primary" onclick="startWorkSession(${i})" title="${recommendedAction}">
                        <span class="material-symbols-rounded" style="font-size:16px;">bolt</span>
                    </button>
                    ${item.last_optimized_at ? `<button class="action-icon" title="View optimization impact" onclick="viewImpact(${item.id}, '${item.item_type}')"><span class="material-symbols-rounded">trending_up</span></button>` : ''}
                </div>
            </td>
        </tr>`;
    }
    html += '</tbody></table></div>';
    container.innerHTML = html;
}

function closeImpactModal() {
    document.getElementById("impact-modal").classList.remove("active");
}

async function viewImpact(itemId, itemType) {
    const modal = document.getElementById("impact-modal");
    const content = document.getElementById("impact-modal-content");
    content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div><p>Loading impact data...</p></div>';
    modal.classList.add("active");

    const storeId = _getSeoStoreId();
    try {
        const data = await apiRequest(`/api/seo/impact?store_id=${storeId}&item_id=${itemId}&item_type=${itemType}`);

        let html = '';
        document.getElementById("impact-modal-title").textContent = `Impact: ${data.item_title}`;

        const statusColors = { improving: 'var(--success)', declining: 'var(--error)', stable: 'var(--on-surface-secondary)', too_early: 'var(--warning)', no_gsc_data: 'var(--on-surface-secondary)' };
        const statusLabels = { improving: 'Improving', declining: 'Declining', stable: 'Stable', too_early: 'Too Early to Tell', no_gsc_data: 'No GSC Data' };
        const statusIcons = { improving: 'trending_up', declining: 'trending_down', stable: 'trending_flat', too_early: 'schedule', no_gsc_data: 'link_off' };

        html += `<div style="text-align:center;margin-bottom:20px;">
            <span class="material-symbols-rounded" style="font-size:40px;color:${statusColors[data.status] || 'var(--primary)'};">${statusIcons[data.status] || 'help'}</span>
            <div style="font-size:18px;font-weight:700;margin-top:4px;color:${statusColors[data.status] || 'var(--primary)'};">${statusLabels[data.status] || data.status}</div>
            <div style="font-size:12px;color:var(--on-surface-secondary);">Optimized ${new Date(data.optimization_date).toLocaleDateString('en-US', {month: 'short', day: 'numeric', year: 'numeric'})} (${data.days_since} days ago)</div>
        </div>`;

        if (data.scores_before) {
            html += `<div style="margin-bottom:16px;padding:12px;background:var(--surface-dim);border-radius:8px;border:1px solid var(--outline);">
                <div style="font-size:12px;font-weight:600;margin-bottom:8px;color:var(--on-surface-secondary);">Scores Before Optimization</div>
                <div style="display:flex;gap:16px;">
                    <div><span style="font-size:11px;color:var(--on-surface-secondary);">On-Page</span><div style="font-size:20px;font-weight:700;">${data.scores_before.on_page != null ? data.scores_before.on_page : '—'}</div></div>
                    <div><span style="font-size:11px;color:var(--on-surface-secondary);">Search</span><div style="font-size:20px;font-weight:700;">${data.scores_before.search != null ? data.scores_before.search : '—'}</div></div>
                    <div><span style="font-size:11px;color:var(--on-surface-secondary);">Composite</span><div style="font-size:20px;font-weight:700;">${data.scores_before.composite != null ? data.scores_before.composite : '—'}</div></div>
                </div>
            </div>`;
        }

        if (data.before && data.after) {
            const metrics = [
                { label: 'Clicks', before: data.before.clicks, after: data.after.clicks, delta: data.delta.clicks, better: d => d > 0 },
                { label: 'Impressions', before: data.before.impressions, after: data.after.impressions, delta: data.delta.impressions, better: d => d > 0 },
                { label: 'CTR', before: (data.before.ctr * 100).toFixed(1) + '%', after: (data.after.ctr * 100).toFixed(1) + '%', delta: data.delta.ctr, fmt: d => (d > 0 ? '+' : '') + (d * 100).toFixed(2) + '%', better: d => d > 0 },
                { label: 'Avg Position', before: data.before.position.toFixed(1), after: data.after.position.toFixed(1), delta: data.delta.position, fmt: d => (d > 0 ? '+' : '') + d.toFixed(1), better: d => d > 0 },
            ];

            html += '<table style="width:100%;font-size:13px;"><thead><tr><th style="text-align:left;">Metric</th><th>Before</th><th>After</th><th>Change</th></tr></thead><tbody>';
            for (const m of metrics) {
                const deltaVal = m.delta;
                const isPositive = m.better(deltaVal);
                const deltaStr = m.fmt ? m.fmt(deltaVal) : (deltaVal > 0 ? '+' : '') + deltaVal.toLocaleString();
                const deltaColor = deltaVal === 0 ? 'var(--on-surface-secondary)' : isPositive ? 'var(--success)' : 'var(--error)';
                html += `<tr>
                    <td style="font-weight:600;">${m.label}</td>
                    <td style="text-align:center;">${typeof m.before === 'number' ? m.before.toLocaleString() : m.before}</td>
                    <td style="text-align:center;">${typeof m.after === 'number' ? m.after.toLocaleString() : m.after}</td>
                    <td style="text-align:center;color:${deltaColor};font-weight:600;">${deltaStr}</td>
                </tr>`;
            }
            html += '</tbody></table>';
        }

        if (data.status === 'too_early') {
            html += `<div style="margin-top:16px;padding:12px;background:var(--warning-container,#fff3cd);border-radius:8px;font-size:12px;color:var(--on-surface);">
                <strong>Note:</strong> It typically takes 2-4 weeks for Google to reflect SEO changes. Check back later for more accurate impact data.
            </div>`;
        }

        content.innerHTML = html;
    } catch (err) {
        content.innerHTML = `<div class="empty-state" style="padding:24px;"><span class="material-symbols-rounded">info</span><p>${err.message || 'No impact data available. This item may not have been optimized yet.'}</p></div>`;
    }
}

document.addEventListener("DOMContentLoaded", () => {
    if (typeof setupModalDismiss === "function") setupModalDismiss("impact-modal", closeImpactModal);
});

function renderCollectionsTable(container, collections) {
    let html = "";

    const storeNames = new Set(collections.map(c => c.store_name));
    const singleStore = storeNames.size <= 1;

    html += `<div class="toolbar-row">
        <button class="btn btn-secondary btn-sm" onclick="openGenerateCollectionsModal()">
            <span class="material-symbols-rounded">auto_awesome</span>
            Generate from Tags
        </button>
        <span style="margin-left: auto; font-size: 13px; color: var(--on-surface-secondary); font-weight: 500;">${collections.length} collection${collections.length !== 1 ? "s" : ""}</span>
    </div>`;

    html += `<div class="filter-groups">
        <div class="filter-group">
            <span class="filter-group-label">Type</span>
            <button class="filter-chip ${collectionFilters.type === "" ? "active" : ""}" onclick="setCollectionFilter('type','')">All</button>
            <button class="filter-chip ${collectionFilters.type === "smart" ? "active" : ""}" onclick="setCollectionFilter('type','smart')">Smart</button>
            <button class="filter-chip ${collectionFilters.type === "manual" ? "active" : ""}" onclick="setCollectionFilter('type','manual')">Manual</button>
        </div>
        <div class="filter-group-divider"></div>
        <div class="filter-group">
            <span class="filter-group-label">Status</span>
            <button class="filter-chip ${collectionFilters.published === "" ? "active" : ""}" onclick="setCollectionFilter('published','')">All</button>
            <button class="filter-chip ${collectionFilters.published === "true" ? "active" : ""}" onclick="setCollectionFilter('published','true')">Published</button>
            <button class="filter-chip ${collectionFilters.published === "false" ? "active" : ""}" onclick="setCollectionFilter('published','false')">Draft</button>
        </div>
        <div class="filter-group-divider"></div>
        <div class="filter-group">
            <span class="filter-group-label">SEO</span>
            <button class="filter-chip ${collectionFilters.seo === "" ? "active" : ""}" onclick="setCollectionFilter('seo','')">All</button>
            <button class="filter-chip ${collectionFilters.seo === "high" ? "active" : ""}" onclick="setCollectionFilter('seo','high')">Good</button>
            <button class="filter-chip ${collectionFilters.seo === "medium" ? "active" : ""}" onclick="setCollectionFilter('seo','medium')">Fair</button>
            <button class="filter-chip ${collectionFilters.seo === "low" ? "active" : ""}" onclick="setCollectionFilter('seo','low')">Poor</button>
        </div>
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
            <button class="btn btn-sm btn-secondary" onclick="startBulkOptimizeCollectionTitles()">
                <span class="material-symbols-rounded">title</span>
                Optimize Titles Only
            </button>
            <button class="btn btn-sm btn-secondary" onclick="startBulkGenerateCollectionImages()">
                <span class="material-symbols-rounded">image</span>
                Generate Images
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

    html += '<div class="table-container"><table class="items-table"><thead><tr>';
    html += '<th class="checkbox-col"><input type="checkbox" class="table-checkbox" id="select-all-collections-checkbox" onchange="toggleSelectAllCollections(this)"></th>';

    const columns = [
        { key: "title", label: "Collection", cls: "item-col" },
    ];
    if (!singleStore) columns.push({ key: "store", label: "Store" });
    columns.push(
        { key: "products_count", label: "Products" },
        { key: "seo_score", label: "Score" },
        { key: "issues_count", label: "Issues" },
        { key: "gsc_clicks", label: "Traffic (28d)" },
        { key: "last_optimized_at", label: "Last Optimized" },
    );

    for (const col of columns) {
        const isActive = collectionSort.by === col.key;
        const dirClass = isActive ? collectionSort.dir : "";
        html += `<th class="sortable-header ${dirClass} ${col.cls || ""}" onclick="toggleCollectionSort('${col.key}')">${col.label}</th>`;
    }

    html += '<th style="width: 88px;"></th>';
    html += "</tr></thead><tbody>";

    for (let idx = 0; idx < collections.length; idx++) {
        const c = collections[idx];
        const checked = selectedCollectionIds.has(c.id) ? "checked" : "";
        const rowClass = selectedCollectionIds.has(c.id) ? "row-selected" : "";

        html += `<tr class="${rowClass} item-row" onclick="onItemRowClick(event, 'collection', ${c.id})">
            <td class="checkbox-col"><input type="checkbox" class="table-checkbox" data-collection-id="${c.id}" data-index="${idx}" ${checked} onclick="toggleCollectionSelect(${c.id}, this, event)"></td>
            <td class="item-col"><div class="item-cell">
                ${renderItemThumb(c.image_url, c.title)}
                <div class="item-cell-text">
                    <div class="item-title"><span>${escapeHtml(c.title)}</span></div>
                    <div class="item-sub">
                        <span class="pub-dot ${c.published ? "pub-on" : "pub-off"}" title="${c.published ? "Published" : "Draft"}"></span>
                        <span>${c.collection_type === "smart" ? "Smart" : "Manual"}</span>
                        ${c.published ? "" : '<span class="item-sub-warn">Draft</span>'}
                        <span>· /${escapeHtml(c.handle || "")}</span>
                    </div>
                </div>
            </div></td>`;
        if (!singleStore) html += `<td>${escapeHtml(c.store_name)}</td>`;
        html += `<td>${c.products_count}</td>
            <td>${renderScoreRing(c.seo_score, 36, c)}</td>
            <td>${renderIssuesBadge(c)}</td>
            <td>${renderTrafficCell(c)}</td>
            <td>${formatOptimizedDate(c.last_optimized_at)}</td>
            <td><div class="row-actions">
                <button class="action-icon" title="View collection details" onclick="viewCollection(${c.id})"><span class="material-symbols-rounded">visibility</span></button>
                <button class="action-icon action-icon-primary" title="AI Analyze" onclick="analyzeCollection(${c.id})"><span class="material-symbols-rounded">smart_toy</span></button>
            </div></td>
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
    document.getElementById("confirm-dialog-message").innerHTML =
        `This will optimize ${selectedCollectionIds.size} collection${selectedCollectionIds.size > 1 ? "s" : ""} using AI and push changes to Shopify.`
        + `<div style="margin-top: 12px;"><label style="display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: 14px;">`
        + `<input type="checkbox" id="bulk-collection-skip-title-checkbox" checked> Keep original collection titles (don't rename)</label></div>`;

    const okBtn = document.getElementById("confirm-dialog-ok");
    okBtn.onclick = () => {
        const skipTitle = document.getElementById("bulk-collection-skip-title-checkbox")?.checked || false;
        closeConfirmDialog();
        executeBulkOptimizeCollections(skipTitle);
    };
    _openConfirmDialog();
}

async function executeBulkOptimizeCollections(skipTitle = false) {
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
            body: { collection_ids: ids, auto_apply: true, skip_title: skipTitle },
        });

        const bulkTaskId = resp.bulk_task_id;
        pollBulkCollectionTask(bulkTaskId, ids.length);
    } catch (err) {
        overlay.classList.remove("active");
        showToast(`Bulk optimization failed: ${err.message}`, "error");
    }
}

async function pollBulkCollectionTask(bulkTaskId, total, subtitleText = "Optimizing collections...") {
    const overlay = document.getElementById("bulk-progress-overlay");
    const bar = document.getElementById("bulk-progress-bar");
    const stats = document.getElementById("bulk-progress-stats");
    const current = document.getElementById("bulk-progress-current");
    const subtitle = document.getElementById("bulk-progress-subtitle");

    try {
        const task = await pollTaskUntilDone(`/api/tasks/bulk/${bulkTaskId}`, {
            timeoutMs: Infinity,
            onProgress: (t) => {
                const done = t.completed + t.failed;
                const pct = total > 0 ? Math.round((done / total) * 100) : 0;

                bar.style.width = `${pct}%`;
                stats.textContent = `${done} / ${total} completed` + (t.failed > 0 ? ` (${t.failed} failed)` : "");
                subtitle.textContent = subtitleText;

                if (t.current_item_title) {
                    current.textContent = `Currently: ${t.current_item_title}`;
                } else if (t.current_product_title) {
                    current.textContent = `Currently: ${t.current_product_title}`;
                }
            },
        });

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
    } catch (err) {
        overlay.classList.remove("active");
        selectedCollectionIds.clear();
        loadCollections();
        showToast(err.message, "error");
    }
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
    const overlay = document.getElementById("bulk-progress-overlay");
    document.getElementById("bulk-progress-subtitle").textContent = "Deleting collections...";
    document.getElementById("bulk-progress-bar").style.width = "0%";
    document.getElementById("bulk-progress-stats").textContent = `0 / ${ids.length} completed`;
    document.getElementById("bulk-progress-current").textContent = "";
    overlay.classList.add("active");

    try {
        const resp = await apiRequest("/api/seo/collections/delete", {
            method: "POST",
            body: { collection_ids: ids },
        });
        pollBulkCollectionTask(resp.bulk_task_id, resp.total, "Deleting collections...");
    } catch (err) {
        overlay.classList.remove("active");
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
    const overlay = document.getElementById("bulk-progress-overlay");
    document.getElementById("bulk-progress-subtitle").textContent = "Publishing collections...";
    document.getElementById("bulk-progress-bar").style.width = "0%";
    document.getElementById("bulk-progress-stats").textContent = `0 / ${ids.length} completed`;
    document.getElementById("bulk-progress-current").textContent = "";
    overlay.classList.add("active");

    try {
        const resp = await apiRequest("/api/seo/collections/publish", {
            method: "POST",
            body: { collection_ids: ids },
        });
        pollBulkCollectionTask(resp.bulk_task_id, resp.total, "Publishing collections...");
    } catch (err) {
        overlay.classList.remove("active");
        showToast(`Publish failed: ${err.message}`, "error");
    }
}

/* Local tab switcher for the item detail modal — deliberately NOT UITabs
 * (that's page-level and syncs ?tab=). Tab state resets on each open. */
const MODAL_TABS = [
    { id: "overview", label: "Overview" },
    { id: "checks", label: "Checks" },
    { id: "performance", label: "Performance" },
    { id: "ai", label: "AI Optimize" },
];

function renderModalTabBar(active = "overview") {
    return '<div class="modal-tabs">' + MODAL_TABS.map(t =>
        `<button class="modal-tab${t.id === active ? " active" : ""}" data-mtab="${t.id}" onclick="switchModalTab('${t.id}')">${t.label}</button>`
    ).join("") + "</div>";
}

function switchModalTab(id) {
    const content = document.getElementById("product-detail-content");
    if (!content) return;
    content.querySelectorAll(".modal-tab").forEach(b => b.classList.toggle("active", b.dataset.mtab === id));
    content.querySelectorAll(".modal-tab-panel").forEach(p => p.classList.toggle("active", p.dataset.mpanel === id));
}

async function viewProduct(id, opts = {}) {
    const modal = document.getElementById("product-modal");
    const content = document.getElementById("product-detail-content");
    content.innerHTML =
        '<div class="loading-spinner"><div class="spinner"></div><p>Loading...</p></div>';
    modal.classList.add("active");

    try {
        const p = await apiRequest(`/api/seo/products/${id}`);
        currentProductId = p.id;
        currentItemStoreId = p.store_id;
        currentItemType = "product";
        document.getElementById("product-modal-title").textContent = p.title;

        let html = "";

        const statusBadge = p.status === "ACTIVE"
            ? '<span class="badge badge-success">Active</span>'
            : `<span class="badge badge-warning">${escapeHtml(p.status || "Unknown")}</span>`;

        // Unified score hero: ring + 3 pillar bars + issue stats
        html += renderUnifiedHero(p, "product");
        if (p.keyword_analysis) {
            html += renderKeywordChips(p.keyword_analysis.detected_keywords);
        }

        const initialTab = opts.tab || "overview";
        html += renderModalTabBar(initialTab);

        // ── Overview tab ──
        html += `<div class="modal-tab-panel${initialTab === "overview" ? " active" : ""}" data-mpanel="overview">`;
        html += '<div class="modal-two-col">';
        html += '<div class="modal-col-main">';

        // SERP Preview
        const serpUrl = p.handle ? `https://example.com/products/${p.handle}` : "https://example.com/products/...";
        html += `<div class="serp-preview">
            <span class="serp-preview-title">${escapeHtml(p.meta_title || p.title)}</span>
            <span class="serp-preview-url">${escapeHtml(serpUrl)}</span>
            <span class="serp-preview-desc">${escapeHtml(p.meta_description || "No meta description set.")}</span>
        </div>`;

        // Description
        html += '<div class="preview-section">';
        html += '<div class="preview-section-title"><span class="material-symbols-rounded">description</span> Product Description</div>';
        const descContent = p.description || "";
        const needsCollapse = descContent.length > 300;
        html += `<div class="description-preview${needsCollapse ? " collapsed" : ""}" id="desc-preview">${descContent}</div>`;
        if (needsCollapse) {
            html += `<button class="description-toggle" id="desc-toggle-btn" onclick="toggleDescription()">Show more</button>`;
        }
        html += '</div>';

        // Images
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

        html += '</div>'; // end modal-col-main

        // ── Overview aside: info grid + tags ──
        html += '<div class="modal-col-aside">';
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
        html += '</div>'; // end modal-col-aside
        html += '</div>'; // end modal-two-col
        html += '</div>'; // end overview panel

        // ── Checks tab ──
        html += `<div class="modal-tab-panel${initialTab === "checks" ? " active" : ""}" data-mpanel="checks">${renderCheckGroups(p)}</div>`;

        // ── Performance tab ──
        html += `<div class="modal-tab-panel${initialTab === "performance" ? " active" : ""}" data-mpanel="performance">`;

        if (p.gsc_impressions != null) {
            html += `<div class="card" style="padding: 16px; margin-bottom: 16px; background: var(--surface-dim); border: 1px solid var(--outline);">
                <h4 style="font-size: 13px; margin-bottom: 10px;"><span class="material-symbols-rounded" style="vertical-align: middle; font-size: 16px;">monitoring</span> Search Console (28d)</h4>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                    <div><span style="font-size: 11px; color: var(--on-surface-secondary);">Clicks</span><div style="font-size: 18px; font-weight: 700;">${Math.round(p.gsc_clicks).toLocaleString()}</div></div>
                    <div><span style="font-size: 11px; color: var(--on-surface-secondary);">Impressions</span><div style="font-size: 18px; font-weight: 700;">${Math.round(p.gsc_impressions).toLocaleString()}</div></div>
                    <div><span style="font-size: 11px; color: var(--on-surface-secondary);">CTR</span><div style="font-size: 18px; font-weight: 700;">${(p.gsc_ctr * 100).toFixed(1)}%</div></div>
                    <div><span style="font-size: 11px; color: var(--on-surface-secondary);">Avg Position</span><div style="font-size: 18px; font-weight: 700;">${renderPositionBadge(p.gsc_position)}</div></div>
                </div>
            </div>`;
        }

        if (p.semrush_volume != null) {
            html += `<div class="card" style="padding: 16px; margin-bottom: 16px; background: var(--surface-dim); border: 1px solid var(--outline);">
                <h4 style="font-size: 13px; margin-bottom: 10px;"><span class="material-symbols-rounded" style="vertical-align: middle; font-size: 16px;">query_stats</span> Semrush Data</h4>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                    <div><span style="font-size: 11px; color: var(--on-surface-secondary);">Monthly Volume</span><div style="font-size: 18px; font-weight: 700;">${p.semrush_volume.toLocaleString()}</div></div>
                    <div><span style="font-size: 11px; color: var(--on-surface-secondary);">Best Position</span><div style="font-size: 18px; font-weight: 700;">${p.semrush_position != null ? renderPositionBadge(p.semrush_position) : '—'}</div></div>
                    ${p.semrush_keyword_count ? `<div><span style="font-size: 11px; color: var(--on-surface-secondary);">Keywords</span><div style="font-size: 18px; font-weight: 700;">${p.semrush_keyword_count}</div></div>` : ''}
                </div>
            </div>`;
        }

        if (p.gsc_impressions == null && p.semrush_volume == null) {
            html += '<p style="color: var(--on-surface-secondary); font-size: 13px; margin-bottom: 16px;">No search data for this product yet.</p>';
        }

        // Variants
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

        // Schema Markup
        html += '<div id="product-schema-preview" style="margin-top: 24px; border-top: 1px solid var(--outline); padding-top: 16px;">';
        html += '<h4 style="margin-bottom: 12px;"><span class="material-symbols-rounded" style="vertical-align: middle;">code</span> Schema Markup</h4>';
        html += '<div id="product-schema-content"><div class="loading-spinner"><div class="spinner"></div><p>Generating schema preview...</p></div></div>';
        html += '</div>';

        html += '</div>'; // end performance panel

        // ── AI Optimize tab ──
        html += `<div class="modal-tab-panel${initialTab === "ai" ? " active" : ""}" data-mpanel="ai"><div id="ai-results-container">
            <div class="modal-tab-empty"><span class="material-symbols-rounded">smart_toy</span>
            <p>No AI analysis yet. Run "Get AI Recommendations" below to get optimization suggestions.</p></div>
        </div></div>`;

        // Sticky footer with action buttons
        html += `<div class="modal-sticky-footer">
            <button class="btn btn-primary" onclick="analyzeProduct(${p.id})">
                <span class="material-symbols-rounded">smart_toy</span>
                Get AI Recommendations
            </button>
            <button class="btn btn-secondary" onclick="optimizeProductTitle(${p.id})">
                <span class="material-symbols-rounded">title</span>
                Optimize Title Only
            </button>
            ${p.last_optimized_at ? `<button class="btn btn-secondary" onclick="viewImpact(${p.id}, 'product')">
                <span class="material-symbols-rounded">trending_up</span>
                View Impact
            </button>` : ''}
        </div>`;

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

function reoptimizeWithKeyword(itemId, keyword) {
    const itemType = currentItemType || "product";
    const context = `Re-optimize targeting primary keyword: "${keyword}". Build the meta title, description, and content around this keyword.`;
    if (itemType === "product") {
        analyzeProduct(itemId, context);
    } else {
        analyzeCollection(itemId, context);
    }
}

async function analyzeProduct(productId, priorityContext) {
    const btn = typeof event !== "undefined" && event && event.target instanceof Element ? event.target.closest("button") : null;
    return guardButton(btn, async () => {
        const modal = document.getElementById("product-modal");
        // Re-render when closed OR showing a different item (work-session
        // navigation keeps the modal open across items).
        if (!modal.classList.contains("active") || currentProductId !== productId || currentItemType !== "product") {
            await viewProduct(productId);
        }

        const resultsContainer = document.getElementById("ai-results-container");
        if (!resultsContainer) return;

        const contextNote = priorityContext ? `<div style="font-size:12px;color:var(--on-surface-secondary);margin-top:8px;padding:6px 10px;background:var(--surface-dim);border-radius:6px;"><strong>Priority focus:</strong> ${escapeHtml(priorityContext)}</div>` : '';
        resultsContainer.innerHTML = `
            <div class="ai-panel" style="margin-top: 20px;">
                <h4><span class="material-symbols-rounded">smart_toy</span> AI Analysis</h4>
                ${contextNote}
                <div class="loading-spinner"><div class="spinner"></div><p>AI is analyzing this product...</p></div>
            </div>`;
        switchModalTab("ai");

        const taskInput = { product_id: productId };
        if (priorityContext) taskInput.priority_context = priorityContext;

        try {
            const taskResp = await apiRequest("/api/tasks", {
                method: "POST",
                body: {
                    task_type: "product_seo",
                    input: taskInput,
                },
            });

            const taskId = taskResp.task_id;
            await pollTaskResult(taskId, resultsContainer, productId);
        } catch (err) {
            resultsContainer.innerHTML = `
                <div class="ai-panel" style="margin-top: 20px;">
                    <h4><span class="material-symbols-rounded">smart_toy</span> AI Analysis</h4>
                    <p style="color: var(--error);">Error: ${escapeHtml(err.message)}</p>
                </div>`;
        }
    });
}

async function pollTaskResult(taskId, container, productId) {
    const startTime = Date.now();
    let pollCount = 0;

    try {
        const task = await pollTaskUntilDone(`/api/tasks/${taskId}`, {
            timeoutMs: 180000, // 3 minutes
            onProgress: () => {
                pollCount++;
                // Update the spinner text with elapsed time
                if (pollCount % 5 === 0) {
                    const secs = Math.round((Date.now() - startTime) / 1000);
                    const spinnerP = container.querySelector(".loading-spinner p");
                    if (spinnerP) spinnerP.textContent = `AI is analyzing... (${secs}s)`;
                }
            },
        });
        renderAIResults(container, task.result, productId);
    } catch (err) {
        container.innerHTML = `
            <div class="ai-panel" style="margin-top: 20px;">
                <h4><span class="material-symbols-rounded">smart_toy</span> AI Analysis</h4>
                <p style="color: var(--error);">${escapeHtml(err.message)}</p>
                <p style="font-size: 12px; color: var(--on-surface-secondary);">Task ID: ${taskId}</p>
            </div>`;
        showToast(err.message, "error");
    }
}

function _applyFieldToggle(field) {
    return `<label style="float: right; display: inline-flex; align-items: center; gap: 4px; font-size: 11px; font-weight: 400; color: var(--on-surface-secondary); cursor: pointer;">
        <input type="checkbox" class="ai-apply-field" data-apply-field="${field}" checked> apply</label>`;
}

function _applyFieldEnabled(field) {
    const cb = document.querySelector(`.ai-apply-field[data-apply-field="${field}"]`);
    return !cb || cb.checked;
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

    html += `<div style="margin-bottom: 12px;"><label style="display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: 14px;">
        <input type="checkbox" id="ai-keep-title-checkbox" onchange="document.getElementById('ai-h1-tag').disabled = this.checked; document.getElementById('ai-h1-tag').style.opacity = this.checked ? '0.5' : '1';">
        Keep original title (don't rename)</label></div>`;

    html += '<div class="side-by-side">';
    html += `<div class="form-group">
        <label>Optimized Title (H1 Tag)</label>
        <input type="text" class="ai-field-editable" id="ai-h1-tag"
            value="${escapeAttr(result.h1_tag || result.optimized_meta_title || "")}">
    </div>`;
    html += `<div class="form-group">
        <label>Meta Title ${_applyFieldToggle("meta_title")}</label>
        <input type="text" class="ai-field-editable" id="ai-meta-title"
            value="${escapeAttr(result.optimized_meta_title || "")}">
    </div>`;
    html += '</div>';

    html += `<div class="form-group">
        <label>Meta Description ${_applyFieldToggle("meta_description")}</label>
        <textarea class="ai-field-editable" id="ai-meta-description" rows="3">${escapeHtml(result.optimized_meta_description || "")}</textarea>
    </div>`;

    html += `<div class="form-group">
        <label>Product Description ${_applyFieldToggle("description")}</label>
        <textarea class="ai-field-editable" id="ai-description" rows="5">${escapeHtml(result.optimized_description || "")}</textarea>
    </div>`;

    html += '<div class="side-by-side">';

    html += '<div class="form-group">';
    html += `<label>Tags ${_applyFieldToggle("tags")}</label>`;
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

    if (result.target_keywords) {
        const tk = result.target_keywords;
        const allKws = [];
        if (tk.primary) allKws.push({kw: String(tk.primary), isPrimary: true});
        for (const kw of (tk.secondary || [])) allKws.push({kw: String(kw), isPrimary: false});

        html += '<div class="form-group">';
        html += '<label><span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">target</span> Target Keywords <span style="font-size:11px;color:var(--on-surface-secondary);">(click any keyword to re-optimize with it as primary)</span></label>';
        html += '<div style="display: flex; flex-wrap: wrap; gap: 6px;">';
        for (const item of allKws) {
            const kwName = item.kw.replace(/\s*\(.*?\)\s*$/, '').trim();
            if (item.isPrimary) {
                html += `<span class="chip-display" style="background:var(--primary-light,#e8f0fe);border:1px solid var(--primary);font-weight:600;cursor:pointer;" title="Currently primary" data-kw="${escapeAttr(kwName)}" onclick="reoptimizeWithKeyword(${productId}, this.dataset.kw)">Primary: ${escapeHtml(item.kw)}</span>`;
            } else {
                html += `<span class="chip-display" style="cursor:pointer;" title="Click to re-optimize with this as primary" data-kw="${escapeAttr(kwName)}" onclick="reoptimizeWithKeyword(${productId}, this.dataset.kw)">${escapeHtml(item.kw)}</span>`;
            }
        }
        if (allKws.length === 0) {
            html += '<span style="font-size: 13px; color: var(--on-surface-secondary);">No search data available</span>';
        }
        html += '</div></div>';
    }

    if (result.competitor_insights) {
        html += `<div class="form-group">
            <label>Competitor Insights</label>
            <div class="ai-field-value" style="font-size: 14px; line-height: 1.6;">${escapeHtml(result.competitor_insights)}</div>
        </div>`;
    }

    if (result.image_alt_suggestions && result.image_alt_suggestions.length > 0) {
        html += `<div class="form-group"><label>Image Alt Text Suggestions ${_applyFieldToggle("image_alts")}</label>`;
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

    html += renderActionableRecommendations(result);

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
    const keepTitle = document.getElementById("ai-keep-title-checkbox")?.checked || false;
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
    if (!keepTitle && title) payload.title = title;
    if (description && _applyFieldEnabled("description")) payload.description_html = description;
    if (metaTitle && _applyFieldEnabled("meta_title")) payload.meta_title = metaTitle;
    if (metaDescription && _applyFieldEnabled("meta_description")) payload.meta_description = metaDescription;
    if (tags.length > 0 && _applyFieldEnabled("tags")) payload.tags = tags;
    if (imageAlts.length > 0 && _applyFieldEnabled("image_alts")) payload.image_alts = imageAlts;

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

async function viewCollection(id, opts = {}) {
    try {
        const c = await apiRequest(`/api/seo/collections/${id}`);
        currentProductId = c.id;
        currentItemStoreId = c.store_id;
        currentItemType = "collection";
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

        // Unified score hero: ring + 3 pillar bars + issue stats
        html += renderUnifiedHero(c, "collection");
        if (c.keyword_analysis) {
            html += renderKeywordChips(c.keyword_analysis.detected_keywords);
        }

        const initialTab = opts.tab || "overview";
        html += renderModalTabBar(initialTab);

        // ── Overview tab ──
        html += `<div class="modal-tab-panel${initialTab === "overview" ? " active" : ""}" data-mpanel="overview">`;
        html += '<div class="modal-two-col">';
        html += '<div class="modal-col-main">';

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

        html += '<div class="preview-section">';
        html += '<div class="preview-section-title"><span class="material-symbols-rounded">image</span> Collection Image</div>';
        if (c.image_url) {
            html += `<div class="collection-image-preview" data-src="${escapeAttr(c.image_url)}">
                <img src="${escapeAttr(c.image_url)}" alt="${escapeAttr(c.title || "")}">
            </div>`;
        } else {
            html += `<p style="color: var(--on-surface-secondary); font-size: 14px;">No collection image set.</p>`;
        }
        html += `<div id="collection-image-gen-area" style="margin-top: 12px;">
            <button class="btn ${c.image_url ? "btn-secondary" : "btn-primary"}" onclick="generateCollectionImage(${c.id})">
                <span class="material-symbols-rounded">auto_awesome</span>
                ${c.image_url ? "Regenerate Image" : "Generate Collection Image"}
            </button>
        </div>`;
        html += '</div>';

        html += '</div>'; // end modal-col-main

        html += '<div class="modal-col-aside">';
        html += `<div class="preview-info-grid">
            <div class="info-item"><span class="info-label">Type</span><span class="info-value">${typeBadge}</span></div>
            <div class="info-item"><span class="info-label">Published</span><span class="info-value">${publishedBadge}</span></div>
            <div class="info-item"><span class="info-label">Products</span><span class="info-value">${c.products_count != null ? c.products_count : "—"}</span></div>
            <div class="info-item"><span class="info-label">Handle</span><span class="info-value mono">${escapeHtml(c.handle || "—")}</span></div>
            <div class="info-item"><span class="info-label">Store</span><span class="info-value">${escapeHtml(c.store_name || "—")}</span></div>
        </div>`;
        html += '</div>'; // end modal-col-aside
        html += '</div>'; // end modal-two-col
        html += '</div>'; // end overview panel

        // ── Checks tab ──
        html += `<div class="modal-tab-panel${initialTab === "checks" ? " active" : ""}" data-mpanel="checks">${renderCheckGroups(c)}</div>`;

        // ── Performance tab ──
        html += `<div class="modal-tab-panel${initialTab === "performance" ? " active" : ""}" data-mpanel="performance">`;

        // GSC Metrics
        if (c.gsc_impressions != null) {
            html += `<div class="card" style="padding: 16px; margin-bottom: 16px; background: var(--surface-dim); border: 1px solid var(--outline);">
                <h4 style="font-size: 13px; margin-bottom: 10px;"><span class="material-symbols-rounded" style="vertical-align: middle; font-size: 16px;">monitoring</span> Search Console (28d)</h4>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                    <div><span style="font-size: 11px; color: var(--on-surface-secondary);">Clicks</span><div style="font-size: 18px; font-weight: 700;">${Math.round(c.gsc_clicks).toLocaleString()}</div></div>
                    <div><span style="font-size: 11px; color: var(--on-surface-secondary);">Impressions</span><div style="font-size: 18px; font-weight: 700;">${Math.round(c.gsc_impressions).toLocaleString()}</div></div>
                    <div><span style="font-size: 11px; color: var(--on-surface-secondary);">CTR</span><div style="font-size: 18px; font-weight: 700;">${(c.gsc_ctr * 100).toFixed(1)}%</div></div>
                    <div><span style="font-size: 11px; color: var(--on-surface-secondary);">Avg Position</span><div style="font-size: 18px; font-weight: 700;">${renderPositionBadge(c.gsc_position)}</div></div>
                </div>
            </div>`;
        }

        // Semrush Data
        if (c.semrush_volume != null) {
            html += `<div class="card" style="padding: 16px; margin-bottom: 16px; background: var(--surface-dim); border: 1px solid var(--outline);">
                <h4 style="font-size: 13px; margin-bottom: 10px;"><span class="material-symbols-rounded" style="vertical-align: middle; font-size: 16px;">query_stats</span> Semrush Data</h4>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                    <div><span style="font-size: 11px; color: var(--on-surface-secondary);">Monthly Volume</span><div style="font-size: 18px; font-weight: 700;">${c.semrush_volume.toLocaleString()}</div></div>
                    <div><span style="font-size: 11px; color: var(--on-surface-secondary);">Best Position</span><div style="font-size: 18px; font-weight: 700;">${c.semrush_position != null ? renderPositionBadge(c.semrush_position) : '—'}</div></div>
                    ${c.semrush_keyword_count ? `<div><span style="font-size: 11px; color: var(--on-surface-secondary);">Keywords</span><div style="font-size: 18px; font-weight: 700;">${c.semrush_keyword_count}</div></div>` : ''}
                </div>
            </div>`;
        }

        if (c.gsc_impressions == null && c.semrush_volume == null) {
            html += '<p style="color: var(--on-surface-secondary); font-size: 13px;">No search data for this collection yet.</p>';
        }

        html += '</div>'; // end performance panel

        // ── AI Optimize tab ──
        html += `<div class="modal-tab-panel${initialTab === "ai" ? " active" : ""}" data-mpanel="ai"><div id="ai-collection-results-container">
            <div class="modal-tab-empty"><span class="material-symbols-rounded">smart_toy</span>
            <p>No AI analysis yet. Run "Get AI Recommendations" below to get optimization suggestions.</p></div>
        </div></div>`;

        // Sticky footer — parity with the product modal
        html += `<div class="modal-sticky-footer">
            <button class="btn btn-primary" onclick="analyzeCollection(${c.id})">
                <span class="material-symbols-rounded">smart_toy</span>
                Get AI Recommendations
            </button>
            <button class="btn btn-secondary" onclick="optimizeCollectionTitle(${c.id})">
                <span class="material-symbols-rounded">title</span>
                Optimize Title Only
            </button>
            ${c.last_optimized_at ? `<button class="btn btn-secondary" onclick="viewImpact(${c.id}, 'collection')">
                <span class="material-symbols-rounded">trending_up</span>
                View Impact
            </button>` : ''}
        </div>`;

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

async function analyzeCollection(collectionId, priorityContext) {
    const btn = typeof event !== "undefined" && event && event.target instanceof Element ? event.target.closest("button") : null;
    return guardButton(btn, async () => {
        const modal = document.getElementById("product-modal");
        if (!modal.classList.contains("active") || currentProductId !== collectionId || currentItemType !== "collection") {
            await viewCollection(collectionId);
        }

        const resultsContainer = document.getElementById("ai-collection-results-container");
        if (!resultsContainer) return;

        const contextNote = priorityContext ? `<div style="font-size:12px;color:var(--on-surface-secondary);margin-top:8px;padding:6px 10px;background:var(--surface-dim);border-radius:6px;"><strong>Priority focus:</strong> ${escapeHtml(priorityContext)}</div>` : '';
        resultsContainer.innerHTML = `
            <div class="ai-panel" style="margin-top: 20px;">
                <h4><span class="material-symbols-rounded">smart_toy</span> AI Analysis</h4>
                ${contextNote}
                <div class="loading-spinner"><div class="spinner"></div><p>AI is analyzing this collection...</p></div>
            </div>`;
        switchModalTab("ai");

        const taskInput = { collection_id: collectionId };
        if (priorityContext) taskInput.priority_context = priorityContext;

        try {
            const taskResp = await apiRequest("/api/tasks", {
                method: "POST",
                body: {
                    task_type: "collection_seo",
                    input: taskInput,
                },
            });

            const taskId = taskResp.task_id;
            await pollCollectionTaskResult(taskId, resultsContainer, collectionId);
        } catch (err) {
            resultsContainer.innerHTML = `
                <div class="ai-panel" style="margin-top: 20px;">
                    <h4><span class="material-symbols-rounded">smart_toy</span> AI Analysis</h4>
                    <p style="color: var(--error);">Error: ${escapeHtml(err.message)}</p>
                </div>`;
        }
    });
}

async function pollCollectionTaskResult(taskId, container, collectionId) {
    try {
        const task = await pollTaskUntilDone(`/api/tasks/${taskId}`);
        renderCollectionAIResults(container, task.result, collectionId);
    } catch (err) {
        container.innerHTML = `
            <div class="ai-panel" style="margin-top: 20px;">
                <h4><span class="material-symbols-rounded">smart_toy</span> AI Analysis</h4>
                <p style="color: var(--error);">Failed: ${escapeHtml(err.message)}</p>
            </div>`;
        showToast(err.message, "error");
    }
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

    html += `<div style="margin-bottom: 12px;"><label style="display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: 14px;">
        <input type="checkbox" id="ai-collection-keep-title-checkbox" onchange="document.getElementById('ai-collection-h1-tag').disabled = this.checked; document.getElementById('ai-collection-h1-tag').style.opacity = this.checked ? '0.5' : '1';">
        Keep original title (don't rename)</label></div>`;

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

    html += renderActionableRecommendations(result);

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
    const keepTitle = document.getElementById("ai-collection-keep-title-checkbox")?.checked || false;
    const title = document.getElementById("ai-collection-h1-tag")?.value?.trim() || "";
    const metaTitle = document.getElementById("ai-collection-meta-title")?.value?.trim() || "";
    const metaDescription = document.getElementById("ai-collection-meta-description")?.value?.trim() || "";
    const description = document.getElementById("ai-collection-description")?.value?.trim() || "";

    const payload = {};
    if (!keepTitle && title) payload.title = title;
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


// ========== Title-Only Optimization ==========

function startBulkOptimizeTitles() {
    if (selectedProductIds.size === 0) return;

    document.getElementById("confirm-dialog-title").textContent = "Optimize Titles Only";
    document.getElementById("confirm-dialog-message").innerHTML =
        `This will optimize titles for ${selectedProductIds.size} product${selectedProductIds.size > 1 ? "s" : ""} using AI and push only the new titles to Shopify. No other fields will be changed.`;

    const okBtn = document.getElementById("confirm-dialog-ok");
    okBtn.onclick = () => {
        closeConfirmDialog();
        executeBulkOptimizeTitles();
    };
    _openConfirmDialog();
}

async function executeBulkOptimizeTitles() {
    const ids = Array.from(selectedProductIds);

    const overlay = document.getElementById("bulk-progress-overlay");
    const bar = document.getElementById("bulk-progress-bar");
    const stats = document.getElementById("bulk-progress-stats");
    const current = document.getElementById("bulk-progress-current");
    const subtitle = document.getElementById("bulk-progress-subtitle");

    bar.style.width = "0%";
    stats.textContent = `0 / ${ids.length} completed`;
    current.textContent = "";
    subtitle.textContent = "Starting title optimization...";
    overlay.classList.add("active");

    try {
        const resp = await apiRequest("/api/tasks/bulk-titles", {
            method: "POST",
            body: { product_ids: ids },
        });

        const bulkTaskId = resp.bulk_task_id;
        pollBulkTask(bulkTaskId, ids.length, "Optimizing titles...");
    } catch (err) {
        overlay.classList.remove("active");
        showToast(`Bulk title optimization failed: ${err.message}`, "error");
    }
}

function startBulkOptimizeCollectionTitles() {
    if (selectedCollectionIds.size === 0) return;

    document.getElementById("confirm-dialog-title").textContent = "Optimize Titles Only";
    document.getElementById("confirm-dialog-message").innerHTML =
        `This will optimize titles for ${selectedCollectionIds.size} collection${selectedCollectionIds.size > 1 ? "s" : ""} using AI and push only the new titles to Shopify. No other fields will be changed.`;

    const okBtn = document.getElementById("confirm-dialog-ok");
    okBtn.onclick = () => {
        closeConfirmDialog();
        executeBulkOptimizeCollectionTitles();
    };
    _openConfirmDialog();
}

async function executeBulkOptimizeCollectionTitles() {
    const ids = Array.from(selectedCollectionIds);

    const overlay = document.getElementById("bulk-progress-overlay");
    const bar = document.getElementById("bulk-progress-bar");
    const stats = document.getElementById("bulk-progress-stats");
    const current = document.getElementById("bulk-progress-current");
    const subtitle = document.getElementById("bulk-progress-subtitle");

    bar.style.width = "0%";
    stats.textContent = `0 / ${ids.length} completed`;
    current.textContent = "";
    subtitle.textContent = "Starting title optimization...";
    overlay.classList.add("active");

    try {
        const resp = await apiRequest("/api/tasks/bulk-collection-titles", {
            method: "POST",
            body: { collection_ids: ids },
        });

        const bulkTaskId = resp.bulk_task_id;
        pollBulkCollectionTask(bulkTaskId, ids.length, "Optimizing titles...");
    } catch (err) {
        overlay.classList.remove("active");
        showToast(`Bulk title optimization failed: ${err.message}`, "error");
    }
}

async function optimizeProductTitle(productId) {
    const btn = typeof event !== "undefined" && event && event.target instanceof Element ? event.target.closest("button") : null;
    return guardButton(btn, async () => {
        const modal = document.getElementById("product-modal");
        if (!modal.classList.contains("active") || currentProductId !== productId || currentItemType !== "product") {
            await viewProduct(productId);
        }

        const resultsContainer = document.getElementById("ai-results-container");
        if (!resultsContainer) return;

        resultsContainer.innerHTML = `
            <div class="ai-panel" style="margin-top: 20px;">
                <h4><span class="material-symbols-rounded">title</span> Title Optimization</h4>
                <div class="loading-spinner"><div class="spinner"></div><p>AI is optimizing the title...</p></div>
            </div>`;
        switchModalTab("ai");

        try {
            const taskResp = await apiRequest("/api/tasks", {
                method: "POST",
                body: {
                    task_type: "product_title_seo",
                    input: { product_id: productId },
                },
            });

            await pollTitleTaskResult(taskResp.task_id, resultsContainer, productId, "product");
        } catch (err) {
            resultsContainer.innerHTML = `
                <div class="ai-panel" style="margin-top: 20px;">
                    <h4><span class="material-symbols-rounded">title</span> Title Optimization</h4>
                    <p style="color: var(--error);">Error: ${escapeHtml(err.message)}</p>
                </div>`;
        }
    });
}

async function optimizeCollectionTitle(collectionId) {
    const btn = typeof event !== "undefined" && event && event.target instanceof Element ? event.target.closest("button") : null;
    return guardButton(btn, async () => {
        const modal = document.getElementById("product-modal");
        if (!modal.classList.contains("active") || currentProductId !== collectionId || currentItemType !== "collection") {
            await viewCollection(collectionId);
        }

        const resultsContainer = document.getElementById("ai-collection-results-container");
        if (!resultsContainer) return;

        resultsContainer.innerHTML = `
            <div class="ai-panel" style="margin-top: 20px;">
                <h4><span class="material-symbols-rounded">title</span> Title Optimization</h4>
                <div class="loading-spinner"><div class="spinner"></div><p>AI is optimizing the title...</p></div>
            </div>`;
        switchModalTab("ai");

        try {
            const taskResp = await apiRequest("/api/tasks", {
                method: "POST",
                body: {
                    task_type: "collection_title_seo",
                    input: { collection_id: collectionId },
                },
            });

            await pollTitleTaskResult(taskResp.task_id, resultsContainer, collectionId, "collection");
        } catch (err) {
            resultsContainer.innerHTML = `
                <div class="ai-panel" style="margin-top: 20px;">
                    <h4><span class="material-symbols-rounded">title</span> Title Optimization</h4>
                    <p style="color: var(--error);">Error: ${escapeHtml(err.message)}</p>
                </div>`;
        }
    });
}

async function pollTitleTaskResult(taskId, container, itemId, itemType) {
    try {
        const task = await pollTaskUntilDone(`/api/tasks/${taskId}`);
        renderTitleResults(container, task.result, itemId, itemType);
    } catch (err) {
        container.innerHTML = `
            <div class="ai-panel" style="margin-top: 20px;">
                <h4><span class="material-symbols-rounded">title</span> Title Optimization</h4>
                <p style="color: var(--error);">Failed: ${escapeHtml(err.message)}</p>
            </div>`;
        showToast(err.message, "error");
    }
}

function renderTitleResults(container, result, itemId, itemType) {
    if (!result) {
        container.innerHTML = "";
        return;
    }

    if (result.raw_response) {
        container.innerHTML = `
            <div class="ai-panel" style="margin-top: 20px;">
                <h4><span class="material-symbols-rounded">title</span> Title Optimization</h4>
                <div class="form-group">
                    <label>AI Response</label>
                    <div class="ai-field-value" style="white-space: pre-wrap;"><code>${escapeHtml(result.raw_response)}</code></div>
                </div>
            </div>`;
        return;
    }

    let html = `
        <div class="ai-panel" style="margin-top: 20px;">
            <div class="success-banner">
                <span class="material-symbols-rounded">check_circle</span>
                Title optimization complete!
            </div>`;

    html += `<div class="form-group">
        <label>Optimized Title</label>
        <input type="text" class="ai-field-editable" id="ai-title-only-input"
            value="${escapeAttr(result.optimized_title || "")}">
    </div>`;

    if (result.reasoning) {
        html += `<div class="form-group">
            <label>Reasoning</label>
            <p style="font-size: 14px; line-height: 1.6;">${escapeHtml(result.reasoning)}</p>
        </div>`;
    }

    if (result.keywords_targeted && result.keywords_targeted.length > 0) {
        html += '<div class="form-group"><label>Keywords Targeted</label>';
        html += '<div style="display: flex; flex-wrap: wrap; gap: 6px;">';
        for (const kw of result.keywords_targeted) {
            html += `<span class="chip-display">${escapeHtml(kw)}</span>`;
        }
        html += '</div></div>';
    }

    const applyFn = itemType === "collection" ? "applyCollectionTitleOnly" : "applyProductTitleOnly";
    html += `<div class="modal-footer">
        <button class="btn btn-secondary" onclick="closeProductModal()">Cancel</button>
        <button class="btn btn-primary" id="apply-title-only-btn" onclick="${applyFn}(${itemId})">
            <span class="material-symbols-rounded">publish</span>
            Apply Title
        </button>
    </div>`;

    html += "</div>";
    container.innerHTML = html;
}

async function applyProductTitleOnly(productId) {
    const title = document.getElementById("ai-title-only-input")?.value?.trim() || "";
    if (!title) {
        showToast("No title to apply", "warning");
        return;
    }

    try {
        const btn = document.getElementById("apply-title-only-btn");
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<span class="material-symbols-rounded">sync</span> Applying...';
        }

        const resp = await apiRequest(`/api/seo/products/${productId}/apply`, {
            method: "POST",
            body: { title: title },
        });

        if (resp.ok) {
            showToast("Title applied!", "success");
            closeProductModal();
            loadProducts();
        } else {
            showToast(`Failed to apply: ${resp.error || "Unknown error"}`, "error");
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '<span class="material-symbols-rounded">publish</span> Apply Title';
            }
        }
    } catch (err) {
        showToast(`Error applying title: ${err.message}`, "error");
        const btn = document.getElementById("apply-title-only-btn");
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<span class="material-symbols-rounded">publish</span> Apply Title';
        }
    }
}

async function applyCollectionTitleOnly(collectionId) {
    const title = document.getElementById("ai-title-only-input")?.value?.trim() || "";
    if (!title) {
        showToast("No title to apply", "warning");
        return;
    }

    try {
        const btn = document.getElementById("apply-title-only-btn");
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<span class="material-symbols-rounded">sync</span> Applying...';
        }

        const resp = await apiRequest(`/api/seo/collections/${collectionId}/apply`, {
            method: "POST",
            body: { title: title },
        });

        if (resp.ok) {
            showToast("Title applied!", "success");
            closeProductModal();
            loadCollections();
        } else {
            showToast(`Failed to apply: ${resp.error || "Unknown error"}`, "error");
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '<span class="material-symbols-rounded">publish</span> Apply Title';
            }
        }
    } catch (err) {
        showToast(`Error applying title: ${err.message}`, "error");
        const btn = document.getElementById("apply-title-only-btn");
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<span class="material-symbols-rounded">publish</span> Apply Title';
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

    html += `<div class="schema-stats-bar">
        <div class="schema-stat"><span class="material-symbols-rounded">inventory_2</span> <strong>${stats.total_products}</strong> Products</div>
        <div class="schema-stat"><span class="material-symbols-rounded">code</span> <strong>${stats.schemas_generated}</strong> Generated</div>
        <div class="schema-stat"><span class="material-symbols-rounded">check_circle</span> <strong>${stats.schemas_valid}</strong> Valid</div>
        <div class="schema-stat"><span class="material-symbols-rounded">cloud_upload</span> <strong>${stats.schemas_pushed}</strong> Pushed</div>
    </div>`;

    html += '<div class="collapsible-section">';
    html += `<button class="collapsible-trigger" id="schema-settings-trigger" onclick="toggleSchemaSettings()">
        <span>Organization Settings</span>
        <span class="material-symbols-rounded">expand_more</span>
    </button>`;
    html += '<div class="collapsible-body" id="schema-settings-body">';
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
    html += "</div></div>";

    html += `<div class="toolbar-row">
        <div class="filter-groups" style="margin-bottom: 0;">
            <div class="filter-group">
                <span class="filter-group-label">Schema</span>
                <button class="filter-chip ${schemaFilter.generated === "" ? "active" : ""}" onclick="setSchemaFilter('generated','')">All</button>
                <button class="filter-chip ${schemaFilter.generated === "yes" ? "active" : ""}" onclick="setSchemaFilter('generated','yes')">Generated</button>
                <button class="filter-chip ${schemaFilter.generated === "no" ? "active" : ""}" onclick="setSchemaFilter('generated','no')">None</button>
            </div>
            <div class="filter-group-divider"></div>
            <div class="filter-group">
                <span class="filter-group-label">Valid</span>
                <button class="filter-chip ${schemaFilter.valid === "" ? "active" : ""}" onclick="setSchemaFilter('valid','')">All</button>
                <button class="filter-chip ${schemaFilter.valid === "yes" ? "active" : ""}" onclick="setSchemaFilter('valid','yes')">Valid</button>
                <button class="filter-chip ${schemaFilter.valid === "no" ? "active" : ""}" onclick="setSchemaFilter('valid','no')">Invalid</button>
            </div>
            <div class="filter-group-divider"></div>
            <div class="filter-group">
                <span class="filter-group-label">Pushed</span>
                <button class="filter-chip ${schemaFilter.pushed === "" ? "active" : ""}" onclick="setSchemaFilter('pushed','')">All</button>
                <button class="filter-chip ${schemaFilter.pushed === "yes" ? "active" : ""}" onclick="setSchemaFilter('pushed','yes')">Pushed</button>
                <button class="filter-chip ${schemaFilter.pushed === "no" ? "active" : ""}" onclick="setSchemaFilter('pushed','no')">Not Pushed</button>
            </div>
        </div>
        <div style="margin-left: auto; display: flex; gap: 6px; flex-shrink: 0;">
            <button class="btn btn-sm btn-primary" onclick="batchGenerateSchemas()">
                <span class="material-symbols-rounded">code</span> Generate All
            </button>
            <button class="btn btn-sm btn-secondary" onclick="batchPushSchemas()">
                <span class="material-symbols-rounded">cloud_upload</span> Push All
            </button>
        </div>
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
            <td><strong>${escapeHtml(p.title)}</strong></td>
            <td><span class="badge badge-info">${escapeHtml(p.status)}</span></td>
            <td>${schemaStatus}</td>
            <td>${validStatus}</td>
            <td>${pushedStatus}</td>
            <td>
                <div class="actions-cell">
                    <button class="action-icon action-icon-primary" title="Generate schema" onclick="generateSchema(${p.id})"><span class="material-symbols-rounded">code</span></button>
                    <button class="action-icon" title="View schema" onclick="viewSchema(${p.id})" ${!p.schema_generated ? "disabled style=\"opacity:0.4;pointer-events:none;\"" : ""}><span class="material-symbols-rounded">visibility</span></button>
                    <button class="action-icon" title="Push to Shopify" onclick="pushSchema(${p.id})" ${!p.schema_generated ? "disabled style=\"opacity:0.4;pointer-events:none;\"" : ""}><span class="material-symbols-rounded">cloud_upload</span></button>
                </div>
            </td>
        </tr>`;
    }

    html += "</tbody></table></div>";
    container.innerHTML = html;
}

// renderStatCard removed — replaced by schema-stats-bar inline rendering

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
            <td class="checkbox-col"><input type="checkbox" class="table-checkbox" data-tag-key="${escapeAttr(key)}" data-index="${idx}" ${checked} onclick="toggleTagSelect(this.dataset.tagKey, this)"></td>
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

    // Snapshot state so a failed batch can roll back instead of leaving the
    // feature disabled with a half-cleared selection until reload.
    const prevSelectedTagKeys = new Set(selectedTagKeys);
    const prevRejectedTags = { ...aiRejectedTags };

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
        aiFilterApplied = false;
        aiRejectedTags = prevRejectedTags;
        selectedTagKeys = prevSelectedTagKeys;
        reRenderTagsTable();
        showToast(`AI filter failed: ${err.message}`, "error");
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
        // Only generate tags still visible under the current min-products
        // filter — stale selections of hidden tags must not run.
        if (t.product_count < minProductsFilter) continue;
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

async function pollGenerateTask(bulkTaskId, total, autoOptimize) {
    const overlay = document.getElementById("generate-progress-overlay");
    const bar = document.getElementById("generate-progress-bar");
    const stats = document.getElementById("generate-progress-stats");
    const current = document.getElementById("generate-progress-current");
    const subtitle = document.getElementById("generate-progress-subtitle");

    try {
        const task = await pollTaskUntilDone(`/api/tasks/bulk/${bulkTaskId}`, {
            timeoutMs: Infinity,
            onProgress: (t) => {
                const done = t.completed + t.failed;
                const pct = total > 0 ? Math.round((done / total) * 100) : 0;

                bar.style.width = `${pct}%`;
                stats.textContent = `${done} / ${total} completed` + (t.failed > 0 ? ` (${t.failed} failed)` : "");
                subtitle.textContent = "Creating collections on Shopify...";

                if (t.current_product_title) {
                    current.textContent = `Currently: ${t.current_product_title}`;
                }
            },
        });

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
    } catch (err) {
        overlay.classList.remove("active");
        loadCollections();
        showToast(err.message, "error");
    }
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
        // Read-only preview: opening the modal must not regenerate (and reset
        // the pushed flag); only generate when no schema exists yet.
        let resp;
        try {
            resp = await apiRequest(`/api/schema/products/${productId}/view`);
        } catch (viewErr) {
            resp = await apiRequest(`/api/schema/products/${productId}/generate`, { method: "POST" });
        }
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

        try {
            const task = await pollTaskUntilDone(`/api/tasks/bulk/${bulkTaskId}`, {
                timeoutMs: Infinity,
                onProgress: (t) => {
                    const done = t.completed + t.failed;
                    const pct = total > 0 ? Math.round((done / total) * 100) : 0;

                    bar.style.width = `${pct}%`;
                    stats.textContent = `${done} / ${total} completed` + (t.failed > 0 ? ` (${t.failed} failed)` : "");

                    if (t.current_product_title) {
                        current.textContent = `Currently: ${t.current_product_title}`;
                    }
                },
            });

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
        } catch (pollErr) {
            overlay.classList.remove("active");
            loadProducts();
            showToast(pollErr.message, "error");
        }
    } catch (err) {
        overlay.classList.remove("active");
        showToast(`Bulk apply failed: ${err.message}`, "error");
    }
}

async function generateCollectionImage(collectionId) {
    const area = document.getElementById("collection-image-gen-area");
    if (!area) return;

    area.innerHTML = `<div class="loading-spinner" style="padding: 16px 0;"><div class="spinner"></div><p>Generating AI collection image... this may take 10-15 seconds</p></div>`;

    try {
        const resp = await apiRequest(`/api/seo/collections/${collectionId}/generate-image`, { method: "POST" });

        area.innerHTML = `
            <div style="margin-top: 8px;">
                <img src="data:image/png;base64,${resp.image_base64}" alt="${escapeAttr(resp.alt_text)}"
                    style="max-width: 100%; border-radius: 8px; border: 1px solid var(--outline); cursor: pointer;"
                    onclick="openLightbox('data:image/png;base64,${resp.image_base64}')">
                <p style="font-size: 13px; color: var(--on-surface-secondary); margin-top: 8px;">
                    Preview generated from ${resp.product_count} product image${resp.product_count !== 1 ? "s" : ""}
                </p>
                <div style="display: flex; gap: 8px; margin-top: 12px;">
                    <button class="btn btn-primary" onclick="applyCollectionImage(${collectionId})">
                        <span class="material-symbols-rounded">check</span>
                        Apply to Shopify
                    </button>
                    <button class="btn btn-secondary" onclick="generateCollectionImage(${collectionId})">
                        <span class="material-symbols-rounded">refresh</span>
                        Regenerate
                    </button>
                </div>
            </div>`;

        window._pendingCollectionImage = {
            image_base64: resp.image_base64,
            filename: resp.filename,
            alt_text: resp.alt_text,
        };
    } catch (err) {
        area.innerHTML = `
            <p style="color: var(--error); font-size: 14px;">Failed to generate image: ${escapeHtml(err.message)}</p>
            <button class="btn btn-secondary" style="margin-top: 8px;" onclick="generateCollectionImage(${collectionId})">
                <span class="material-symbols-rounded">refresh</span>
                Retry
            </button>`;
    }
}

async function applyCollectionImage(collectionId) {
    const area = document.getElementById("collection-image-gen-area");
    if (!area) return;

    const imageData = window._pendingCollectionImage;
    if (!imageData) {
        showToast("No image to apply. Generate one first.", "error");
        return;
    }

    area.innerHTML = `<div class="loading-spinner" style="padding: 16px 0;"><div class="spinner"></div><p>Applying image to Shopify...</p></div>`;

    try {
        await apiRequest(`/api/seo/collections/${collectionId}/apply-image`, {
            method: "POST",
            body: imageData,
        });

        window._pendingCollectionImage = null;
        showToast("Collection image applied successfully!", "success");
        viewCollection(collectionId);
    } catch (err) {
        area.innerHTML = `
            <p style="color: var(--error); font-size: 14px;">Failed to apply image: ${escapeHtml(err.message)}</p>
            <button class="btn btn-primary" style="margin-top: 8px;" onclick="applyCollectionImage(${collectionId})">
                <span class="material-symbols-rounded">refresh</span>
                Retry
            </button>`;
    }
}

function startBulkGenerateCollectionImages() {
    if (selectedCollectionIds.size === 0) return;

    document.getElementById("confirm-dialog-title").textContent = "Generate Collection Images";
    document.getElementById("confirm-dialog-message").textContent =
        `This will generate and apply promotional images for ${selectedCollectionIds.size} collection${selectedCollectionIds.size > 1 ? "s" : ""} using their top product images.`;

    const okBtn = document.getElementById("confirm-dialog-ok");
    okBtn.onclick = () => {
        closeConfirmDialog();
        executeBulkGenerateCollectionImages();
    };
    _openConfirmDialog();
}

async function executeBulkGenerateCollectionImages() {
    const ids = Array.from(selectedCollectionIds);

    const overlay = document.getElementById("bulk-progress-overlay");
    const bar = document.getElementById("bulk-progress-bar");
    const stats = document.getElementById("bulk-progress-stats");
    const current = document.getElementById("bulk-progress-current");
    const subtitle = document.getElementById("bulk-progress-subtitle");

    bar.style.width = "0%";
    stats.textContent = `0 / ${ids.length} completed`;
    current.textContent = "";
    subtitle.textContent = "Starting image generation...";
    overlay.classList.add("active");

    try {
        const resp = await apiRequest("/api/seo/collections/bulk-generate-images", {
            method: "POST",
            body: { collection_ids: ids },
        });

        const bulkTaskId = resp.bulk_task_id;
        pollBulkCollectionTask(bulkTaskId, ids.length, "Generating AI collection images...");
    } catch (err) {
        overlay.classList.remove("active");
        showToast(`Bulk image generation failed: ${err.message}`, "error");
    }
}
