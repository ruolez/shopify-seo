/* Keyword Strategy & Site Audit */
let ksStoreId = null;
let ksPlanPage = 0;
let ksKwSort = { col: "search_volume", dir: "desc" };
let ksKwPage = 1;
const KS_PAGE_SIZE = 50;
let ksActiveStep = "keywords";
let ksAuditSort = { col: "keyword_count", dir: "desc" };
let ksAuditPage = 1;
const KS_AUDIT_PAGE_SIZE = 50;

// ── Init ─────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
    loadKsStores();
    initStepNav();
    initKsEvents();
});

async function loadKsStores() {
    const stores = await apiRequest("/api/stores");
    const sel = document.getElementById("ks-store-select");
    stores.forEach(s => { const o = document.createElement("option"); o.value = s.id; o.textContent = s.name; sel.appendChild(o); });
    // The select is hidden — the global topbar StoreSelector (ui.js) drives it.
    StoreSelector.bindLegacy(sel);
    const globalId = StoreSelector.getStoreId();
    if (globalId && stores.some(s => s.id === globalId)) { sel.value = globalId; onKsStore(globalId); }
    else if (stores.length === 1) { sel.value = stores[0].id; onKsStore(stores[0].id); }
    else {
        // StoreSelector.init() may still be resolving — try once more shortly.
        setTimeout(() => {
            const g = StoreSelector.getStoreId();
            if (g && !sel.value) { sel.value = g; onKsStore(g); }
        }, 600);
    }
}

document.getElementById("ks-store-select").addEventListener("change", e => { if (e.target.value) onKsStore(parseInt(e.target.value)); });

function onKsStore(id) {
    ksStoreId = id;
    loadKwOverview();
    loadKeywords();
    loadAuditStatus();
    loadPlanData();
    loadChanges();
}

// ── Step Navigation ──────────────────────────────────────────────────────
function initStepNav() {
    document.querySelectorAll(".ks-step").forEach(step => {
        step.addEventListener("click", () => {
            switchToStep(step.dataset.step);
        });
    });

    // Audit sub-tabs
    document.querySelectorAll(".ks-sub-tab").forEach(btn => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".ks-sub-tab").forEach(b => b.classList.remove("active"));
            document.querySelectorAll(".audit-subtab").forEach(c => c.style.display = "none");
            btn.classList.add("active");
            const target = document.getElementById("audit-sub-" + btn.dataset.subtab);
            if (target) target.style.display = "";
        });
    });
}

function switchToStep(step) {
    ksActiveStep = step;
    // Update step buttons
    document.querySelectorAll(".ks-step").forEach(s => s.classList.remove("active"));
    const activeStep = document.querySelector(`.ks-step[data-step="${step}"]`);
    if (activeStep) activeStep.classList.add("active");

    // Show/hide panels
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    const panel = document.getElementById("panel-" + step);
    if (panel) panel.classList.add("active");
}

// ── Events ───────────────────────────────────────────────────────────────
function initKsEvents() {
    // Consolidate
    document.getElementById("ks-consolidate-btn").addEventListener("click", runConsolidate);
    document.getElementById("ks-cluster-btn").addEventListener("click", runClustering);
    document.getElementById("ks-add-kw-btn").addEventListener("click", () => document.getElementById("ks-add-modal").classList.add("active"));
    document.getElementById("ks-save-kw-btn").addEventListener("click", saveManualKeyword);

    // Search / filters
    let kwSearchT;
    document.getElementById("ks-kw-search").addEventListener("input", () => { clearTimeout(kwSearchT); kwSearchT = setTimeout(() => { ksKwPage = 1; loadKeywords(); }, 300); });
    ["ks-intent-filter", "ks-source-filter", "ks-cluster-filter"].forEach(id =>
        document.getElementById(id).addEventListener("change", () => { ksKwPage = 1; loadKeywords(); }));

    // Sort
    document.querySelectorAll("#panel-keywords .sortable").forEach(th => {
        th.addEventListener("click", () => {
            const col = th.dataset.sort;
            if (ksKwSort.col === col) ksKwSort.dir = ksKwSort.dir === "asc" ? "desc" : "asc";
            else { ksKwSort.col = col; ksKwSort.dir = "desc"; }
            loadKeywords();
        });
    });

    // Audit
    document.getElementById("ks-audit-btn").addEventListener("click", runAudit);
    let auditSearchT;
    document.getElementById("ks-audit-search").addEventListener("input", () => { clearTimeout(auditSearchT); auditSearchT = setTimeout(() => { ksAuditPage = 1; loadAuditResults(); }, 300); });
    document.getElementById("ks-audit-type-filter").addEventListener("change", () => { ksAuditPage = 1; loadAuditResults(); });
    document.getElementById("ks-uncovered-min").addEventListener("change", loadUncoveredKeywords);

    // Audit column sorting
    document.querySelectorAll("#panel-audit .sortable").forEach(th => {
        th.addEventListener("click", () => {
            const col = th.dataset.sort;
            if (ksAuditSort.col === col) ksAuditSort.dir = ksAuditSort.dir === "asc" ? "desc" : "asc";
            else { ksAuditSort.col = col; ksAuditSort.dir = "desc"; }
            ksAuditPage = 1;
            loadAuditResults();
        });
    });

    // Plan
    document.getElementById("ks-plan-btn").addEventListener("click", runPlanGeneration);
    document.getElementById("ks-bulk-accept-btn").addEventListener("click", bulkAcceptPlan);
    document.getElementById("ks-plan-select-all").addEventListener("change", e => {
        document.querySelectorAll(".plan-checkbox").forEach(cb => cb.checked = e.target.checked);
        toggleBulkAccept();
    });
    let planSearchT;
    document.getElementById("ks-plan-search").addEventListener("input", () => { clearTimeout(planSearchT); planSearchT = setTimeout(() => { ksPlanPage = 0; loadPlanData(); }, 300); });
    ["ks-plan-status-filter", "ks-plan-type-filter", "ks-plan-priority-filter", "ks-plan-per-page"].forEach(id =>
        document.getElementById(id).addEventListener("change", () => { ksPlanPage = 0; loadPlanData(); }));
}

