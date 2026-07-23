function sanitizeAiHtml(html) {
    if (!html) return "";
    return html
        .replace(/<\s*(script|style)\b[^>]*>[\s\S]*?<\s*\/\s*\1\s*>/gi, "")
        .replace(/\s+on[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, "")
        .replace(/\s+(href|src)\s*=\s*(["']?)\s*javascript:[^"'\s>]*\2/gi, "");
}

let scanType = loadFilter("aiSeoScanType", "product");
let currentItems = [];
let scanPollTimer = null;
let selectedIds = new Set();
let batchPollTimer = null;
let sortBy = loadFilter("aiSeoSortBy", "weakest");
let sortDir = loadFilter("aiSeoSortDir", "asc");
let lastCheckedId = null;

const COMPONENT_LABELS = {
    structured_data: { label: "Structured data", icon: "data_object" },
    extractability: { label: "Extractability", icon: "quiz" },
    fact_density: { label: "Fact density", icon: "insights" },
    crawlability: { label: "AI crawlability", icon: "smart_toy" },
    entity_authority: { label: "Entity authority", icon: "verified" },
    freshness: { label: "Freshness", icon: "schedule" },
};

function getStoreId() {
    const sel = document.getElementById("store-select");
    return sel ? sel.value : "";
}

function setScanType(type) {
    scanType = type;
    saveFilter("aiSeoScanType", type);
    document.querySelectorAll(".filter-chip[data-type]").forEach((c) => {
        c.classList.toggle("active", c.dataset.type === type);
    });
    loadItems();
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
        sel.addEventListener("change", () => {
            visibilityLoaded = false;
            feedLoaded = false;
            offsiteLoaded = false;
            loadOverview();
            loadItems();
            const active = document.querySelector(".tab.active")?.dataset.tab;
            if (["visibility", "feed", "offsite"].includes(active)) switchTab(active);
        });
    } catch (err) {
        showToast(`Failed to load stores: ${err.message}`, "error");
    }
}

async function loadOverview() {
    const storeId = getStoreId();
    const container = document.getElementById("overview-cards");
    if (!storeId) {
        container.innerHTML = "";
        return;
    }
    try {
        const o = await apiRequest(`/api/ai-seo/overview?store_id=${storeId}`);
        container.innerHTML = `
            <div class="stat-card">
                ${renderScoreRing(o.avg_score || 0, 56)}
                <div class="stat-label" style="margin-top:8px;">Avg AI readiness</div>
                <div class="stat-label" style="color:var(--on-surface-secondary);">${o.scanned_count} items scanned</div>
            </div>
            <div class="stat-card">
                ${renderScoreRing(o.crawlability.score, 56)}
                <div class="stat-label" style="margin-top:8px;">AI crawlability</div>
                <div class="stat-label" style="color:var(--on-surface-secondary);">${escapeHtml(o.crawlability.detail)}</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon ${o.llms_txt ? "green" : "orange"}">
                    <span class="material-symbols-rounded">${o.llms_txt ? "check_circle" : "cancel"}</span>
                </div>
                <div class="stat-value" style="font-size:20px;">llms.txt</div>
                <div class="stat-label">${o.llms_txt ? "Published" : "Not found at site root"}</div>
            </div>`;
    } catch (err) {
        container.innerHTML = "";
    }
}

async function loadItems() {
    const storeId = getStoreId();
    const container = document.getElementById("items-container");
    if (!storeId) return;
    try {
        currentItems = await apiRequest(`/api/ai-seo/items?store_id=${storeId}&type=${scanType}`);
        selectedIds.clear();
        lastCheckedId = null;
        renderItems();
    } catch (err) {
        showToast(`Failed to load items: ${err.message}`, "error");
    }
}

function renderItems() {
    const container = document.getElementById("items-container");
    if (!currentItems.length) {
        container.innerHTML = `<div class="empty-state">
            <span class="material-symbols-rounded">radar</span>
            <p>No scores yet for ${scanType}s. Run a scan to generate AI-readiness scores.</p>
        </div>`;
        return;
    }
    const weakestScore = (it) => {
        const w = weakestComponent(it.breakdown);
        return w ? w.score : 0;
    };
    const signalScore = (it) => {
        if (sortBy === "overall") return it.score;
        if (sortBy === "weakest") return weakestScore(it);
        return it.breakdown?.[sortBy]?.score ?? 0;
    };
    // Tiebreak by overall score so ties (e.g. many items whose weakest signal is 0)
    // still order meaningfully instead of looking unsorted.
    currentItems.sort((a, b) => {
        let d = signalScore(a) - signalScore(b);
        if (d === 0) d = a.score - b.score;
        return sortDir === "asc" ? d : -d;
    });

    const label = scanType === "collection" ? "Collection" : "Product";
    const showsWeakest = sortBy === "overall" || sortBy === "weakest";
    const signalLabel = showsWeakest ? "Weakest signal" : COMPONENT_LABELS[sortBy].label;
    const allSelected = currentItems.every((it) => selectedIds.has(it.resource_id));
    const rows = currentItems
        .map((it, i) => {
            const checked = selectedIds.has(it.resource_id) ? "checked" : "";
            let signalCell;
            if (showsWeakest) {
                const worst = weakestComponent(it.breakdown);
                signalCell = worst ? `<span class="badge badge-warning">${COMPONENT_LABELS[worst.key].label}: ${worst.score}</span>` : "";
            } else {
                signalCell = getScoreBadge(it.breakdown?.[sortBy]?.score ?? 0);
            }
            return `<tr onclick="openBreakdown(${i})" style="cursor:pointer;">
                <td onclick="event.stopPropagation()" style="width:36px;text-align:center;"><input type="checkbox" ${checked} onclick="handleCheckboxClick(event, ${i})"></td>
                <td style="width:60px;">${renderScoreRing(it.score, 44)}</td>
                <td><div style="font-weight:600;">${escapeHtml(it.title || it.handle || "—")}</div></td>
                <td>${signalCell}</td>
                <td style="color:var(--on-surface-secondary);font-size:12px;">${formatRelativeTime(it.scanned_at)}</td>
            </tr>`;
        })
        .join("");

    const sortOptions = [
        `<option value="weakest" ${sortBy === "weakest" ? "selected" : ""}>Weakest signal</option>`,
        `<option value="overall" ${sortBy === "overall" ? "selected" : ""}>Overall score</option>`,
    ]
        .concat(Object.entries(COMPONENT_LABELS).map(([k, v]) => `<option value="${k}" ${sortBy === k ? "selected" : ""}>${v.label}</option>`))
        .join("");
    container.innerHTML = `
        <div class="toolbar-row" style="align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap;">
            <label style="font-size:13px;color:var(--on-surface-secondary);">Sort by</label>
            <select class="form-control" style="max-width:170px;padding:4px 8px;" onchange="setSortBy(this.value)">${sortOptions}</select>
            <button class="btn btn-secondary btn-sm" title="Toggle direction" onclick="toggleSortDir()">
                <span class="material-symbols-rounded">${sortDir === "asc" ? "arrow_upward" : "arrow_downward"}</span> ${sortDir === "asc" ? "Worst first" : "Best first"}
            </button>
            <span id="sel-count" style="font-size:13px;color:var(--on-surface-secondary);margin-left:8px;">${selectedIds.size} selected</span>
            <button id="batch-selected-btn" class="btn btn-primary btn-sm" ${selectedIds.size ? "" : "disabled"} onclick="batchOptimize('selected')">
                <span class="material-symbols-rounded">auto_fix_high</span> Optimize selected
            </button>
            <button class="btn btn-secondary btn-sm" onclick="batchOptimize('all')">
                <span class="material-symbols-rounded">bolt</span> Optimize all (${currentItems.length})
            </button>
            <button class="btn btn-secondary btn-sm" onclick="boostFactsBatch()" title="Research-driven fact-density pass; only changes items where verifiable facts raise the score">
                <span class="material-symbols-rounded">insights</span> Boost fact density
            </button>
            <div id="batch-progress" style="margin-left:auto;font-size:13px;color:var(--on-surface-secondary);"></div>
        </div>
        <p style="font-size:11px;color:var(--on-surface-secondary);margin:0 0 8px;">Tip: click a checkbox, then Shift-click another to select the whole range.</p>
        <div class="table-container"><table>
            <thead><tr>
                <th style="width:36px;text-align:center;"><input type="checkbox" ${allSelected ? "checked" : ""} onchange="toggleSelectAll(this.checked)"></th>
                <th>Score</th><th>${label}</th><th>${signalLabel}</th><th>Scanned</th>
            </tr></thead>
            <tbody>${rows}</tbody>
        </table></div>`;
}

function setSortBy(value) {
    sortBy = value;
    saveFilter("aiSeoSortBy", value);
    renderItems();
}

function toggleSortDir() {
    sortDir = sortDir === "asc" ? "desc" : "asc";
    saveFilter("aiSeoSortDir", sortDir);
    renderItems();
}

function updateSelCount() {
    const el = document.getElementById("sel-count");
    if (el) el.textContent = `${selectedIds.size} selected`;
    const btn = document.getElementById("batch-selected-btn");
    if (btn) btn.disabled = selectedIds.size === 0;
}

function handleCheckboxClick(event, index) {
    const id = currentItems[index].resource_id;
    const checked = event.target.checked;
    if (event.shiftKey && lastCheckedId != null) {
        const lastIdx = currentItems.findIndex((x) => x.resource_id === lastCheckedId);
        if (lastIdx !== -1) {
            const lo = Math.min(lastIdx, index);
            const hi = Math.max(lastIdx, index);
            for (let i = lo; i <= hi; i++) {
                const rid = currentItems[i].resource_id;
                if (checked) selectedIds.add(rid);
                else selectedIds.delete(rid);
            }
            lastCheckedId = id;
            renderItems();
            return;
        }
    }
    if (checked) selectedIds.add(id);
    else selectedIds.delete(id);
    lastCheckedId = id;
    updateSelCount();
}

function toggleSelectAll(checked) {
    if (checked) currentItems.forEach((it) => selectedIds.add(it.resource_id));
    else selectedIds.clear();
    renderItems();
}

async function batchOptimize(mode) {
    const storeId = getStoreId();
    const ids = mode === "all" ? "all" : [...selectedIds];
    const count = mode === "all" ? currentItems.length : ids.length;
    if (!count) {
        showToast("Nothing selected", "warning");
        return;
    }
    if (!confirm(`Optimize ${count} ${scanType}(s)? This generates GEO content and applies it live to Shopify.`)) return;
    const prog = document.getElementById("batch-progress");
    if (prog) prog.textContent = "Starting…";
    try {
        const res = await apiRequest("/api/ai-seo/optimize/batch", {
            method: "POST",
            body: { store_id: parseInt(storeId), type: scanType, ids },
        });
        pollBatch(res.batch_id);
    } catch (err) {
        showToast(`Batch failed to start: ${err.message}`, "error");
        if (prog) prog.textContent = "";
    }
}

async function boostFactsBatch() {
    const storeId = getStoreId();
    let ids, label;
    if (selectedIds.size) {
        ids = [...selectedIds];
        label = `${ids.length} selected ${scanType}(s)`;
    } else {
        ids = "low";
        label = `all low-fact-density ${scanType}s (score < 50)`;
    }
    if (!confirm(`Boost fact density on ${label}? It researches verifiable facts and only changes items where the fact-density score actually rises — everything else is left alone. Applies live to Shopify.`)) return;
    const prog = document.getElementById("batch-progress");
    if (prog) prog.textContent = "Starting…";
    try {
        const res = await apiRequest("/api/ai-seo/facts/batch", {
            method: "POST",
            body: { store_id: parseInt(storeId), type: scanType, ids },
        });
        pollBatch(res.batch_id);
    } catch (err) {
        showToast(`Fact boost failed to start: ${err.message}`, "error");
        if (prog) prog.textContent = "";
    }
}

function pollBatch(batchId) {
    if (batchPollTimer) clearInterval(batchPollTimer);
    let consecutiveErrors = 0;
    batchPollTimer = setInterval(async () => {
        try {
            const s = await apiRequest(`/api/ai-seo/optimize/batch/${batchId}`);
            consecutiveErrors = 0;
            const prog = document.getElementById("batch-progress");
            if (prog) prog.textContent = `Optimizing ${s.completed + s.failed}/${s.total}${s.current ? " · " + s.current : ""}`;
            if (s.status === "completed" || s.status === "failed") {
                clearInterval(batchPollTimer);
                showToast(`Batch done — ${s.completed} optimized, ${s.failed} failed`, s.failed ? "warning" : "success");
                selectedIds.clear();
                loadItems();
                loadOverview();
            }
        } catch (err) {
            consecutiveErrors++;
            if (consecutiveErrors >= 5) {
                clearInterval(batchPollTimer);
                const prog = document.getElementById("batch-progress");
                if (prog) prog.textContent = "";
                showToast(`Lost contact with the batch (${err.message}). It may still be running — reload to check.`, "error");
            }
        }
    }, 2500);
}

function weakestComponent(breakdown) {
    if (!breakdown) return null;
    let worst = null;
    for (const [key, v] of Object.entries(breakdown)) {
        if (!COMPONENT_LABELS[key]) continue;
        if (!worst || v.score < worst.score) worst = { key, score: v.score };
    }
    return worst;
}

let currentOptimizeItem = null;

function openBreakdown(index) {
    const it = currentItems[index];
    if (!it) return;
    currentOptimizeItem = it;
    document.getElementById("breakdown-title").textContent = it.title || it.handle || "AI Readiness";
    const rows = Object.entries(it.breakdown || {})
        .map(([key, v]) => {
            const meta = COMPONENT_LABELS[key] || { label: key, icon: "circle" };
            return `<div class="breakdown-row" style="display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--outline);">
                <span class="material-symbols-rounded" style="color:var(--primary);">${meta.icon}</span>
                <div style="flex:1;">
                    <div style="font-weight:600;">${meta.label}</div>
                    <div style="font-size:13px;color:var(--on-surface-secondary);">${escapeHtml(v.detail)}</div>
                </div>
                ${getScoreBadge(v.score)}
            </div>`;
        })
        .join("");
    document.getElementById("breakdown-content").innerHTML = `
        <div style="display:flex;align-items:center;gap:16px;margin-bottom:16px;">
            ${renderScoreRing(it.score, 72)}
            <div style="flex:1;">
                <div style="font-size:15px;font-weight:600;">Overall AI readiness</div>
                <div style="font-size:13px;color:var(--on-surface-secondary);">Higher = more likely to be cited by AI answer engines</div>
            </div>
            <button class="btn btn-secondary" onclick="boostFactsPreview()" title="Research-driven pass that adds verifiable facts (or leaves the item alone)">
                <span class="material-symbols-rounded">insights</span> Boost fact density
            </button>
            <button class="btn btn-primary" onclick="optimizePreview()">
                <span class="material-symbols-rounded">auto_fix_high</span> Optimize for AI
            </button>
        </div>
        ${rows}
        <div id="optimize-result"></div>`;
    document.getElementById("breakdown-modal").classList.add("active");
}

async function optimizePreview() {
    const it = currentOptimizeItem;
    if (!it) return;
    const storeId = getStoreId();
    const box = document.getElementById("optimize-result");
    box.innerHTML = `<div class="loading-spinner" style="padding:24px 0;"><div class="spinner"></div><p>Generating GEO-optimized content…</p></div>`;
    try {
        const res = await apiRequest("/api/ai-seo/optimize/preview", {
            method: "POST",
            body: { store_id: parseInt(storeId), type: scanType, id: it.resource_id },
        });
        renderOptimizePreview(res);
    } catch (err) {
        box.innerHTML = `<div class="empty-state"><span class="material-symbols-rounded">error</span><p>${escapeHtml(err.message)}</p></div>`;
    }
}

async function boostFactsPreview() {
    const it = currentOptimizeItem;
    if (!it) return;
    const box = document.getElementById("optimize-result");
    box.innerHTML = `<div class="loading-spinner" style="padding:24px 0;"><div class="spinner"></div><p>Researching verifiable facts…</p></div>`;
    try {
        const res = await apiRequest("/api/ai-seo/facts/preview", {
            method: "POST",
            body: { store_id: parseInt(getStoreId()), type: scanType, id: it.resource_id },
        });
        if (res.no_change) {
            box.innerHTML = `<div style="border-top:2px solid var(--outline);margin-top:20px;padding-top:16px;">
                <div style="font-size:14px;"><span class="material-symbols-rounded" style="vertical-align:middle;color:var(--warning);">info</span>
                No verifiable, relevant facts found — left unchanged.</div>
                <div style="font-size:13px;color:var(--on-surface-secondary);margin-top:4px;">${escapeHtml(res.reason || "")}</div></div>`;
            return;
        }
        renderFactsPreview(res);
    } catch (err) {
        box.innerHTML = `<div class="empty-state"><span class="material-symbols-rounded">error</span><p>${escapeHtml(err.message)}</p></div>`;
    }
}

function renderFactsPreview(res) {
    const p = res.preview || {};
    const gain = res.after_fact_density - res.before_fact_density;
    const col = gain > 0 ? "var(--success)" : "var(--on-surface-secondary)";
    const facts = (p.facts_added || []).map((f) => `<li style="font-size:13px;">${escapeHtml(f)}</li>`).join("");
    const sources = (p.sources || []).map((s) => escapeHtml(s)).join(", ");
    window._factsPayload = { store_id: parseInt(getStoreId()), type: scanType, id: res.item.id, description_html: p.optimized_description_html };
    document.getElementById("optimize-result").innerHTML = `
        <div style="border-top:2px solid var(--outline);margin-top:20px;padding-top:16px;">
            <div style="display:flex;align-items:center;gap:16px;margin-bottom:12px;">
                <div style="font-size:14px;">Fact density: <b>${res.before_fact_density}</b> →
                    <b style="color:${col};">${res.after_fact_density}</b> <span style="color:${col};">(${gain >= 0 ? "+" : ""}${gain})</span></div>
                <button class="btn btn-primary" style="margin-left:auto;" ${gain > 0 ? "" : "disabled title='No measurable gain'"} onclick="applyFacts(this)">
                    <span class="material-symbols-rounded">cloud_upload</span> Apply to Shopify
                </button>
            </div>
            <h4 style="margin:8px 0 6px;">Facts added</h4>
            <ul style="margin:0 0 8px;padding-left:20px;">${facts || '<li style="color:var(--on-surface-secondary);">—</li>'}</ul>
            ${sources ? `<div style="font-size:12px;color:var(--on-surface-secondary);">Sources: ${sources}</div>` : ""}
            <h4 style="margin:12px 0 6px;">Enriched description</h4>
            <div style="border:1px solid var(--outline);border-radius:8px;padding:12px;max-height:320px;overflow:auto;">${sanitizeAiHtml(p.optimized_description_html || "")}</div>
        </div>`;
}

async function applyFacts(btn) {
    if (!window._factsPayload) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded">hourglass_empty</span> Applying…';
    try {
        const res = await apiRequest("/api/ai-seo/facts/apply", { method: "POST", body: window._factsPayload });
        showToast(`Applied — new score ${res.new_score}`, "success");
        closeBreakdown();
        loadItems();
        loadOverview();
    } catch (err) {
        showToast(`Apply failed: ${err.message}`, "error");
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded">cloud_upload</span> Apply to Shopify';
    }
}

function renderOptimizePreview(res) {
    const p = res.preview || {};
    const delta = res.after_score - res.before_score;
    const deltaColor = delta >= 0 ? "var(--success)" : "var(--error)";
    const faqHtml = (p.faq || [])
        .map((f) => `<div style="margin-bottom:10px;"><div style="font-weight:600;">${escapeHtml(f.question)}</div><div style="font-size:13px;color:var(--on-surface-secondary);">${escapeHtml(f.answer)}</div></div>`)
        .join("");
    const edits = (p.geo_edits_applied || []).map((e) => `<span class="badge badge-success" style="margin:2px;">${escapeHtml(e)}</span>`).join(" ");

    const targeted = Object.keys(COMPONENT_LABELS)
        .map((k) => ({ k, before: res.before_breakdown?.[k]?.score, after: res.after_breakdown?.[k]?.score }))
        .filter((x) => x.before != null && x.after != null && x.after !== x.before);
    const signalsHtml = targeted.length
        ? `<div style="font-weight:600;font-size:13px;margin:4px 0 6px;">Signals this improves</div>
           ${targeted
               .map((x) => {
                   const up = x.after > x.before;
                   const col = up ? "var(--success)" : "var(--error)";
                   return `<div style="display:flex;align-items:center;gap:8px;font-size:13px;padding:3px 0;">
                        <span style="flex:1;">${COMPONENT_LABELS[x.k].label}</span>
                        <span style="color:var(--on-surface-secondary);">${x.before}</span>
                        <span class="material-symbols-rounded" style="font-size:16px;color:${col};">${up ? "arrow_upward" : "arrow_downward"}</span>
                        <span style="color:${col};font-weight:600;">${x.after}</span>
                    </div>`;
               })
               .join("")}`
        : "";

    const pk = p.preserved_keywords || [];
    const dk = p.dropped_keywords || [];
    let kwHtml = "";
    if (pk.length || dk.length) {
        kwHtml = `<div style="margin:4px 0 12px;">
            <div style="font-weight:600;font-size:13px;margin-bottom:4px;">Keyword preservation</div>
            ${pk.length ? `<div style="font-size:13px;color:var(--success);"><span class="material-symbols-rounded" style="font-size:15px;vertical-align:middle;">check_circle</span> ${pk.length} existing keyword${pk.length > 1 ? "s" : ""} kept</div>` : ""}
            ${dk.length ? `<div style="font-size:13px;color:var(--error);margin-top:4px;"><span class="material-symbols-rounded" style="font-size:15px;vertical-align:middle;">warning</span> Missing from new content — review before applying: ${dk.map(escapeHtml).join(", ")}</div>` : ""}
        </div>`;
    }

    window._optimizePayload = {
        store_id: parseInt(getStoreId()),
        type: scanType,
        id: res.item.id,
        description_html: p.optimized_description_html,
        meta_title: p.meta_title,
        meta_description: p.meta_description,
        faqpage_jsonld: p.faqpage_jsonld,
    };

    document.getElementById("optimize-result").innerHTML = `
        <div style="border-top:2px solid var(--outline);margin-top:20px;padding-top:16px;">
            <div style="display:flex;align-items:center;gap:16px;margin-bottom:16px;">
                <div style="font-size:14px;">Projected: <b>${res.before_score}</b> →
                    <b style="color:${deltaColor};">${res.after_score}</b>
                    <span style="color:${deltaColor};">(${delta >= 0 ? "+" : ""}${delta})</span></div>
                <button class="btn btn-primary" style="margin-left:auto;" onclick="applyOptimization(this)">
                    <span class="material-symbols-rounded">cloud_upload</span> Apply to Shopify
                </button>
            </div>
            <div style="margin-bottom:12px;">${signalsHtml}</div>
            ${kwHtml}
            <div style="font-weight:600;font-size:13px;margin:4px 0 6px;">GEO levers applied</div>
            <div style="margin-bottom:12px;">${edits || '<span style="color:var(--on-surface-secondary);font-size:13px;">—</span>'}</div>
            <h4 style="margin:12px 0 6px;">Meta title</h4>
            <div style="font-size:13px;">${escapeHtml(p.meta_title || "")}</div>
            <h4 style="margin:12px 0 6px;">Meta description</h4>
            <div style="font-size:13px;">${escapeHtml(p.meta_description || "")}</div>
            ${p.expert_verdict ? `<h4 style="margin:12px 0 6px;">Expert Verdict</h4>
                <div style="font-size:13px;font-style:italic;border-left:3px solid var(--primary);padding-left:10px;">${escapeHtml(p.expert_verdict)}</div>` : ""}
            ${p.product_facts && Object.keys(p.product_facts).length ? `<div style="font-size:12px;color:var(--on-surface-secondary);margin-top:8px;"><b>Specifications table sourced from your data:</b> ${Object.keys(p.product_facts).map(escapeHtml).join(", ")}</div>` : ""}
            <h4 style="margin:12px 0 6px;">Optimized description</h4>
            <div style="border:1px solid var(--outline);border-radius:8px;padding:12px;max-height:340px;overflow:auto;">${sanitizeAiHtml(p.optimized_description_html || "")}</div>
            <h4 style="margin:16px 0 6px;">FAQ (FAQPage schema)</h4>
            ${faqHtml || '<div style="color:var(--on-surface-secondary);">No FAQ generated</div>'}
            <div style="margin-top:14px;padding:8px 12px;background:var(--surface-dim);border-radius:8px;font-size:12px;color:var(--on-surface-secondary);">
                <span class="material-symbols-rounded" style="font-size:15px;vertical-align:middle;color:var(--primary);">data_object</span>
                On apply, structured data (JSON-LD) is also pushed: <b>FAQPage</b> + enriched
                <b>${scanType === "collection" ? "CollectionPage" : "Product"}</b> schema
                ${scanType === "collection" ? "" : "(brand, offers, price, availability, GTIN/MPN, condition)"} — the structured-data signal AI engines reward.
            </div>
        </div>`;
}

async function applyOptimization(btn) {
    if (!window._optimizePayload) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded">hourglass_empty</span> Applying…';
    try {
        const res = await apiRequest("/api/ai-seo/optimize/apply", {
            method: "POST",
            body: window._optimizePayload,
        });
        // Update the item in place so the score reflects instantly — no full rescan needed.
        const idx = currentItems.findIndex((x) => x.resource_id === window._optimizePayload.id);
        if (idx >= 0) {
            currentItems[idx].score = res.new_score;
            currentItems[idx].breakdown = res.breakdown;
            currentItems[idx].scanned_at = new Date().toISOString();
            renderItems();
        }
        showToast(`Applied — new score ${res.new_score}`, "success");
        closeBreakdown();
        loadOverview();
    } catch (err) {
        showToast(`Apply failed: ${err.message}`, "error");
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded">cloud_upload</span> Apply to Shopify';
    }
}

function closeBreakdown() {
    document.getElementById("breakdown-modal").classList.remove("active");
}

async function runScan() {
    const storeId = getStoreId();
    if (!storeId) {
        showToast("Select a store first", "warning");
        return;
    }
    const btn = document.getElementById("scan-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded">hourglass_empty</span> Scanning…';
    try {
        const res = await apiRequest("/api/ai-seo/scan", {
            method: "POST",
            body: { store_id: parseInt(storeId), type: scanType },
        });
        pollScan(res.task_id);
    } catch (err) {
        showToast(`Scan failed to start: ${err.message}`, "error");
        resetScanBtn();
    }
}

function pollScan(taskId) {
    if (scanPollTimer) clearInterval(scanPollTimer);
    let consecutiveErrors = 0;
    scanPollTimer = setInterval(async () => {
        try {
            const task = await apiRequest(`/api/ai-seo/scan/${taskId}`);
            consecutiveErrors = 0;
            if (task.status === "completed") {
                clearInterval(scanPollTimer);
                resetScanBtn();
                showToast(`Scan complete — ${task.result?.scanned ?? 0} items scored`, "success");
                loadOverview();
                loadItems();
            } else if (task.status === "failed") {
                clearInterval(scanPollTimer);
                resetScanBtn();
                showToast(`Scan failed: ${task.error || "unknown error"}`, "error");
            }
        } catch (err) {
            consecutiveErrors++;
            if (consecutiveErrors >= 5) {
                clearInterval(scanPollTimer);
                resetScanBtn();
                showToast(`Lost contact with the scan (${err.message}). It may still be running — reload to check.`, "error");
            }
        }
    }, 2000);
}

function resetScanBtn() {
    const btn = document.getElementById("scan-btn");
    btn.disabled = false;
    btn.innerHTML = '<span class="material-symbols-rounded">radar</span> Scan AI readiness';
}

// ── Tab switching ───────────────────────────────────────────────
let visibilityLoaded = false;
let feedLoaded = false;
let offsiteLoaded = false;

function switchTab(tab) {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
    if (tab === "visibility" && !visibilityLoaded) {
        visibilityLoaded = true;
        loadPrompts();
        loadRuns();
    }
    if (tab === "feed" && !feedLoaded) {
        feedLoaded = true;
        loadFeedOverview();
        loadFeedItems();
    }
    if (tab === "offsite" && !offsiteLoaded) {
        offsiteLoaded = true;
        loadOffsite();
    }
    if (tab === "assets") loadAeo();
    if (tab === "activity") loadActivity();
    else if (activityTimer) { clearInterval(activityTimer); activityTimer = null; }
}

// ── Feed readiness tab ──────────────────────────────────────────
const FEED_COMPONENT_LABELS = {
    gtin: { label: "GTIN / barcode", icon: "barcode" },
    sku: { label: "SKU", icon: "tag" },
    images: { label: "Images & alt", icon: "image" },
    product_type: { label: "Category", icon: "category" },
    title: { label: "Title quality", icon: "title" },
    description: { label: "Description", icon: "notes" },
    price: { label: "Price", icon: "attach_money" },
    availability: { label: "Availability", icon: "inventory" },
    brand: { label: "Brand", icon: "verified" },
    review_signals: { label: "Review data", icon: "reviews" },
};
let feedItemsCache = [];

async function runFeedScan() {
    const storeId = getStoreId();
    if (!storeId) { showToast("Select a store first", "warning"); return; }
    const btn = document.getElementById("feed-scan-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded">hourglass_empty</span> Auditing…';
    const reset = () => { btn.disabled = false; btn.innerHTML = '<span class="material-symbols-rounded">inventory_2</span> Audit feed'; };
    try {
        const res = await apiRequest("/api/ai-seo/feed/scan", { method: "POST", body: { store_id: parseInt(storeId) } });
        let errors = 0;
        const timer = setInterval(async () => {
            try {
                const task = await apiRequest(`/api/ai-seo/scan/${res.task_id}`);
                errors = 0;
                if (task.status === "completed") {
                    clearInterval(timer); reset();
                    showToast(`Feed audit complete — ${task.result?.scanned ?? 0} products scored`, "success");
                    loadFeedOverview(); loadFeedItems();
                } else if (task.status === "failed") {
                    clearInterval(timer); reset();
                    showToast(`Feed audit failed: ${task.error || "unknown error"}`, "error");
                }
            } catch (err) {
                if (++errors >= 5) { clearInterval(timer); reset(); showToast("Lost contact with the audit — reload to check.", "error"); }
            }
        }, 2000);
    } catch (err) {
        reset();
        showToast(`Audit failed to start: ${err.message}`, "error");
    }
}

async function loadFeedOverview() {
    const storeId = getStoreId();
    const cards = document.getElementById("feed-overview-cards");
    const coverage = document.getElementById("feed-coverage");
    const checklist = document.getElementById("feed-checklist");
    if (!storeId) return;
    try {
        const o = await apiRequest(`/api/ai-seo/feed/overview?store_id=${storeId}`);
        if (!o.scanned_count) {
            cards.innerHTML = "";
            coverage.innerHTML = "";
            checklist.innerHTML = feedChecklistCard(o);
            return;
        }
        const cov = o.coverage || {};
        const pct = (c) => (o.scanned_count ? Math.round(((cov[c] || {}).full_count || 0) / o.scanned_count * 100) : 0);
        cards.innerHTML = `
            <div class="stat-card">${renderScoreRing(o.avg_score || 0, 56)}
                <div class="stat-label" style="margin-top:8px;">Avg feed readiness</div>
                <div class="stat-label" style="color:var(--on-surface-secondary);">${o.scanned_count} products audited</div></div>
            <div class="stat-card"><div class="stat-value">${pct("gtin")}%</div>
                <div class="stat-label">GTIN coverage</div>
                <div class="stat-label" style="color:var(--on-surface-secondary);">products with barcodes on every variant</div></div>
            <div class="stat-card"><div class="stat-value">${pct("images")}%</div>
                <div class="stat-label">Image + alt coverage</div>
                <div class="stat-label" style="color:var(--on-surface-secondary);">products fully imaged with alt text</div></div>
            <div class="stat-card"><div class="stat-value">${pct("review_signals")}%</div>
                <div class="stat-label">With review data</div>
                <div class="stat-label" style="color:var(--on-surface-secondary);">aggregateRating detected</div></div>`;
        const chips = Object.keys(FEED_COMPONENT_LABELS)
            .map((c) => {
                const avg = (cov[c] || {}).avg;
                const color = avg == null ? "var(--on-surface-secondary)" : avg >= 80 ? "var(--success)" : avg >= 50 ? "var(--warning,#f9ab00)" : "var(--error)";
                return `<div style="display:flex;align-items:center;gap:6px;font-size:12.5px;">
                    <span class="material-symbols-rounded" style="font-size:16px;color:${color};">${FEED_COMPONENT_LABELS[c].icon}</span>
                    ${FEED_COMPONENT_LABELS[c].label}: <b style="color:${color};">${avg == null ? "—" : avg}</b></div>`;
            })
            .join("");
        coverage.innerHTML = `<div class="card" style="padding:12px 16px;display:flex;gap:18px;flex-wrap:wrap;">${chips}</div>`;
        checklist.innerHTML = feedChecklistCard(o);
    } catch (err) {
        cards.innerHTML = "";
    }
}

function feedChecklistCard(o) {
    const items = (o.checklist || [])
        .map((c) => `<li style="margin-bottom:6px;"><a href="${escapeHtml(c.url)}" target="_blank"><b>${escapeHtml(c.label)}</b></a><br>
            <span style="color:var(--on-surface-secondary);">${escapeHtml(c.detail)}</span></li>`)
        .join("");
    const unsynced = (o.unsynced || [])
        .map((u) => `<li style="margin-bottom:4px;"><b>${escapeHtml(u.label)}</b> (${escapeHtml(u.status)}) — <span style="color:var(--on-surface-secondary);">${escapeHtml(u.why)}</span></li>`)
        .join("");
    const noReviews = o.scanned_count && !((o.coverage || {}).review_signals || {}).full_count;
    const reviewCard = noReviews
        ? `<div style="display:flex;gap:10px;align-items:flex-start;background:var(--warning-light);border:1px solid var(--outline);border-radius:var(--radius-sm);padding:10px 12px;margin-bottom:12px;">
            <span class="material-symbols-rounded" style="color:var(--warning);font-size:20px;">reviews</span>
            <div style="font-size:12.5px;line-height:1.5;">No review data detected on any product. AI shopping ranks products with review stats higher — install a review app that writes <code>aggregateRating</code> JSON-LD (Judge.me, Loox, Okendo), then re-audit.</div></div>`
        : "";
    return `${reviewCard}<details class="card" style="padding:0;">
        <summary style="padding:14px 16px;cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;">
            <span class="material-symbols-rounded" style="color:var(--on-surface-secondary);">checklist</span>
            <b style="flex:1;">Agentic Storefront eligibility checklist</b>
            <span style="font-size:12px;color:var(--on-surface-secondary);">Shopify admin settings this app can't read — verify manually</span>
        </summary>
        <div style="padding:0 16px 16px;">
            <ol style="font-size:12.5px;line-height:1.5;padding-left:18px;margin:8px 0;">${items}</ol>
            <div style="font-weight:600;font-size:12px;margin:12px 0 4px;">Feed fields not yet audited (not in local sync)</div>
            <ul style="font-size:12px;line-height:1.5;padding-left:18px;margin:0;">${unsynced}</ul>
        </div>
    </details>`;
}

async function loadFeedItems() {
    const storeId = getStoreId();
    const container = document.getElementById("feed-items-container");
    if (!storeId) return;
    try {
        feedItemsCache = await apiRequest(`/api/ai-seo/feed/items?store_id=${storeId}`);
        if (!feedItemsCache.length) {
            container.innerHTML = `<div class="empty-state"><span class="material-symbols-rounded">inventory_2</span><p>No feed scores yet — run the audit.</p></div>`;
            return;
        }
        const weakest = (b) => {
            let worst = null;
            for (const k of Object.keys(FEED_COMPONENT_LABELS)) {
                if (k === "review_signals") continue;
                const s = b && b[k] ? b[k].score : null;
                if (s == null) continue;
                if (!worst || s < worst.score) worst = { key: k, score: s };
            }
            return worst;
        };
        const rows = feedItemsCache
            .map((it, i) => {
                const w = weakest(it.breakdown);
                const wLabel = w ? `${FEED_COMPONENT_LABELS[w.key].label} (${w.score})` : "—";
                const fx = (it.fixables || []).length;
                return `<tr style="cursor:pointer;" onclick="openFeedBreakdown(${i})">
                    <td style="width:58px;">${renderScoreRing(it.score, 40)}</td>
                    <td><div style="font-weight:500;">${escapeHtml(it.title || "")}</div>
                        <div style="font-size:11.5px;color:var(--on-surface-secondary);">/products/${escapeHtml(it.handle || "")}</div></td>
                    <td style="font-size:12.5px;">${wLabel}</td>
                    <td>${fx ? `<span class="badge badge-warning">${fx} fix${fx > 1 ? "es" : ""}</span>` : '<span class="badge badge-success">clean</span>'}</td>
                </tr>`;
            })
            .join("");
        container.innerHTML = `<div class="table-container"><table>
            <thead><tr><th>Score</th><th>Product</th><th>Weakest feed signal</th><th>Fixables</th></tr></thead>
            <tbody>${rows}</tbody></table></div>`;
    } catch (err) {
        showToast(`Failed to load feed items: ${err.message}`, "error");
    }
}

function openFeedBreakdown(i) {
    const it = feedItemsCache[i];
    if (!it) return;
    document.getElementById("breakdown-title").textContent = `Feed readiness — ${it.title}`;
    const comps = Object.keys(FEED_COMPONENT_LABELS)
        .map((k) => {
            const c = (it.breakdown || {})[k];
            if (!c) return "";
            const color = c.score >= 80 ? "var(--success)" : c.score >= 50 ? "var(--warning,#f9ab00)" : "var(--error)";
            return `<div style="display:flex;gap:10px;align-items:flex-start;padding:8px 0;border-bottom:1px solid var(--outline);">
                <span class="material-symbols-rounded" style="color:${color};">${FEED_COMPONENT_LABELS[k].icon}</span>
                <div style="flex:1;"><b style="font-size:13px;">${FEED_COMPONENT_LABELS[k].label}</b>
                    <div style="font-size:12px;color:var(--on-surface-secondary);">${escapeHtml(c.detail || "")}</div></div>
                <b style="color:${color};">${c.score}</b>
            </div>`;
        })
        .join("");
    const fixes = (it.fixables || []).length
        ? `<h4 style="margin:16px 0 6px;">Fix list</h4><ul style="font-size:13px;line-height:1.6;padding-left:18px;">${it.fixables.map((f) => `<li>${escapeHtml(f)}</li>`).join("")}</ul>`
        : "";
    document.getElementById("breakdown-content").innerHTML = `<div style="max-width:760px;">${comps}${fixes}</div>`;
    document.getElementById("breakdown-modal").classList.add("active");
}

// ── Off-site visibility tab ─────────────────────────────────────
let offsitePollTimer = null;

async function runOffsiteAudit() {
    const storeId = getStoreId();
    if (!storeId) { showToast("Select a store first", "warning"); return; }
    const btn = document.getElementById("offsite-run-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded">hourglass_empty</span> Auditing…';
    try {
        await apiRequest("/api/ai-seo/offsite/run", { method: "POST", body: { store_id: parseInt(storeId) } });
        pollOffsite();
    } catch (err) {
        showToast(`Audit failed to start: ${err.message}`, "error");
        resetOffsiteBtn();
    }
}

function resetOffsiteBtn() {
    const btn = document.getElementById("offsite-run-btn");
    btn.disabled = false;
    btn.innerHTML = '<span class="material-symbols-rounded">public</span> Run off-site audit';
}

function pollOffsite() {
    if (offsitePollTimer) clearInterval(offsitePollTimer);
    let errors = 0;
    offsitePollTimer = setInterval(async () => {
        try {
            const runs = await apiRequest(`/api/ai-seo/offsite/runs?store_id=${getStoreId()}`);
            errors = 0;
            const latest = runs[0];
            if (!latest || ["pending", "running"].includes(latest.status)) return;
            clearInterval(offsitePollTimer);
            resetOffsiteBtn();
            if (latest.status === "completed") {
                showToast("Off-site audit complete", "success");
                loadOffsite();
            } else {
                showToast(`Off-site audit failed: ${latest.error || "unknown error"}`, "error");
            }
        } catch (err) {
            if (++errors >= 5) { clearInterval(offsitePollTimer); resetOffsiteBtn(); }
        }
    }, 2500);
}

async function loadOffsite() {
    const storeId = getStoreId();
    if (!storeId) return;
    const scorecardEl = document.getElementById("offsite-scorecard");
    const actionsEl = document.getElementById("offsite-actions");
    const resultsEl = document.getElementById("offsite-results");
    const trendEl = document.getElementById("offsite-trend");
    try {
        const runs = await apiRequest(`/api/ai-seo/offsite/runs?store_id=${storeId}`);
        const latest = runs.find((r) => r.status === "completed");
        if (runs[0] && ["pending", "running"].includes(runs[0].status)) pollOffsite();
        if (!latest) {
            scorecardEl.innerHTML = `<div class="empty-state" style="grid-column:1/-1;"><span class="material-symbols-rounded">public_off</span><p>No off-site audits yet — run one to see where your brand appears across the web.</p></div>`;
            actionsEl.innerHTML = resultsEl.innerHTML = trendEl.innerHTML = "";
            return;
        }
        const d = await apiRequest(`/api/ai-seo/offsite/${latest.id}`);
        renderOffsiteScorecard(d.scorecard || {}, scorecardEl);
        renderOffsiteActions(d.actions || [], actionsEl);
        renderOffsiteResults(d.results || {}, resultsEl);
        trendEl.innerHTML = runs.filter((r) => r.status === "completed").length > 1
            ? `<details class="card" style="padding:0;"><summary style="padding:14px 16px;cursor:pointer;list-style:none;"><b>Previous audits</b></summary>
                <div style="padding:0 16px 16px;font-size:12.5px;line-height:1.8;">${runs
                    .filter((r) => r.status === "completed")
                    .map((r) => {
                        const sc = r.scorecard || {};
                        return `${formatRelativeTime(r.started_at)}: Reddit ${((sc.reddit || {}).count) || 0} · listicles ${((sc.listicles || {}).included) || 0}/${((sc.listicles || {}).checked) || 0} · review platforms ${Object.values(sc.review_platforms || {}).filter(Boolean).length}`;
                    })
                    .join("<br>")}</div></details>`
            : "";
    } catch (err) {
        showToast(`Failed to load off-site data: ${err.message}`, "error");
    }
}

function renderOffsiteScorecard(sc, el) {
    const yesNo = (v) => (v
        ? '<span class="material-symbols-rounded" style="color:var(--success);font-size:20px;vertical-align:middle;">check_circle</span>'
        : '<span class="material-symbols-rounded" style="color:var(--error);font-size:20px;vertical-align:middle;">cancel</span>');
    const reddit = sc.reddit || {};
    const lists = sc.listicles || {};
    const rp = sc.review_platforms || {};
    const rpFound = Object.entries(rp).filter(([, v]) => v).map(([k]) => k);
    const compInc = lists.competitor_inclusions || {};
    const bestComp = Object.entries(compInc).sort((a, b) => b[1] - a[1])[0];
    el.innerHTML = `
        <div class="stat-card"><div class="stat-value">${yesNo(reddit.found)} ${reddit.count || 0}</div>
            <div class="stat-label">Reddit mentions</div>
            <div class="stat-label" style="color:var(--on-surface-secondary);">threads naming your brand</div></div>
        <div class="stat-card"><div class="stat-value">${yesNo(rpFound.length)} ${rpFound.length}</div>
            <div class="stat-label">Review platforms</div>
            <div class="stat-label" style="color:var(--on-surface-secondary);">${rpFound.length ? escapeHtml(rpFound.join(", ")) : "no profiles found"}</div></div>
        <div class="stat-card"><div class="stat-value">${lists.included || 0}<span style="font-size:14px;color:var(--on-surface-secondary);"> / ${lists.checked || 0}</span></div>
            <div class="stat-label">"Best of" roundup inclusion</div>
            <div class="stat-label" style="color:var(--on-surface-secondary);">${bestComp ? `top competitor appears in ${bestComp[1]}` : "roundups checked for your brand"}</div></div>
        <div class="stat-card"><div class="stat-value">${sc.news_mentions || 0}</div>
            <div class="stat-label">News/blog mentions</div>
            <div class="stat-label" style="color:var(--on-surface-secondary);">third-party articles naming you</div></div>`;
}

function renderOffsiteActions(actions, el) {
    if (!actions.length) { el.innerHTML = ""; return; }
    const badge = (p) => `<span class="badge ${p === "high" ? "badge-error" : p === "medium" ? "badge-warning" : "badge-success"}">${p}</span>`;
    el.innerHTML = `<div class="card" style="padding:16px;">
        <h3 style="margin:0 0 10px;">What to do about it</h3>
        ${actions.map((a) => `<div style="display:flex;gap:10px;align-items:flex-start;padding:8px 0;border-bottom:1px solid var(--outline);">
            ${badge(a.priority || "medium")}
            <div style="flex:1;"><b style="font-size:13px;">${escapeHtml(a.title || "")}</b>
                <div style="font-size:12px;color:var(--on-surface-secondary);margin-top:2px;">${escapeHtml(a.why || "")}</div>
                ${a.action ? `<div style="font-size:12.5px;margin-top:4px;">${escapeHtml(a.action)}</div>` : ""}</div>
        </div>`).join("")}
    </div>`;
}

function renderOffsiteResults(groups, el) {
    const kinds = Object.keys(groups);
    if (!kinds.length) { el.innerHTML = ""; return; }
    const KIND_LABELS = { brand_reviews: "Brand reviews", reddit: "Reddit", review_platform: "Review platforms", listicle: "Category roundups", brand_general: "Brand search" };
    const section = (kind) => {
        const rows = (groups[kind] || [])
            .map((r) => `<tr>
                <td style="font-size:12px;">${r.position ?? ""}</td>
                <td><a href="${escapeHtml(r.url || "#")}" target="_blank" rel="noopener" style="font-size:12.5px;">${escapeHtml(r.title || r.url || "")}</a>
                    <div style="font-size:11px;color:var(--on-surface-secondary);">${escapeHtml(r.domain || "")}</div></td>
                <td><span class="badge" style="background:var(--surface-dim);color:var(--on-surface);">${escapeHtml(r.classification || "")}</span></td>
                <td>${r.brand_present ? '<span class="badge badge-success">you</span>' : ""}
                    ${(r.competitors_present || []).slice(0, 3).map((c) => `<span class="badge badge-warning" style="margin-left:2px;">${escapeHtml(c)}</span>`).join("")}</td>
            </tr>`)
            .join("");
        return `<details style="border-bottom:1px solid var(--outline);padding:6px 0;">
            <summary style="cursor:pointer;font-size:13px;font-weight:600;">${KIND_LABELS[kind] || kind} (${(groups[kind] || []).length})</summary>
            <div class="table-container" style="margin-top:6px;"><table>
                <thead><tr><th>#</th><th>Result</th><th>Type</th><th>Mentions</th></tr></thead>
                <tbody>${rows}</tbody></table></div>
        </details>`;
    };
    el.innerHTML = `<details class="card" style="padding:0;">
        <summary style="padding:14px 16px;cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;">
            <span class="material-symbols-rounded" style="color:var(--on-surface-secondary);">travel_explore</span>
            <b style="flex:1;">Search results by query type</b>
            <span style="font-size:12px;color:var(--on-surface-secondary);">what Brave found for each probe</span>
        </summary>
        <div style="padding:0 16px 16px;">${kinds.map(section).join("")}</div>
    </details>`;
}

let activityTimer = null;
const BATCH_KIND = { geo_optimize: "GEO optimize", fact_boost: "Fact-density boost" };

async function loadActivity() {
    const storeId = getStoreId();
    const container = document.getElementById("activity-container");
    try {
        const batches = await apiRequest(`/api/ai-seo/batches?store_id=${storeId}`);
        if (!batches.length) {
            container.innerHTML = `<div class="empty-state"><span class="material-symbols-rounded">history</span><p>No batch jobs yet. Run "Optimize all" or "Boost fact density" from AI Readiness.</p></div>`;
        } else {
            container.innerHTML = batches.map(batchCard).join("");
        }
        // Auto-refresh while anything is running.
        const anyRunning = batches.some((b) => b.status === "running" || b.status === "pending");
        if (anyRunning && !activityTimer) {
            activityTimer = setInterval(() => { if (document.querySelector(".tab.active")?.dataset.tab === "activity") loadActivity(); }, 5000);
        } else if (!anyRunning && activityTimer) {
            clearInterval(activityTimer);
            activityTimer = null;
        }
    } catch (err) {
        showToast(`Failed to load activity: ${err.message}`, "error");
    }
}

function batchCard(b) {
    const running = b.status === "running" || b.status === "pending";
    const done = b.completed + b.failed;
    const pct = b.total ? Math.round((done / b.total) * 100) : 0;
    const kind = BATCH_KIND[b.kind] || b.kind;
    const statusBadge = running
        ? `<span class="badge badge-warning">running</span>`
        : b.status === "failed" ? `<span class="badge badge-error">failed</span>` : `<span class="badge badge-success">done</span>`;
    const bar = `<div style="background:var(--surface-dim);border-radius:6px;height:8px;overflow:hidden;margin:8px 0;">
        <div style="background:var(--primary);height:100%;width:${pct}%;transition:width .3s;"></div></div>`;
    let stats = "";
    if (b.kind === "fact_boost") {
        stats = `<span class="badge badge-success">${b.enriched} enriched</span>
                 <span class="badge" style="background:var(--surface-dim);color:var(--on-surface);">${b.skipped} left unchanged</span>
                 <span class="badge badge-warning">${b.facts_added} facts added</span>`;
    } else {
        stats = `<span class="badge badge-success">${b.completed} optimized</span>${b.dropped_kw_items ? ` <span class="badge badge-error">${b.dropped_kw_items} w/ dropped keywords</span>` : ""}`;
    }
    if (b.failed) stats += ` <span class="badge badge-error">${b.failed} failed</span>`;
    const clearBtn = running
        ? ""
        : `<button class="icon-btn" title="Clear from activity" onclick="clearBatch(${b.id})" style="padding:2px;margin-left:4px;"><span class="material-symbols-rounded" style="font-size:18px;">close</span></button>`;
    const retryBtn = (!running && b.failed)
        ? `<div style="margin-top:10px;"><button class="btn btn-secondary btn-sm" onclick="retryBatch(${b.id}, this)"><span class="material-symbols-rounded">refresh</span> Retry ${b.failed} failed</button></div>`
        : "";
    const finishedLine = b.completed_at ? ` · finished ${formatDate(b.completed_at)}` : "";
    const timeTitle = `Started ${formatDate(b.created_at)}${finishedLine}`;
    const timeBlock = `<div style="margin-left:auto;text-align:right;line-height:1.25;" title="${escapeHtml(timeTitle)}">
            <div style="font-size:12px;color:var(--on-surface-secondary);">${formatRelativeTime(b.created_at)}</div>
            <div style="font-size:11px;color:var(--on-surface-secondary);opacity:.7;">${formatDate(b.created_at)}</div>
        </div>`;
    return `<div class="card" style="padding:14px;margin-bottom:12px;">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
            <b>${kind}</b><span style="color:var(--on-surface-secondary);font-size:13px;">${b.resource_type === "collection" ? "collections" : "products"}</span>
            ${statusBadge}
            ${timeBlock}
            ${clearBtn}
        </div>
        ${bar}
        <div style="font-size:13px;color:var(--on-surface-secondary);">${done}/${b.total} processed${running && b.current ? " · " + escapeHtml(b.current) : ""}</div>
        <div style="margin-top:8px;display:flex;gap:4px;flex-wrap:wrap;">${stats}</div>
        ${retryBtn}
    </div>`;
}

async function retryBatch(id, btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded">hourglass_empty</span> Starting…';
    try {
        const res = await apiRequest(`/api/ai-seo/batches/${id}/retry`, { method: "POST", body: {} });
        showToast(`Retrying ${res.total} failed item${res.total > 1 ? "s" : ""}…`, "info");
        loadActivity();
    } catch (err) {
        showToast(`Retry failed: ${err.message}`, "error");
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded">refresh</span> Retry failed';
    }
}

async function clearBatch(id) {
    try {
        await apiRequest(`/api/ai-seo/batches/${id}`, { method: "DELETE" });
        loadActivity();
    } catch (err) {
        showToast(`Clear failed: ${err.message}`, "error");
    }
}

async function clearFinishedBatches() {
    try {
        const res = await apiRequest("/api/ai-seo/batches/clear", { method: "POST", body: { store_id: parseInt(getStoreId()) } });
        showToast(res.cleared ? `Cleared ${res.cleared} finished job${res.cleared > 1 ? "s" : ""}` : "Nothing to clear", "success");
        loadActivity();
    } catch (err) {
        showToast(`Clear failed: ${err.message}`, "error");
    }
}

// ── AEO assets ──────────────────────────────────────────────────
async function loadAeo() {
    const storeId = getStoreId();
    const container = document.getElementById("aeo-container");
    container.innerHTML = `<div class="loading-spinner" style="padding:24px 0;"><div class="spinner"></div><p>Reading current storefront state…</p></div>`;
    try {
        const a = await apiRequest(`/api/ai-seo/aeo/preview?store_id=${storeId}`);
        window._aeo = a;
        container.innerHTML =
            aeoOverview(a) +
            robotsCard(a.robots) +
            botCheckCard(a.bot_check) +
            indexnowCard(a.indexnow) +
            siteSchemaCard(a.site_schema) +
            llmsCard(a.llms, a.base_url) +
            a11yCard(a.a11y);
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><span class="material-symbols-rounded">error</span><p>${escapeHtml(err.message)}</p></div>`;
    }
}

function statusPill(status) {
    if (status === "published") return `<span class="badge badge-success">Published</span>`;
    if (status === "draft") return `<span class="badge badge-warning">Draft</span>`;
    return "";
}

const AEO_PRE = "background:var(--surface-dim);color:var(--on-surface);border:1px solid var(--outline);border-radius:8px;padding:12px;max-height:240px;overflow:auto;font-size:12px;white-space:pre-wrap;margin-top:8px;";

function aeoDetails(summaryText, content) {
    if (!content) return "";
    return `<details style="margin-top:8px;">
        <summary style="cursor:pointer;font-size:12px;color:var(--on-surface-secondary);">${summaryText}</summary>
        <pre style="${AEO_PRE}">${escapeHtml(content)}</pre>
    </details>`;
}

function improveLine(signal, current, projected) {
    if (current == null || projected == null) return "";
    if (current === projected) return `<b>Improves ${signal}:</b> already optimal (${current}/100) — no change needed`;
    const up = projected > current;
    const col = up ? "var(--success)" : "var(--error)";
    return `<b>Improves ${signal}:</b> ${current} <span class="material-symbols-rounded" style="font-size:14px;vertical-align:middle;color:${col};">${up ? "arrow_upward" : "arrow_downward"}</span> <span style="color:${col};font-weight:600;">${projected}</span>`;
}

function aeoOverview(a) {
    const bc = a.bot_check || {};
    const items = [
        ["dns", "robots.txt", a.robots && a.robots.published_status],
        ["travel_explore", "Bot access", bc.tested ? (bc.ok === bc.tested ? "published" : "issues") : null],
        ["send", "IndexNow", a.indexnow && a.indexnow.published_status],
        ["schema", "Site schema", a.site_schema && a.site_schema.published_status],
        ["description", "llms.txt", a.llms && a.llms.published_status],
        ["accessibility_new", "Accessibility", a.a11y && a.a11y.published_status],
    ];
    return `<div class="card" style="padding:12px 16px;margin-bottom:16px;display:flex;gap:20px;flex-wrap:wrap;">
        ${items.map(([icon, label, s]) => {
            const pub = s === "published";
            return `<div style="display:flex;align-items:center;gap:6px;font-size:13px;">
                <span class="material-symbols-rounded" style="font-size:17px;color:${pub ? "var(--success)" : "var(--on-surface-secondary)"};">${pub ? "check_circle" : "radio_button_unchecked"}</span>
                <span>${label}</span>
                <span style="font-size:11px;color:${pub ? "var(--success)" : "var(--on-surface-secondary)"};">${pub ? "live" : "off"}</span>
            </div>`;
        }).join("")}
    </div>`;
}

function aeoDownloadBtn(key) {
    return `<button class="btn btn-secondary btn-sm" onclick="downloadAeo('${key}')" title="Download file"><span class="material-symbols-rounded">download</span></button>`;
}

function aeoProposed(label, text) {
    return aeoDetails(label, text || "");
}

// Compact asset card: header (icon/title/status) + one-line desc + concise target
// + visible actions, with all verbose detail folded into a "Details & preview" panel.
function aeoCard({ icon, title, status, desc, target, body, actions, details }) {
    return `<div class="card" style="padding:0;margin-bottom:12px;">
        <div style="padding:14px 16px;">
            <div style="display:flex;align-items:center;gap:10px;">
                <span class="material-symbols-rounded" style="color:var(--primary);">${icon}</span>
                <h3 style="margin:0;font-size:15px;flex:1;">${title}</h3>
                ${status || ""}
            </div>
            <p style="font-size:12.5px;color:var(--on-surface-secondary);margin:8px 0 0;line-height:1.5;">${desc}</p>
            ${target ? `<div style="font-size:12px;color:var(--on-surface);margin-top:8px;display:flex;gap:6px;align-items:flex-start;">
                <span class="material-symbols-rounded" style="font-size:15px;color:var(--primary);">target</span><span>${target}</span></div>` : ""}
            ${body || ""}
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;align-items:center;">${actions || ""}</div>
        </div>
        ${details ? `<details style="border-top:1px solid var(--outline);">
            <summary style="padding:10px 16px;cursor:pointer;font-size:12px;color:var(--on-surface-secondary);">Details &amp; preview</summary>
            <div style="padding:0 16px 16px;">${details}</div>
        </details>` : ""}
    </div>`;
}

function robotsCard(r) {
    const c = r.current || {};
    let current;
    if (!c.present) {
        current = `<div style="color:var(--on-surface-secondary);font-size:13px;">Using Shopify's default robots.txt (couldn't read a custom one).</div>`;
    } else {
        const okChip = (n) => `<span class="badge badge-success" style="margin:2px;">${n}</span>`;
        const noChip = (n) => `<span class="badge badge-error" style="margin:2px;">${n}</span>`;
        current = `<div style="font-size:13px;margin-bottom:6px;">${c.ai_bots_allowed.length}/${c.ai_bots_total} AI crawlers allowed · cart/checkout ${c.blocks_sensitive ? "blocked ✓" : "not blocked"}</div>
            <div>${c.ai_bots_allowed.map(okChip).join("")}${c.ai_bots_blocked.map(noChip).join("")}</div>
            ${aeoDetails("View current robots.txt", c.raw)}`;
    }
    const details = `<div style="font-weight:600;font-size:12px;margin:12px 0 6px;">Current on your store</div>${current}
        <div style="font-size:12px;margin-top:10px;">${improveLine((r.impact || {}).signal, (r.impact || {}).current, (r.impact || {}).projected)}</div>
        ${aeoProposed("View proposed robots.txt", r.proposed)}`;
    return aeoCard({
        icon: "smart_toy",
        title: "robots.txt AI rules",
        status: statusPill(r.published_status),
        desc: "Explicitly allows GPTBot, ClaudeBot, PerplexityBot, Google-Extended and more, while keeping cart/checkout private. Publishes <code>templates/robots.txt.liquid</code> to your theme.",
        target: "Targets <b>AI crawlability</b> — if answer engines can't fetch your pages, they can't cite you.",
        actions: `${aeoDownloadBtn("robots")}<button class="btn btn-primary btn-sm" onclick="applyAeo('robots', this)"><span class="material-symbols-rounded">cloud_upload</span> Publish to theme</button>`,
        details,
    });
}

function botCheckCard(b) {
    b = b || {};
    const bots = (b.results && b.results.bots) || {};
    const names = Object.keys(bots);
    let body;
    if (!names.length) {
        body = `<div style="color:var(--on-surface-secondary);font-size:13px;">Not verified yet — run a scan or click Verify now.</div>`;
    } else {
        const chip = (name, statuses) => {
            const vals = Object.values(statuses);
            const allOk = vals.every((v) => v === "ok");
            const challenged = vals.includes("challenged");
            const cls = allOk ? "badge-success" : "badge-error";
            const label = allOk ? name : `${name}: ${challenged ? "challenged" : vals.find((v) => v !== "ok") || "blocked"}`;
            return `<span class="badge ${cls}" style="margin:2px;">${escapeHtml(label)}</span>`;
        };
        const controlOk = b.results && b.results.control_ok;
        const anyChallenged = names.some((n) => Object.values(bots[n]).includes("challenged"));
        body = `<div style="font-size:13px;margin-bottom:6px;">${b.ok}/${b.tested} AI crawlers verified with live fetches${b.checked_at ? ` · checked ${new Date(b.checked_at).toLocaleString()}` : ""}</div>
            <div>${names.map((n) => chip(n, bots[n])).join("")}</div>
            ${anyChallenged && controlOk ? `<div style="font-size:12px;color:var(--error);margin-top:8px;"><span class="material-symbols-rounded" style="font-size:14px;vertical-align:middle;">warning</span> Your WAF/CDN (e.g. Cloudflare) is challenging AI crawlers while normal browsers pass — allowlist these bots or they can't read your pages.</div>` : ""}
            ${!controlOk && names.length ? `<div style="font-size:12px;color:var(--on-surface-secondary);margin-top:8px;">Control fetch also failed — results may reflect a store-wide issue, not bot blocking.</div>` : ""}`;
    }
    return aeoCard({
        icon: "travel_explore",
        title: "AI crawler access — live verification",
        status: b.tested ? `<span class="badge ${b.ok === b.tested ? "badge-success" : "badge-error"}">${b.ok}/${b.tested} ok</span>` : "",
        desc: "Fetches your homepage and a product page pretending to be GPTBot, OAI-SearchBot, ClaudeBot and PerplexityBot — the crawlers answer engines actually use. robots.txt can say \"allowed\" while a firewall still blocks them; this checks reality.",
        target: "Feeds <b>AI crawlability</b> — verified live access counts for 15 of its 100 points.",
        actions: `<button class="btn btn-primary btn-sm" onclick="runBotCheck(this)"><span class="material-symbols-rounded">refresh</span> Verify now</button>`,
        details: `<div style="margin-top:8px;">${body}</div>`,
    });
}

async function runBotCheck(btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded">hourglass_empty</span> Verifying…';
    try {
        const res = await apiRequest("/api/ai-seo/aeo/bot-check", { method: "POST", body: { store_id: parseInt(getStoreId()) } });
        showToast(`${res.verified}/${res.tested} AI crawlers verified`, res.verified === res.tested ? "success" : "warning");
    } catch (err) {
        showToast(`Verification failed: ${err.message}`, "error");
    }
    loadAeo();
}

function llmsCard(l) {
    const c = l.current || {};
    const current = c.present
        ? `<div style="font-size:13px;">Present at <a href="${c.url}" target="_blank">${escapeHtml(c.url)}</a> (${c.location})</div>${aeoDetails("View current llms.txt", c.raw)}`
        : `<div style="color:var(--on-surface-secondary);font-size:13px;">No llms.txt found on your store yet.</div>`;
    const details = `<div style="font-weight:600;font-size:12px;margin:12px 0 6px;">Current on your store</div>${current}
        <div style="font-size:12px;margin-top:10px;">${improveLine((l.impact || {}).signal, (l.impact || {}).current, (l.impact || {}).projected)}</div>
        ${aeoProposed("View proposed llms.txt", l.proposed)}`;
    return aeoCard({
        icon: "description",
        title: "llms.txt index",
        status: statusPill(l.published_status),
        desc: "A Markdown map of your key collections & products. <b>Optional:</b> studies of 137k+ sites show AI crawlers almost never read llms.txt, and it no longer affects your crawlability score. Shopify can't serve <code>/llms.txt</code> at the root, so this publishes a <code>/pages/llms</code> page (download to self-host at root).",
        target: "Low-evidence, harmless extra — publish only if you want it; real crawl access is what the live verification above measures.",
        actions: `${aeoDownloadBtn("llms")}<button class="btn btn-primary btn-sm" onclick="applyAeo('llms', this)"><span class="material-symbols-rounded">cloud_upload</span> Publish /pages/llms</button>`,
        details,
    });
}

function indexnowCard(x) {
    x = x || {};
    const published = x.published_status === "published";
    const lp = x.last_ping;
    const pingLog = (x.pings || [])
        .map((p) => `${p.created_at ? new Date(p.created_at).toLocaleString() : "?"} · ${p.reason || "?"} · ${p.url_count} URL(s) · ${p.ok ? "ok" : `failed${p.status_code ? ` (${p.status_code})` : ""}${p.error ? `: ${p.error}` : ""}`}`)
        .join("\n");
    // Visible status block: key URL + last ping outcome (with the full error).
    let body = "";
    if (published && x.key_location) {
        let pingLine;
        if (!lp) {
            pingLine = `<div style="font-size:12px;color:var(--on-surface-secondary);margin-top:6px;">No pings yet — publish content or click Submit all URLs.</div>`;
        } else if (lp.ok) {
            pingLine = `<div style="font-size:12px;margin-top:6px;"><span class="badge badge-success">delivered</span> Last ping: ${escapeHtml(lp.reason || "")} · ${lp.url_count} URL(s) · ${lp.created_at ? new Date(lp.created_at).toLocaleString() : ""}</div>`;
        } else {
            pingLine = `<div style="font-size:12px;margin-top:6px;"><span class="badge badge-error">failed${lp.status_code ? ` (${lp.status_code})` : ""}</span> Last ping: ${escapeHtml(lp.reason || "")} · ${lp.url_count} URL(s) · ${lp.created_at ? new Date(lp.created_at).toLocaleString() : ""}</div>
                <div style="font-size:12px;color:var(--error);margin-top:4px;word-break:break-word;">${escapeHtml(lp.error || "")}</div>
                <div style="font-size:12px;color:var(--on-surface-secondary);margin-top:4px;">Click <b>Re-verify setup</b> to re-check the key, then <b>Submit all URLs</b> again.</div>`;
        }
        body = `<div style="margin-top:10px;padding:10px 12px;background:var(--surface-dim);border-radius:8px;">
            <div style="font-size:12.5px;">Key served at <a href="${escapeHtml(x.key_location)}" target="_blank">${escapeHtml(x.key_location)}</a>${x.override_active ? ' <span class="badge badge-warning">self-hosted override</span>' : ""}</div>
            ${pingLine}
        </div>`;
    } else {
        body = `<div style="margin-top:10px;color:var(--on-surface-secondary);font-size:13px;">Not set up yet. Setup generates a verification key, hosts it on a hidden store page, and from then on every publish (blog, news, landing pages, GEO applies) pings Bing automatically.</div>`;
    }
    const bing = `<div style="font-weight:600;font-size:12px;margin:12px 0 4px;">Bing Webmaster Tools (manual, one-time)</div>
        <ol style="font-size:12px;color:var(--on-surface-secondary);margin:0;padding-left:18px;line-height:1.7;">
            <li>Go to <a href="https://www.bing.com/webmasters" target="_blank">bing.com/webmasters</a> and sign in.</li>
            <li>Use <b>Import from Google Search Console</b> — one click, no DNS records needed.</li>
            <li>Submit your sitemap: <code>/sitemap.xml</code>.</li>
        </ol>`;
    const fallback = `<div style="font-size:12px;color:var(--on-surface-secondary);margin-top:8px;"><b>If Bing keeps rejecting the key:</b> host <code>&lt;key&gt;.txt</code> containing the key at your domain root via your CDN (e.g. a Cloudflare rule) and set the <code>indexnow.key_location_override</code> setting.</div>`;
    return aeoCard({
        icon: "send",
        title: "IndexNow — instant Bing indexing",
        status: statusPill(x.published_status),
        desc: "ChatGPT search runs on Bing's index. IndexNow tells Bing about new and updated URLs immediately (crawled in ~24h instead of days), so your optimized pages reach AI answers faster. Once set up, pings fire automatically on every publish.",
        target: "Targets <b>freshness &amp; discovery</b> — shortens the loop between publishing an optimization and AI search seeing it.",
        body,
        actions: `<button class="btn btn-primary btn-sm" onclick="setupIndexnow(this)"><span class="material-symbols-rounded">key</span> ${published ? "Re-verify setup" : "Set up IndexNow"}</button>
            ${published ? `<button class="btn btn-secondary btn-sm" onclick="submitAllIndexnow(this)"><span class="material-symbols-rounded">upload</span> Submit all URLs</button>
            <button class="btn btn-secondary btn-sm" onclick="unpublishIndexnow(this)"><span class="material-symbols-rounded">undo</span> Remove</button>` : ""}`,
        details: `${bing}${fallback}${pingLog ? aeoDetails("Ping log (last 10)", pingLog) : ""}`,
    });
}

async function setupIndexnow(btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded">hourglass_empty</span> Setting up…';
    try {
        const res = await apiRequest("/api/ai-seo/indexnow/setup", { method: "POST", body: { store_id: parseInt(getStoreId()) } });
        if (res.verified === true) showToast("IndexNow key published and verified", "success");
        else if (res.verified === null) showToast("Key published, but served with extra markup — Bing will likely accept it; watch the ping log", "warning");
        else showToast("Key page published but could not be verified yet — check the key URL", "warning");
    } catch (err) {
        showToast(`Setup failed: ${err.message}`, "error");
    }
    loadAeo();
}

async function submitAllIndexnow(btn) {
    if (!confirm("Submit your homepage, all active products, collections and articles to IndexNow now?")) return;
    btn.disabled = true;
    try {
        const res = await apiRequest("/api/ai-seo/indexnow/submit-all", { method: "POST", body: { store_id: parseInt(getStoreId()) } });
        showToast(res.ok ? `Submitted ${res.url_count} URLs to IndexNow` : `Submission failed${res.error ? `: ${res.error}` : ""}`, res.ok ? "success" : "error");
    } catch (err) {
        showToast(`Submission failed: ${err.message}`, "error");
    }
    loadAeo();
}

async function unpublishIndexnow(btn) {
    if (!confirm("Remove the IndexNow key page from your store? Automatic pings will stop.")) return;
    btn.disabled = true;
    try {
        await apiRequest("/api/ai-seo/indexnow/unpublish", { method: "POST", body: { store_id: parseInt(getStoreId()) } });
        showToast("IndexNow key removed", "success");
    } catch (err) {
        showToast(`Remove failed: ${err.message}`, "error");
    }
    loadAeo();
}

function _inp(id, label, value, ph) {
    return `<div style="margin-bottom:8px;"><label style="font-size:12px;color:var(--on-surface-secondary);display:block;margin-bottom:2px;">${label}</label>
        <input id="${id}" class="form-control" style="width:100%;" value="${escapeHtml(value || "")}" placeholder="${ph || ""}"></div>`;
}

function siteSchemaCard(o) {
    const org = o.org || {};
    const published = o.published_status === "published";
    const updateAvail = published && o.update_available;
    const im = o.impact || {};
    const live = o.current_live || {};
    const improve = im.current_avg != null
        ? `<b>Improves Structured data:</b> your pages average ${im.current_avg}/100 — publishing adds <b>Organization</b> + <b>BreadcrumbList</b> to every page (2 of the 4 desired schema types), lifting per-page structured-data toward ~90+.`
        : `<b>Improves Structured data:</b> adds Organization + BreadcrumbList site-wide.`;
    const liveNote = (live.organization || live.breadcrumb)
        ? `<div style="font-size:12px;color:var(--on-surface-secondary);">Detected on storefront: ${live.organization ? "Organization ✓" : ""} ${live.breadcrumb ? "BreadcrumbList ✓" : ""} — publishing will reinforce/replace with a complete version.</div>`
        : `<div style="font-size:12px;color:var(--on-surface-secondary);">No Organization/BreadcrumbList detected on your storefront yet.</div>`;

    const form = `
        <div style="font-weight:600;font-size:13px;margin:6px 0;">Brand details (for the Organization entity)</div>
        ${_inp("org-name", "Business name", org.name)}
        ${_inp("org-url", "Website URL", org.url, "https://a2ztobacco.com")}
        ${_inp("org-logo", "Logo URL", org.logo_url, "https://…/logo.png")}
        ${_inp("org-desc", "Short description", org.description, "What your brand sells, in one line")}
        <div style="display:flex;gap:8px;">${_inp("org-email", "Contact email", org.email)}${_inp("org-phone", "Contact phone", org.phone)}</div>
        <div style="margin-bottom:8px;"><label style="font-size:12px;color:var(--on-surface-secondary);display:block;margin-bottom:2px;">Social / profile URLs — <b>sameAs</b> (one per line; strongest AI entity signal)</label>
            <textarea id="org-sameas" class="form-control" style="width:100%;height:70px;" placeholder="https://www.facebook.com/yourbrand&#10;https://www.instagram.com/yourbrand">${escapeHtml((org.same_as || []).join("\n"))}</textarea></div>
        ${_inp("org-knows", "Topics you're known for — knowsAbout (comma-separated)", (org.knows_about || []).join(", "))}
        <button class="btn btn-secondary btn-sm" onclick="saveOrgSettings(this)"><span class="material-symbols-rounded">save</span> Save brand details</button>
    `;

    const hasBrand = !!(org.name && org.name.trim());
    const actions = `${aeoDownloadBtn("schema")}
        ${published ? `<button class="btn btn-secondary btn-sm" onclick="unpublishSchema(this)"><span class="material-symbols-rounded">undo</span> Unpublish</button>` : ""}
        <button class="btn btn-primary btn-sm" onclick="publishSchema(this)"><span class="material-symbols-rounded">cloud_upload</span> ${published ? "Re-publish" : "Publish to theme"}</button>`;
    const details = `
        <details style="margin-top:12px;" ${hasBrand ? "" : "open"}>
            <summary style="cursor:pointer;font-weight:600;font-size:12px;">Brand details${hasBrand ? "" : ' — <span style="color:var(--warning,#f9ab00);">set these before publishing</span>'}</summary>
            <div style="margin-top:8px;">${form}</div>
        </details>
        <div style="font-size:12px;margin:12px 0 6px;">${improve}</div>
        <div style="margin-bottom:6px;">${liveNote}</div>
        ${aeoDetails("View generated Organization JSON-LD", JSON.stringify(o.proposed_org, null, 2))}
        ${aeoDetails("View theme snippet (ai-seo-schema.liquid)", o.proposed_snippet)}`;
    return aeoCard({
        icon: "schema",
        title: "Site-wide Schema — Organization + Breadcrumbs + per-item JSON-LD",
        status: statusPill(o.published_status) + (updateAvail ? ' <span class="badge badge-warning">Update available</span>' : ""),
        desc: "A theme snippet with static <b>Organization</b> + <b>WebSite</b> JSON-LD, a <b>BreadcrumbList</b> on every page, and — critically — it renders the enriched <b>Product/CollectionPage + FAQPage</b> JSON-LD the optimizer pushes to metafields (without this, that schema is invisible to crawlers). Reversible." +
            (updateAvail ? " <b style=\"color:var(--warning,#f9ab00);\">Your published snippet is an older version that doesn't render per-item schema — Re-publish to fix.</b>" : "") +
            (hasBrand ? "" : " <b>Add your brand details</b> below first."),
        target: "Targets <b>structured data</b> — site-wide entity schema plus the per-item schema that pushed metafields alone can't expose.",
        actions,
        details,
    });
}

async function saveOrgSettings(btn) {
    btn.disabled = true;
    const body = {
        store_id: parseInt(getStoreId()),
        name: document.getElementById("org-name").value,
        url: document.getElementById("org-url").value,
        logo_url: document.getElementById("org-logo").value,
        description: document.getElementById("org-desc").value,
        email: document.getElementById("org-email").value,
        phone: document.getElementById("org-phone").value,
        same_as: document.getElementById("org-sameas").value.split("\n").map((s) => s.trim()).filter(Boolean),
        knows_about: document.getElementById("org-knows").value.split(",").map((s) => s.trim()).filter(Boolean),
    };
    try {
        await apiRequest("/api/ai-seo/aeo/org-settings", { method: "POST", body });
        showToast("Brand details saved", "success");
        loadAeo();
    } catch (err) {
        showToast(`Save failed: ${err.message}`, "error");
        btn.disabled = false;
    }
}

async function publishSchema(btn) {
    if (!confirm("Publish Organization + BreadcrumbList schema to your live theme? (reversible)")) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded">hourglass_empty</span> Publishing…';
    try {
        const res = await apiRequest("/api/ai-seo/aeo/schema/apply", { method: "POST", body: { store_id: parseInt(getStoreId()) } });
        showToast(res.message || "Published", "success");
    } catch (err) {
        showToast(`Publish failed: ${err.message}`, "error");
    }
    loadAeo();
}

async function unpublishSchema(btn) {
    if (!confirm("Remove the site-wide schema from your theme?")) return;
    btn.disabled = true;
    try {
        const res = await apiRequest("/api/ai-seo/aeo/schema/unpublish", { method: "POST", body: { store_id: parseInt(getStoreId()) } });
        showToast(res.message || "Removed", "success");
    } catch (err) {
        showToast(`Unpublish failed: ${err.message}`, "error");
    }
    loadAeo();
}

function a11yCard(x) {
    const published = x && x.published_status === "published";
    const actions = `${aeoDownloadBtn("a11y")}
        ${published ? `<button class="btn btn-secondary btn-sm" onclick="unpublishA11y(this)"><span class="material-symbols-rounded">undo</span> Unpublish</button>` : ""}
        <button class="btn btn-primary btn-sm" onclick="publishA11y(this)"><span class="material-symbols-rounded">cloud_upload</span> ${published ? "Re-publish" : "Publish to theme"}</button>`;
    return aeoCard({
        icon: "accessibility_new",
        title: "Storefront Accessibility",
        status: statusPill(x && x.published_status),
        desc: 'A small theme script that gives an accessible name to unlabeled links/buttons — e.g. the icon-only "Powered by Shopify" footer link. Fixes the "Links must have discernible text" audit. Note: this runs as JavaScript, so AI <i>crawlers</i> reading raw HTML never see it — it does not affect crawl or citation scores. Reversible.',
        target: "Targets <b>screen readers &amp; agentic browsers</b> (ChatGPT Atlas, Claude in Chrome) — tools driving a real browser use the accessibility tree; unlabeled controls make it malformed.",
        actions,
        details: aeoProposed("View theme snippet (ai-seo-a11y.liquid)", (x && x.proposed) || ""),
    });
}

async function publishA11y(btn) {
    if (!confirm("Publish the accessibility helper to your live theme? (reversible)")) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded">hourglass_empty</span> Publishing…';
    try {
        const res = await apiRequest("/api/ai-seo/aeo/a11y/apply", { method: "POST", body: { store_id: parseInt(getStoreId()) } });
        showToast(res.message || "Published", "success");
    } catch (err) {
        showToast(`Publish failed: ${err.message}`, "error");
    }
    loadAeo();
}

async function unpublishA11y(btn) {
    if (!confirm("Remove the accessibility helper from your theme?")) return;
    btn.disabled = true;
    try {
        const res = await apiRequest("/api/ai-seo/aeo/a11y/unpublish", { method: "POST", body: { store_id: parseInt(getStoreId()) } });
        showToast(res.message || "Removed", "success");
    } catch (err) {
        showToast(`Unpublish failed: ${err.message}`, "error");
    }
    loadAeo();
}

function downloadAeo(key) {
    const map = { robots: window._aeo.robots.proposed, llms: window._aeo.llms.proposed, schema: window._aeo.site_schema.proposed_snippet, a11y: window._aeo.a11y.proposed };
    const names = { robots: "robots.txt.liquid", llms: "llms.txt", schema: "ai-seo-schema.liquid", a11y: "ai-seo-a11y.liquid" };
    const blob = new Blob([map[key] || ""], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = names[key];
    a.click();
    URL.revokeObjectURL(url);
}

async function applyAeo(key, btn) {
    if (!confirm("This writes to your live store. Continue?")) return;
    const storeId = getStoreId();
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded">hourglass_empty</span> Applying…';
    try {
        const res = await apiRequest(`/api/ai-seo/aeo/${key}/apply`, { method: "POST", body: { store_id: parseInt(storeId) } });
        showToast(res.message || "Applied", "success");
    } catch (err) {
        showToast(`Apply failed: ${err.message}`, "error");
    }
    btn.disabled = false;
    loadAeo();
}

// ── Visibility: prompts ─────────────────────────────────────────
async function loadPrompts() {
    const storeId = getStoreId();
    const container = document.getElementById("prompts-container");
    try {
        const prompts = await apiRequest(`/api/ai-seo/prompts?store_id=${storeId}`);
        if (!prompts.length) {
            container.innerHTML = `<div class="empty-state"><span class="material-symbols-rounded">forum</span>
                <p>No prompts yet. Click <b>Auto-generate</b> to seed a buyer-intent prompt set.</p></div>`;
            return;
        }
        container.innerHTML = prompts
            .map(
                (p) => `<div style="display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--outline);">
                <span class="material-symbols-rounded" style="color:var(--on-surface-secondary);font-size:18px;">forum</span>
                <div style="flex:1;font-size:13px;">${escapeHtml(p.prompt)}</div>
                <button class="icon-btn" onclick="deletePrompt(${p.id})"><span class="material-symbols-rounded" style="font-size:18px;">delete</span></button>
            </div>`
            )
            .join("");
    } catch (err) {
        showToast(`Failed to load prompts: ${err.message}`, "error");
    }
}

async function generatePrompts() {
    const storeId = getStoreId();
    const container = document.getElementById("prompts-container");
    container.innerHTML = `<div class="loading-spinner" style="padding:24px 0;"><div class="spinner"></div><p>Generating buyer-intent prompts…</p></div>`;
    try {
        await apiRequest("/api/ai-seo/prompts", { method: "POST", body: { action: "generate", store_id: parseInt(storeId) } });
        showToast("Prompts generated", "success");
        loadPrompts();
    } catch (err) {
        showToast(`Generation failed: ${err.message}`, "error");
        loadPrompts();
    }
}

async function deletePrompt(id) {
    const storeId = getStoreId();
    try {
        await apiRequest("/api/ai-seo/prompts", { method: "POST", body: { action: "delete", store_id: parseInt(storeId), prompt_id: id } });
        loadPrompts();
    } catch (err) {
        showToast(`Delete failed: ${err.message}`, "error");
    }
}

// ── Visibility: tracking runs ───────────────────────────────────
let latestRunId = null;
const visState = { detail: null, competitors: [], productBrands: [], showAllComps: false };

function fmtPct(v) {
    return v == null ? "—" : `${Math.round(+v)}%`;
}

function currentStoreName() {
    const sel = document.getElementById("store-select");
    return sel?.selectedOptions?.[0]?.textContent || "Your store";
}

async function runTracking() {
    const storeId = getStoreId();
    const btn = document.getElementById("track-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded">hourglass_empty</span> Running…';
    try {
        const res = await apiRequest("/api/ai-seo/tracking/run", { method: "POST", body: { store_id: parseInt(storeId) } });
        showToast("Tracking run started — this may take a minute", "info");
        pollRun(res.run_id, btn);
    } catch (err) {
        showToast(`Failed to start: ${err.message}`, "error");
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded">travel_explore</span> Run tracking';
    }
}

function pollRun(runId, btn, idleLabel = '<span class="material-symbols-rounded">travel_explore</span> Run tracking') {
    let consecutiveErrors = 0;
    const timer = setInterval(async () => {
        try {
            const d = await apiRequest(`/api/ai-seo/tracking/${runId}`);
            consecutiveErrors = 0;
            if (["completed", "budget_capped", "failed"].includes(d.status)) {
                clearInterval(timer);
                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = idleLabel;
                }
                if (d.status === "failed") showToast("Tracking failed — see run history for the error", "error");
                else if (d.total_answers)
                    showToast(`Done — your store appeared in ${d.brand_hits} of ${d.total_answers} AI answers`, "success");
                else showToast(`Tracking ${d.status}`, "success");
                loadRuns();
            }
        } catch (err) {
            consecutiveErrors++;
            if (consecutiveErrors >= 5) {
                clearInterval(timer);
                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = idleLabel;
                }
                showToast(`Lost contact with the run (${err.message}). It may still be running — reload to check.`, "error");
            }
        }
    }, 3000);
}

async function loadRuns() {
    const storeId = getStoreId();
    const metrics = document.getElementById("visibility-metrics");
    const summary = document.getElementById("visibility-summary");
    const container = document.getElementById("runs-container");
    try {
        const runs = await apiRequest(`/api/ai-seo/tracking/runs?store_id=${storeId}`);
        const latest = runs.find((r) => ["completed", "budget_capped", "reanalyzing"].includes(r.status));
        renderHeadline(latest, summary, metrics);

        latestRunId = latest ? latest.id : null;
        ["models-panel", "competitors-panel", "brands-panel", "gap-panel", "breakdown-panel"].forEach(
            (id) => (document.getElementById(id).innerHTML = "")
        );
        if (latestRunId) loadRunDetail(latestRunId);
        loadExcluded();

        container.innerHTML = runs.length ? renderRunsTable(runs, latest) : "";
    } catch (err) {
        showToast(`Failed to load runs: ${err.message}`, "error");
    }
}

function renderHeadline(latest, summary, metrics) {
    if (!latest) {
        summary.innerHTML = "";
        metrics.innerHTML = `<div class="empty-state" style="grid-column:1/-1;"><span class="material-symbols-rounded">travel_explore</span><p>No tracking runs yet — click <b>Run tracking</b> to see how the AI models answer your buyers.</p></div>`;
        return;
    }
    const s = latest.summary || {};
    const total = s.total_answers;
    const hits = s.brand_hits;
    const summaryLine =
        total != null
            ? `Last run ${formatRelativeTime(latest.started_at)}: ${latest.prompt_count} buyer questions were asked across ${(latest.models || []).length || "several"} AI models — <b>${total} answers</b> in total. Your store appeared in <b>${hits}</b> of them.`
            : `Last run ${formatRelativeTime(latest.started_at)}.`;
    const zeroCallout =
        total > 0 && hits === 0
            ? `<div style="display:flex;gap:10px;align-items:flex-start;background:var(--warning-light);border:1px solid var(--outline);border-radius:var(--radius-sm);padding:10px 12px;margin-top:10px;">
                <span class="material-symbols-rounded" style="color:var(--warning);font-size:20px;">visibility_off</span>
                <div style="font-size:12.5px;line-height:1.45;">AI models never recommended your store for these buyer questions — your share of voice is going to the retailers in the list below. The <b>gap analysis</b> shows what to change to start appearing.</div>
               </div>`
            : "";
    summary.innerHTML = `<p style="font-size:13px;color:var(--on-surface-secondary);margin:12px 0 0;">${summaryLine}</p>${zeroCallout}`;

    const brandKnown = s.brand_known_rate;
    const retr = s.retrieval;
    const retrCard = retr
        ? `<div class="stat-card"><div class="stat-value">${fmtPct(retr.citation_rate)}</div>
            <div class="stat-label">Live-search citations 🌐</div>
            <div class="stat-label" style="color:var(--on-surface-secondary);">web-grounded answers citing your site (${retr.answers} probed)</div></div>`
        : `<div class="stat-card"><div class="stat-value">${latest.avg_rank ?? "—"}</div>
            <div class="stat-label">Avg position when listed</div>
            <div class="stat-label" style="color:var(--on-surface-secondary);">1 = first recommendation</div></div>`;
    const mentionLabel = s.memory && retr ? "Answers naming your store (memory)" : "Answers naming your store";
    metrics.innerHTML = `
        <div class="stat-card">${renderScoreRing(Math.round(latest.share_of_voice || 0), 56)}
            <div class="stat-label" style="margin-top:8px;">Share of voice</div>
            <div class="stat-label" style="color:var(--on-surface-secondary);">your mentions vs. competitors'</div></div>
        <div class="stat-card"><div class="stat-value">${total != null ? `${hits}<span style="font-size:14px;color:var(--on-surface-secondary);"> / ${total}</span>` : fmtPct(latest.brand_mention_rate)}</div>
            <div class="stat-label">${mentionLabel}</div>
            <div class="stat-label" style="color:var(--on-surface-secondary);">out of all AI answers probed</div></div>
        <div class="stat-card"><div class="stat-value">${fmtPct(brandKnown)}</div>
            <div class="stat-label">Brand knowledge</div>
            <div class="stat-label" style="color:var(--on-surface-secondary);">do models know your store exists</div></div>
        ${retrCard}`;
}

function renderModels(d) {
    const panel = document.getElementById("models-panel");
    const models = d.models || [];
    if (!models.length) {
        panel.innerHTML = "";
        return;
    }
    const brandRows = (d.results || []).filter((r) => r.intent === "brand");
    const shortName = (m) => m.replace(/^[a-z-]+\//, "");
    const pctBadge = (v, goodAt) => {
        if (v == null) return '<span style="color:var(--on-surface-secondary);">—</span>';
        const cls = v >= goodAt ? "badge-success" : v > 0 ? "badge-warning" : "badge-error";
        return `<span class="badge ${cls}">${v}%</span>`;
    };
    const rows = models
        .map((m) => {
            const answers = brandRows.filter((r) => r.model === m.model && r.response_text);
            const answerFold = answers.length
                ? `<details style="margin-top:4px;"><summary style="cursor:pointer;font-size:11.5px;color:var(--on-surface-secondary);">what it says about you (${answers.length})</summary>
                    ${answers.map((r) => `<div style="margin:6px 0;padding:8px 10px;background:var(--surface-dim);border-radius:6px;">
                        <div style="font-size:11px;color:var(--on-surface-secondary);margin-bottom:4px;">${escapeHtml(r.prompt)}</div>
                        <div style="font-size:12px;white-space:pre-wrap;line-height:1.5;">${escapeHtml(r.response_text || "")}</div>
                    </div>`).join("")}</details>`
                : "";
            return `<tr>
                <td><div style="font-weight:500;font-size:13px;">${escapeHtml(shortName(m.model))}${m.grounded ? ' <span title="Answers with live web retrieval">🌐</span>' : ""}</div>
                    <div style="font-size:11px;color:var(--on-surface-secondary);">${m.grounded ? "live web search" : "training-data memory"}</div>${answerFold}</td>
                <td>${pctBadge(m.brand_known_rate, 100)}</td>
                <td>${pctBadge(m.mention_rate, 10)}<div style="font-size:11px;color:var(--on-surface-secondary);">${m.buyer_answers} answers</div></td>
                <td>${pctBadge(m.citation_rate, 5)}</td>
            </tr>`;
        })
        .join("");
    panel.innerHTML = `<div class="card" style="padding:16px;">
        <h3 style="margin:0 0 4px;">Model scoreboard</h3>
        <p style="font-size:12px;color:var(--on-surface-secondary);margin:0 0 10px;">Which AI models know your store, recommend it to buyers, and cite your site. Expand a model to read its actual answers about your brand.</p>
        <div class="table-container"><table>
            <thead><tr>
                <th>Model</th>
                <th title="Answered brand questions about you without an ignorance disclaimer">Knows your brand</th>
                <th title="Buyer-question answers that named your store">Named you</th>
                <th title="Answers that cited your domain">Cited your site</th>
            </tr></thead>
            <tbody>${rows}</tbody></table></div>
    </div>`;
}

function renderRunsTable(runs, latest) {
    const statusBadge = (st) =>
        `<span class="badge ${st === "completed" ? "badge-success" : st === "failed" ? "badge-error" : "badge-warning"}">${st}</span>`;
    const rows = runs
        .map((r) => {
            const s = r.summary || {};
            if (r.status === "failed")
                return `<tr>
                    <td style="font-size:12px;">${formatRelativeTime(r.started_at)}</td>
                    <td>${statusBadge(r.status)}</td>
                    <td colspan="4" style="font-size:12px;color:var(--error);">${escapeHtml(r.error || "Unknown error")}</td>
                    <td></td>
                </tr>`;
            const appeared =
                s.total_answers != null
                    ? `${s.brand_hits} of ${s.total_answers} (${fmtPct(s.total_answers ? (s.brand_hits / s.total_answers) * 100 : null)})`
                    : fmtPct(r.brand_mention_rate);
            const action =
                latest && r.id === latest.id && r.status !== "reanalyzing"
                    ? `<button class="btn btn-secondary btn-sm" title="Re-extract retailers &amp; brands from this run's stored answers — no new AI probing cost" onclick="reanalyzeRun(${r.id}, this)"><span class="material-symbols-rounded">refresh</span> Re-analyze</button>`
                    : "";
            return `<tr>
                <td style="font-size:12px;">${formatRelativeTime(r.started_at)}</td>
                <td>${statusBadge(r.status)}</td>
                <td>${r.prompt_count || 0}</td>
                <td>${appeared}</td>
                <td>${fmtPct(r.share_of_voice)}</td>
                <td style="font-size:12px;">$${(+r.cost_estimate || 0).toFixed(3)}</td>
                <td>${action}</td>
            </tr>`;
        })
        .join("");
    return `<div class="table-container"><table>
        <thead><tr>
            <th>When</th><th>Status</th>
            <th title="Buyer + brand questions asked">Questions</th>
            <th title="AI answers that named your store">You appeared</th>
            <th title="Your mentions as a share of all retailer recommendations">Share of voice</th>
            <th>Cost</th><th></th>
        </tr></thead>
        <tbody>${rows}</tbody></table></div>`;
}

async function reanalyzeRun(runId, btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded">hourglass_empty</span> Re-analyzing…';
    try {
        await apiRequest(`/api/ai-seo/tracking/${runId}/reanalyze`, { method: "POST", body: {} });
        showToast("Re-analyzing the stored answers with improved extraction…", "info");
        pollRun(runId, btn, '<span class="material-symbols-rounded">refresh</span> Re-analyze');
    } catch (err) {
        showToast(`Re-analyze failed: ${err.message}`, "error");
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded">refresh</span> Re-analyze';
    }
}

async function loadRunDetail(runId) {
    try {
        const d = await apiRequest(`/api/ai-seo/tracking/${runId}`);
        visState.detail = d;
        renderModels(d);
        renderLeaderboard(d);
        renderProductBrands(d.product_brands || []);
        renderGap(d.gap_analysis);
        renderBreakdown(d.results || []);
    } catch (err) {
        /* non-fatal */
    }
}

function renderLeaderboard(d) {
    const panel = document.getElementById("competitors-panel");
    const comps = d.competitors || [];
    visState.competitors = comps;
    if (!comps.length && !d.brand_hits) {
        panel.innerHTML = "";
        return;
    }
    const wasOpen = panel.querySelector("details")?.open ?? true;
    const max = Math.max(d.brand_hits || 0, ...comps.map((c) => c.count), 1);
    const bar = (count, you) =>
        `<div class="sov-bar-track"><div class="sov-bar-fill${you ? " sov-bar-you" : ""}" style="width:${Math.max(2, Math.round((count / max) * 100))}%;"></div></div>`;

    const youRow = `<div class="sov-row sov-row-you">
        <span class="sov-rank">—</span>
        <div style="flex:1;min-width:0;">
            <div style="font-size:13px;font-weight:700;">${escapeHtml(currentStoreName())} <span class="badge" style="background:var(--primary-surface);color:var(--primary);margin-left:4px;">you</span></div>
        </div>
        ${bar(d.brand_hits || 0, true)}
        <span class="sov-count" title="AI answers that recommended your store">${d.brand_hits || 0}×</span>
        <span style="width:152px;flex-shrink:0;"></span>
    </div>`;

    const limit = visState.showAllComps ? comps.length : 10;
    const rows = comps
        .slice(0, limit)
        .map((c, i) => {
            const isDomainName = c.domain && c.domain.toLowerCase() === (c.name || "").toLowerCase();
            const name = isDomainName ? c.name.replace(/\.[a-z]{2,}$/i, "") : c.name;
            const site = c.domain
                ? `<a href="https://${escapeHtml(c.domain)}" target="_blank" rel="noopener" class="comp-site"><span class="material-symbols-rounded" style="font-size:13px;">public</span>${escapeHtml(c.domain)}<span style="opacity:.55;">&nbsp;↗</span></a>`
                : `<span class="comp-site" style="color:var(--on-surface-secondary);opacity:.7;"><span class="material-symbols-rounded" style="font-size:13px;">help</span>site not detected</span>`;
            const trackBtn = c.tracked
                ? `<button class="btn btn-secondary btn-sm" style="color:var(--success);" title="Tracking as a competitor — click to stop" onclick="toggleTracked(${i}, this)"><span class="material-symbols-rounded">check</span> Tracking</button>`
                : `<button class="btn btn-secondary btn-sm" title="Track this retailer as a competitor" onclick="toggleTracked(${i}, this)"><span class="material-symbols-rounded">add</span> Track</button>`;
            return `<div class="sov-row">
                <span class="sov-rank">${i + 1}</span>
                <div style="flex:1;min-width:0;">
                    <div style="font-size:13px;font-weight:600;line-height:1.3;">${escapeHtml(name)}</div>
                    <div style="margin-top:1px;">${site}</div>
                </div>
                ${bar(c.count, false)}
                <span class="sov-count" title="AI answers that recommended this retailer">${c.count}×</span>
                <span style="width:152px;flex-shrink:0;display:inline-flex;gap:4px;justify-content:flex-end;">
                    ${trackBtn}
                    <button class="icon-btn" title="Not a competitor — exclude from all results" onclick="excludeBrand('comp', ${i})"><span class="material-symbols-rounded">block</span></button>
                </span>
            </div>`;
        })
        .join("");

    const showAllBtn =
        comps.length > 10
            ? `<button class="btn btn-secondary btn-sm" style="margin-top:10px;" onclick="visState.showAllComps=!visState.showAllComps;renderLeaderboard(visState.detail);">${visState.showAllComps ? "Show top 10" : `Show all ${comps.length}`}</button>`
            : "";
    const missing = comps.filter((c) => !c.domain).length;
    const resolveNote =
        missing && window._domainResolveRun !== latestRunId
            ? `<div style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--on-surface-secondary);margin-top:10px;"><div class="spinner spinner-sm"></div> Finding official sites for ${missing} brand${missing > 1 ? "s" : ""}…</div>`
            : "";

    panel.innerHTML = `<details class="card" style="padding:0;" ${wasOpen ? "open" : ""}>
        <summary style="padding:14px 16px;cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;">
            <span class="material-symbols-rounded" style="color:var(--on-surface-secondary);">social_leaderboard</span>
            <b style="flex:1;">Share of voice — you vs. the retailers AI recommends</b>
            <span class="badge badge-warning">${comps.length} retailer${comps.length === 1 ? "" : "s"}</span>
        </summary>
        <div style="padding:0 16px 16px;">
            <p style="font-size:12px;color:var(--on-surface-secondary);margin:0 0 10px;">Each count is the number of AI answers that recommended that store for your buyer questions. <b>Track</b> a real competitor to keep an eye on it, or <b>exclude</b> anything that isn't one.</p>
            ${youRow}
            ${rows}
            ${showAllBtn}
            ${resolveNote}
            <div><button class="btn btn-primary btn-sm" style="margin-top:12px;" onclick="runGapAnalysis(this)"><span class="material-symbols-rounded">analytics</span> Analyze competitive gap</button></div>
        </div>
    </details>`;

    if (missing && window._domainResolveRun !== latestRunId) {
        window._domainResolveRun = latestRunId;
        resolveCompetitorDomains();
    }
}

async function resolveCompetitorDomains() {
    if (!latestRunId) return;
    try {
        const res = await apiRequest("/api/ai-seo/tracking/resolve-domains", {
            method: "POST",
            body: { store_id: parseInt(getStoreId()), run_id: latestRunId },
        });
        if (res.competitors && visState.detail) {
            visState.detail.competitors = res.competitors;
            renderLeaderboard(visState.detail);
        }
    } catch (err) {
        /* non-fatal — leave "site not detected" rows as-is */
    }
}

async function toggleTracked(index, btn) {
    const c = visState.competitors[index];
    if (!c) return;
    btn.disabled = true;
    const newStatus = c.tracked ? null : "tracked";
    try {
        await apiRequest("/api/ai-seo/tracking/prefs", {
            method: "POST",
            body: { store_id: parseInt(getStoreId()), key: c.key, name: c.name, domain: c.domain, status: newStatus },
        });
        c.tracked = !c.tracked;
        showToast(c.tracked ? `Tracking ${c.name} as a competitor` : `Stopped tracking ${c.name}`, "success");
        renderLeaderboard(visState.detail);
    } catch (err) {
        showToast(`Update failed: ${err.message}`, "error");
        btn.disabled = false;
    }
}

async function excludeBrand(kind, index) {
    const c = kind === "comp" ? visState.competitors[index] : visState.productBrands[index];
    if (!c) return;
    try {
        await apiRequest("/api/ai-seo/tracking/prefs", {
            method: "POST",
            body: { store_id: parseInt(getStoreId()), key: c.key, name: c.name, domain: c.domain || null, status: "excluded" },
        });
        showToast(`Excluded "${c.name}" — undo anytime under Excluded brands`, "success");
        if (latestRunId) loadRunDetail(latestRunId);
        loadExcluded();
    } catch (err) {
        showToast(`Exclude failed: ${err.message}`, "error");
    }
}

function renderProductBrands(brands) {
    const panel = document.getElementById("brands-panel");
    visState.productBrands = brands;
    if (!brands.length) {
        panel.innerHTML = "";
        return;
    }
    const carried = brands.filter((b) => b.carried).length;
    const rows = brands
        .map(
            (b, i) => `<div class="sov-row">
            <div style="flex:1;min-width:0;font-size:13px;font-weight:600;">${escapeHtml(b.name)}
                ${b.carried ? '<span class="badge badge-success" style="margin-left:6px;" title="Matches a vendor in your synced catalog">you carry this</span>' : ""}
            </div>
            <span class="sov-count" title="AI answers that mentioned this brand">${b.count}×</span>
            <button class="icon-btn" title="Exclude from all results" onclick="excludeBrand('brand', ${i})"><span class="material-symbols-rounded">block</span></button>
        </div>`
        )
        .join("");
    panel.innerHTML = `<details class="card" style="padding:0;">
        <summary style="padding:14px 16px;cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;">
            <span class="material-symbols-rounded" style="color:var(--on-surface-secondary);">sell</span>
            <b style="flex:1;">Product brands in AI answers</b>
            <span style="font-size:12px;color:var(--on-surface-secondary);">${carried} of ${brands.length} are brands you carry</span>
        </summary>
        <div style="padding:0 16px 16px;">
            <p style="font-size:12px;color:var(--on-surface-secondary);margin:0 0 8px;">Products the AI talks about for your buyer questions — not competitors, but a signal of what to stock and feature. Kept out of the share-of-voice math.</p>
            ${rows}
        </div>
    </details>`;
}

function renderBreakdown(results) {
    const panel = document.getElementById("breakdown-panel");
    const buyer = results.filter((r) => (r.intent || "buyer") !== "brand");
    if (!buyer.length) {
        panel.innerHTML = "";
        return;
    }
    const byPrompt = new Map();
    for (const r of buyer) {
        if (!byPrompt.has(r.prompt)) byPrompt.set(r.prompt, []);
        byPrompt.get(r.prompt).push(r);
    }
    const groups = [...byPrompt.entries()]
        .map(([prompt, rows]) => {
            const hits = rows.filter((r) => r.brand_mentioned).length;
            const recCounts = {};
            for (const r of rows)
                for (const b of r.recommended_brands || []) {
                    if (!b.name) continue;
                    recCounts[b.name] = (recCounts[b.name] || 0) + 1;
                }
            const top = Object.entries(recCounts).sort((a, b) => b[1] - a[1]).slice(0, 6);
            const perAnswer = rows
                .map((r) => {
                    const cites = (r.citations || []).slice(0, 5);
                    const citeLine = r.grounded && cites.length
                        ? `<div style="font-size:11px;color:var(--on-surface-secondary);padding:0 0 2px 23px;">cites: ${cites.map((u) => { try { return escapeHtml(new URL(u).hostname.replace(/^www\./, "")); } catch (e) { return ""; } }).filter(Boolean).join(", ")}</div>`
                        : "";
                    const answerFold = r.response_text
                        ? `<details style="padding:0 0 2px 23px;"><summary style="cursor:pointer;font-size:11px;color:var(--on-surface-secondary);">view full answer</summary>
                            <div style="font-size:12px;white-space:pre-wrap;line-height:1.5;background:var(--surface-dim);border-radius:6px;padding:8px 10px;margin:4px 0;">${escapeHtml(r.response_text)}</div></details>`
                        : "";
                    return `<div style="display:flex;gap:8px;align-items:baseline;padding:4px 0;font-size:12px;">
                    <span class="material-symbols-rounded" style="font-size:15px;color:${r.brand_mentioned ? "var(--success)" : "var(--on-surface-secondary)"};">${r.brand_mentioned ? "check_circle" : "radio_button_unchecked"}</span>
                    <span style="min-width:170px;color:var(--on-surface-secondary);">${escapeHtml(r.model)} · run ${(r.run_index ?? 0) + 1}${r.grounded ? ' <span title="Web-grounded answer (live retrieval)">🌐</span>' : ""}</span>
                    <span style="flex:1;">${(r.recommended_brands || []).map((b) => escapeHtml(b.name)).join(", ") || "<i>no retailers named</i>"}</span>
                </div>${citeLine}${answerFold}`;
                })
                .join("");
            return `<details style="border-bottom:1px solid var(--outline);padding:8px 0;">
                <summary style="cursor:pointer;list-style:none;display:flex;gap:10px;align-items:baseline;">
                    <span class="badge ${hits ? "badge-success" : "badge-warning"}" style="flex-shrink:0;" title="Answers naming your store for this question">${hits}/${rows.length}</span>
                    <span style="flex:1;font-size:13px;">${escapeHtml(prompt)}</span>
                </summary>
                <div style="padding:6px 0 4px 8px;">
                    ${top.length ? `<div style="font-size:12px;color:var(--on-surface-secondary);margin-bottom:4px;">AI recommends: ${top.map(([n, c]) => `${escapeHtml(n)} (${c}×)`).join(", ")}</div>` : ""}
                    ${perAnswer}
                </div>
            </details>`;
        })
        .join("");
    panel.innerHTML = `<details class="card" style="padding:0;">
        <summary style="padding:14px 16px;cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;">
            <span class="material-symbols-rounded" style="color:var(--on-surface-secondary);">quiz</span>
            <b style="flex:1;">Question-by-question breakdown</b>
            <span style="font-size:12px;color:var(--on-surface-secondary);">what each model actually answered</span>
        </summary>
        <div style="padding:0 16px 16px;">${groups}</div>
    </details>`;
}

async function loadExcluded() {
    const panel = document.getElementById("excluded-panel");
    try {
        const prefs = await apiRequest(`/api/ai-seo/tracking/prefs?store_id=${getStoreId()}`);
        const excluded = prefs.excluded || [];
        if (!excluded.length) {
            panel.innerHTML = "";
            return;
        }
        const rows = excluded
            .map(
                (p) => `<div class="sov-row">
                <div style="flex:1;font-size:13px;">${escapeHtml(p.name || p.brand_key)}</div>
                <button class="btn btn-secondary btn-sm" onclick="restorePref('${escapeHtml(p.brand_key)}')"><span class="material-symbols-rounded">undo</span> Restore</button>
            </div>`
            )
            .join("");
        panel.innerHTML = `<details class="card" style="padding:0;">
            <summary style="padding:14px 16px;cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;">
                <span class="material-symbols-rounded" style="color:var(--on-surface-secondary);">block</span>
                <b style="flex:1;">Excluded brands</b>
                <span class="badge" style="background:var(--surface-dim);color:var(--on-surface);">${excluded.length}</span>
            </summary>
            <div style="padding:0 16px 16px;">
                <p style="font-size:12px;color:var(--on-surface-secondary);margin:0 0 8px;">Hidden from every run's results. Restore one to see it again.</p>
                ${rows}
            </div>
        </details>`;
    } catch (err) {
        /* non-fatal */
    }
}

async function restorePref(key) {
    try {
        await apiRequest("/api/ai-seo/tracking/prefs", {
            method: "POST",
            body: { store_id: parseInt(getStoreId()), key, status: null },
        });
        showToast("Restored", "success");
        loadExcluded();
        if (latestRunId) loadRunDetail(latestRunId);
    } catch (err) {
        showToast(`Restore failed: ${err.message}`, "error");
    }
}

async function runGapAnalysis(btn) {
    if (!latestRunId) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-rounded">hourglass_empty</span> Analyzing…';
    const gap = document.getElementById("gap-panel");
    gap.innerHTML = `<div class="loading-spinner" style="padding:24px 0;"><div class="spinner"></div><p>Crawling the competitor sites and analyzing the gap… (up to ~30s)</p></div>`;
    try {
        const res = await apiRequest(`/api/ai-seo/tracking/${latestRunId}/gap-analysis`, { method: "POST", body: {} });
        renderGap(res.gap_analysis);
        showToast("Gap analysis ready", "success");
    } catch (err) {
        gap.innerHTML = `<div class="empty-state"><span class="material-symbols-rounded">error</span><p>${escapeHtml(err.message)}</p></div>`;
    }
    btn.disabled = false;
    btn.innerHTML = '<span class="material-symbols-rounded">analytics</span> Analyze competitive gap';
}

function renderGap(gap) {
    const panel = document.getElementById("gap-panel");
    if (!gap) {
        panel.innerHTML = "";
        return;
    }
    const chip = "background:var(--surface-dim);color:var(--on-surface);";
    const pageRecs = (gap.page_improvements || gap.recommendations || [])
        .map((r) => {
            const pc = r.priority === "high" ? "badge-error" : r.priority === "medium" ? "badge-warning" : "badge-success";
            const applies = r.applies_to ? `<span class="badge" style="${chip}">${escapeHtml(r.applies_to)}</span>` : "";
            return `<div style="padding:10px 0;border-bottom:1px solid var(--outline);">
                <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;"><span class="badge ${pc}">${escapeHtml(r.priority || "")}</span>${applies}<b>${escapeHtml(r.title || "")}</b></div>
                <div style="font-size:13px;color:var(--on-surface-secondary);margin:4px 0;"><b>Why:</b> ${escapeHtml(r.why || "")}</div>
                <div style="font-size:13px;"><b>Do this:</b> ${escapeHtml(r.action || "")}</div>
            </div>`;
        })
        .join("");
    const contentRecs = (gap.content_ideas || [])
        .map((r) => `<div style="padding:10px 0;border-bottom:1px solid var(--outline);">
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">${r.format ? `<span class="badge" style="${chip}">${escapeHtml(r.format)}</span>` : ""}<b>${escapeHtml(r.title || "")}</b></div>
            ${r.target_query ? `<div style="font-size:12px;color:var(--on-surface-secondary);margin:3px 0;">Targets query: “${escapeHtml(r.target_query)}”</div>` : ""}
            <div style="font-size:13px;">${escapeHtml(r.why || "")}</div>
        </div>`)
        .join("");
    panel.innerHTML = `<div class="card" style="padding:16px;">
        <h3 style="margin:0 0 6px;">Competitive gap analysis</h3>
        <p style="font-size:13px;margin:0 0 10px;">${escapeHtml(gap.summary || "")}</p>
        ${gap.competitors_analyzed?.length ? `<p style="font-size:11px;color:var(--on-surface-secondary);margin:0 0 12px;">Crawled: ${gap.competitors_analyzed.map(escapeHtml).join(", ")}</p>` : ""}
        ${pageRecs ? `<h4 style="margin:12px 0 4px;">Improve your product &amp; collection pages</h4>${pageRecs}` : ""}
        ${contentRecs ? `<h4 style="margin:16px 0 4px;">Content to create to compete</h4>${contentRecs}` : ""}
    </div>`;
}

document.addEventListener("DOMContentLoaded", async () => {
    initThemeToggle();
    initSidebar();
    setScanType(scanType);
    await loadStores();
    loadOverview();
    loadItems();
});
