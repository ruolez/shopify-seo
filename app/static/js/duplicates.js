// Duplicates section — detect & fix duplicate SEO titles / meta descriptions
// across products, collections, blog articles, news articles, and pages.

// Loaded groups, keyed by field, so AI-fix can look items up by group index.
let duplicateData = { seo_title: [], meta_description: [] };
let dupTypeFilter = "all";

const DUP_FIELDS = [
    { key: "seo_title", label: "Duplicate SEO Titles", apply: "meta_title" },
    { key: "meta_description", label: "Duplicate Meta Descriptions", apply: "meta_description" },
];

const DUP_TYPES = {
    product: { label: "product", badge: "badge-info", endpoint: (id) => `/api/seo/products/${id}/apply` },
    collection: { label: "collection", badge: "badge-warning", endpoint: (id) => `/api/seo/collections/${id}/apply` },
    article: { label: "blog", badge: "badge-success", endpoint: (id) => `/api/blog/articles/${id}/apply` },
    news: { label: "news", badge: "badge-success", endpoint: (id) => `/api/blog/articles/${id}/apply` },
    page: { label: "page", badge: "badge-info", endpoint: (id) => `/api/landing-pages/synced/${id}/ai-apply` },
};

function getStoreId() {
    const sel = document.getElementById("store-select");
    return sel ? sel.value : "";
}

async function loadStores() {
    const sel = document.getElementById("store-select");
    try {
        const stores = await apiRequest("/api/stores");
        if (!stores || !stores.length) {
            sel.innerHTML = '<option value="">No stores</option>';
            return;
        }
        sel.innerHTML = stores
            .map((s) => `<option value="${s.id}">${escapeHtml(s.name)}</option>`)
            .join("");
        const saved = loadFilter("dupStoreId", "");
        if (saved && stores.some((s) => String(s.id) === String(saved))) {
            sel.value = saved;
        }
        sel.addEventListener("change", () => {
            saveFilter("dupStoreId", sel.value);
            loadDuplicates();
        });
    } catch (err) {
        showToast(`Failed to load stores: ${err.message}`, "error");
    }
}

