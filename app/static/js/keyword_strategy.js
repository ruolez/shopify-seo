/* Keyword Strategy — explorer, coverage gaps, audit, plan, changes */
let ksStoreId = null;
let ksPlanPage = 0;
let ksKwSort = { col: "search_volume", dir: "desc" };
let ksKwPage = 1;
const KS_PAGE_SIZE = 50;
let ksActiveStep = "explorer";
let ksAuditSort = { col: "keyword_count", dir: "desc" };
let ksAuditPage = 1;
const KS_AUDIT_PAGE_SIZE = 50;
let ksFamilyCache = {};
let ksKwDrawer = null;
let ksExpandedCid = null;

// ── Init ─────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
    loadKsStores();
    initStepNav();
    initKsEvents();
    initOptimizeModalObserver();
    let tab = new URLSearchParams(location.search).get("tab");
    if (tab === "coverage") {
        // Legacy deep link: the Coverage Gaps tab merged into the Explorer
        tab = "explorer";
        const gapsOnly = document.getElementById("ks-gaps-only");
        if (gapsOnly) gapsOnly.checked = true;
    }
    if (tab && document.getElementById("panel-" + tab)) switchToStep(tab);
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
    ksFamilyCache = {};
    ksExpandedCid = null;
    loadKwOverview();
    updateExplorerView();
    loadFreshness();
    resumeClassifyRun();
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

    // Deep-linkable ?tab=
    const url = new URL(location.href);
    url.searchParams.set("tab", step);
    history.replaceState(null, "", url);
}

// ── Events ───────────────────────────────────────────────────────────────
function initKsEvents() {
    // Explorer actions
    document.getElementById("ks-consolidate-btn").addEventListener("click", runConsolidate);
    document.getElementById("ks-classify-btn").addEventListener("click", runAiClassify);
    document.getElementById("ks-fresh-audit-btn").addEventListener("click", () => runAudit());
    document.getElementById("ks-reanalyze-btn").addEventListener("click", ksReanalyzeAll);
    document.getElementById("ks-add-kw-btn").addEventListener("click", () => document.getElementById("ks-add-modal").classList.add("active"));
    document.getElementById("ks-save-kw-btn").addEventListener("click", saveManualKeyword);

    // Search / filters — any active filter flips the explorer into the flat
    // keyword list; clearing them all returns to the family view
    let kwSearchT;
    document.getElementById("ks-kw-search").addEventListener("input", () => { clearTimeout(kwSearchT); kwSearchT = setTimeout(() => { ksKwPage = 1; updateExplorerView(); }, 300); });
    ["ks-intent-filter", "ks-source-filter", "ks-cluster-filter"].forEach(id =>
        document.getElementById(id).addEventListener("change", () => { ksKwPage = 1; updateExplorerView(); }));
    document.getElementById("ks-fam-sort").addEventListener("change", () => loadFamilies());
    document.getElementById("ks-gaps-only").addEventListener("change", () => loadFamilies());
    document.getElementById("ks-min-volume").addEventListener("change", () => loadFamilies());
    document.addEventListener("click", () => closeKsPopover());

    // Sort
    document.querySelectorAll("#panel-explorer .sortable").forEach(th => {
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

// ── Data freshness strip ─────────────────────────────────────────────────
function ksAge(hours) {
    if (hours === null || hours === undefined) return "never";
    if (hours < 1) return "just now";
    if (hours < 48) return Math.round(hours) + "h ago";
    return Math.round(hours / 24) + "d ago";
}

async function loadFreshness() {
    if (!ksStoreId) return;
    try {
        const d = await apiRequest(`/api/keyword-strategy/freshness?store_id=${ksStoreId}`);
        const set = (id, e) => {
            const el = document.getElementById(id);
            if (!el || !e) return;
            el.textContent = ksAge(e.age_hours);
            el.className = "ks-fresh-age " + (e.level || "none");
            el.title = e.at ? new Date(e.at).toLocaleString() : "Never run";
        };
        set("ks-fresh-gsc", d.gsc);
        set("ks-fresh-consolidate", d.consolidate);
        set("ks-fresh-classify", d.ai_classify);
        set("ks-fresh-audit", d.audit);
        const a = document.getElementById("ks-fresh-analyses");
        if (a && d.analyses) {
            a.className = "ks-fresh-age " + (d.analyses.level || "none");
            a.textContent = d.analyses.stale_count
                ? `${d.analyses.stale_count} of ${d.analyses.total} stale`
                : d.analyses.total ? "all fresh" : "never";
            a.title = d.analyses.at
                ? `Newest analysis: ${new Date(d.analyses.at).toLocaleString()} — pages not re-analyzed in 7 days count as stale`
                : "No pages analyzed yet";
        }
    } catch (e) {}
}

async function ksReanalyzeAll() {
    if (!ksStoreId) return;
    const btn = document.getElementById("ks-reanalyze-btn");
    if (btn.disabled) return;
    btn.disabled = true;
    const orig = btn.innerHTML;
    btn.innerHTML = '<span class="spinner" style="width:12px;height:12px;border-width:2px;"></span> Running...';
    try {
        for (const t of ["product", "collection"]) {
            const resp = await apiRequest("/api/technical-seo/bulk-analyze", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ item_type: t, all: true, store_id: ksStoreId }),
            });
            await pollTaskUntilDone(`/api/tasks/bulk/${resp.bulk_task_id}`, {
                timeoutMs: 60 * 60 * 1000,
                onProgress: task => showProgress(
                    `Re-analyzing ${t}s... ${task.completed || 0} / ${task.total || 0}`
                    + (task.current_product_title ? ` — ${task.current_product_title}` : ""),
                    task.total ? Math.round(((task.completed || 0) / task.total) * 100) : 0),
            });
        }
        hideProgress();
        showToast("All pages re-analyzed", "success");
        ksFamilyCache = {};
        loadKwOverview(); updateExplorerView(); loadFreshness();
    } catch (e) {
        hideProgress();
        showToast("Re-analyze failed: " + e.message, "error");
    } finally {
        btn.disabled = false;
        btn.innerHTML = orig;
    }
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
                ksFamilyCache = {};
                loadKwOverview();
                updateExplorerView();
                loadFreshness();
                showToast("Keywords consolidated", "success");
            }, (run) => {
                const pct = run.total_items > 0 ? Math.round((run.completed_items / run.total_items) * 100) : 30;
                showProgress(`Consolidating... ${(run.completed_items || 0).toLocaleString()} keywords`, pct);
            });
        } catch (e) { showToast("Consolidation failed: " + e.message, "error"); hideProgress(); }
        finally { btn.innerHTML = 'Consolidate'; }
    });
}

