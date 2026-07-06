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
            loadOverview();
            loadItems();
            if (document.querySelector(".tab.active")?.dataset.tab === "visibility") switchTab("visibility");
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

function pollBatch(batchId) {
    if (batchPollTimer) clearInterval(batchPollTimer);
    batchPollTimer = setInterval(async () => {
        try {
            const s = await apiRequest(`/api/ai-seo/optimize/batch/${batchId}`);
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
            clearInterval(batchPollTimer);
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
            <div style="border:1px solid var(--outline);border-radius:8px;padding:12px;max-height:340px;overflow:auto;">${p.optimized_description_html || ""}</div>
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
    scanPollTimer = setInterval(async () => {
        try {
            const task = await apiRequest(`/api/ai-seo/scan/${taskId}`);
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
            clearInterval(scanPollTimer);
            resetScanBtn();
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

function switchTab(tab) {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
    if (tab === "visibility" && !visibilityLoaded) {
        visibilityLoaded = true;
        loadPrompts();
        loadRuns();
    }
    if (tab === "assets") loadAeo();
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
            robotsCard(a.robots) +
            llmsCard(a.llms, a.base_url) +
            orgCard(a.organization);
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

function aeoImpact(targetText, improveHtml) {
    return `<div style="display:flex;gap:8px;align-items:flex-start;background:var(--primary-light,rgba(59,130,246,.1));border-left:3px solid var(--primary);border-radius:6px;padding:8px 12px;margin:6px 0 12px;">
        <span class="material-symbols-rounded" style="color:var(--primary);font-size:18px;">target</span>
        <div style="font-size:12px;color:var(--on-surface);">
            <div>${targetText}</div>
            ${improveHtml ? `<div style="margin-top:4px;">${improveHtml}</div>` : ""}
        </div>
    </div>`;
}

function aeoShell(title, desc, targetText, improveHtml, key, applyLabel, statusHtml, currentHtml, proposed) {
    return `<div class="card" style="margin-bottom:16px;padding:16px;">
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
            <h3 style="margin:0;flex:1;">${title}</h3>
            ${statusHtml}
            <button class="btn btn-secondary btn-sm" onclick="downloadAeo('${key}')"><span class="material-symbols-rounded">download</span></button>
            <button class="btn btn-primary btn-sm" onclick="applyAeo('${key}', this)"><span class="material-symbols-rounded">cloud_upload</span> ${applyLabel}</button>
        </div>
        <p style="font-size:12px;color:var(--on-surface-secondary);margin:6px 0 8px;">${desc}</p>
        ${aeoImpact(targetText, improveHtml)}
        <div style="font-weight:600;font-size:13px;margin-bottom:6px;">Current on your store</div>
        ${currentHtml}
        <details style="margin-top:12px;">
            <summary style="cursor:pointer;font-weight:600;font-size:13px;">Proposed content</summary>
            <pre style="${AEO_PRE}">${escapeHtml(proposed || "")}</pre>
        </details>
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
    return aeoShell("robots.txt AI rules",
        "Explicit Allow rules for GPTBot, ClaudeBot, PerplexityBot, Google-Extended and more; blocks cart/checkout. Publishing writes templates/robots.txt.liquid to your theme.",
        "<b>Targets the AI crawlability signal.</b> If answer engines can't fetch your pages, they can't cite you — this guarantees GPTBot, ClaudeBot, PerplexityBot & Google-Extended are allowed while keeping cart/checkout private.",
        improveLine((r.impact || {}).signal, (r.impact || {}).current, (r.impact || {}).projected),
        "robots", "Publish to theme", statusPill(r.published_status), current, r.proposed);
}

function llmsCard(l) {
    const c = l.current || {};
    const current = c.present
        ? `<div style="font-size:13px;">Present at <a href="${c.url}" target="_blank">${escapeHtml(c.url)}</a> (${c.location})</div>${aeoDetails("View current llms.txt", c.raw)}`
        : `<div style="color:var(--on-surface-secondary);font-size:13px;">No llms.txt found on your store yet.</div>`;
    return aeoShell("llms.txt index",
        "A Markdown index of your collections & products for AI crawlers. Shopify can't serve /llms.txt at the domain root, so this publishes a /pages/llms page (download to self-host at root).",
        "<b>Targets discoverability & structured-data coverage.</b> Gives AI crawlers a clean map of your key collections and products so they index the right pages faster and pull accurate product facts into answers.",
        improveLine((l.impact || {}).signal, (l.impact || {}).current, (l.impact || {}).projected),
        "llms", "Publish /pages/llms", statusPill(l.published_status), current, l.proposed);
}

function orgCard(o) {
    const c = o.current || {};
    const current = c.configured
        ? `<div style="font-size:13px;">Name: <b>${escapeHtml(c.name || "—")}</b>${c.url ? ` · URL: ${escapeHtml(c.url)}` : ""}${c.logo ? " · logo set" : " · <span style='color:var(--warning)'>no logo</span>"}</div>`
        : `<div style="color:var(--on-surface-secondary);font-size:13px;">Organization details not configured (set them in Schema settings).</div>`;
    const im = o.impact || {};
    const orgImprove = im.current_avg != null
        ? `<b>Improves Entity authority:</b> your pages average ${im.current_avg}/100 now — adding site-wide Organization schema lifts pages missing it by +${im.uplift}.`
        : `<b>Improves Entity authority:</b> adds a site-wide brand entity signal (+${im.uplift || 30} on pages that lack it).`;
    return aeoShell("Organization JSON-LD",
        "Site-wide brand entity schema. Generates a snippet to paste into your theme (layout/theme.liquid).",
        "<b>Targets the entity-authority signal.</b> Helps AI models recognise your brand as a known entity (name, URL, logo) — a trust signal that influences which sources get cited.",
        orgImprove,
        "schema", "Generate snippet", statusPill(o.published_status), current,
        JSON.stringify(o.proposed, null, 2));
}

function downloadAeo(key) {
    const map = { robots: window._aeo.robots.proposed, llms: window._aeo.llms.proposed, schema: JSON.stringify(window._aeo.organization.proposed, null, 2) };
    const names = { robots: "robots.txt.liquid", llms: "llms.txt", schema: "organization.jsonld" };
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

function pollRun(runId, btn) {
    const timer = setInterval(async () => {
        try {
            const d = await apiRequest(`/api/ai-seo/tracking/${runId}`);
            if (["completed", "budget_capped", "failed"].includes(d.status)) {
                clearInterval(timer);
                btn.disabled = false;
                btn.innerHTML = '<span class="material-symbols-rounded">travel_explore</span> Run tracking';
                if (d.status === "failed") showToast("Tracking failed", "error");
                else showToast(`Tracking ${d.status} — SoV ${d.share_of_voice ?? 0}%`, "success");
                loadRuns();
            }
        } catch (err) {
            clearInterval(timer);
            btn.disabled = false;
            btn.innerHTML = '<span class="material-symbols-rounded">travel_explore</span> Run tracking';
        }
    }, 3000);
}

let latestRunId = null;

async function loadRuns() {
    const storeId = getStoreId();
    const metrics = document.getElementById("visibility-metrics");
    const container = document.getElementById("runs-container");
    try {
        const runs = await apiRequest(`/api/ai-seo/tracking/runs?store_id=${storeId}`);
        const latest = runs.find((r) => r.status === "completed" || r.status === "budget_capped");
        const brandKnown = latest?.summary?.brand_known_rate;
        metrics.innerHTML = latest
            ? `<div class="stat-card">${renderScoreRing(Math.round(latest.share_of_voice || 0), 56)}<div class="stat-label" style="margin-top:8px;">Share of voice</div></div>
               <div class="stat-card"><div class="stat-value">${latest.brand_mention_rate ?? 0}%</div><div class="stat-label">Brand mention rate</div></div>
               <div class="stat-card"><div class="stat-value">${brandKnown != null ? brandKnown + "%" : "—"}</div><div class="stat-label">Brand knowledge (do models know you)</div></div>
               <div class="stat-card"><div class="stat-value">${latest.avg_rank ?? "—"}</div><div class="stat-label">Avg rank when listed</div></div>`
            : `<div class="empty-state" style="grid-column:1/-1;"><span class="material-symbols-rounded">travel_explore</span><p>No tracking runs yet.</p></div>`;

        latestRunId = latest ? latest.id : null;
        document.getElementById("competitors-panel").innerHTML = "";
        document.getElementById("gap-panel").innerHTML = "";
        if (latestRunId) loadRunDetail(latestRunId);

        if (!runs.length) {
            container.innerHTML = "";
            return;
        }
        container.innerHTML = `<div class="table-container"><table>
            <thead><tr><th>When</th><th>Status</th><th>Prompts</th><th>SoV</th><th>Mentions</th><th>Citations</th><th>Cost</th></tr></thead>
            <tbody>${runs
                .map(
                    (r) => `<tr>
                    <td style="font-size:12px;">${formatRelativeTime(r.started_at)}</td>
                    <td><span class="badge ${r.status === "completed" ? "badge-success" : r.status === "failed" ? "badge-error" : "badge-warning"}">${r.status}</span></td>
                    <td>${r.prompt_count || 0}</td>
                    <td>${r.share_of_voice ?? "—"}%</td>
                    <td>${r.brand_mention_rate ?? "—"}%</td>
                    <td>${r.citation_rate ?? "—"}%</td>
                    <td style="font-size:12px;">$${(r.cost_estimate || 0)}</td>
                </tr>`
                )
                .join("")}</tbody></table></div>`;
    } catch (err) {
        showToast(`Failed to load runs: ${err.message}`, "error");
    }
}

async function loadRunDetail(runId) {
    try {
        const d = await apiRequest(`/api/ai-seo/tracking/${runId}`);
        renderCompetitors(d.competitors || []);
        renderGap(d.gap_analysis);
    } catch (err) {
        /* non-fatal */
    }
}

function renderCompetitors(comps) {
    const panel = document.getElementById("competitors-panel");
    window._competitors = comps;
    if (!comps.length) {
        panel.innerHTML = "";
        return;
    }
    const rows = comps
        .map((c, i) => {
            const site = c.domain && c.domain.toLowerCase() !== (c.name || "").toLowerCase()
                ? `<a href="https://${escapeHtml(c.domain)}" target="_blank" rel="noopener" style="font-size:11px;color:var(--on-surface-secondary);margin-left:8px;text-decoration:none;">${escapeHtml(c.domain)} ↗</a>`
                : "";
            return `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--outline);">
                <div style="flex:1;min-width:0;"><span style="font-size:13px;font-weight:500;">${escapeHtml(c.name)}</span>${site}</div>
                <span class="badge badge-warning">${c.count}× recommended</span>
                ${c.is_configured
                    ? '<span class="badge badge-success">tracked</span>'
                    : `<button class="btn btn-secondary btn-sm" onclick="addCompetitor(${i}, this)"><span class="material-symbols-rounded">add</span> Add</button>`}
                <button class="icon-btn" title="Exclude from future results" onclick="excludeCompetitor(${i})"><span class="material-symbols-rounded">block</span></button>
            </div>`;
        })
        .join("");
    panel.innerHTML = `<div class="card" style="padding:16px;">
        <h3 style="margin:0 0 4px;">Who the AI recommends instead</h3>
        <p style="font-size:12px;color:var(--on-surface-secondary);margin:0 0 10px;">Brands/retailers surfaced in AI answers to your buyer queries — this is where your share of voice is going. Add any to your competitor list, or exclude ones that aren't real competitors (e.g. product brands you sell).</p>
        ${rows}
        <button class="btn btn-primary btn-sm" style="margin-top:12px;" onclick="runGapAnalysis(this)"><span class="material-symbols-rounded">analytics</span> Analyze competitive gap</button>
    </div>`;
}

async function addCompetitor(index, btn) {
    const c = window._competitors?.[index];
    if (!c) return;
    btn.disabled = true;
    try {
        await apiRequest("/api/ai-seo/tracking/competitor", {
            method: "POST",
            body: { store_id: parseInt(getStoreId()), name: c.name, domain: c.domain },
        });
        showToast(`Added ${c.name} to competitors`, "success");
        btn.outerHTML = '<span class="badge badge-success">tracked</span>';
    } catch (err) {
        showToast(`Add failed: ${err.message}`, "error");
        btn.disabled = false;
    }
}

async function excludeCompetitor(index) {
    const c = window._competitors?.[index];
    if (!c) return;
    try {
        await apiRequest("/api/ai-seo/tracking/exclude", {
            method: "POST",
            body: { store_id: parseInt(getStoreId()), key: c.key, name: c.name, domain: c.domain },
        });
        showToast(`Excluded "${c.name}" from future results`, "success");
        if (latestRunId) loadRunDetail(latestRunId);
    } catch (err) {
        showToast(`Exclude failed: ${err.message}`, "error");
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
