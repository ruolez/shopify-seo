let srStoreId = null;
let srKeywordData = [];
let srKeywordSort = { col: "search_volume", dir: "desc" };
let srSelectedFile = null;
const SR_PAGE_SIZE = 50;
let srKeywordPage = 1;
let srStrikingPage = 1;
let srXrefPage = 1;
let srAuditPage = 1;

document.addEventListener("DOMContentLoaded", () => {
    loadSrStores();
    initSrTabs();
    initSrUpload();
});

// ─── Store Selector ───

async function loadSrStores() {
    try {
        const stores = await apiRequest("/api/stores");
        const select = document.getElementById("sr-store-select");
        stores.forEach(s => {
            const opt = document.createElement("option");
            opt.value = s.id;
            opt.textContent = s.name;
            select.appendChild(opt);
        });
        if (stores.length === 1) {
            select.value = stores[0].id;
            onSrStoreSelected(stores[0].id);
        }
    } catch (err) {
        showToast("Failed to load stores", "error");
    }
}

document.getElementById("sr-store-select").addEventListener("change", (e) => {
    const id = parseInt(e.target.value);
    if (id) onSrStoreSelected(id);
});

async function onSrStoreSelected(storeId) {
    srStoreId = storeId;
    loadSrOverview();
    loadSrActiveTab();
}

// ─── Upload Zone ───

function initSrUpload() {
    const dropZone = document.getElementById("sr-drop-zone");
    const fileInput = document.getElementById("sr-file-input");
    const fileBtn = document.getElementById("sr-file-btn");

    fileBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", (e) => {
        if (e.target.files[0]) handleSrFile(e.target.files[0]);
    });

    dropZone.addEventListener("dragover", (e) => { e.preventDefault(); dropZone.classList.add("drag-over"); });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
    dropZone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropZone.classList.remove("drag-over");
        if (e.dataTransfer.files[0]) handleSrFile(e.dataTransfer.files[0]);
    });
}

async function handleSrFile(file) {
    if (!file.name.toLowerCase().endsWith(".csv")) {
        showToast("Only CSV files are supported", "error");
        return;
    }
    srSelectedFile = file;

    const preview = document.getElementById("sr-preview");
    preview.style.display = "";
    preview.innerHTML = '<div style="padding: 20px; text-align: center;"><div class="spinner" style="margin: 0 auto;"></div><p style="margin-top: 8px; font-size: 13px;">Analyzing CSV...</p></div>';

    const formData = new FormData();
    formData.append("file", file);

    try {
        const resp = await fetch("/api/semrush/upload/preview", { method: "POST", body: formData });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error);
        renderSrPreview(data, file);
    } catch (err) {
        preview.innerHTML = `<p style="padding: 16px; color: var(--error);">Preview failed: ${escapeHtml(err.message)}</p>`;
    }
}

function renderSrPreview(data, file) {
    const typeStr = data.type || "unknown";
    const typeBadge = typeStr !== "unknown"
        ? `<span class="badge badge-success">${escapeHtml(typeStr.replace(/_/g, " "))}</span> <span style="font-size:12px;color:var(--on-surface-secondary);">(${Math.round((data.confidence || 0) * 100)}% confidence)</span>`
        : '<span class="badge badge-error">Unknown format</span>';

    let tableHtml = '<div class="table-container" style="max-height: 250px; overflow: auto; margin: 12px 0;"><table class="data-table" style="font-size: 12px;"><thead><tr>';
    data.columns.forEach(c => { tableHtml += `<th>${escapeHtml(c)}</th>`; });
    tableHtml += "</tr></thead><tbody>";
    data.preview_rows.forEach(row => {
        tableHtml += "<tr>";
        row.forEach(cell => { tableHtml += `<td>${escapeHtml(cell)}</td>`; });
        tableHtml += "</tr>";
    });
    tableHtml += "</tbody></table></div>";

    document.getElementById("sr-preview").innerHTML = `
        <div style="padding: 16px; border-top: 1px solid var(--outline);">
            <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap;">
                <strong>${escapeHtml(file.name)}</strong>
                <span style="font-size: 12px; color: var(--on-surface-secondary);">${data.total_rows.toLocaleString()} rows</span>
                ${typeBadge}
                <select id="sr-force-type" class="form-control" style="width: auto; font-size: 13px;">
                    <option value="">Auto-detected</option>
                    <option value="position_tracking" ${data.type === "position_tracking" ? "selected" : ""}>Position Tracking</option>
                    <option value="organic_research" ${data.type === "organic_research" ? "selected" : ""}>Organic Research</option>
                    <option value="site_audit" ${data.type === "site_audit" ? "selected" : ""}>Site Audit</option>
                    <option value="keyword_gap" ${data.type === "keyword_gap" ? "selected" : ""}>Keyword Gap</option>
                </select>
                <button class="btn btn-primary btn-sm" onclick="executeSrImport()" id="sr-import-btn">
                    <span class="material-symbols-rounded" style="font-size:14px;vertical-align:middle;">cloud_upload</span> Import
                </button>
                <button class="btn btn-secondary btn-sm" onclick="cancelSrPreview()">Cancel</button>
            </div>
            ${tableHtml}
        </div>`;
}

