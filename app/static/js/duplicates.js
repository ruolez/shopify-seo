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
    loadDuplicateCollections();

    try {
        const data = await apiRequest(`/api/duplicates?store_id=${storeId}`);
        duplicateData = { seo_title: data.seo_title || [], meta_description: data.meta_description || [], scanned: data.scanned || {}, similar_bodies: data.similar_bodies || [] };
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

const SIMILAR_OPEN = {
    product: (id) => `/seo?optimize=product&id=${id}`,
    collection: (id) => `/seo?optimize=collection&id=${id}`,
    article: (id) => `/blog?article=${id}`,
};

// Near-identical description text across pages of one type.
function renderSimilarBodies(groups) {
    if (!groups.length) {
        return `<div class="card" style="margin-top:12px;font-size:13px;"><span class="material-symbols-rounded" style="vertical-align:middle;color:var(--success);font-size:18px;">verified</span>
            <strong>Description text:</strong> no two pages share near-identical descriptions. Each page says something of its own.</div>`;
    }
    return `<details class="card" style="margin-top:12px;" open>
        <summary style="cursor:pointer;font-weight:600;">Near-identical descriptions — ${groups.length} group${groups.length === 1 ? "" : "s"}
            <span style="font-weight:400;font-size:13px;color:var(--on-surface-secondary);"> · Pages whose text is almost the same. Google often indexes only one of them. Rewrite each to say what is specific to it.</span></summary>
        ${groups.map((g) => `<div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--outline);">
            <div style="font-size:12px;color:var(--on-surface-secondary);margin-bottom:4px;">${g.count} ${escapeHtml(g.items[0].type)}s · ${g.similarity}% the same</div>
            ${g.items.map((it) => `<div style="display:flex;justify-content:space-between;gap:8px;padding:3px 0;font-size:13px;">
                <span>${escapeHtml(it.title)}</span>
                ${it.id && SIMILAR_OPEN[it.type] ? `<a class="btn btn-secondary btn-sm" href="${SIMILAR_OPEN[it.type](it.id)}">Open</a>` : ""}</div>`).join("")}
        </div>`).join("")}
    </details>`;
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
    <div class="status-filter-bar" style="margin-top:12px;">${filterChips}</div>` + renderSimilarBodies(data.similar_bodies || []);

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
    // Theme/sidebar init is app.js's job — a second call here double-attached
    // the toggle listeners, making each click a no-op.
    await loadStores();
    loadDuplicates();
});


// ─── Collections that duplicate each other ───

const DUP_REASON = {
    same_name: "Same brand name and shared products",
    same_products: "Nearly the same products",
};

async function loadDuplicateCollections() {
    const box = document.getElementById("dup-collections");
    const storeId = getStoreId();
    if (!box || !storeId) return;
    let data;
    try {
        data = await apiRequest(`/api/duplicates/collections?store_id=${storeId}`);
    } catch (err) {
        box.innerHTML = `<div class="card" style="margin-bottom:12px;color:var(--error);">Duplicate collections: ${escapeHtml(err.message)}</div>`;
        return;
    }
    const side = (c, role) => `<div style="flex:1;min-width:220px;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--on-surface-secondary);">${role}</div>
        <a href="/seo?optimize=collection&id=${c.id}" style="font-weight:600;">${escapeHtml(c.title)}</a>
        <div style="font-size:12px;color:var(--on-surface-secondary);">/collections/${escapeHtml(c.handle)} · ${c.products} product${c.products === 1 ? "" : "s"} · ${c.clicks} clicks (90 days)
            · ${c.indexed ? '<span style="color:var(--success);">indexed</span>' : escapeHtml(c.coverage_state || "index state unknown")}${c.generated ? " · made by this app" : ""}</div></div>`;
    const pairs = data.pairs.map((p, i) => `<div style="padding:10px 0;border-top:1px solid var(--outline);" data-pair="${i}">
        <div style="font-size:12px;color:var(--on-surface-secondary);margin-bottom:6px;">${DUP_REASON[p.reason] || p.reason} · ${p.shared} shared product${p.shared === 1 ? "" : "s"}</div>
        <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start;">
            ${side(p.keep, "Keep")}${side(p.drop, "Merge into it")}
            <button class="btn btn-primary btn-sm" onclick="mergeDuplicateCollections(${p.keep.id}, ${p.drop.id}, ${p.only_in_drop}, ${p.can_add_products}, this)">Merge</button>
        </div></div>`).join("");
    const merges = data.merges.map((m) => `<div style="display:flex;justify-content:space-between;gap:8px;padding:4px 0;font-size:12.5px;">
        <span>/collections/${escapeHtml(m.drop_handle)} → <a href="/seo?optimize=collection&id=${m.keep_id}">${escapeHtml(m.keep_title || m.keep_handle)}</a>
            <span style="color:var(--on-surface-secondary);">· ${formatRelativeTime(m.created_at)}${m.added ? ` · ${m.added} products added` : ""}</span></span>
        ${m.undone_at ? '<span style="font-size:12px;">Undone</span>' : `<button class="btn btn-secondary btn-sm" style="padding:1px 8px;font-size:11px;" onclick="undoCollectionMerge(${m.id}, this)">Undo</button>`}</div>`).join("");
    box.innerHTML = `<details class="card" style="margin-bottom:12px;" ${data.pairs.length ? "open" : ""}>
        <summary style="cursor:pointer;font-weight:600;">Collections that duplicate each other — ${data.pairs.length}
            <span style="font-weight:400;font-size:13px;color:var(--on-surface-secondary);"> · Google indexes one of each pair and splits links and clicks between them. A merge keeps one (the one Google indexes, else the one with more clicks), adds the other's missing products to it, hides the other from the Online Store and redirects its address. Undo publishes it again.</span></summary>
        ${pairs || '<p style="font-size:13px;margin:8px 0 0;">No duplicate collections found.</p>'}
        ${merges ? `<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--outline);"><div style="font-weight:600;font-size:13px;">Merged</div>${merges}</div>` : ""}
    </details>`;
}

async function mergeDuplicateCollections(keepId, dropId, onlyInDrop, canAdd, btn) {
    const extra = onlyInDrop
        ? (canAdd ? ` ${onlyInDrop} product${onlyInDrop === 1 ? "" : "s"} only in the merged collection will be added to the kept one.`
                  : ` The kept collection picks its products by rules, so the ${onlyInDrop} product${onlyInDrop === 1 ? "" : "s"} only in the other one won't be added.`)
        : "";
    const ok = await UIConfirm.ask({ title: "Merge these collections?", message: `The second collection is hidden from your Online Store and its address redirects to the first.${extra} You can undo this.`, confirmLabel: "Merge" });
    if (!ok) return;
    btn.disabled = true;
    try {
        const r = await apiRequest("/api/duplicates/collections/merge", { method: "POST", body: JSON.stringify({ store_id: getStoreId(), keep_id: keepId, drop_id: dropId }) });
        showToast(`Merged${r.added_products ? `, ${r.added_products} products added` : ""}; redirect ${r.redirect}`, "success");
        loadDuplicateCollections();
    } catch (err) {
        btn.disabled = false;
        showToast(err.message, "error");
    }
}

async function undoCollectionMerge(mergeId, btn) {
    btn.disabled = true;
    try {
        await apiRequest(`/api/duplicates/collections/merges/${mergeId}/undo`, { method: "POST" });
        showToast("Merge undone: the collection is published again and its redirect removed", "success");
        loadDuplicateCollections();
    } catch (err) {
        btn.disabled = false;
        showToast(err.message, "error");
    }
}