// ── Progress bar ─────────────────────────────────────────────────────────
function showProgress(text, percent) {
    const wrap = document.getElementById("ks-progress-wrap");
    wrap.classList.add("visible");
    document.getElementById("ks-progress-fill").style.width = percent + "%";
    document.getElementById("ks-progress-text").textContent = text;
}

function hideProgress() {
    document.getElementById("ks-progress-wrap").classList.remove("visible");
}

function markStepCompleted(step) {
    const el = document.querySelector(`.ks-step[data-step="${step}"]`);
    if (el) el.classList.add("completed");
}

// ── Phase 1: Keywords ────────────────────────────────────────────────────
async function runConsolidate() {
    if (!ksStoreId) return;
    const btn = document.getElementById("ks-consolidate-btn");
    return guardButton(btn, async () => {
        btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Consolidating...';
        showProgress("Consolidating keywords from Semrush & GSC...", 30);
        try {
            await apiRequest(`/api/keyword-strategy/consolidate?store_id=${ksStoreId}`, { method: "POST" });
            pollRun("consolidate", (run) => {
                hideProgress();
                loadKwOverview();
                loadKeywords();
                markStepCompleted("keywords");
                showToast("Keywords consolidated", "success");
            }, (run) => {
                const pct = run.total_items > 0 ? Math.round((run.completed_items / run.total_items) * 100) : 30;
                showProgress(`Consolidating... ${(run.completed_items || 0).toLocaleString()} keywords`, pct);
            });
        } catch (e) { showToast("Consolidation failed: " + e.message, "error"); hideProgress(); }
        finally { btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">sync</span> Consolidate Keywords'; }
    });
}

async function runClustering() {
    if (!ksStoreId) return;
    const btn = document.getElementById("ks-cluster-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Clustering...';
    try {
        const r = await apiRequest(`/api/keyword-strategy/clusters/generate?store_id=${ksStoreId}`, { method: "POST" });
        showToast(`Created ${r.clusters} clusters from ${r.keywords} keywords`, "success");
        loadKwOverview(); loadKeywords(); loadClusters();
    } catch (e) { showToast("Clustering failed: " + e.message, "error"); }
    finally { btn.disabled = false; btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">hub</span> Auto-Cluster'; }
}

async function saveManualKeyword() {
    if (!ksStoreId) return;
    const kw = document.getElementById("ks-new-keyword").value.trim();
    if (!kw) { showToast("Keyword is required", "error"); return; }
    try {
        await apiRequest(`/api/keyword-strategy/keywords?store_id=${ksStoreId}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                keyword: kw,
                search_volume: parseInt(document.getElementById("ks-new-volume").value) || 0,
                intent: document.getElementById("ks-new-intent").value || null,
            }),
        });
        showToast("Keyword added", "success");
        document.getElementById("ks-add-modal").classList.remove("active");
        document.getElementById("ks-new-keyword").value = "";
        document.getElementById("ks-new-volume").value = "";
        loadKwOverview(); loadKeywords();
    } catch (e) { showToast("Failed: " + e.message, "error"); }
}

async function loadKwOverview() {
    if (!ksStoreId) return;
    try {
        const d = await apiRequest(`/api/keyword-strategy/keywords/overview?store_id=${ksStoreId}`);
        if (!d || !d.total_active) { document.getElementById("ks-overview").style.display = "none"; return; }
        document.getElementById("ks-overview").style.display = "";
        document.getElementById("ks-total").textContent = (d.total_active || 0).toLocaleString();
        document.getElementById("ks-volume").textContent = (d.total_volume || 0).toLocaleString();
        document.getElementById("ks-semrush").textContent = (d.from_semrush || 0).toLocaleString();
        document.getElementById("ks-gsc").textContent = (d.from_gsc || 0).toLocaleString();
        document.getElementById("ks-both").textContent = (d.from_both || 0).toLocaleString();
        document.getElementById("ks-clusters").textContent = (d.clusters_count || 0).toLocaleString();
        // Update step description
        document.getElementById("ks-step-kw-desc").textContent = `${(d.total_active || 0).toLocaleString()} keywords`;
        if (d.total_active > 0) markStepCompleted("keywords");
        loadClusters();
    } catch (e) {}
}

async function loadClusters() {
    if (!ksStoreId) return;
    try {
        const d = await apiRequest(`/api/keyword-strategy/clusters?store_id=${ksStoreId}`);
        const sel = document.getElementById("ks-cluster-filter");
        const val = sel.value;
        sel.innerHTML = '<option value="">All Clusters</option>';
        (d.clusters || []).forEach(c => {
            const o = document.createElement("option");
            o.value = c.id;
            o.textContent = `${c.name} (${c.keyword_count})`;
            sel.appendChild(o);
        });
        sel.value = val;
    } catch (e) {}
}

async function loadKeywords() {
    if (!ksStoreId) return;
    document.getElementById("ks-kw-content").style.display = "";
    const tbody = document.getElementById("ks-kw-tbody");
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center"><div class="spinner" style="margin:20px auto"></div></td></tr>';

    const search = document.getElementById("ks-kw-search").value.trim();
    const intent = document.getElementById("ks-intent-filter").value;
    const source = document.getElementById("ks-source-filter").value;
    const cluster = document.getElementById("ks-cluster-filter").value;

    let url = `/api/keyword-strategy/keywords?store_id=${ksStoreId}&sort_by=${ksKwSort.col}&sort_dir=${ksKwSort.dir}&limit=${KS_PAGE_SIZE}&offset=${(ksKwPage-1)*KS_PAGE_SIZE}`;
    if (search) url += `&search=${encodeURIComponent(search)}`;
    if (intent) url += `&intent=${intent}`;
    if (source) url += `&source=${source}`;
    if (cluster) url += `&cluster_id=${cluster}`;

    try {
        const d = await apiRequest(url);
        const kws = d.keywords || [];
        const total = d.total || 0;
        document.getElementById("ks-kw-count").textContent = `${total.toLocaleString()} keywords`;

        if (!kws.length) {
            tbody.innerHTML = "";
            document.getElementById("ks-kw-empty").style.display = "";
            document.getElementById("ks-kw-pagination").innerHTML = "";
            return;
        }
        document.getElementById("ks-kw-empty").style.display = "none";

        tbody.innerHTML = kws.map(kw => `<tr>
            <td style="max-width:250px;word-break:break-word;">${escapeHtml(kw.keyword)}</td>
            <td>${(kw.search_volume || 0).toLocaleString()}</td>
            <td>${kw.best_position ? parseFloat(kw.best_position).toFixed(1) : '-'}</td>
            <td>${(kw.clicks || 0).toLocaleString()}</td>
            <td>${(kw.impressions || 0).toLocaleString()}</td>
            <td>${kw.ctr ? (parseFloat(kw.ctr)*100).toFixed(1) + '%' : '-'}</td>
            <td><span class="badge badge-${intentBadge(kw.intent)}">${kw.intent || '-'}</span></td>
            <td><span class="badge badge-${sourceBadge(kw.source)}">${kw.source || '-'}</span></td>
            <td>${kw.cluster_name ? escapeHtml(kw.cluster_name) : '-'}</td>
            <td>
                <button class="icon-btn" title="Ignore" onclick="toggleKwStatus(${kw.id}, '${kw.status === 'ignored' ? 'active' : 'ignored'}')">
                    <span class="material-symbols-rounded" style="font-size:18px;">${kw.status === 'ignored' ? 'visibility' : 'visibility_off'}</span>
                </button>
                <button class="icon-btn" title="Delete" onclick="deleteKeyword(${kw.id})">
                    <span class="material-symbols-rounded" style="font-size:18px;">delete</span>
                </button>
            </td>
        </tr>`).join("");

        renderPagination("ks-kw-pagination", total, KS_PAGE_SIZE, ksKwPage, p => { ksKwPage = p; loadKeywords(); });
    } catch (e) { tbody.innerHTML = `<tr><td colspan="10" style="text-align:center;color:var(--error);">Error loading keywords</td></tr>`; }
}

async function toggleKwStatus(id, status) {
    try {
        await apiRequest(`/api/keyword-strategy/keywords/${id}/status?store_id=${ksStoreId}`, {
            method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }),
        });
        loadKeywords();
    } catch (e) { showToast("Failed: " + e.message, "error"); }
}

async function deleteKeyword(id) {
    if (!confirm("Delete this keyword?")) return;
    try {
        await apiRequest(`/api/keyword-strategy/keywords/${id}?store_id=${ksStoreId}`, { method: "DELETE" });
        showToast("Keyword deleted", "success");
        loadKwOverview(); loadKeywords();
    } catch (e) { showToast("Failed: " + e.message, "error"); }
}

// ── Phase 2: Content Audit ───────────────────────────────────────────────
async function runAudit() {
    if (!ksStoreId) return;
    const btn = document.getElementById("ks-audit-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Running Audit...';
    showProgress("Starting content audit...", 5);
    try {
        const result = await apiRequest(`/api/keyword-strategy/audit?store_id=${ksStoreId}`, { method: "POST" });
        if (result.error) {
            showToast(result.error, "error");
            hideProgress();
            btn.disabled = false;
            btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">fact_check</span> Run Content Audit';
            return;
        }
        showToast("Content audit started", "success");
        pollAudit();
    } catch (e) {
        showToast("Audit failed: " + e.message, "error");
        hideProgress();
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">fact_check</span> Run Content Audit';
    }
}

function pollAudit() {
    const interval = setInterval(async () => {
        try {
            const run = await apiRequest(`/api/keyword-strategy/audit/status?store_id=${ksStoreId}`);
            if (!run || !run.status) return;
            if (run.status === "running") {
                const pct = run.total_items > 0 ? Math.round((run.completed_items / run.total_items) * 100) : 10;
                showProgress(`Scanning keywords... ${(run.completed_items || 0).toLocaleString()} / ${(run.total_items || 0).toLocaleString()}`, pct);
            } else {
                clearInterval(interval);
                hideProgress();
                const btn = document.getElementById("ks-audit-btn");
                btn.disabled = false;
                btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">fact_check</span> Run Content Audit';
                if (run.status === "completed") {
                    showToast("Content audit completed", "success");
                    const summary = typeof run.summary === "string" ? JSON.parse(run.summary) : run.summary;
                    onAuditComplete(summary);
                } else {
                    showToast("Audit failed: " + (run.error || "Unknown error"), "error");
                }
            }
        } catch (e) { clearInterval(interval); hideProgress(); }
    }, 2000);
}

async function loadAuditStatus() {
    if (!ksStoreId) return;
    try {
        const run = await apiRequest(`/api/keyword-strategy/audit/status?store_id=${ksStoreId}`);
        if (run && run.status === "running") {
            const btn = document.getElementById("ks-audit-btn");
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Running Audit...';
            pollAudit();
            return;
        }
        if (run && run.status === "completed" && run.summary) {
            const summary = typeof run.summary === "string" ? JSON.parse(run.summary) : run.summary;
            onAuditComplete(summary);
        }
    } catch (e) {}
}

function onAuditComplete(summary) {
    if (!summary || summary.error) return;
    // Show overview
    document.getElementById("ks-audit-overview").style.display = "";
    document.getElementById("ks-audit-subtabs").style.display = "";
    document.getElementById("ks-audit-items").textContent = (summary.items_audited || 0).toLocaleString();
    document.getElementById("ks-audit-mapped").textContent = (summary.keywords_mapped || 0).toLocaleString();
    // Update step description
    document.getElementById("ks-step-audit-desc").textContent = `${(summary.items_audited || 0)} items, ${(summary.keywords_mapped || 0)} mappings`;
    markStepCompleted("audit");
    // Load all data
    loadAuditResults();
    loadCannibalization();
    loadUncoveredKeywords();
}

async function loadAuditResults() {
    if (!ksStoreId) return;
    const tbody = document.getElementById("ks-audit-tbody");
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center"><div class="spinner" style="margin:20px auto"></div></td></tr>';

    const search = document.getElementById("ks-audit-search").value.trim();
    const type = document.getElementById("ks-audit-type-filter").value;
    let url = `/api/keyword-strategy/audit/results?store_id=${ksStoreId}&sort_by=${ksAuditSort.col}&sort_dir=${ksAuditSort.dir}&limit=${KS_AUDIT_PAGE_SIZE}&offset=${(ksAuditPage-1)*KS_AUDIT_PAGE_SIZE}`;
    if (search) url += `&search=${encodeURIComponent(search)}`;
    if (type) url += `&item_type=${type}`;

    try {
        const d = await apiRequest(url);
        const items = d.items || [];
        const total = d.total || 0;
        document.getElementById("ks-audit-count").textContent = `${total.toLocaleString()} items`;

        if (!items.length) { tbody.innerHTML = ""; document.getElementById("ks-audit-empty").style.display = ""; document.getElementById("ks-audit-pagination").innerHTML = ""; return; }
        document.getElementById("ks-audit-empty").style.display = "none";

        tbody.innerHTML = items.map(it => `<tr style="cursor:pointer;" onclick="showItemAudit('${it.item_type}', ${it.item_id})">
            <td>${escapeHtml(it.title || 'Untitled')}</td>
            <td><span class="badge badge-${typeBadge(it.item_type)}">${it.item_type}</span></td>
            <td>${escapeHtml(it.primary_keyword || '-')}</td>
            <td>${(it.primary_volume || 0).toLocaleString()}</td>
            <td>${it.keyword_count || 0}</td>
        </tr>`).join("");

        renderPagination("ks-audit-pagination", total, KS_AUDIT_PAGE_SIZE, ksAuditPage, p => { ksAuditPage = p; loadAuditResults(); });
    } catch (e) { tbody.innerHTML = ""; }
}

async function showItemAudit(itemType, itemId) {
    try {
        const d = await apiRequest(`/api/keyword-strategy/audit/item/${itemType}/${itemId}?store_id=${ksStoreId}`);
        const modal = document.getElementById("ks-detail-modal");
        document.getElementById("ks-detail-title").textContent = d.title || "Item Details";
        let html = `<p style="margin-bottom:12px;"><span class="badge badge-${typeBadge(itemType)}">${itemType}</span></p>`;
        html += '<table class="data-table"><thead><tr><th>Keyword</th><th>Volume</th><th>Position</th><th>Found In</th><th>Primary</th><th>Relevance</th></tr></thead><tbody>';
        (d.keywords || []).forEach(kw => {
            const foundIn = typeof kw.found_in === "string" ? JSON.parse(kw.found_in) : (kw.found_in || []);
            html += `<tr>
                <td>${escapeHtml(kw.keyword)}</td>
                <td>${(kw.search_volume || 0).toLocaleString()}</td>
                <td>${kw.best_position ? parseFloat(kw.best_position).toFixed(1) : '-'}</td>
                <td>${foundIn.map(f => `<span class="badge" style="font-size:10px;margin:1px;">${f}</span>`).join(" ")}</td>
                <td>${kw.is_primary ? '<span class="material-symbols-rounded" style="color:var(--success);font-size:18px;">check_circle</span>' : ''}</td>
                <td>${parseFloat(kw.relevance_score).toFixed(1)}</td>
            </tr>`;
        });
        html += '</tbody></table>';
        document.getElementById("ks-detail-content").innerHTML = html;
        modal.classList.add("active");
    } catch (e) { showToast("Failed to load details", "error"); }
}

async function loadCannibalization() {
    if (!ksStoreId) return;
    try {
        const d = await apiRequest(`/api/keyword-strategy/audit/cannibalization?store_id=${ksStoreId}`);
        const items = d.items || [];
        document.getElementById("ks-audit-cannibal").textContent = (d.total || 0).toLocaleString();
        const tbody = document.getElementById("ks-cannibal-tbody");
        if (!items.length) { tbody.innerHTML = ""; document.getElementById("ks-cannibal-empty").style.display = ""; return; }
        document.getElementById("ks-cannibal-empty").style.display = "none";

        tbody.innerHTML = items.map(it => `<tr>
            <td>${escapeHtml(it.keyword)}</td>
            <td>${(it.search_volume || 0).toLocaleString()}</td>
            <td>${(it.pages || []).map(p => `<div style="font-size:12px;margin:2px 0;"><span class="badge badge-${typeBadge(p.item_type)}" style="font-size:10px;">${p.item_type}</span> ${escapeHtml(p.title || '')} <small>(${parseFloat(p.relevance_score).toFixed(1)})</small></div>`).join("")}</td>
            <td><span class="badge badge-${it.severity === 'high' ? 'error' : 'warning'}">${it.severity}</span></td>
        </tr>`).join("");
    } catch (e) {}
}

async function loadUncoveredKeywords() {
    if (!ksStoreId) return;
    const minVol = parseInt(document.getElementById("ks-uncovered-min").value) || 0;
    try {
        const d = await apiRequest(`/api/keyword-strategy/audit/uncovered?store_id=${ksStoreId}&min_volume=${minVol}&limit=100`);
        const kws = d.keywords || [];
        document.getElementById("ks-audit-uncovered").textContent = (d.total || 0).toLocaleString();
        const tbody = document.getElementById("ks-uncovered-tbody");
        if (!kws.length) { tbody.innerHTML = ""; document.getElementById("ks-uncovered-empty").style.display = ""; return; }
        document.getElementById("ks-uncovered-empty").style.display = "none";

        tbody.innerHTML = kws.map(kw => `<tr>
            <td>${escapeHtml(kw.keyword)}</td>
            <td>${(kw.search_volume || 0).toLocaleString()}</td>
            <td><span class="badge badge-${intentBadge(kw.intent)}">${kw.intent || '-'}</span></td>
            <td><span class="badge badge-${sourceBadge(kw.source)}">${kw.source || '-'}</span></td>
        </tr>`).join("");
    } catch (e) {}
}

// ── Phase 3: Assignment Plan ─────────────────────────────────────────────
async function runPlanGeneration() {
    if (!ksStoreId) return;
    const btn = document.getElementById("ks-plan-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Generating Plan...';
    showProgress("Generating assignment plan...", 10);
    try {
        await apiRequest(`/api/keyword-strategy/plan/generate?store_id=${ksStoreId}`, { method: "POST" });
        showToast("Plan generation started", "success");
        pollRun("plan", () => {
            hideProgress();
            loadPlanData();
            markStepCompleted("plan");
            showToast("Assignment plan generated", "success");
            btn.disabled = false;
            btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">auto_fix_high</span> Generate Plan';
        }, (run) => {
            const pct = run.total_items > 0 ? Math.round((run.completed_items / run.total_items) * 100) : 15;
            showProgress(`Assigning keywords... ${(run.completed_items || 0).toLocaleString()} / ${(run.total_items || 0).toLocaleString()}`, pct);
        }, () => {
            btn.disabled = false;
            btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">auto_fix_high</span> Generate Plan';
        });
    } catch (e) {
        showToast("Failed: " + e.message, "error");
        hideProgress();
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">auto_fix_high</span> Generate Plan';
    }
}

async function loadPlanData() {
    if (!ksStoreId) return;
    document.getElementById("ks-plan-content").style.display = "";

    const search = document.getElementById("ks-plan-search").value.trim();
    const status = document.getElementById("ks-plan-status-filter").value;
    const type = document.getElementById("ks-plan-type-filter").value;
    const priority = document.getElementById("ks-plan-priority-filter").value;

    const perPage = parseInt(document.getElementById("ks-plan-per-page").value) || 0;
    const limit = perPage || 10000;
    const offset = perPage ? ksPlanPage * perPage : 0;

    let url = `/api/keyword-strategy/plan?store_id=${ksStoreId}&limit=${limit}&offset=${offset}`;
    if (search) url += `&search=${encodeURIComponent(search)}`;
    if (status) url += `&status=${status}`;
    if (type) url += `&item_type=${type}`;
    if (priority) url += `&priority=${priority}`;

    try {
        const d = await apiRequest(url);
        const items = d.assignments || [];
        const total = d.total || 0;
        document.getElementById("ks-plan-count").textContent = perPage && total > perPage
            ? `${offset + 1}–${Math.min(offset + perPage, total)} of ${total.toLocaleString()}`
            : `${total} assignments`;

        if (total > 0) {
            document.getElementById("ks-plan-overview").style.display = "";
            document.getElementById("ks-plan-total").textContent = total;
            // Count by status from visible items (approximate)
            const proposed = items.filter(a => a.status === "proposed").length;
            const accepted = items.filter(a => a.status === "accepted").length;
            const applied = items.filter(a => a.status === "applied").length;
            document.getElementById("ks-plan-proposed").textContent = proposed;
            document.getElementById("ks-plan-accepted").textContent = accepted;
            document.getElementById("ks-plan-applied").textContent = applied;
            document.getElementById("ks-step-plan-desc").textContent = `${total} assignments`;
            markStepCompleted("plan");
        }

        const tbody = document.getElementById("ks-plan-tbody");
        if (!items.length) { tbody.innerHTML = ""; document.getElementById("ks-plan-empty").style.display = ""; return; }
        document.getElementById("ks-plan-empty").style.display = "none";

        // Store plan items globally for detail panel access
        window._ksPlanItems = {};
        items.forEach(a => { window._ksPlanItems[a.id] = a; });

        tbody.innerHTML = items.map(a => {
            const title = a.assigned_title || _getAssignedTitle(a);
            const cannibs = a.cannibalization_items || [];
            const pc = a.proposed_changes || {};
            const steps = pc.steps || [];
            const hasSteps = steps.length > 0;
            const appliedCount = steps.filter(s => s.applied).length;
            const stepsLabel = hasSteps ? `${appliedCount}/${steps.length} applied` : '';

            let statusInfo = '';
            if (a.status === 'proposed') statusInfo = 'Pending review';
            else if (a.status === 'accepted' && !hasSteps) statusInfo = 'Ready for planning';
            else if (a.status === 'accepted' && hasSteps) statusInfo = `${stepsLabel}`;
            else if (a.status === 'applied') statusInfo = 'All applied';
            else if (a.status === 'rejected') statusInfo = 'Rejected';

            return `<tr class="ks-plan-row" data-id="${a.id}" onclick="togglePlanDetail(${a.id}, this)" style="cursor:pointer;">
                <td onclick="event.stopPropagation();"><input type="checkbox" class="plan-checkbox" value="${a.id}" onchange="toggleBulkAccept()" ${a.status !== 'proposed' ? 'disabled' : ''}></td>
                <td>${escapeHtml(a.keyword)}</td>
                <td>${(a.search_volume || 0).toLocaleString()}</td>
                <td style="max-width:220px;white-space:normal;word-break:break-word;">${escapeHtml(title)}</td>
                <td><span class="badge badge-${typeBadge(a.assigned_item_type)}">${a.assigned_item_type || '-'}</span></td>
                <td><span class="badge badge-${priorityBadge(a.priority)}">${a.priority || '-'}</span></td>
                <td><span class="badge badge-${statusBadge(a.status)}">${a.status || '-'}</span></td>
                <td style="white-space:nowrap;" onclick="event.stopPropagation();">
                    ${a.status === 'proposed' ? `<button class="btn btn-sm btn-success" style="font-size:11px;padding:3px 8px;" onclick="acceptAssignment(${a.id})">Accept</button>
                    <button class="btn btn-sm btn-error" style="font-size:11px;padding:3px 8px;" onclick="rejectAssignment(${a.id})">Reject</button>` : ''}
                    ${a.status === 'accepted' || a.status === 'applied' ? `<span style="font-size:11px;color:var(--on-surface-secondary);">${statusInfo}</span>` : ''}
                </td>
            </tr>
            <tr class="ks-plan-detail" id="ks-detail-${a.id}" style="display:none;">
                <td colspan="8" style="padding:0;border-top:none;">
                    <div class="ks-detail-panel" id="ks-detail-content-${a.id}">
                        ${_renderDetailPanel(a)}
                    </div>
                </td>
            </tr>`;
        }).join("");

        // Pagination
        const pagDiv = document.getElementById("ks-plan-pagination");
        if (perPage && total > perPage) {
            const totalPages = Math.ceil(total / perPage);
            const showing = `${offset + 1}–${Math.min(offset + perPage, total)} of ${total.toLocaleString()}`;
            let btns = `<span style="font-size:12px;color:var(--on-surface-secondary);align-self:center;">${showing}</span>`;
            btns += `<button class="btn btn-sm btn-outline" ${ksPlanPage === 0 ? 'disabled' : ''} onclick="ksPlanPage=0;loadPlanData();">&#171;</button>`;
            btns += `<button class="btn btn-sm btn-outline" ${ksPlanPage === 0 ? 'disabled' : ''} onclick="ksPlanPage--;loadPlanData();">&#8249;</button>`;
            btns += `<button class="btn btn-sm btn-outline" ${ksPlanPage >= totalPages - 1 ? 'disabled' : ''} onclick="ksPlanPage++;loadPlanData();">&#8250;</button>`;
            btns += `<button class="btn btn-sm btn-outline" ${ksPlanPage >= totalPages - 1 ? 'disabled' : ''} onclick="ksPlanPage=${totalPages - 1};loadPlanData();">&#187;</button>`;
            pagDiv.innerHTML = btns;
            pagDiv.style.display = "";
        } else {
            pagDiv.innerHTML = "";
            pagDiv.style.display = "none";
        }
    } catch (e) {
        document.getElementById("ks-plan-tbody").innerHTML = "";
        document.getElementById("ks-plan-pagination").innerHTML = "";
    }
}

function _getAssignedTitle(a) {
    return `${a.assigned_item_type || ''}#${a.assigned_item_id || ''}`;
}

function togglePlanDetail(id, rowEl) {
    const detail = document.getElementById(`ks-detail-${id}`);
    const isOpen = detail.style.display !== 'none';
    // Close all open details first
    document.querySelectorAll('.ks-plan-detail').forEach(d => d.style.display = 'none');
    document.querySelectorAll('.ks-plan-row').forEach(r => r.classList.remove('expanded'));
    if (!isOpen) {
        detail.style.display = '';
        rowEl.classList.add('expanded');
    }
}

function _renderDetailPanel(a) {
    const title = a.assigned_title || _getAssignedTitle(a);
    const cannibs = a.cannibalization_items || [];
    const pc = a.proposed_changes || {};
    const steps = pc.steps || [];
    const summary = pc.summary || '';

    let html = '<div class="ks-detail-inner">';

    // Target assignment
    html += `<div class="ks-detail-section">
        <div class="ks-detail-section-title">Target Assignment</div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <span class="badge badge-${typeBadge(a.assigned_item_type)}">${a.assigned_item_type || '-'}</span>
            <strong style="font-size:14px;">${escapeHtml(title)}</strong>
        </div>
        <div style="font-size:12px;color:var(--on-surface-secondary);margin-top:4px;">${escapeHtml(a.assignment_reason || '')}</div>
    </div>`;

    // Competing pages
    if (cannibs.length > 0) {
        html += `<div class="ks-detail-section">
            <div class="ks-detail-section-title">Currently Also Using This Keyword (${cannibs.length} page${cannibs.length > 1 ? 's' : ''})</div>
            <div class="ks-competing-list">`;
        cannibs.forEach(c => {
            const actionClass = c.action === 'de-optimize' ? 'error' : 'warning';
            html += `<div class="ks-competing-item">
                <span class="badge badge-${typeBadge(c.item_type)}" style="font-size:10px;">${c.item_type}</span>
                <span style="font-size:13px;flex:1;">${escapeHtml(c.title || `${c.item_type}#${c.item_id}`)}</span>
                <span class="badge badge-${actionClass}" style="font-size:10px;">${c.action || 'review'}</span>
            </div>`;
        });
        html += '</div></div>';
    }

    // Summary (if steps generated)
    if (summary) {
        html += `<div class="ks-detail-section">
            <div class="ks-detail-section-title">Strategy Summary</div>
            <div style="font-size:13px;color:var(--on-surface-secondary);">${escapeHtml(summary)}</div>
        </div>`;
    }

    // Implementation steps
    if (steps.length > 0) {
        const appliedCount = steps.filter(s => s.applied).length;
        html += `<div class="ks-detail-section">
            <div class="ks-detail-section-title" style="display:flex;justify-content:space-between;align-items:center;">
                <span>Implementation Steps (${appliedCount}/${steps.length} applied)</span>
                ${appliedCount < steps.length ? `<button class="btn btn-sm btn-primary" onclick="event.stopPropagation();applyAllSteps(${a.id})">Apply All Remaining</button>` : ''}
            </div>
            <div class="ks-steps-list">`;
        steps.forEach((s, idx) => {
            const actionColors = { 'optimize': 'success', 'de-optimize': 'error', 'differentiate': 'warning' };
            const actionColor = actionColors[s.action] || 'info';
            html += `<div class="ks-step-card ${s.applied ? 'applied' : ''}">
                <div class="ks-step-header">
                    <span class="ks-step-num">${s.step_number || idx + 1}</span>
                    <span class="badge badge-${actionColor}" style="font-size:10px;text-transform:capitalize;">${s.action || 'update'}</span>
                    <span style="font-size:13px;font-weight:500;">${escapeHtml(s.title || '')} &mdash; <code>${s.field}</code></span>
                    ${s.applied
                        ? '<span class="badge badge-success" style="margin-left:auto;font-size:10px;">Applied</span>'
                        : `<button class="btn btn-sm btn-primary" style="margin-left:auto;font-size:11px;padding:2px 10px;" onclick="event.stopPropagation();applySingleStep(${a.id},${idx})">Apply</button>`
                    }
                </div>
                <div class="ks-step-diff">
                    <div class="ks-step-old"><span class="ks-diff-label">Current:</span> ${escapeHtml(s.current_value || '(empty)')}</div>
                    <div class="ks-step-new"><span class="ks-diff-label">Proposed:</span> ${escapeHtml(s.proposed_value || '(empty)')}</div>
                </div>
                <div class="ks-step-explain">${escapeHtml(s.explanation || '')}</div>
            </div>`;
        });
        html += '</div></div>';
    } else if (a.status === 'accepted') {
        html += `<div class="ks-detail-section" style="text-align:center;padding:20px 0;">
            <p style="color:var(--on-surface-secondary);font-size:13px;margin-bottom:12px;">No implementation steps generated yet.</p>
            <button class="btn btn-primary" id="ks-gen-steps-btn-${a.id}" onclick="event.stopPropagation();generateSteps(${a.id})">
                <span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">psychology</span> Generate Implementation Steps
            </button>
        </div>`;
    } else if (a.status === 'proposed') {
        html += `<div class="ks-detail-section" style="text-align:center;padding:16px 0;">
            <p style="color:var(--on-surface-secondary);font-size:13px;">Accept this assignment first, then generate implementation steps.</p>
        </div>`;
    }

    html += '</div>';
    return html;
}

async function acceptAssignment(id) {
    try {
        await apiRequest(`/api/keyword-strategy/plan/${id}/accept?store_id=${ksStoreId}`, { method: "PUT" });
        showToast("Assignment accepted", "success");
        loadPlanData();
    } catch (e) { showToast("Failed: " + e.message, "error"); }
}

async function rejectAssignment(id) {
    try {
        await apiRequest(`/api/keyword-strategy/plan/${id}/reject?store_id=${ksStoreId}`, { method: "PUT" });
        showToast("Assignment rejected", "success");
        loadPlanData();
    } catch (e) { showToast("Failed: " + e.message, "error"); }
}

async function bulkAcceptPlan() {
    const ids = [...document.querySelectorAll(".plan-checkbox:checked")].map(cb => parseInt(cb.value));
    if (!ids.length) return;
    try {
        await apiRequest(`/api/keyword-strategy/plan/bulk-accept?store_id=${ksStoreId}`, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids }),
        });
        showToast(`${ids.length} assignments accepted`, "success");
        loadPlanData();
    } catch (e) { showToast("Failed: " + e.message, "error"); }
}

function toggleBulkAccept() {
    const checked = document.querySelectorAll(".plan-checkbox:checked").length;
    document.getElementById("ks-bulk-accept-btn").style.display = checked > 0 ? "" : "none";
}

async function generateSteps(assignmentId) {
    const btn = document.getElementById(`ks-gen-steps-btn-${assignmentId}`);
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="material-symbols-rounded spin" style="font-size:16px;vertical-align:middle;">progress_activity</span> Generating...'; }
    try {
        const result = await apiRequest(`/api/keyword-strategy/plan/${assignmentId}/generate-steps?store_id=${ksStoreId}`, { method: "POST" });
        showToast(`${(result.steps || []).length} implementation steps generated`, "success");
        // Update local data and re-render detail panel
        if (window._ksPlanItems[assignmentId]) {
            window._ksPlanItems[assignmentId].proposed_changes = result;
        }
        const panel = document.getElementById(`ks-detail-content-${assignmentId}`);
        if (panel && window._ksPlanItems[assignmentId]) {
            panel.innerHTML = _renderDetailPanel(window._ksPlanItems[assignmentId]);
        }
    } catch (e) {
        showToast("Failed: " + e.message, "error");
        if (btn) { btn.disabled = false; btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">psychology</span> Generate Implementation Steps'; }
    }
}

async function applySingleStep(assignmentId, stepIndex) {
    try {
        showToast("Applying step...", "info");
        const result = await apiRequest(`/api/keyword-strategy/plan/${assignmentId}/apply-step?store_id=${ksStoreId}`, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ step_index: stepIndex }),
        });
        if (result.already_applied) { showToast("Step already applied", "info"); return; }
        showToast(`Applied: ${result.field} on ${result.item_type}`, "success");
        // Update local data
        if (window._ksPlanItems[assignmentId]) {
            const steps = (window._ksPlanItems[assignmentId].proposed_changes || {}).steps || [];
            if (steps[stepIndex]) steps[stepIndex].applied = true;
            if (result.all_done) window._ksPlanItems[assignmentId].status = 'applied';
            const panel = document.getElementById(`ks-detail-content-${assignmentId}`);
            if (panel) panel.innerHTML = _renderDetailPanel(window._ksPlanItems[assignmentId]);
        }
        if (result.all_done) loadPlanData();
        loadChanges();
    } catch (e) { showToast("Apply failed: " + e.message, "error"); }
}

async function applyAllSteps(assignmentId) {
    const a = window._ksPlanItems[assignmentId];
    if (!a) return;
    const steps = (a.proposed_changes || {}).steps || [];
    let applied = 0;
    for (let i = 0; i < steps.length; i++) {
        if (steps[i].applied) continue;
        try {
            const result = await apiRequest(`/api/keyword-strategy/plan/${assignmentId}/apply-step?store_id=${ksStoreId}`, {
                method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ step_index: i }),
            });
            if (!result.already_applied) {
                steps[i].applied = true;
                applied++;
            }
            if (result.all_done) a.status = 'applied';
        } catch (e) {
            showToast(`Step ${i + 1} failed: ${e.message}`, "error");
            break;
        }
    }
    showToast(`${applied} step${applied !== 1 ? 's' : ''} applied to Shopify`, "success");
    const panel = document.getElementById(`ks-detail-content-${assignmentId}`);
    if (panel) panel.innerHTML = _renderDetailPanel(a);
    loadPlanData();
    loadChanges();
}

// Keep legacy functions for backwards compat
async function generateFix(assignmentId) { return generateSteps(assignmentId); }
async function previewFix(assignmentId) {
    const detail = document.getElementById(`ks-detail-${assignmentId}`);
    if (detail) detail.style.display = '';
}
async function applyFix(assignmentId) { return applyAllSteps(assignmentId); }

// ── Phase 4: Applied Changes ─────────────────────────────────────────────
async function loadChanges() {
    if (!ksStoreId) return;
    const tbody = document.getElementById("ks-changes-tbody");
    try {
        const d = await apiRequest(`/api/keyword-strategy/changes?store_id=${ksStoreId}&limit=100`);
        const changes = d.changes || [];
        if (!changes.length) { tbody.innerHTML = ""; document.getElementById("ks-changes-empty").style.display = ""; return; }
        document.getElementById("ks-changes-empty").style.display = "none";
        document.getElementById("ks-step-changes-desc").textContent = `${changes.length} changes`;

        tbody.innerHTML = changes.map(c => `<tr>
            <td>${escapeHtml(c.keyword || '-')}</td>
            <td>${escapeHtml(c.title || `${c.item_type}#${c.item_id}`)}</td>
            <td><span class="badge badge-${typeBadge(c.item_type)}">${c.item_type}</span></td>
            <td>${escapeHtml(c.field_name)}</td>
            <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(c.old_value || '')}">${escapeHtml(c.old_value || '(empty)')}</td>
            <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(c.new_value || '')}">${escapeHtml(c.new_value || '(empty)')}</td>
            <td style="font-size:12px;">${c.applied_at ? new Date(c.applied_at).toLocaleDateString() : '-'}</td>
            <td>${c.reverted_at ? '<span class="badge">Reverted</span>' : `<button class="icon-btn" title="Revert" onclick="revertChange(${c.id})"><span class="material-symbols-rounded" style="font-size:18px;">undo</span></button>`}</td>
        </tr>`).join("");
    } catch (e) { tbody.innerHTML = ""; }
}

async function revertChange(changeId) {
    if (!confirm("Revert this change? The old value will be restored on Shopify.")) return;
    try {
        await apiRequest(`/api/keyword-strategy/fix/${changeId}/revert?store_id=${ksStoreId}`, { method: "POST" });
        showToast("Change reverted", "success");
        loadChanges();
    } catch (e) { showToast("Revert failed: " + e.message, "error"); }
}

// ── Helpers ──────────────────────────────────────────────────────────────
function pollRun(runType, onComplete, onProgress, onFail) {
    let consecutiveErrors = 0;
    const interval = setInterval(async () => {
        try {
            const d = await apiRequest(`/api/keyword-strategy/runs?store_id=${ksStoreId}`);
            consecutiveErrors = 0;
            const runs = d.runs || [];
            const latest = runs.find(r => r.run_type === runType);
            if (!latest) { clearInterval(interval); hideProgress(); if (onFail) onFail(); return; }
            if (latest.status === "completed") {
                clearInterval(interval);
                onComplete(latest);
            } else if (latest.status === "failed") {
                clearInterval(interval);
                hideProgress();
                showToast(`${runType} failed: ${latest.error || 'Unknown'}`, "error");
                if (onFail) onFail(latest);
            } else if (onProgress) {
                onProgress(latest);
            }
        } catch (e) {
            consecutiveErrors++;
            if (consecutiveErrors >= 5) {
                clearInterval(interval);
                hideProgress();
                showToast(`Lost contact with the ${runType} run (${e.message}). It may still be running — reload to check.`, "error");
                if (onFail) onFail();
            }
        }
    }, 2000);
}

function intentBadge(intent) {
    const m = { transactional: "success", commercial: "warning", informational: "info" };
    return m[intent] || "";
}

function sourceBadge(source) {
    const m = { semrush: "warning", gsc: "info", both: "success", manual: "" };
    return m[source] || "";
}

function typeBadge(type) {
    const m = { product: "success", collection: "warning", article: "info", page: "" };
    return m[type] || "";
}

function priorityBadge(p) {
    const m = { high: "error", medium: "warning", low: "" };
    return m[p] || "";
}

function statusBadge(s) {
    const m = { proposed: "warning", accepted: "info", applied: "success", rejected: "error" };
    return m[s] || "";
}

function renderPagination(containerId, total, pageSize, currentPage, onPageChange) {
    const container = document.getElementById(containerId);
    const totalPages = Math.ceil(total / pageSize);
    if (totalPages <= 1) { container.innerHTML = ""; return; }

    let html = "";
    if (currentPage > 1) html += `<button class="btn btn-outline btn-sm" onclick="void(0)" data-page="${currentPage-1}">&laquo;</button>`;
    const start = Math.max(1, currentPage - 2);
    const end = Math.min(totalPages, currentPage + 2);
    for (let p = start; p <= end; p++) {
        html += `<button class="btn ${p === currentPage ? 'btn-primary' : 'btn-outline'} btn-sm" onclick="void(0)" data-page="${p}">${p}</button>`;
    }
    if (currentPage < totalPages) html += `<button class="btn btn-outline btn-sm" onclick="void(0)" data-page="${currentPage+1}">&raquo;</button>`;

    container.innerHTML = html;
    container.querySelectorAll("[data-page]").forEach(btn => {
        btn.addEventListener("click", () => onPageChange(parseInt(btn.dataset.page)));
    });
}