function cancelSrPreview() {
    document.getElementById("sr-preview").style.display = "none";
    document.getElementById("sr-preview").innerHTML = "";
    srSelectedFile = null;
    document.getElementById("sr-file-input").value = "";
}

async function executeSrImport() {
    if (!srSelectedFile) return;
    if (!srStoreId) {
        showToast("Select a store first", "warning");
        return;
    }
    const btn = document.getElementById("sr-import-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Importing...';

    const formData = new FormData();
    formData.append("file", srSelectedFile);
    formData.append("store_id", srStoreId);
    const forceType = document.getElementById("sr-force-type")?.value;
    if (forceType) formData.append("type", forceType);

    try {
        const resp = await fetch("/api/semrush/upload", { method: "POST", body: formData });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error);
        showToast(`Import complete: ${data.row_count} rows (${data.type.replace("_", " ")})`, "success");
        cancelSrPreview();
        loadSrOverview();
        loadSrActiveTab();
    } catch (err) {
        showToast("Import failed: " + err.message, "error");
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:14px;vertical-align:middle;">cloud_upload</span> Import';
    }
}

// ─── Overview ───

async function loadSrOverview() {
    if (!srStoreId) return;
    try {
        const data = await apiRequest(`/api/semrush/overview?store_id=${srStoreId}`);
        if (data.total_keywords === 0) {
            document.getElementById("sr-overview").style.display = "none";
            document.getElementById("sr-tabs-container").style.display = "none";
            return;
        }
        document.getElementById("sr-overview").style.display = "";
        document.getElementById("sr-tabs-container").style.display = "";
        document.getElementById("sr-total-kw").textContent = data.total_keywords.toLocaleString();
        document.getElementById("sr-avg-pos").textContent = data.avg_position;
        document.getElementById("sr-total-vol").textContent = data.total_volume.toLocaleString();
        document.getElementById("sr-top3").textContent = `${data.top3} top 3`;
        document.getElementById("sr-top10").textContent = `${data.top10} 4-10`;
        document.getElementById("sr-beyond").textContent = `${data.beyond20} 20+`;
    } catch (err) {
        console.error("Failed to load overview:", err);
    }
}

// ─── Tabs ───

function initSrTabs() {
    document.querySelectorAll("[data-sr-tab]").forEach(tab => {
        tab.addEventListener("click", () => {
            document.querySelectorAll("[data-sr-tab]").forEach(t => t.classList.remove("active"));
            document.querySelectorAll(".sr-tab-panel").forEach(p => p.style.display = "none");
            tab.classList.add("active");
            const panel = document.getElementById("sr-tab-" + tab.dataset.srTab);
            if (panel) panel.style.display = "";
            loadSrActiveTab();
        });
    });

    let kwSearchTimeout;
    document.getElementById("sr-kw-search").addEventListener("input", () => {
        clearTimeout(kwSearchTimeout);
        kwSearchTimeout = setTimeout(() => loadSrKeywords(), 300);
    });
    document.getElementById("sr-kw-source").addEventListener("change", () => loadSrKeywords());

    let xrefSearchTimeout;
    document.getElementById("sr-xref-search").addEventListener("input", () => {
        clearTimeout(xrefSearchTimeout);
        xrefSearchTimeout = setTimeout(() => loadSrCrossRef(), 300);
    });
    document.getElementById("sr-audit-severity").addEventListener("change", () => loadSrAudit());

    document.getElementById("sr-striking-range").addEventListener("change", () => loadSrStriking());
    let strikingSearchTimeout;
    document.getElementById("sr-striking-search").addEventListener("input", () => {
        clearTimeout(strikingSearchTimeout);
        strikingSearchTimeout = setTimeout(() => loadSrStriking(), 300);
    });
}