const KS_CLASSIFY_BTN_HTML = 'Classify';

async function runAiClassify() {
    if (!ksStoreId) return;
    const btn = document.getElementById("ks-classify-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Classifying...';
    showProgress("Building topic-family taxonomy...", 3);
    try {
        await apiRequest(`/api/keyword-strategy/ai/classify?store_id=${ksStoreId}`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: "full" }),
        });
        pollClassifyRun();
    } catch (e) {
        showToast("Classification failed to start: " + e.message, "error");
        hideProgress();
        btn.disabled = false;
        btn.innerHTML = KS_CLASSIFY_BTN_HTML;
    }
}

function pollClassifyRun() {
    pollRun("ai_classify", (run) => {
        hideProgress();
        const btn = document.getElementById("ks-classify-btn");
        btn.disabled = false;
        btn.innerHTML = KS_CLASSIFY_BTN_HTML;
        const s = typeof run.summary === "string" ? JSON.parse(run.summary) : (run.summary || {});
        showToast(`Classified keywords into ${s.families || 0} families`, "success");
        ksFamilyCache = {};
        loadKwOverview(); updateExplorerView(); loadFreshness();
    }, (run) => {
        const pct = run.total_items > 0 ? Math.round((run.completed_items / run.total_items) * 100) : 3;
        showProgress(`AI classifying keywords... ${(run.completed_items || 0).toLocaleString()} / ${(run.total_items || 0).toLocaleString()}`, pct);
    }, () => {
        const btn = document.getElementById("ks-classify-btn");
        btn.disabled = false;
        btn.innerHTML = KS_CLASSIFY_BTN_HTML;
    });
}

async function resumeClassifyRun() {
    // A classification may be running from a previous page load or the
    // post-consolidation hook — surface its progress instead of sitting idle.
    try {
        const d = await apiRequest(`/api/keyword-strategy/runs?store_id=${ksStoreId}`);
        const latest = (d.runs || []).find(r => r.run_type === "ai_classify");
        if (latest && latest.status === "running") {
            const btn = document.getElementById("ks-classify-btn");
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Classifying...';
            pollClassifyRun();
        }
    } catch (e) {}
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
        document.getElementById("ks-clusters").textContent = (d.clusters_count || 0).toLocaleString();
        const pct = d.total_active ? Math.round(((d.ai_classified || 0) / d.total_active) * 100) : 0;
        document.getElementById("ks-classified").textContent = pct + "%";
        // Update step description
        document.getElementById("ks-step-kw-desc").textContent = `${(d.total_active || 0).toLocaleString()} keywords`;
        loadClusters();
    } catch (e) {}
}