async function loadDuplicates() {
    const container = document.getElementById("duplicates-container");
    const storeId = getStoreId();
    if (!storeId) {
        container.innerHTML = '<div class="empty-state"><span class="material-symbols-rounded">store</span><p>No stores found. Add a store first.</p></div>';
        return;
    }
    container.innerHTML = '<div class="loading-spinner" style="padding:60px 0;"><div class="spinner"></div><p>Scanning products, collections, articles &amp; pages for duplicates...</p></div>';

    try {
        const data = await apiRequest(`/api/duplicates?store_id=${storeId}`);
        duplicateData = { seo_title: data.seo_title || [], meta_description: data.meta_description || [], scanned: data.scanned || {} };
        renderDuplicates(container);
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>${escapeHtml(err.message)}</p></div>`;
    }
}

function setDupTypeFilter(type) {
    dupTypeFilter = type;
    renderDuplicates(document.getElementById("duplicates-container"));
}

function dupGroupMatchesFilter(group) {
    if (dupTypeFilter === "all") return true;
    return group.items.some((it) => it.type === dupTypeFilter);
}

function renderDuplicates(container) {
    const data = duplicateData;
    const scanned = data.scanned || {};
    const totalGroups =
        (data.seo_title || []).length + (data.meta_description || []).length;

    const scannedLine = [
        `${scanned.product || 0} products`,
        `${scanned.collection || 0} collections`,
        `${scanned.article || 0} articles`,
        `${scanned.page || 0} pages`,
    ].join(" · ");

    const filterChips = [["all", "All"], ["product", "Products"], ["collection", "Collections"], ["article", "Blog"], ["news", "News"], ["page", "Pages"]]
        .map(([type, label]) =>
            `<button class="filter-chip ${dupTypeFilter === type ? "active" : ""}" onclick="setDupTypeFilter('${type}')">${label}</button>`)
        .join("");

    let html = `<div class="toolbar-row" style="gap:12px;flex-wrap:wrap;align-items:center;">
        <div style="font-size:13px;color:var(--on-surface-secondary);">
            Compares all page types in the same store — ${scannedLine} scanned live.
            Fix buttons need synced items: sync products &amp; blogs from <a href="/stores">Stores</a>, pages from Landing Pages.
        </div>
        <button class="btn btn-secondary btn-sm" style="margin-left:auto;" onclick="loadDuplicates()">
            <span class="material-symbols-rounded" style="font-size:16px;">refresh</span> Rescan
        </button>
    </div>
    <div class="status-filter-bar" style="margin-top:12px;">${filterChips}</div>`;

    if (totalGroups === 0) {
        container.innerHTML =
            html +
            '<div class="empty-state"><span class="material-symbols-rounded">verified</span><p>No duplicate SEO titles or meta descriptions found.</p></div>';
        return;
    }

    for (const field of DUP_FIELDS) {
        const groups = data[field.key] || [];
        const visible = groups.filter(dupGroupMatchesFilter);
        html += `<h3 style="margin:24px 0 8px;">${field.label} <span style="color:var(--on-surface-secondary);font-weight:400;">(${visible.length}${visible.length !== groups.length ? ` of ${groups.length}` : ""})</span></h3>`;
        if (visible.length === 0) {
            html += '<p style="color:var(--on-surface-secondary);font-size:13px;">None found.</p>';
            continue;
        }
        groups.forEach((group, gi) => {
            if (!dupGroupMatchesFilter(group)) return;
            html += renderDuplicateGroup(field, gi, group);
        });
    }

    container.innerHTML = html;
}

function renderDuplicateGroup(field, gi, group) {
    // Canonical = highest on-page score among fixable (synced) items; suggested to keep.
    const keepId = dedupeKeepId(group);

    let rows = "";
    group.items.forEach((it) => {
        const meta = DUP_TYPES[it.type] || DUP_TYPES.product;
        const current = field.key === "seo_title" ? it.meta_title || "" : it.meta_description || "";
        const inputId = `dupinput-${field.key}-${gi}-${it.id}`;
        const typeBadge = `<span class="badge ${meta.badge}">${meta.label}</span>`;
        const keepBadge = it.id != null && it.id === keepId ? '<span class="badge badge-success" title="Highest SEO score — suggested to keep">keep</span>' : "";
        const blankHint =
            field.key === "seo_title" && it.has_blank_meta_title
                ? '<span style="color:var(--warning);font-size:11px;"> · blank SEO title, falls back to name</span>'
                : "";
        const unsynced = it.id == null;
        const unsyncedHint = unsynced
            ? `<span style="color:var(--warning);font-size:11px;"> · not synced — ${it.type === "page" ? "sync pages from Landing Pages" : "re-sync store"} to fix</span>`
            : "";
        rows += `<div class="dup-item" style="display:flex;gap:10px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--outline);${unsynced ? "opacity:0.6;" : ""}">
            <div style="flex:1;min-width:0;">
                <div style="font-size:13px;font-weight:500;">${escapeHtml(it.title || "(untitled)")} ${typeBadge} ${keepBadge}</div>
                <div style="font-size:11px;color:var(--on-surface-secondary);">${escapeHtml(it.handle || "")} · score ${it.on_page_score}${blankHint}${unsyncedHint}</div>
                <textarea id="${inputId}" rows="2" style="width:100%;margin-top:6px;font-size:13px;" placeholder="${escapeHtml(it.title || "")}" ${unsynced ? "disabled" : ""}>${escapeHtml(current)}</textarea>
            </div>
            <button class="btn btn-primary btn-sm" style="white-space:nowrap;margin-top:22px;" ${unsynced ? "disabled" : ""}
                onclick="applyDuplicateFix('${field.key}', '${it.type}', ${it.id}, '${inputId}', this)">Apply</button>
        </div>`;
    });

    const groupId = `dupgroup-${field.key}-${gi}`;
    return `<div class="card" id="${groupId}" style="margin-bottom:12px;padding:14px;">
        <div style="display:flex;gap:10px;align-items:center;">
            <div style="flex:1;min-width:0;">
                <div style="font-size:12px;color:var(--on-surface-secondary);">Shared value (${group.count} items)</div>
                <div style="font-weight:600;word-break:break-word;">${escapeHtml(group.value)}</div>
            </div>
            <button class="btn btn-secondary btn-sm" style="white-space:nowrap;"
                onclick="fixGroupWithAI('${field.key}', ${gi}, ${keepId}, this)">
                <span class="material-symbols-rounded" style="font-size:16px;">smart_toy</span> Fix group with AI
            </button>
        </div>
        ${rows}
    </div>`;
}

function dedupeKeepId(group) {
    const fixable = group.items.filter((it) => it.id != null);
    if (!fixable.length) return null;
    return fixable.reduce((best, it) => (it.on_page_score > best.on_page_score ? it : best)).id;
}

async function fixGroupWithAI(fieldKey, gi, keepId, btn) {
    const group = (duplicateData[fieldKey] || [])[gi];
    if (!group) return;

    // Only synced items can be applied afterwards, so only send those to the AI.
    const fixable = group.items.filter((it) => it.id != null);
    if (fixable.length < 2) {
        showToast("Sync the store to enable AI fixes for this group", "warning");
        return;
    }

    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded spin" style="font-size:16px;">progress_activity</span> Generating...';

    try {
        const resp = await apiRequest("/api/tasks", {
            method: "POST",
            body: {
                store_id: group.store_id,
                task_type: "dedupe_group",
                input: { field: fieldKey, keep_id: keepId, items: fixable },
            },
        });
        const suggestions = await pollDedupeTask(resp.task_id);
        let filled = 0;
        (suggestions || []).forEach((s) => {
            const value = fieldKey === "seo_title" ? s.meta_title : s.meta_description;
            const input = document.getElementById(`dupinput-${fieldKey}-${gi}-${s.id}`);
            if (input && value) {
                input.value = value;
                input.style.borderColor = "var(--success)";
                filled++;
            }
        });
        showToast(
            filled ? `AI filled ${filled} suggestion${filled === 1 ? "" : "s"} — review, then Apply` : "AI returned no changes",
            filled ? "success" : "warning",
        );
    } catch (err) {
        showToast(`AI fix failed: ${err.message}`, "error");
    } finally {
        btn.disabled = false;
        btn.innerHTML = original;
    }
}

function pollDedupeTask(taskId) {
    return new Promise((resolve, reject) => {
        const poll = async () => {
            try {
                const task = await apiRequest(`/api/tasks/${taskId}`);
                if (task.status === "completed") {
                    const result = task.result || {};
                    if (result.error) return reject(new Error(result.error));
                    return resolve(result.suggestions || []);
                }
                if (task.status === "failed") {
                    return reject(new Error(task.error || "Task failed"));
                }
                setTimeout(poll, 2000);
            } catch (err) {
                reject(err);
            }
        };
        poll();
    });
}

async function applyDuplicateFix(fieldKey, itemType, itemId, inputId, btn) {
    const input = document.getElementById(inputId);
    const value = (input?.value || "").trim();
    if (!value) {
        showToast("Value cannot be empty", "error");
        return;
    }

    const applyField = fieldKey === "seo_title" ? "meta_title" : "meta_description";
    const meta = DUP_TYPES[itemType];
    if (!meta) {
        showToast(`Unknown item type: ${itemType}`, "error");
        return;
    }
    const endpoint = meta.endpoint(itemId);

    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = "Applying...";

    try {
        const resp = await apiRequest(endpoint, {
            method: "POST",
            body: { [applyField]: value, change_type: "duplicate_fix" },
        });
        // Response shapes differ per endpoint: {ok:true} (product/collection),
        // {ok,success} (article), {success:true} (page).
        if (resp.ok || resp.success) {
            showToast("Applied to Shopify", "success");
            const row = btn.closest(".dup-item");
            if (row) {
                row.style.opacity = "0.5";
                btn.textContent = "Applied";
            }
        } else {
            btn.disabled = false;
            btn.innerHTML = original;
            showToast(`Failed: ${resp.error || "Unknown error"}`, "error");
        }
    } catch (err) {
        btn.disabled = false;
        btn.innerHTML = original;
        showToast(`Failed: ${err.message}`, "error");
    }
}

document.addEventListener("DOMContentLoaded", async () => {
    initThemeToggle();
    initSidebar();
    await loadStores();
    loadDuplicates();
});