function loadSrActiveTab() {
    const active = document.querySelector("[data-sr-tab].active");
    if (!active || !srStoreId) return;
    const tab = active.dataset.srTab;
    if (tab === "keywords") loadSrKeywords();
    else if (tab === "striking") loadSrStriking();
    else if (tab === "cross-ref") loadSrCrossRef();
    else if (tab === "audit") loadSrAudit();
    else if (tab === "imports") loadSrImports();
}

// ─── Keywords Tab ───

function sortSrKeywords(col) {
    if (srKeywordSort.col === col) {
        srKeywordSort.dir = srKeywordSort.dir === "desc" ? "asc" : "desc";
    } else {
        srKeywordSort.col = col;
        srKeywordSort.dir = (col === "keyword") ? "asc" : "desc";
    }
    srKeywordPage = 1;
    renderSrKeywords();
}

function srSortHeader(col, label) {
    const arrow = srKeywordSort.col === col ? (srKeywordSort.dir === "asc" ? " \u2191" : " \u2193") : "";
    const active = srKeywordSort.col === col ? "color:var(--primary);" : "";
    return `<th class="sortable-header" style="cursor:pointer;user-select:none;${active}" onclick="sortSrKeywords('${col}')">${label}${arrow}</th>`;
}

async function loadSrKeywords() {
    if (!srStoreId) return;
    const tbody = document.getElementById("sr-kw-tbody");
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;"><div class="spinner" style="margin:20px auto;"></div></td></tr>';

    const search = document.getElementById("sr-kw-search").value.trim();
    const source = document.getElementById("sr-kw-source").value;
    let url = `/api/semrush/keywords?store_id=${srStoreId}&limit=10000`;
    if (search) url += `&search=${encodeURIComponent(search)}`;
    if (source) url += `&source_type=${source}`;

    try {
        const data = await apiRequest(url);
        srKeywordData = data.keywords || [];
        srKeywordPage = 1;
        if (!srKeywordData.length) {
            tbody.innerHTML = "";
            document.getElementById("sr-kw-count").textContent = "0 keywords";
            document.getElementById("sr-kw-empty").style.display = "";
            return;
        }
        document.getElementById("sr-kw-empty").style.display = "none";
        renderSrKeywords();
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="8" style="color:var(--error);">Failed: ${escapeHtml(err.message)}</td></tr>`;
    }
}

function goSrKeywordPage(p) { srKeywordPage = p; renderSrKeywords(); }

function renderSrKeywords() {
    const thead = document.getElementById("sr-kw-thead");
    thead.innerHTML = srSortHeader("keyword", "Keyword")
        + srSortHeader("position", "Position")
        + srSortHeader("change", "Change")
        + srSortHeader("search_volume", "Monthly Volume")
        + srSortHeader("cpc", "CPC")
        + srSortHeader("traffic", "Traffic")
        + srSortHeader("competition", "Comp.")
        + '<th style="width:25%;">URL</th>';

    const sorted = [...srKeywordData].sort((a, b) => {
        const col = srKeywordSort.col;
        let va = a[col], vb = b[col];
        if (typeof va === "string") return srKeywordSort.dir === "asc" ? (va||"").localeCompare(vb||"") : (vb||"").localeCompare(va||"");
        va = va ?? -99999; vb = vb ?? -99999;
        return srKeywordSort.dir === "asc" ? va - vb : vb - va;
    });

    const start = (srKeywordPage - 1) * SR_PAGE_SIZE;
    const page = sorted.slice(start, start + SR_PAGE_SIZE);
    document.getElementById("sr-kw-count").textContent = `${srKeywordData.length.toLocaleString()} keywords`;

    document.getElementById("sr-kw-tbody").innerHTML = page.map(r => `
        <tr>
            <td><strong>${escapeHtml(r.keyword)}</strong><div>${writeArticleLink(r.keyword)}</div></td>
            <td>${r.position != null ? positionBadge(r.position) : '—'}</td>
            <td>${renderChange(r.change)}</td>
            <td>${r.search_volume != null ? r.search_volume.toLocaleString() : '—'}</td>
            <td>${r.cpc != null ? '$' + r.cpc.toFixed(2) : '—'}</td>
            <td>${r.traffic != null ? Math.round(r.traffic).toLocaleString() : '—'}</td>
            <td>${r.competition != null ? r.competition.toFixed(2) : '—'}</td>
            <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;" title="${escapeHtml(r.url || '')}">${r.url ? shortenUrl(r.url) : '—'}</td>
        </tr>
    `).join("") + `<tr><td colspan="8">${renderPagination("sr-kw", srKeywordPage, sorted.length, "goSrKeywordPage")}</td></tr>`;
}

function renderPagination(containerId, currentPage, totalItems, onPageChange) {
    const totalPages = Math.ceil(totalItems / SR_PAGE_SIZE);
    if (totalPages <= 1) return "";
    let html = '<div style="display:flex;align-items:center;justify-content:center;gap:6px;padding:12px 0;">';
    html += `<button class="btn btn-secondary btn-sm" ${currentPage <= 1 ? "disabled" : ""} onclick="${onPageChange}(${currentPage - 1})">&laquo; Prev</button>`;
    const start = Math.max(1, currentPage - 2);
    const end = Math.min(totalPages, currentPage + 2);
    if (start > 1) html += `<button class="btn btn-secondary btn-sm" onclick="${onPageChange}(1)">1</button>`;
    if (start > 2) html += '<span style="color:var(--on-surface-secondary);">...</span>';
    for (let i = start; i <= end; i++) {
        html += `<button class="btn btn-sm ${i === currentPage ? 'btn-primary' : 'btn-secondary'}" onclick="${onPageChange}(${i})">${i}</button>`;
    }
    if (end < totalPages - 1) html += '<span style="color:var(--on-surface-secondary);">...</span>';
    if (end < totalPages) html += `<button class="btn btn-secondary btn-sm" onclick="${onPageChange}(${totalPages})">${totalPages}</button>`;
    html += `<button class="btn btn-secondary btn-sm" ${currentPage >= totalPages ? "disabled" : ""} onclick="${onPageChange}(${currentPage + 1})">Next &raquo;</button>`;
    html += `<span style="font-size:12px;color:var(--on-surface-secondary);margin-left:8px;">Page ${currentPage} of ${totalPages} (${totalItems.toLocaleString()} total)</span>`;
    html += '</div>';
    return html;
}

function renderChange(change) {
    if (change == null) return '<span style="color:var(--on-surface-secondary);">—</span>';
    if (change > 0) return `<span style="color:var(--success);font-weight:600;">\u25B2${change}</span>`;
    if (change < 0) return `<span style="color:var(--error);font-weight:600;">\u25BC${Math.abs(change)}</span>`;
    return '<span style="color:var(--on-surface-secondary);">=</span>';
}

function positionBadge(pos) {
    const rounded = typeof pos === "number" ? (Number.isInteger(pos) ? pos : pos.toFixed(1)) : pos;
    if (pos <= 3) return `<span class="badge badge-success">${rounded}</span>`;
    if (pos <= 10) return `<span class="badge badge-warning">${rounded}</span>`;
    return `<span class="badge badge-error">${rounded}</span>`;
}

function shortenUrl(url) {
    try {
        const u = new URL(url);
        return u.pathname + u.search;
    } catch {
        return url;
    }
}

// ─── Striking Distance Tab ───

let srStrikingData = [];
let srStrikingSort = { col: "search_volume", dir: "desc" };

function sortSrStriking(col) {
    if (srStrikingSort.col === col) {
        srStrikingSort.dir = srStrikingSort.dir === "desc" ? "asc" : "desc";
    } else {
        srStrikingSort.col = col;
        srStrikingSort.dir = (col === "keyword" || col === "url") ? "asc" : "desc";
    }
    srStrikingPage = 1;
    renderSrStriking();
}

function strikingSortHeader(col, label) {
    const arrow = srStrikingSort.col === col ? (srStrikingSort.dir === "asc" ? " \u2191" : " \u2193") : "";
    const active = srStrikingSort.col === col ? "color:var(--primary);" : "";
    return `<th class="sortable-header" style="cursor:pointer;user-select:none;${active}" onclick="sortSrStriking('${col}')">${label}${arrow}</th>`;
}

async function loadSrStriking() {
    if (!srStoreId) return;
    const tbody = document.getElementById("sr-striking-tbody");
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;"><div class="spinner" style="margin:20px auto;"></div></td></tr>';

    const range = document.getElementById("sr-striking-range").value.split("-");
    const posMin = parseInt(range[0]);
    const posMax = parseInt(range[1]);
    const search = document.getElementById("sr-striking-search").value.trim();

    let url = `/api/semrush/keywords/striking-distance?store_id=${srStoreId}&pos_min=${posMin}&pos_max=${posMax}&limit=10000`;
    if (search) url += `&search=${encodeURIComponent(search)}`;

    try {
        srStrikingData = await apiRequest(url);
        srStrikingPage = 1;
        document.getElementById("sr-striking-count").textContent = `${srStrikingData.length} keywords in range`;
        if (!srStrikingData.length) {
            tbody.innerHTML = "";
            document.getElementById("sr-striking-empty").style.display = "";
            return;
        }
        document.getElementById("sr-striking-empty").style.display = "none";
        renderSrStriking();
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="8" style="color:var(--error);">Failed: ${escapeHtml(err.message)}</td></tr>`;
    }
}