async function loadClusters() {
    if (!ksStoreId) return;
    try {
        const d = await apiRequest(`/api/keyword-strategy/clusters?store_id=${ksStoreId}`);
        const sel = document.getElementById("ks-cluster-filter");
        const val = sel.value;
        sel.innerHTML = '<option value="">All Families</option>';
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

        tbody.innerHTML = kws.map(kw => `<tr style="cursor:pointer;" onclick="openKeywordDrawer(${kw.id})">
            <td style="max-width:250px;word-break:break-word;">${escapeHtml(kw.keyword)}</td>
            <td>${(kw.search_volume || 0).toLocaleString()}</td>
            <td>${kw.best_position ? parseFloat(kw.best_position).toFixed(1) : '-'}</td>
            <td>${(kw.clicks || 0).toLocaleString()}</td>
            <td>${(kw.impressions || 0).toLocaleString()}</td>
            <td>${kw.ctr ? (parseFloat(kw.ctr)*100).toFixed(1) + '%' : '-'}</td>
            <td><span class="badge badge-${intentBadge(kw.intent)}">${kw.intent || '-'}</span></td>
            <td><span class="badge badge-${sourceBadge(kw.source)}">${kw.source || '-'}</span></td>
            <td>${kw.cluster_name ? escapeHtml(kw.cluster_name) : '-'}</td>
            <td onclick="event.stopPropagation();">
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

// ── Explorer: family view ────────────────────────────────────────────────
function _explorerFiltersActive() {
    return !!(document.getElementById("ks-kw-search").value.trim()
        || document.getElementById("ks-intent-filter").value
        || document.getElementById("ks-source-filter").value
        || document.getElementById("ks-cluster-filter").value);
}

function updateExplorerView() {
    if (!ksStoreId) return;
    document.getElementById("ks-kw-content").style.display = "";
    const flat = _explorerFiltersActive();
    document.getElementById("ks-fam-view").style.display = flat ? "none" : "";
    document.getElementById("ks-flat-view").style.display = flat ? "" : "none";
    ["ks-fam-sort", "ks-gaps-only-wrap", "ks-min-volume", "ks-fam-legend"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = flat ? "none" : "";
    });
    if (flat) loadKeywords();
    else loadFamilies();
}

async function loadFamilies() {
    if (!ksStoreId) return;
    const tbody = document.getElementById("ks-fam-tbody");
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center"><div class="spinner" style="margin:20px auto"></div></td></tr>';
    const sortBy = document.getElementById("ks-fam-sort").value;
    const minVol = parseInt(document.getElementById("ks-min-volume").value) || 0;
    const gapsOnly = document.getElementById("ks-gaps-only").checked ? 1 : 0;
    try {
        const d = await apiRequest(`/api/keyword-strategy/coverage/matrix?store_id=${ksStoreId}&sort_by=${sortBy}&min_volume=${minVol}&only_gaps=${gapsOnly}&limit=200`);
        const fams = d.families || [];
        document.getElementById("ks-kw-count").textContent = `${d.total || 0} families`;
        document.getElementById("ks-gap-volume").textContent = (d.gap_volume || 0).toLocaleString();
        if (!fams.length) {
            tbody.innerHTML = "";
            document.getElementById("ks-fam-empty").style.display = "";
            return;
        }
        document.getElementById("ks-fam-empty").style.display = "none";
        tbody.innerHTML = fams.map(f => {
            const cid = f.cluster_id === null ? 0 : f.cluster_id;
            return `<tr class="ks-fam-row" data-cid="${cid}" onclick="toggleFamily(${cid}, this)">
                <td><span class="material-symbols-rounded ks-fam-caret" style="font-size:18px;color:var(--on-surface-secondary);">chevron_right</span></td>
                <td style="font-weight:600;">${escapeHtml(f.name)}${f.unclustered ? ' <span class="badge" style="font-size:10px;">needs classification</span>' : ''}</td>
                <td>${f.keyword_count}</td>
                <td>${(f.total_volume || 0).toLocaleString()}</td>
                <td>${f.pages_count ?? '—'}</td>
                ${["transactional", "commercial", "informational", "navigational"].map(i => `<td>${renderMatrixCell(f.intents[i], cid, i)}</td>`).join("")}
                <td>${f.gap_score ? f.gap_score.toLocaleString() : '—'}</td>
            </tr>
            <tr class="ks-fam-expand" id="ks-fam-expand-${cid}" style="display:none;"><td colspan="10"><div class="ks-fam-kws" id="ks-fam-kws-${cid}"></div></td></tr>`;
        }).join("");
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="10" style="text-align:center;color:var(--error);">Error loading families</td></tr>`;
    }
}

async function toggleFamily(cid, rowEl) {
    const expand = document.getElementById(`ks-fam-expand-${cid}`);
    const isOpen = expand.style.display !== "none";
    document.querySelectorAll(".ks-fam-expand").forEach(r => r.style.display = "none");
    document.querySelectorAll(".ks-fam-row").forEach(r => r.classList.remove("expanded"));
    ksExpandedCid = null;
    if (isOpen) return;
    expand.style.display = "";
    rowEl.classList.add("expanded");
    ksExpandedCid = cid;
    const box = document.getElementById(`ks-fam-kws-${cid}`);
    if (!ksFamilyCache[cid]) {
        box.innerHTML = '<div class="spinner" style="margin:14px auto"></div>';
        try {
            ksFamilyCache[cid] = await apiRequest(`/api/keyword-strategy/families/${cid}?store_id=${ksStoreId}`);
        } catch (e) {
            box.innerHTML = '<div style="color:var(--error);font-size:13px;padding:10px;">Failed to load family</div>';
            return;
        }
    }
    box.innerHTML = renderFamilyDetail(ksFamilyCache[cid], cid);
}

const KS_STALENESS = {
    fresh: { label: "fresh", badge: "success" },
    stale: { label: "stale", badge: "warning" },
    never_optimized: { label: "never optimized", badge: "error" },
    low_score: { label: "low score", badge: "error" },
};

function renderFamilyDetail(fam, cid) {
    const pages = fam.pages || [];
    const unowned = fam.unowned || [];
    let html = "";

    // ── Pages covering this family ──
    html += `<div class="ks-pages-label">Pages covering this family (${pages.length})</div>`;
    if (!pages.length) {
        html += '<div style="font-size:13px;color:var(--on-surface-secondary);padding:2px 0 6px;">No pages own keywords in this family yet.</div>';
    }
    pages.forEach(p => {
        const st = KS_STALENESS[p.staleness] || KS_STALENESS.fresh;
        const tip = [p.stale_reason, p.last_optimized_at ? `Last optimized ${p.last_optimized_at.slice(0, 10)}` : null]
            .filter(Boolean).join(" — ");
        const canOptimize = (p.item_type === "product" || p.item_type === "collection");
        const pk = p.primary_keyword;
        const kwArgs = pk ? `${JSON.stringify(pk.keyword)}, ${pk.search_volume || 0}` : "null, 0";

        let kwLine = "";
        if (pk) {
            kwLine += `<span>primary:</span><span class="ks-kw-chip" title="${pk.role === 'assigned' || pk.manual ? 'manually assigned' : 'resolver primary'}">
                ${escapeHtml(pk.keyword)} <span style="color:var(--on-surface-secondary);">(${(pk.search_volume || 0).toLocaleString()})</span>
                ${pk.manual ? '<span class="material-symbols-rounded" style="font-size:12px;" title="manual">push_pin</span>' : ''}</span>`;
            if (!pk.in_family) kwLine += '<span style="font-style:italic;">outside this family</span>';
        } else {
            kwLine += '<span style="font-style:italic;">no primary keyword</span>';
        }
        (p.owned_keywords || []).forEach(k => {
            kwLine += `<span class="ks-kw-chip" onclick="event.stopPropagation();openKeywordDrawer(${k.id})">${escapeHtml(k.keyword)} <span style="color:var(--on-surface-secondary);">(${(k.search_volume || 0).toLocaleString()})</span></span>`;
        });

        html += `<div class="ks-page-row">
            <div class="ks-page-info">
                <div class="ks-page-head">
                    <span class="badge badge-${typeBadge(p.item_type)}" style="font-size:10px;">${p.item_type}</span>
                    <span class="ks-page-title" ${canOptimize ? `onclick="ksOpenPage('${p.item_type}', ${p.item_id})"` : ""}>${escapeHtml(p.title || '#' + p.item_id)}</span>
                    ${p.seo_score !== null && p.seo_score !== undefined ? `<span class="ks-page-score" style="color:${p.seo_score >= 80 ? 'var(--success)' : p.seo_score >= 60 ? 'var(--warning)' : 'var(--error)'};">${p.seo_score}</span>` : ''}
                    <span class="badge badge-${st.badge}" style="font-size:10px;" ${tip ? `title="${escapeAttr(tip)}"` : ''}>${st.label}</span>
                    ${p.conflict_key ? '<span class="badge badge-error" style="font-size:10px;" title="Another page targets a variant of the same keyword — see Keyword conflicts below">keyword conflict</span>' : ''}
                </div>
                <div class="ks-page-kws">${kwLine}</div>
            </div>
            <div class="ks-page-actions">
                ${canOptimize && p.staleness !== "fresh"
                    ? `<button class="btn btn-sm btn-primary" style="font-size:11.5px;white-space:nowrap;" onclick='ksOptimizePage("${p.item_type}", ${p.item_id}, ${kwArgs})'>
                        <span class="material-symbols-rounded" style="font-size:14px;vertical-align:middle;">auto_fix_high</span> Optimize</button>`
                    : ''}
                ${canOptimize
                    ? `<button class="icon-btn" title="Open item" onclick="ksOpenPage('${p.item_type}', ${p.item_id})"><span class="material-symbols-rounded" style="font-size:18px;">open_in_full</span></button>`
                    : ''}
            </div>
        </div>`;
    });

    // ── Keyword conflicts (cannibalization) ──
    const conflicts = fam.conflicts || [];
    if (conflicts.length) {
        html += `<div class="ks-pages-label">Keyword conflicts (${conflicts.length})</div>`;
        conflicts.forEach(c => {
            const top = c.keywords[0] || {};
            const owners = c.contenders.filter(x => x.is_owner).length;
            html += `<div class="ks-conflict-box">
                <div class="ks-conflict-head">
                    <span class="material-symbols-rounded">swords</span>
                    <span>"${escapeHtml(top.keyword || c.key)}" (${(top.search_volume || 0).toLocaleString()} vol) —
                        ${owners} pages hold variants of this keyword and compete for one SERP</span>
                </div>`;
            c.contenders.forEach(x => {
                const pos = x.gsc_position !== null && x.gsc_position !== undefined
                    ? `pos ${x.gsc_position}` : 'not ranking';
                const name = x.title || x.url || `${x.item_type} #${x.item_id}`;
                const canAct = x.item_id && (x.item_type === "product" || x.item_type === "collection");
                html += `<div class="ks-contender-row">
                    <span class="ks-contender-pos" style="color:${x.gsc_position != null ? (x.gsc_position <= 20 ? 'var(--success)' : 'var(--warning)') : 'var(--on-surface-secondary)'};">${pos}</span>
                    ${x.item_type ? `<span class="badge badge-${typeBadge(x.item_type)}" style="font-size:9.5px;">${x.item_type}</span>` : ''}
                    <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;" ${canAct ? `class="ks-page-title" onclick="ksOpenPage('${x.item_type}', ${x.item_id})"` : ''}>${escapeHtml(name)}</span>
                    ${x.is_owner
                        ? `<span style="font-size:11px;color:var(--on-surface-secondary);white-space:nowrap;">primary: ${escapeHtml(x.primary_keyword || '')}${x.manual ? ' 📌' : ''}</span>`
                        : '<span style="font-size:11px;color:var(--warning);white-space:nowrap;">ranks but doesn\'t own it</span>'}
                    ${x.gsc_impressions ? `<span style="font-size:11px;color:var(--on-surface-secondary);white-space:nowrap;">${x.gsc_clicks} clicks · ${x.gsc_impressions.toLocaleString()} impr</span>` : ''}
                    ${canAct
                        ? `<button class="btn btn-sm btn-outline" style="font-size:11px;white-space:nowrap;" onclick='ksResolveConflict(${cid}, ${JSON.stringify(c.key)}, "${x.item_type}", ${x.item_id})'>${x.is_owner ? 'Keep primary here' : 'Make owner'}</button>`
                        : ''}
                </div>`;
            });
            html += '</div>';
        });
    }

    // ── Unowned demand ──
    const unownedVol = unowned.reduce((s, u) => s + (u.search_volume || 0), 0);
    html += `<div class="ks-pages-label">Unowned demand (${unowned.length} keyword${unowned.length !== 1 ? 's' : ''} · ${unownedVol.toLocaleString()} vol)</div>`;
    if (!unowned.length) {
        html += '<div style="font-size:13px;color:var(--on-surface-secondary);padding:2px 0 6px;">Every keyword with demand has an owning page.</div>';
    }
    unowned.forEach(u => {
        html += `<div class="ks-unowned-row" onclick="openKeywordDrawer(${u.id})">
            <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(u.keyword)}</span>
            <span style="color:var(--on-surface-secondary);font-size:12px;white-space:nowrap;">${(u.search_volume || 0).toLocaleString()}</span>
            <span class="badge badge-${intentBadge(u.intent)}" style="font-size:10px;">${u.intent}</span>
            <span class="ks-req-type">&rarr; needs ${(u.required_types || []).join(" / ") || "content"}</span>
            <span onclick="event.stopPropagation();" style="display:flex;gap:4px;">
                <button class="btn btn-sm btn-outline" style="font-size:11px;white-space:nowrap;" onclick="ksActionAssign(${u.id})">Assign</button>
                ${u.action === "research_topic"
                    ? `<button class="btn btn-sm btn-outline" style="font-size:11px;white-space:nowrap;" onclick='ksActionResearch(${cid}, ${JSON.stringify(u.intent)})'>Research</button>`
                    : ''}
            </span>
        </div>`;
    });
    return html;
}

async function ksResolveConflict(cid, key, itemType, itemId) {
    // Consolidate a variant-cannibalization group onto one winner: pin the
    // highest-volume variant as the winner's manual primary, then re-resolve
    // the losing owners so they walk to their next-best unclaimed keyword.
    const fam = ksFamilyCache[cid];
    const conflict = (fam && (fam.conflicts || []).find(c => c.key === key));
    if (!conflict) return;
    const kw = (conflict.keywords[0] || {}).keyword;
    const winner = conflict.contenders.find(x => x.item_type === itemType && x.item_id === itemId);
    const losers = conflict.contenders.filter(x =>
        x.is_owner && !(x.item_type === itemType && x.item_id === itemId)
        && (x.item_type === "product" || x.item_type === "collection"));
    if (!kw || !winner) return;
    if (!confirm(`Pin "${kw}" as the manual primary of "${winner.title || winner.item_type + ' #' + itemId}"`
        + (losers.length ? ` and re-resolve ${losers.length} competing page(s) to their next-best keywords?` : '?'))) return;

    try {
        showToast("Consolidating keyword ownership...", "info");
        await apiRequest(`/api/seo/items/${itemType}/${itemId}/primary-keyword?store_id=${ksStoreId}`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ keyword: kw }),
        });
        for (const loser of losers) {
            try {
                await apiRequest("/api/technical-seo/analyze", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ item_type: loser.item_type, item_id: loser.item_id, store_id: ksStoreId }),
                });
            } catch (e) {
                showToast(`Re-resolve failed for ${loser.title || loser.item_id}: ${e.message}`, "error");
            }
        }
        showToast(`"${kw}" consolidated onto ${winner.title || 'the selected page'}`, "success");
        refreshExpandedFamily(cid);
    } catch (e) {
        showToast("Failed: " + e.message, "error");
    }
}

