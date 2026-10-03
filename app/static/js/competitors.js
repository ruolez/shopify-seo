let compStoreId = null;

document.addEventListener("DOMContentLoaded", () => {
    loadCompStores();
    initCompTabs();
    initCompEvents();
});

async function loadCompStores() {
    const stores = await apiRequest("/api/stores");
    const select = document.getElementById("comp-store-select");
    stores.forEach(s => { const o = document.createElement("option"); o.value = s.id; o.textContent = s.name; select.appendChild(o); });
    if (stores.length === 1) { select.value = stores[0].id; onCompStore(stores[0].id); }
}

document.getElementById("comp-store-select").addEventListener("change", e => { if (e.target.value) onCompStore(parseInt(e.target.value)); });

function onCompStore(id) {
    compStoreId = id;
    loadCompetitors();
    loadCompOverview();
    loadCompActiveTab();
}

function initCompTabs() {
    document.querySelectorAll("[data-comp-tab]").forEach(tab => {
        tab.addEventListener("click", () => {
            document.querySelectorAll("[data-comp-tab]").forEach(t => t.classList.remove("active"));
            document.querySelectorAll(".comp-tab-panel").forEach(p => p.style.display = "none");
            tab.classList.add("active");
            document.getElementById("comp-tab-" + tab.dataset.compTab).style.display = "";
            loadCompActiveTab();
        });
    });
}

function loadCompActiveTab() {
    if (!compStoreId) return;
    const active = document.querySelector("[data-comp-tab].active")?.dataset?.compTab;
    if (active === "rankings") loadCompRankings();
    else if (active === "alerts") loadCompAlerts();
    else if (active === "history") loadCompHistory();
}