function goSrStrikingPage(p) { srStrikingPage = p; renderSrStriking(); }

function renderSrStriking() {
    const thead = document.getElementById("sr-striking-thead");
    thead.innerHTML = strikingSortHeader("keyword", "Keyword")
        + strikingSortHeader("position", "Position")
        + strikingSortHeader("change", "Change")
        + strikingSortHeader("search_volume", "Monthly Volume")
        + strikingSortHeader("potential", "Potential Traffic")
        + strikingSortHeader("cpc", "CPC")
        + strikingSortHeader("traffic", "Current Traffic")
        + '<th style="width:20%;">URL</th>';

    const sorted = [...srStrikingData].sort((a, b) => {
        const col = srStrikingSort.col;
        let va = a[col], vb = b[col];
        if (typeof va === "string") return srStrikingSort.dir === "asc" ? (va||"").localeCompare(vb||"") : (vb||"").localeCompare(va||"");
        va = va ?? -99999; vb = vb ?? -99999;
        return srStrikingSort.dir === "asc" ? va - vb : vb - va;
    });

    const start = (srStrikingPage - 1) * SR_PAGE_SIZE;
    const page = sorted.slice(start, start + SR_PAGE_SIZE);

    document.getElementById("sr-striking-tbody").innerHTML = page.map(r => `
        <tr>
            <td><strong>${escapeHtml(r.keyword)}</strong><div>${writeArticleLink(r.keyword)}</div></td>
            <td>${r.position != null ? positionBadge(r.position) : '—'}</td>
            <td>${renderChange(r.change)}</td>
            <td><strong>${r.search_volume != null ? r.search_volume.toLocaleString() : '—'}</strong></td>
            <td style="color: var(--success); font-weight: 600;">${r.potential ? '+' + r.potential.toLocaleString() : '—'}</td>
            <td>${r.cpc != null ? '$' + r.cpc.toFixed(2) : '—'}</td>
            <td>${r.traffic != null ? Math.round(r.traffic).toLocaleString() : '—'}</td>
            <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;" title="${escapeHtml(r.url || '')}">${r.url ? shortenUrl(r.url) : '—'}</td>
        </tr>
    `).join("") + `<tr><td colspan="8">${renderPagination("sr-striking", srStrikingPage, sorted.length, "goSrStrikingPage")}</td></tr>`;
}