// ── In-place optimize (reuses the /seo item modal loaded on this page) ─────
function ksOpenPage(itemType, itemId) {
    if (typeof _cachedStoreId !== "undefined") _cachedStoreId = ksStoreId;
    if (itemType === "product") viewProduct(itemId);
    else if (itemType === "collection") viewCollection(itemId);
}

async function ksOptimizePage(itemType, itemId, keyword, volume) {
    if (itemType !== "product" && itemType !== "collection") return;
    if (typeof _cachedStoreId !== "undefined") _cachedStoreId = ksStoreId;
    const opts = keyword ? { targetKeyword: keyword, volume: volume || null } : undefined;
    try {
        if (itemType === "product") {
            await viewProduct(itemId);
            analyzeProduct(itemId, null, opts);
        } else {
            await viewCollection(itemId);
            analyzeCollection(itemId, null, opts);
        }
    } catch (e) {
        showToast("Failed to open item: " + e.message, "error");
    }
}

function initOptimizeModalObserver() {
    const modal = document.getElementById("product-modal");
    if (!modal) return;
    let wasActive = modal.classList.contains("active");
    new MutationObserver(() => {
        const active = modal.classList.contains("active");
        if (wasActive && !active && ksExpandedCid !== null) refreshExpandedFamily(ksExpandedCid);
        wasActive = active;
    }).observe(modal, { attributes: true, attributeFilter: ["class"] });
}