function initCompEvents() {
    document.getElementById("comp-discover-btn").addEventListener("click", discoverCompetitors);

    document.getElementById("comp-add-btn").addEventListener("click", async () => {
        const domain = document.getElementById("comp-domain").value.trim();
        const name = document.getElementById("comp-name").value.trim();
        if (!domain || !compStoreId) { showToast("Enter a domain", "error"); return; }
        try {
            await apiRequest(`/api/competitors?store_id=${compStoreId}`, { method: "POST", body: { domain, name } });
            document.getElementById("comp-domain").value = "";
            document.getElementById("comp-name").value = "";
            showToast("Competitor added", "success");
            loadCompetitors(); loadCompOverview();
        } catch (e) { showToast(e.message, "error"); }
    });

    document.getElementById("comp-check-btn").addEventListener("click", async () => {
        if (!compStoreId) return;
        const btn = document.getElementById("comp-check-btn");
        const kwLimit = document.getElementById("comp-keyword-limit").value;
        btn.disabled = true; btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Checking...';
        try {
            const r = await apiRequest(`/api/competitors/check?store_id=${compStoreId}&keyword_limit=${kwLimit}`, { method: "POST" });
            showToast(`Check complete: ${r.keywords_checked} keywords, ${r.alerts} alerts`, r.alerts ? "warning" : "success");
            loadCompOverview(); loadCompActiveTab();
        } catch (e) { showToast("Check failed: " + e.message, "error"); }
        finally { btn.disabled = false; btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:14px;vertical-align:middle;">sync</span> Run Check Now'; }
    });

    document.getElementById("comp-alert-severity").addEventListener("change", loadCompAlerts);
    document.getElementById("comp-mark-all-read").addEventListener("click", markAllAlertsRead);

    let searchT;
    document.getElementById("comp-rank-search").addEventListener("input", () => { clearTimeout(searchT); searchT = setTimeout(loadCompRankings, 300); });
}

async function loadCompetitors() {
    if (!compStoreId) return;
    const list = document.getElementById("comp-list");
    try {
        const comps = await apiRequest(`/api/competitors?store_id=${compStoreId}`);
        if (!comps.length) { list.innerHTML = '<p style="color:var(--on-surface-secondary);font-size:13px;">No competitors tracked. Add one above.</p>'; return; }
        list.innerHTML = '<div style="display:flex;gap:8px;flex-wrap:wrap;">' + comps.map(c => `
            <div style="display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--surface-dim);border-radius:var(--radius-sm);border:1px solid var(--outline);">
                <strong style="font-size:13px;">${escapeHtml(c.name || c.domain)}</strong>
                <span style="font-size:12px;color:var(--on-surface-secondary);">${escapeHtml(c.domain)}</span>
                <button class="action-icon" title="Remove" onclick="removeComp(${c.id})"><span class="material-symbols-rounded" style="font-size:14px;color:var(--error);">close</span></button>
            </div>
        `).join("") + '</div>';
    } catch (e) { list.innerHTML = `<p style="color:var(--error);">${escapeHtml(e.message)}</p>`; }
}

async function removeComp(id) {
    if (!confirm("Remove this competitor?")) return;
    await apiRequest(`/api/competitors/${id}?store_id=${compStoreId}`, { method: "DELETE" });
    loadCompetitors(); loadCompOverview();
}

async function loadCompOverview() {
    if (!compStoreId) return;
    try {
        const d = await apiRequest(`/api/competitors/overview?store_id=${compStoreId}`);
        document.getElementById("comp-overview").style.display = "";
        document.getElementById("comp-tabs").style.display = "";
        document.getElementById("comp-count").textContent = d.competitors;
        document.getElementById("comp-kw-count").textContent = d.keywords_tracked;
        document.getElementById("comp-alerts-count").textContent = d.unread_alerts;
        document.getElementById("comp-last-check").textContent = d.last_check ? formatRelativeTime(d.last_check) : "Never";
    } catch (e) {}
}

let _cachedRankings = [];
let _rankSort = { col: 'keyword', dir: 'asc' };
let _rankDomains = [];
let _showUnchallenged = false;
let _showGapsOnly = false;
let _rankStats = { challenged: 0, unchallenged: 0, gaps: 0, weak: 0 };

async function loadCompRankings() {
    if (!compStoreId) return;
    const container = document.getElementById("comp-rankings-list");
    container.innerHTML = '<div class="spinner" style="margin:20px auto;"></div>';

    const search = document.getElementById("comp-rank-search").value.trim();
    let url = `/api/competitors/rankings?store_id=${compStoreId}&limit=500&include_unchallenged=${_showUnchallenged}`;
    if (search) url += `&keyword=${encodeURIComponent(search)}`;

    try {
        const resp = await apiRequest(url);
        const data = resp.items || resp;
        _rankStats = { challenged: resp.challenged || 0, unchallenged: resp.unchallenged || 0, gaps: resp.gaps || 0, weak: resp.weak || 0 };
        if (!data.length) { container.innerHTML = ""; document.getElementById("comp-rankings-empty").style.display = ""; return; }
        document.getElementById("comp-rankings-empty").style.display = "none";
        _cachedRankings = data;

        // Build domain list once
        const allDomains = new Map();
        data.forEach(g => g.competitors.forEach(c => {
            if (!allDomains.has(c.domain)) allDomains.set(c.domain, c.name || c.domain);
        }));
        _rankDomains = [...allDomains.entries()];

        _rankSort = { col: 'keyword', dir: 'asc' };
        renderCompRankings();
    } catch (e) { container.innerHTML = `<p style="color:var(--error);">Failed: ${escapeHtml(e.message)}</p>`; }
}

function toggleUnchallenged() {
    _showUnchallenged = !_showUnchallenged;
    if (_showUnchallenged) _showGapsOnly = false;
    loadCompRankings();
}

function toggleGapsOnly() {
    _showGapsOnly = !_showGapsOnly;
    if (_showGapsOnly) _showUnchallenged = false;
    loadCompRankings();
}

function toggleRankSort(col) {
    if (_rankSort.col === col) {
        _rankSort.dir = _rankSort.dir === 'asc' ? 'desc' : 'asc';
    } else {
        _rankSort.col = col;
        _rankSort.dir = col === 'keyword' ? 'asc' : col === '__volume__' ? 'desc' : 'asc';
    }
    renderCompRankings();
}

function _getPosition(group, domain) {
    if (domain === '__you__') return group.our_position || 999;
    if (domain === '__volume__') return -(group.search_volume || 0);
    const c = group.competitors.find(c => c.domain === domain);
    return c ? (c.position || 999) : 999;
}

function renderCompRankings() {
    const container = document.getElementById("comp-rankings-list");
    const countEl = document.getElementById("comp-rank-count");
    let data = [..._cachedRankings];
    const domains = _rankDomains;

    // Sort
    const { col, dir } = _rankSort;
    const mult = dir === 'asc' ? 1 : -1;
    if (col === 'keyword') {
        data.sort((a, b) => mult * a.keyword.localeCompare(b.keyword));
    } else {
        data.sort((a, b) => mult * (_getPosition(a, col) - _getPosition(b, col)));
    }

    if (countEl) {
        let countHtml = `${_rankStats.challenged} contested`;
        if (_rankStats.gaps > 0) {
            countHtml += ` · <span style="color:var(--error);">${_rankStats.gaps} gaps</span>`;
        }
        if (_rankStats.weak > 0) {
            countHtml += ` · <span style="color:var(--warning);">${_rankStats.weak} weak</span>`;
        }
        if (_rankStats.unchallenged > 0) {
            countHtml += ` · ${_rankStats.unchallenged} unchallenged`;
        }
        countHtml += ` · <label style="cursor:pointer;${_showGapsOnly ? 'color:var(--error);font-weight:600;' : ''}"><input type="checkbox" ${_showGapsOnly ? 'checked' : ''} onchange="toggleGapsOnly()" style="vertical-align:middle;margin-right:3px;">Gaps only</label>`;
        countHtml += ` · <label style="cursor:pointer;"><input type="checkbox" ${_showUnchallenged ? 'checked' : ''} onchange="toggleUnchallenged()" style="vertical-align:middle;margin-right:3px;">Unchallenged</label>`;
        countEl.innerHTML = countHtml;
    }

    // Apply gaps filter client-side (gaps = no ranking or >100)
    if (_showGapsOnly) {
        data = data.filter(g => g.competitors.length > 0 && (!g.our_position || g.our_position > 100));
    }

    const arrow = (c) => _rankSort.col === c ? (_rankSort.dir === 'asc' ? ' \u25B2' : ' \u25BC') : '';

    let html = '<div class="table-container"><table class="data-table comp-rank-table"><thead><tr>';
    html += `<th class="sortable-th" style="min-width:150px;position:sticky;left:0;z-index:2;background:var(--surface);" onclick="toggleRankSort('keyword')">Keyword${arrow('keyword')}</th>`;
    html += `<th class="sortable-th" style="text-align:center;min-width:60px;" onclick="toggleRankSort('__volume__')"><div style="font-size:12px;font-weight:700;">Vol${arrow('__volume__')}</div></th>`;
    html += `<th class="sortable-th" style="text-align:center;min-width:60px;" onclick="toggleRankSort('__you__')"><div style="font-size:12px;font-weight:700;color:var(--primary);">You${arrow('__you__')}</div><div style="font-size:10px;color:var(--on-surface-secondary);font-weight:400;">GSC</div></th>`;
    domains.forEach(([domain, name]) => {
        const short = name || domain.replace(/^www\./, '').split('.')[0];
        html += `<th class="sortable-th" style="text-align:center;min-width:120px;" onclick="toggleRankSort('${escapeAttr(domain)}')"><div style="font-size:12px;font-weight:700;">${escapeHtml(short)}${arrow(domain)}</div><div style="font-size:10px;color:var(--on-surface-secondary);font-weight:400;">${escapeHtml(domain)}</div></th>`;
    });
    html += '</tr></thead><tbody>';

    data.forEach(group => {
        const compMap = {};
        group.competitors.forEach(c => { compMap[c.domain] = c; });

        const bestCompPos = Math.min(...group.competitors.map(c => c.position || 999));
        const ourPos = group.our_position;

        html += `<tr${group.unchallenged ? ' style="opacity:0.55;"' : ''}>`;

        const vol = group.search_volume;
        html += `<td style="position:sticky;left:0;z-index:1;background:var(--surface);"><strong style="font-size:13px;">${escapeHtml(group.keyword)}</strong>${!ourPos || ourPos > 20 ? `<div>${writeArticleLink(group.keyword, "You are not ranking — write an article")}</div>` : ""}</td>`;
        html += `<td style="text-align:center;font-size:12px;color:var(--on-surface-secondary);">${vol ? vol.toLocaleString() : '—'}</td>`;

        // Our position column (from GSC)
        if (ourPos) {
            let ourColor, ourBg;
            if (ourPos <= 3) { ourColor = 'var(--success)'; ourBg = 'rgba(30,142,62,0.12)'; }
            else if (ourPos <= 10) { ourColor = 'var(--warning)'; ourBg = 'rgba(249,171,0,0.10)'; }
            else if (ourPos <= 20) { ourColor = 'var(--on-surface)'; ourBg = 'transparent'; }
            else { ourColor = 'var(--on-surface-secondary)'; ourBg = 'transparent'; }
            const losing = ourPos > bestCompPos;
            html += `<td style="text-align:center;background:${ourBg};${losing ? 'border-left:3px solid var(--error);' : ''}">
                <span style="font-size:18px;font-weight:800;color:${ourColor};">${Math.round(ourPos)}</span>
                ${losing ? `<div style="font-size:10px;color:var(--error);">behind</div>` : ourPos <= bestCompPos ? `<div style="font-size:10px;color:var(--success);">winning</div>` : ''}
            </td>`;
        } else {
            html += '<td style="text-align:center;color:var(--on-surface-secondary);font-size:12px;">—</td>';
        }

        domains.forEach(([domain]) => {
            const c = compMap[domain];
            if (!c) {
                html += '<td style="text-align:center;color:var(--on-surface-secondary);font-size:12px;">—</td>';
                return;
            }

            const pos = c.position;
            let posColor, posBg;
            if (pos <= 3) { posColor = 'var(--success)'; posBg = 'rgba(30,142,62,0.12)'; }
            else if (pos <= 10) { posColor = 'var(--warning)'; posBg = 'rgba(249,171,0,0.10)'; }
            else if (pos <= 20) { posColor = 'var(--on-surface)'; posBg = 'transparent'; }
            else { posColor = 'var(--on-surface-secondary)'; posBg = 'transparent'; }

            let changeHtml = '';
            if (c.change != null && c.change !== 0) {
                if (c.change > 0) {
                    changeHtml = `<div style="font-size:11px;color:var(--error);font-weight:600;">\u25B2${c.change} gained</div>`;
                } else {
                    changeHtml = `<div style="font-size:11px;color:var(--success);">\u25BC${Math.abs(c.change)} dropped</div>`;
                }
            } else if (c.change === 0) {
                changeHtml = `<div style="font-size:11px;color:var(--on-surface-secondary);">no change</div>`;
            }

            const srcBadge = c.source === 'semrush' ? '<span title="Position from Semrush (Google data)" style="font-size:8px;font-weight:700;color:var(--primary);vertical-align:super;margin-left:1px;">S</span>' : '';
            html += `<td style="text-align:center;background:${posBg};">
                <span style="font-size:18px;font-weight:800;color:${posColor};">${pos}</span>${srcBadge}
                ${changeHtml}
            </td>`;
        });

        html += '</tr>';
    });

    html += '</tbody></table></div>';
    container.innerHTML = html;
}

async function loadCompAlerts() {
    if (!compStoreId) return;
    const container = document.getElementById("comp-alerts-list");
    container.innerHTML = '<div class="spinner" style="margin:20px auto;"></div>';

    const severity = document.getElementById("comp-alert-severity").value;
    let url = `/api/competitors/alerts?store_id=${compStoreId}`;
    if (severity) url += `&severity=${severity}`;

    try {
        const data = await apiRequest(url);
        if (!data.length) { container.innerHTML = ""; document.getElementById("comp-alerts-empty").style.display = ""; return; }
        document.getElementById("comp-alerts-empty").style.display = "none";

        const grouped = {};
        data.forEach(a => {
            const key = a.keyword || "unknown";
            if (!grouped[key]) grouped[key] = [];
            grouped[key].push(a);
        });

        const sevIcon = { high: "error", medium: "warning", low: "info" };
        const sevColor = { high: "var(--error)", medium: "var(--warning)", low: "var(--primary)" };
        const typeLabels = {
            competitor_surge: "Competitor ranking",
            competitor_outrank: "Competitor outranking you",
            position_lost: "You lost ranking",
            new_competitor: "New competitor appeared",
        };

        let html = '';
        const sortedKeys = Object.keys(grouped).sort((a, b) => {
            const aMax = Math.max(...grouped[a].map(x => x.severity === 'high' ? 3 : x.severity === 'medium' ? 2 : 1));
            const bMax = Math.max(...grouped[b].map(x => x.severity === 'high' ? 3 : x.severity === 'medium' ? 2 : 1));
            return bMax - aMax;
        });

        for (const keyword of sortedKeys) {
            const alerts = grouped[keyword];
            const topSeverity = alerts.some(a => a.severity === 'high') ? 'high' : alerts.some(a => a.severity === 'medium') ? 'medium' : 'low';
            const allRead = alerts.every(a => a.is_read);
            const borderColor = sevColor[topSeverity];

            html += `<div class="comp-alert-group" style="border:1px solid ${allRead ? 'var(--outline)' : borderColor};border-radius:var(--radius-sm);margin-bottom:12px;${allRead ? 'opacity:0.55;' : ''}overflow:hidden;">`;
            html += `<div style="display:flex;align-items:center;gap:10px;padding:12px 16px;background:var(--surface-dim);">
                <span class="material-symbols-rounded" style="font-size:20px;color:${borderColor};">${sevIcon[topSeverity]}</span>
                <div style="flex:1;">
                    <div style="font-weight:700;font-size:14px;">${escapeHtml(keyword)}</div>
                    <div style="font-size:11px;color:var(--on-surface-secondary);">${alerts.length} competitor${alerts.length > 1 ? 's' : ''} ranking for this keyword</div>
                </div>
                <span class="badge badge-${topSeverity === 'high' ? 'error' : topSeverity === 'medium' ? 'warning' : 'info'}" style="font-size:11px;">${topSeverity}</span>
            </div>`;

            html += '<div style="padding:8px 16px 12px;">';
            for (const a of alerts) {
                const posColor = a.competitor_position <= 3 ? 'var(--success)' : a.competitor_position <= 10 ? 'var(--warning)' : 'var(--error)';
                const typeLabel = typeLabels[a.alert_type] || a.alert_type.replace(/_/g, " ");
                html += `<div style="display:flex;align-items:center;gap:12px;padding:8px 0;${a !== alerts[alerts.length-1] ? 'border-bottom:1px solid var(--outline);' : ''}">
                    <div style="min-width:36px;height:36px;border-radius:50%;background:var(--surface-dim);display:flex;align-items:center;justify-content:center;">
                        <span style="font-weight:800;font-size:14px;color:${posColor};">#${a.competitor_position || '?'}</span>
                    </div>
                    <div style="flex:1;min-width:0;">
                        <div style="font-weight:600;font-size:13px;">${escapeHtml(a.competitor_domain || 'Unknown')}</div>
                        <div style="font-size:11px;color:var(--on-surface-secondary);">${typeLabel}${a.our_position ? ` — you rank #${a.our_position}` : ''} &middot; ${formatRelativeTime(a.created_at)}</div>
                    </div>
                    ${!a.is_read ? `<button class="action-icon" title="Mark as read" onclick="markAlertRead(${a.id}, this)"><span class="material-symbols-rounded" style="font-size:18px;">check_circle</span></button>` : '<span class="material-symbols-rounded" style="font-size:16px;color:var(--on-surface-secondary);opacity:0.4;">done</span>'}
                </div>`;
            }
            html += '</div></div>';
        }

        container.innerHTML = html;
    } catch (e) { container.innerHTML = `<p style="color:var(--error);padding:16px;">Failed to load alerts: ${escapeHtml(e.message)}</p>`; }
}

async function markAlertRead(alertId, btn) {
    try {
        await apiRequest(`/api/competitors/alerts/${alertId}/read?store_id=${compStoreId}`, { method: "PUT" });
        if (btn) {
            const row = btn.closest('[style*="display:flex"]');
            if (row) row.style.opacity = '0.5';
            btn.outerHTML = '<span class="material-symbols-rounded" style="font-size:16px;color:var(--success);">done</span>';
        }
        loadCompOverview();
    } catch (e) { showToast("Failed to mark as read", "error"); }
}

async function markAllAlertsRead() {
    if (!compStoreId) return;
    try {
        const data = await apiRequest(`/api/competitors/alerts?store_id=${compStoreId}&is_read=false`);
        for (const a of data) {
            await apiRequest(`/api/competitors/alerts/${a.id}/read?store_id=${compStoreId}`, { method: "PUT" });
        }
        showToast(`Marked ${data.length} alerts as read`, "success");
        loadCompAlerts();
        loadCompOverview();
    } catch (e) { showToast("Failed: " + e.message, "error"); }
}

async function loadCompHistory() {
    if (!compStoreId) return;
    const tbody = document.getElementById("comp-history-tbody");
    try {
        const data = await apiRequest(`/api/competitors/history?store_id=${compStoreId}`);
        if (!data.length) { tbody.innerHTML = '<tr><td colspan="5">No checks yet</td></tr>'; return; }
        const sc = { completed: "badge-success", failed: "badge-error", running: "badge-warning" };
        tbody.innerHTML = data.map(r => `
            <tr>
                <td>${formatDate(r.started_at)}</td>
                <td>${r.keywords_checked}</td>
                <td>${r.competitors_checked}</td>
                <td>${r.alerts_generated}</td>
                <td><span class="badge ${sc[r.status] || ''}">${r.status}</span></td>
            </tr>
        `).join("");
    } catch (e) { tbody.innerHTML = `<tr><td colspan="5" style="color:var(--error);">Failed</td></tr>`; }
}


async function discoverCompetitors() {
    if (!compStoreId) { showToast("Select a store first", "error"); return; }
    const modal = document.getElementById("discover-modal");
    const content = document.getElementById("discover-modal-content");
    modal.classList.add("active");

    content.innerHTML = `<div style="text-align:center;padding:40px;">
        <div class="spinner" style="margin:0 auto 12px;"></div>
        <p style="font-size:14px;font-weight:600;">Discovering competitors...</p>
        <p style="font-size:12px;color:var(--on-surface-secondary);margin-top:4px;">Searching your top keywords to find who's ranking against you</p>
    </div>`;

    try {
        const result = await apiRequest(`/api/competitors/discover?store_id=${compStoreId}`, {
            method: "POST",
            body: { keyword_limit: 20 },
        });

        if (!result.domains || !result.domains.length) {
            content.innerHTML = '<div class="empty-state"><span class="material-symbols-rounded" style="font-size:40px;">search_off</span><p>No competitors found. Import Semrush keyword data first.</p></div>';
            return;
        }

        let html = `<div style="margin-bottom:16px;font-size:13px;color:var(--on-surface-secondary);">
            Searched <strong>${result.keywords_searched}</strong> keywords, found <strong>${result.total_domains_found}</strong> competing domains. Showing top 20.
        </div>`;

        html += '<div style="display:flex;flex-direction:column;gap:10px;">';
        for (const d of result.domains) {
            const threatColor = d.threat_score >= 150 ? 'var(--error)' : d.threat_score >= 80 ? 'var(--warning)' : 'var(--on-surface-secondary)';
            const threatLabel = d.threat_score >= 150 ? 'High Threat' : d.threat_score >= 80 ? 'Medium' : 'Low';
            const kwPills = d.top_keywords.map(k =>
                `<span style="display:inline-block;padding:2px 8px;background:var(--surface-dim);border-radius:12px;font-size:11px;margin:2px;">${escapeHtml(k.keyword)} <span style="color:var(--on-surface-secondary);">#${k.position}</span></span>`
            ).join("");

            html += `<div style="border:1px solid var(--outline);border-radius:var(--radius-sm);padding:14px 16px;display:flex;align-items:flex-start;gap:14px;" id="disc-${escapeAttr(d.domain)}">
                <div style="flex:1;min-width:0;">
                    <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
                        <strong style="font-size:14px;">${escapeHtml(d.domain)}</strong>
                        ${d.already_tracked ? '<span class="badge badge-success" style="font-size:10px;">Tracked</span>' : ''}
                        <span style="font-size:11px;color:${threatColor};font-weight:600;">${threatLabel}</span>
                    </div>
                    <div style="display:flex;gap:16px;font-size:12px;color:var(--on-surface-secondary);margin-bottom:6px;">
                        <span><strong>${d.keywords_count}</strong> shared keywords</span>
                        <span>Avg pos: <strong>${d.avg_position}</strong></span>
                        <span>Best: <strong>#${d.best_position}</strong></span>
                        <span>Total vol: <strong>${d.total_volume.toLocaleString()}</strong></span>
                    </div>
                    <div style="line-height:1.8;">${kwPills}</div>
                </div>
                ${d.already_tracked
                    ? '<span style="font-size:12px;color:var(--success);white-space:nowrap;">Already tracked</span>'
                    : `<button class="btn btn-primary btn-sm" style="white-space:nowrap;" onclick="addDiscoveredComp('${escapeAttr(d.domain)}')">
                        <span class="material-symbols-rounded" style="font-size:14px;vertical-align:middle;">add</span> Track
                    </button>`
                }
            </div>`;
        }
        html += '</div>';
        content.innerHTML = html;
    } catch (e) {
        content.innerHTML = `<p style="color:var(--error);padding:16px;">Discovery failed: ${escapeHtml(e.message)}</p>`;
    }
}

async function addDiscoveredComp(domain) {
    if (!compStoreId) return;
    try {
        await apiRequest(`/api/competitors?store_id=${compStoreId}`, { method: "POST", body: { domain, name: "" } });
        showToast(`Now tracking ${domain}`, "success");
        const el = document.getElementById(`disc-${domain}`);
        if (el) {
            const btn = el.querySelector('button');
            if (btn) btn.outerHTML = '<span style="font-size:12px;color:var(--success);white-space:nowrap;">Added!</span>';
        }
        loadCompetitors();
        loadCompOverview();
    } catch (e) { showToast(e.message, "error"); }
}