// ─── Cross-Reference Tab ───

let srXrefData = [];
let srXrefSort = { col: "sr_volume", dir: "desc" };

function sortSrXref(col) {
    if (srXrefSort.col === col) {
        srXrefSort.dir = srXrefSort.dir === "desc" ? "asc" : "desc";
    } else {
        srXrefSort.col = col;
        srXrefSort.dir = (col === "keyword" || col === "url") ? "asc" : "desc";
    }
    srXrefPage = 1;
    renderSrCrossRef();
}

function xrefSortHeader(col, label) {
    const arrow = srXrefSort.col === col ? (srXrefSort.dir === "asc" ? " \u2191" : " \u2193") : "";
    const active = srXrefSort.col === col ? "color:var(--primary);" : "";
    return `<th class="sortable-header" style="cursor:pointer;user-select:none;${active}" onclick="sortSrXref('${col}')">${label}${arrow}</th>`;
}

async function loadSrCrossRef() {
    if (!srStoreId) return;
    const tbody = document.getElementById("sr-xref-tbody");
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;"><div class="spinner" style="margin:20px auto;"></div></td></tr>';

    const search = document.getElementById("sr-xref-search").value.trim();
    let url = `/api/semrush/keywords/cross-reference?store_id=${srStoreId}&limit=10000`;
    if (search) url += `&search=${encodeURIComponent(search)}`;

    try {
        srXrefData = await apiRequest(url);
        srXrefPage = 1;
        if (!srXrefData.length) {
            tbody.innerHTML = "";
            document.getElementById("sr-xref-empty").style.display = "";
            return;
        }
        document.getElementById("sr-xref-empty").style.display = "none";
        renderSrCrossRef();
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="7" style="color:var(--error);">Failed: ${escapeHtml(err.message)}</td></tr>`;
    }
}

function goSrXrefPage(p) { srXrefPage = p; renderSrCrossRef(); }

function renderSrCrossRef() {
    const thead = document.querySelector("#sr-tab-cross-ref thead tr");
    thead.innerHTML = xrefSortHeader("keyword", "Keyword")
        + xrefSortHeader("sr_position", "SR Position")
        + xrefSortHeader("gsc_position", "GSC Position")
        + xrefSortHeader("sr_volume", "SR Volume")
        + xrefSortHeader("gsc_clicks", "GSC Clicks")
        + xrefSortHeader("gsc_impressions", "GSC Impressions")
        + '<th>URL</th>';

    const sorted = [...srXrefData].sort((a, b) => {
        const col = srXrefSort.col;
        let va = a[col], vb = b[col];
        if (typeof va === "string") return srXrefSort.dir === "asc" ? (va||"").localeCompare(vb||"") : (vb||"").localeCompare(va||"");
        va = va ?? -99999; vb = vb ?? -99999;
        return srXrefSort.dir === "asc" ? va - vb : vb - va;
    });

    const start = (srXrefPage - 1) * SR_PAGE_SIZE;
    const page = sorted.slice(start, start + SR_PAGE_SIZE);

    document.getElementById("sr-xref-tbody").innerHTML = page.map(r => `
        <tr>
            <td><strong>${escapeHtml(r.keyword)}</strong></td>
            <td>${r.sr_position != null ? positionBadge(r.sr_position) : '—'}</td>
            <td>${r.gsc_position != null ? positionBadge(r.gsc_position) : '—'}</td>
            <td>${r.sr_volume != null ? r.sr_volume.toLocaleString() : '—'}</td>
            <td>${Math.round(r.gsc_clicks).toLocaleString()}</td>
            <td>${Math.round(r.gsc_impressions).toLocaleString()}</td>
            <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;">${r.url ? shortenUrl(r.url) : '—'}</td>
        </tr>
    `).join("") + `<tr><td colspan="7">${renderPagination("sr-xref", srXrefPage, sorted.length, "goSrXrefPage")}</td></tr>`;
}

// ─── Audit Issues Tab ───

let srAuditData = [];

async function loadSrAudit() {
    if (!srStoreId) return;
    const tbody = document.getElementById("sr-audit-tbody");
    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;"><div class="spinner" style="margin:20px auto;"></div></td></tr>';

    try {
        const severity = document.getElementById("sr-audit-severity").value;
        const [summary, data] = await Promise.all([
            apiRequest(`/api/semrush/audit-summary?store_id=${srStoreId}`),
            apiRequest(`/api/semrush/audit-issues?store_id=${srStoreId}&limit=10000${severity ? '&severity=' + severity : ''}`),
        ]);

        document.getElementById("sr-audit-counts").innerHTML =
            `<span class="badge badge-error">${summary.error} errors</span>
             <span class="badge badge-warning">${summary.warning} warnings</span>
             <span class="badge badge-info">${summary.notice} notices</span>
             <span style="font-size:12px;color:var(--on-surface-secondary);">${summary.total} total</span>`;

        srAuditData = data;
        srAuditPage = 1;
        if (!srAuditData.length) {
            tbody.innerHTML = "";
            document.getElementById("sr-audit-empty").style.display = "";
            return;
        }
        document.getElementById("sr-audit-empty").style.display = "none";
        renderSrAudit();
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="4" style="color:var(--error);">Failed: ${escapeHtml(err.message)}</td></tr>`;
    }
}