async function refreshExpandedFamily(cid) {
    // Re-fetch just this family so ownership/staleness reflect the optimize,
    // keeping the accordion open and the scroll position untouched.
    delete ksFamilyCache[cid];
    const expand = document.getElementById(`ks-fam-expand-${cid}`);
    const box = document.getElementById(`ks-fam-kws-${cid}`);
    if (!expand || !box || expand.style.display === "none") return;
    try {
        const fam = await apiRequest(`/api/keyword-strategy/families/${cid}?store_id=${ksStoreId}`);
        ksFamilyCache[cid] = fam;
        box.innerHTML = renderFamilyDetail(fam, cid);
        const row = document.querySelector(`.ks-fam-row[data-cid="${cid}"]`);
        if (row) {
            const tds = row.querySelectorAll("td");
            if (tds.length >= 10) {
                tds[4].textContent = (fam.pages || []).length;
                ["transactional", "commercial", "informational", "navigational"].forEach((i, idx) => {
                    tds[5 + idx].innerHTML = renderMatrixCell((fam.coverage || {})[i], cid, i);
                });
                tds[9].textContent = fam.gap_score ? fam.gap_score.toLocaleString() : "—";
            }
        }
        loadKwOverview();
    } catch (e) {}
}

// ── Keyword detail drawer ────────────────────────────────────────────────
async function openKeywordDrawer(keywordId) {
    if (!ksKwDrawer) ksKwDrawer = UIDrawer.create({ id: "ks-kw-drawer", title: "Keyword", width: 560 });
    ksKwDrawer.setBody('<div class="spinner" style="margin:40px auto"></div>').open();
    try {
        const d = await apiRequest(`/api/keyword-strategy/keywords/${keywordId}/detail?store_id=${ksStoreId}`);
        renderKeywordDrawer(d);
    } catch (e) {
        ksKwDrawer.setBody(`<div style="color:var(--error);padding:20px;">Failed to load keyword: ${escapeHtml(e.message)}</div>`);
    }
}

function renderKeywordDrawer(d) {
    const kw = d.keyword;
    ksKwDrawer.setTitle(escapeHtml(kw.keyword));
    const intents = ["transactional", "commercial", "informational", "navigational"];
    const metrics = [];
    if (kw.search_volume) metrics.push(`vol ${(kw.search_volume || 0).toLocaleString()}`);
    if (kw.cpc) metrics.push(`cpc $${parseFloat(kw.cpc).toFixed(2)}`);
    if (kw.clicks) metrics.push(`${Math.round(kw.clicks).toLocaleString()} clicks`);
    if (kw.impressions) metrics.push(`${Math.round(kw.impressions).toLocaleString()} impr`);
    if (kw.best_position) metrics.push(`pos ${parseFloat(kw.best_position).toFixed(1)}`);

    let html = `<div class="ks-drawer-section" style="padding-top:0;">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
            <span class="ks-drawer-kw">${escapeHtml(kw.keyword)}</span>
            <select id="ks-drawer-intent" class="form-control" style="width:auto;font-size:12px;padding:3px 8px;"
                    onchange="setKeywordIntent(${kw.id}, this.value)">
                ${intents.map(i => `<option value="${i}" ${kw.intent === i ? 'selected' : ''}>${i}</option>`).join("")}
            </select>
            <span class="badge" style="font-size:10px;">${kw.intent_source === 'manual' ? 'manual' : kw.intent_source === 'ai' ? 'ai classified' : 'rules'}</span>
            <span class="badge badge-${sourceBadge(kw.source)}" style="font-size:10px;">${kw.source || ''}</span>
        </div>
        <div class="ks-drawer-metrics">${metrics.join(" · ") || "no demand data"}</div>
    </div>`;

    // Owner
    html += '<div class="ks-drawer-section"><div class="ks-drawer-label">Owner</div>';
    if (d.owner) {
        const roleLabels = { resolved_primary: "resolved primary", assigned: "assigned", mapped_primary: "audit primary" };
        html += `<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <span class="ks-owner-chip">
                <span class="badge badge-${typeBadge(d.owner.item_type)}" style="font-size:10px;">${d.owner.item_type}</span>
                <strong style="overflow:hidden;text-overflow:ellipsis;">${escapeHtml(d.owner.title || '#' + d.owner.item_id)}</strong>
            </span>
            <span style="font-size:11.5px;color:var(--on-surface-secondary);">${roleLabels[d.owner.role] || d.owner.role}</span>
        </div>`;
    } else {
        html += '<div style="font-size:13px;color:var(--on-surface-secondary);">No page owns this keyword yet.</div>';
    }
    if (d.mentions_count) {
        html += `<div style="font-size:12px;color:var(--on-surface-secondary);margin-top:6px;cursor:pointer;" onclick="loadKeywordMentions(${kw.id}, this)">
            Mentioned on ${d.mentions_count} page(s) <span class="material-symbols-rounded" style="font-size:14px;vertical-align:middle;">expand_more</span>
        </div><div id="ks-drawer-mentions"></div>`;
    }
    html += '</div>';

    // Family
    if (d.family) {
        const fam = d.family;
        html += `<div class="ks-drawer-section">
            <div class="ks-drawer-label">Family — ${escapeHtml(fam.cluster.name)} (${fam.keyword_count} keywords, ${(fam.total_volume || 0).toLocaleString()} vol)</div>
            ${renderFamilyKwMini(fam)}
            <button class="btn btn-sm btn-outline" style="margin-top:8px;font-size:11.5px;"
                    onclick="ksKwDrawer.close();ksViewFamily(${fam.cluster.id === null ? 0 : fam.cluster.id})">
                <span class="material-symbols-rounded" style="font-size:14px;vertical-align:middle;">travel_explore</span>
                Open family in Explorer
            </button>
        </div>`;
        const gaps = fam.gaps || [];
        if (gaps.length) {
            html += '<div class="ks-drawer-section"><div class="ks-drawer-label">Gaps</div>';
            gaps.forEach((g, i) => {
                html += `<div class="ks-gap-item">
                    <span class="material-symbols-rounded">${g.action === 'research_topic' ? 'edit_note' : 'link_off'}</span>
                    <span style="flex:1;">${escapeHtml(g.message)}${g.volume ? ` <span style="color:var(--on-surface-secondary);">(${g.volume.toLocaleString()} vol)</span>` : ''}</span>
                    ${renderGapAction(g, fam.cluster.id)}
                </div>`;
            });
            html += '</div>';
        }
    } else {
        html += `<div class="ks-drawer-section">
            <div class="ks-drawer-label">Family</div>
            <div style="font-size:13px;color:var(--on-surface-secondary);">Not assigned to a topic family yet — run AI Classify &amp; Cluster.</div>
        </div>`;
    }
    ksKwDrawer.setBody(html);
}

function renderFamilyKwMini(fam) {
    // Compact top-8 keyword list for the drawer — the full page-grouped
    // picture lives in the Explorer expansion.
    const all = [];
    ["transactional", "commercial", "informational", "navigational"].forEach(intent => {
        ((fam.keywords_by_intent || {})[intent] || []).forEach(k => all.push(k));
    });
    all.sort((a, b) => (b.search_volume || 0) - (a.search_volume || 0));
    const rows = all.slice(0, 8).map(k => `<div class="ks-fam-kw-row" onclick="openKeywordDrawer(${k.id})">
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(k.keyword)}</span>
        <span style="color:var(--on-surface-secondary);font-size:12px;white-space:nowrap;">${(k.search_volume || 0).toLocaleString()}</span>
        <span class="badge badge-${intentBadge(k.intent)}" style="font-size:9.5px;">${k.intent || '-'}</span>
        ${k.owner
            ? `<span class="ks-owner-chip"><span class="badge badge-${typeBadge(k.owner.item_type)}" style="font-size:9.5px;">${k.owner.item_type}</span><span style="overflow:hidden;text-overflow:ellipsis;max-width:160px;">${escapeHtml(k.owner.title || '')}</span></span>`
            : '<span style="font-size:11.5px;color:var(--on-surface-secondary);font-style:italic;">unowned</span>'}
    </div>`).join("");
    const more = all.length > 8
        ? `<div style="font-size:11.5px;color:var(--on-surface-secondary);padding:2px 8px;">+ ${all.length - 8} more in the Explorer</div>`
        : "";
    return (rows || '<div style="font-size:13px;color:var(--on-surface-secondary);">No keywords in this family.</div>') + more;
}