function goSrAuditPage(p) { srAuditPage = p; renderSrAudit(); }

function renderSrAudit() {
    const start = (srAuditPage - 1) * SR_PAGE_SIZE;
    const page = srAuditData.slice(start, start + SR_PAGE_SIZE);

    document.getElementById("sr-audit-tbody").innerHTML = page.map(r => {
        const sevCls = (r.severity || "").toLowerCase() === "error" ? "badge-error" : (r.severity || "").toLowerCase() === "warning" ? "badge-warning" : "badge-info";
        return `<tr>
            <td style="max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;" title="${escapeHtml(r.page_url)}">${escapeHtml(r.page_url)}</td>
            <td>${escapeHtml(r.issue_type || '—')}</td>
            <td><span class="badge ${sevCls}">${escapeHtml(r.severity || 'notice')}</span></td>
            <td style="font-size:12px;">${escapeHtml(r.description || '—')}</td>
        </tr>`;
    }).join("") + `<tr><td colspan="4">${renderPagination("sr-audit", srAuditPage, srAuditData.length, "goSrAuditPage")}</td></tr>`;
}

// ─── Import History Tab ───

async function loadSrImports() {
    if (!srStoreId) return;
    const tbody = document.getElementById("sr-imports-tbody");
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;"><div class="spinner" style="margin:20px auto;"></div></td></tr>';

    try {
        const data = await apiRequest(`/api/semrush/imports?store_id=${srStoreId}`);
        if (!data.length) {
            tbody.innerHTML = "";
            document.getElementById("sr-imports-empty").style.display = "";
            return;
        }
        document.getElementById("sr-imports-empty").style.display = "none";
        tbody.innerHTML = data.map(r => {
            const statusCls = r.status === "completed" ? "badge-success" : r.status === "failed" ? "badge-error" : "badge-warning";
            return `<tr>
                <td>${formatDate(r.created_at)}</td>
                <td>${escapeHtml(r.filename || '—')}</td>
                <td><span class="badge">${escapeHtml(r.import_type.replace("_", " "))}</span></td>
                <td>${r.row_count.toLocaleString()}</td>
                <td><span class="badge ${statusCls}">${r.status}</span></td>
                <td><button class="action-icon" title="Delete import" onclick="deleteSrImport(${r.id})"><span class="material-symbols-rounded" style="color:var(--error);">delete</span></button></td>
            </tr>`;
        }).join("");
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="6" style="color:var(--error);">Failed: ${escapeHtml(err.message)}</td></tr>`;
    }
}

async function deleteSrImport(importId) {
    if (!confirm("Delete this import and all its data?")) return;
    try {
        await apiRequest(`/api/semrush/imports/${importId}?store_id=${srStoreId}`, { method: "DELETE" });
        showToast("Import deleted", "success");
        loadSrOverview();
        loadSrImports();
    } catch (err) {
        showToast("Delete failed: " + err.message, "error");
    }
}