function renderGapAction(gap, clusterId) {
    if (gap.action === "research_topic") {
        return `<button class="btn btn-sm btn-outline" style="font-size:11px;white-space:nowrap;" onclick='ksActionResearch(${clusterId || 0}, ${JSON.stringify(gap.intent)})'>Research topic</button>`;
    }
    const kwArg = gap.keyword_id || (gap.keywords && gap.keywords[0] ? gap.keywords[0].id : null);
    if (!kwArg) return "";
    return `<button class="btn btn-sm btn-outline" style="font-size:11px;white-space:nowrap;" onclick="ksActionAssign(${kwArg})">Assign to page</button>`;
}

async function loadKeywordMentions(keywordId, trigger) {
    const box = document.getElementById("ks-drawer-mentions");
    if (!box) return;
    if (box.innerHTML) { box.innerHTML = ""; return; }
    box.innerHTML = '<div class="spinner" style="margin:10px auto"></div>';
    try {
        const d = await apiRequest(`/api/keyword-strategy/keywords/${keywordId}/mentions?store_id=${ksStoreId}&limit=25`);
        box.innerHTML = (d.mentions || []).map(m => `<div style="display:flex;align-items:center;gap:8px;font-size:12.5px;padding:3px 0;">
            <span class="badge badge-${typeBadge(m.item_type)}" style="font-size:9.5px;">${m.item_type}</span>
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(m.title || '#' + m.item_id)}</span>
            ${m.is_primary ? '<span class="badge badge-success" style="font-size:9.5px;">primary</span>' : ''}
        </div>`).join("") || '<div style="font-size:12px;color:var(--on-surface-secondary);">No mentions.</div>';
    } catch (e) { box.innerHTML = ""; }
}

async function setKeywordIntent(keywordId, intent) {
    try {
        await apiRequest(`/api/keyword-strategy/keywords/${keywordId}/intent?store_id=${ksStoreId}`, {
            method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ intent }),
        });
        showToast(`Intent set to ${intent}`, "success");
        ksFamilyCache = {};
        loadFamilies();
        openKeywordDrawer(keywordId);
    } catch (e) { showToast("Failed: " + e.message, "error"); }
}

// ── Intent coverage cells (family table) ─────────────────────────────────
function renderMatrixCell(cell, cid, intent) {
    if (!cell || (!cell.count && !cell.volume)) return '<div class="ks-matrix-cell na">—</div>';
    if (cell.na) return `<div class="ks-matrix-cell na" title="Volume below threshold">${(cell.volume || 0).toLocaleString()}</div>`;
    const partial = cell.covered && cell.covered_volume < cell.volume;
    const cls = !cell.covered ? "gap" : partial ? "partial" : "covered";
    const actionable = cls !== "covered";
    return `<div class="ks-matrix-cell ${cls}${actionable ? ' actionable' : ''}"
                 ${actionable ? `onclick='event.stopPropagation();openGapPopover(event, ${cid}, ${JSON.stringify(intent)})'` : ''}>
        <div class="ks-matrix-vol">${(cell.covered_volume || 0).toLocaleString()} / ${(cell.volume || 0).toLocaleString()}</div>
        <div class="ks-matrix-sub">${cell.count} kw${cell.count !== 1 ? 's' : ''}</div>
    </div>`;
}

let ksPopoverEl = null;

function closeKsPopover() {
    if (ksPopoverEl) { ksPopoverEl.remove(); ksPopoverEl = null; }
}

function openGapPopover(ev, cid, intent) {
    closeKsPopover();
    const el = document.createElement("div");
    el.className = "ks-popover";
    const research = intent === "informational" || intent === "commercial"
        ? `<button onclick='closeKsPopover();ksActionResearch(${cid}, ${JSON.stringify(intent)})'><span class="material-symbols-rounded">edit_note</span> Research blog topic</button>` : "";
    el.innerHTML = `${research}
        <button onclick="closeKsPopover();ksActionAssignTop(${cid}, '${intent}')"><span class="material-symbols-rounded">link</span> Assign top keyword to page</button>
        <button onclick="closeKsPopover();ksViewFamily(${cid})"><span class="material-symbols-rounded">travel_explore</span> Open family</button>`;
    document.body.appendChild(el);
    const x = Math.min(ev.clientX, window.innerWidth - 240);
    const y = Math.min(ev.clientY + 8, window.innerHeight - 140);
    el.style.left = x + "px";
    el.style.top = y + "px";
    el.addEventListener("click", e => e.stopPropagation());
    ksPopoverEl = el;
}

function ksViewFamily(cid) {
    switchToStep("explorer");
    const row = document.querySelector(`.ks-fam-row[data-cid="${cid}"]`);
    if (row) { row.scrollIntoView({ block: "center" }); toggleFamily(cid, row); }
}

async function ksActionAssignTop(cid, intent) {
    // Resolve the family's highest-volume unowned keyword for that intent
    try {
        if (!ksFamilyCache[cid]) ksFamilyCache[cid] = await apiRequest(`/api/keyword-strategy/families/${cid}?store_id=${ksStoreId}`);
        const bucket = (ksFamilyCache[cid].keywords_by_intent || {})[intent] || [];
        const target = bucket.find(k => !k.owner) || bucket[0];
        if (!target) { showToast("No keywords in this intent bucket", "info"); return; }
        ksActionAssign(target.id);
    } catch (e) { showToast("Failed: " + e.message, "error"); }
}

// ── Gap actions ──────────────────────────────────────────────────────────
async function ksActionResearch(cid, intent) {
    if (!ksStoreId) return;
    try {
        if (!ksFamilyCache[cid]) ksFamilyCache[cid] = await apiRequest(`/api/keyword-strategy/families/${cid}?store_id=${ksStoreId}`);
        const fam = ksFamilyCache[cid];
        const bucket = ((fam.keywords_by_intent || {})[intent] || []);
        const targets = (bucket.filter(k => !k.owner).length ? bucket.filter(k => !k.owner) : bucket).slice(0, 15);
        if (!targets.length) { showToast("No keywords to research in this bucket", "info"); return; }
        const kwList = targets.map(k => `<li>${escapeHtml(k.keyword)} <span style="color:var(--on-surface-secondary);">(${(k.search_volume || 0).toLocaleString()})</span></li>`).join("");
        const modal = UIModal.open({
            title: "Research blog topics",
            bodyHtml: `<p style="font-size:13.5px;margin-bottom:10px;">
                    AI topic research will look for blog topics targeting these uncovered
                    <strong>${escapeHtml(intent)}</strong> keywords from the
                    <strong>${escapeHtml(fam.cluster.name)}</strong> family:</p>
                <ul style="font-size:13px;margin:0 0 12px 18px;max-height:220px;overflow-y:auto;">${kwList}</ul>
                <p style="font-size:12px;color:var(--on-surface-secondary);">Suggestions appear in Blog Posts &rarr; Research Topics. Money-page keywords stay protected.</p>`,
            footerHtml: `<button class="btn btn-outline" data-ks-cancel>Cancel</button>
                <button class="btn btn-primary" data-ks-go><span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">edit_note</span> Start research</button>`,
        });
        modal.el.querySelector("[data-ks-cancel]").addEventListener("click", () => modal.close());
        modal.el.querySelector("[data-ks-go]").addEventListener("click", async (ev) => {
            ev.target.disabled = true;
            try {
                await apiRequest("/api/blog/topics/research", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        store_id: ksStoreId, count: 2,
                        seed: {
                            cluster_id: fam.cluster.id, family: fam.cluster.name, intent,
                            keywords: targets.map(k => ({ keyword: k.keyword, volume: k.search_volume || 0 })),
                        },
                    }),
                });
                modal.close();
                showToast("Topic research started — suggestions will appear in Blog Posts", "success");
            } catch (e) { showToast("Failed to start research: " + e.message, "error"); ev.target.disabled = false; }
        });
    } catch (e) { showToast("Failed: " + e.message, "error"); }
}

async function ksActionAssign(keywordId) {
    if (!ksStoreId) return;
    let kw;
    try {
        kw = (await apiRequest(`/api/keyword-strategy/keywords/${keywordId}/detail?store_id=${ksStoreId}`)).keyword;
    } catch (e) { showToast("Failed: " + e.message, "error"); return; }

    let pickType = "collection";
    const modal = UIModal.open({
        title: `Assign "${escapeHtml(kw.keyword)}" to a page`,
        bodyHtml: `<div style="display:flex;gap:8px;margin-bottom:10px;">
                <select id="ks-pick-type" class="form-control" style="width:auto;font-size:13px;">
                    <option value="collection">Collection</option>
                    <option value="product">Product</option>
                </select>
                <input type="text" id="ks-pick-search" class="form-control" placeholder="Search pages..." style="flex:1;font-size:13px;">
            </div>
            <div id="ks-pick-results" style="max-height:280px;overflow-y:auto;"></div>
            <p style="font-size:12px;color:var(--on-surface-secondary);margin-top:10px;">
                The keyword becomes the page's manual primary — the resolver re-analyzes
                the page and every AI flow will optimize toward it.</p>`,
    });
    const results = modal.el.querySelector("#ks-pick-results");
    const searchInput = modal.el.querySelector("#ks-pick-search");
    modal.el.querySelector("#ks-pick-type").addEventListener("change", e => { pickType = e.target.value; doSearch(); });

    let t;
    searchInput.addEventListener("input", () => { clearTimeout(t); t = setTimeout(doSearch, 300); });
    searchInput.value = kw.keyword;
    setTimeout(doSearch, 50);

    async function doSearch() {
        const q = searchInput.value.trim();
        if (!q) { results.innerHTML = ""; return; }
        results.innerHTML = '<div class="spinner" style="margin:14px auto"></div>';
        try {
            const items = await apiRequest(`/api/blog/wizard/search/${pickType}s?store_id=${ksStoreId}&q=${encodeURIComponent(q)}`);
            if (!items.length) { results.innerHTML = '<div style="font-size:13px;color:var(--on-surface-secondary);padding:10px;">No matches.</div>'; return; }
            results.innerHTML = items.map(it => `<div class="ks-fam-kw-row" data-item-id="${it.id}">
                <span class="badge badge-${typeBadge(pickType)}" style="font-size:10px;">${pickType}</span>
                <span style="flex:1;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(it.title)}</span>
            </div>`).join("");
            results.querySelectorAll("[data-item-id]").forEach(row => row.addEventListener("click", () => pick(parseInt(row.dataset.itemId))));
        } catch (e) { results.innerHTML = ""; }
    }

    async function pick(itemId) {
        results.innerHTML = '<div class="spinner" style="margin:14px auto"></div>';
        try {
            // Full resolver pipeline: manual assignment + re-resolve + re-analysis
            await apiRequest(`/api/seo/items/${pickType}/${itemId}/primary-keyword?store_id=${ksStoreId}`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ keyword: kw.keyword }),
            });
            modal.close();
            showToast(`"${kw.keyword}" assigned as primary`, "success");
            ksFamilyCache = {};
            loadFamilies();
            if (ksKwDrawer && ksKwDrawer.isOpen()) openKeywordDrawer(keywordId);
        } catch (e) {
            showToast("Assign failed: " + e.message, "error");
            doSearch();
        }
    }
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
    loadFreshness();
    ksFamilyCache = {};
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
    const m = { transactional: "success", commercial: "warning", informational: "info", navigational: "" };
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
