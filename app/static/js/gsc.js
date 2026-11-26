let currentStoreId = null;
let gscPropertiesCache = [];
let currentDateRange = 28;
let currentDevice = "";
let currentCountry = "";

document.addEventListener("DOMContentLoaded", () => {
    loadStores();
    initGSCTabs();
    initGSCEvents();

    const params = new URLSearchParams(window.location.search);
    if (params.get("connected") === "true") {
        showToast("Google Search Console connected!", "success");
    }
    if (params.get("error")) {
        showToast("GSC connection error: " + params.get("error"), "error");
    }
});

// ─── Store Selector ───

async function loadStores() {
    try {
        const stores = await apiRequest("/api/stores");
        const select = document.getElementById("gsc-store-select");
        stores.forEach(s => {
            const opt = document.createElement("option");
            opt.value = s.id;
            opt.textContent = s.name;
            select.appendChild(opt);
        });
        if (stores.length === 1) {
            select.value = stores[0].id;
            onStoreSelected(stores[0].id);
        }
    } catch (err) {
        showToast("Failed to load stores", "error");
    }
}

document.getElementById("gsc-store-select").addEventListener("change", (e) => {
    const storeId = parseInt(e.target.value);
    if (storeId) onStoreSelected(storeId);
});

async function onStoreSelected(storeId) {
    currentStoreId = storeId;
    currentCountry = "";
    const countrySelect = document.getElementById("gsc-country-filter");
    if (countrySelect) countrySelect.innerHTML = '<option value="">All Countries</option>';
    hideAll();
    try {
        const status = await apiRequest(`/api/gsc/status?store_id=${storeId}`);
        if (!status.connected) {
            document.getElementById("gsc-not-connected").style.display = "";
        } else if (!status.property) {
            document.getElementById("gsc-select-property").style.display = "";
            loadProperties();
        } else {
            showDashboard(status);
        }
    } catch (err) {
        showToast("Failed to load GSC status", "error");
    }
}

function hideAll() {
    document.getElementById("gsc-not-connected").style.display = "none";
    document.getElementById("gsc-select-property").style.display = "none";
    document.getElementById("gsc-dashboard").style.display = "none";
}

// ─── OAuth Connect ───

document.getElementById("gsc-connect-btn").addEventListener("click", async () => {
    if (!currentStoreId) return;
    try {
        const data = await apiRequest(`/api/gsc/auth/start?store_id=${currentStoreId}`);
        if (data.auth_url) {
            window.location.href = data.auth_url;
        }
    } catch (err) {
        showToast("Failed to start OAuth: " + err.message, "error");
    }
});

// ─── Property Selection ───

async function loadProperties() {
    const container = document.getElementById("gsc-properties-list");
    container.innerHTML = '<div class="spinner"></div>';
    try {
        const props = await apiRequest(`/api/gsc/properties?store_id=${currentStoreId}`);
        gscPropertiesCache = props;
        if (!props.length) {
            container.innerHTML = '<p style="color: var(--on-surface-secondary);">No verified properties found. Verify your site in Google Search Console first.</p>';
            return;
        }
        container.innerHTML = props.map((p, i) => `
            <div style="display: flex; align-items: center; justify-content: space-between; padding: 12px; border: 1px solid var(--outline); border-radius: var(--radius-sm); margin-bottom: 8px;">
                <div>
                    <strong style="font-size: 14px;">${escapeHtml(p.site_url)}</strong>
                    <span class="badge" style="margin-left: 8px; font-size: 11px;">${p.permission_level}</span>
                </div>
                <button class="btn btn-primary btn-sm" onclick="selectProperty(${i})">Select</button>
            </div>
        `).join("");
    } catch (err) {
        container.innerHTML = `<p style="color: var(--error);">Failed to load properties: ${escapeHtml(err.message)}</p>`;
    }
}

async function selectProperty(idx) {
    const prop = gscPropertiesCache[idx];
    if (!prop) return;
    const siteUrl = prop.site_url;
    try {
        await apiRequest(`/api/gsc/properties/select?store_id=${currentStoreId}`, {
            method: "POST", body: { site_url: siteUrl }
        });
        showToast("Property selected! Backfilling data (this may take a minute)...", "success");
        onStoreSelected(currentStoreId);
    } catch (err) {
        showToast(err.message, "error");
    }
}

// ─── Dashboard ───

function showDashboard(status) {
    document.getElementById("gsc-dashboard").style.display = "";
    document.getElementById("gsc-property-name").textContent = status.property;

    if (status.data_range && status.data_range.first_date) {
        document.getElementById("gsc-data-range").textContent =
            `Data: ${status.data_range.first_date} to ${status.data_range.last_date} (${status.data_range.total_rows.toLocaleString()} rows)`;
    }

    loadCountries();
    loadDashboardData();
}

async function loadCountries() {
    try {
        const countries = await apiRequest(`/api/gsc/countries?store_id=${currentStoreId}`);
        const select = document.getElementById("gsc-country-filter");
        select.innerHTML = '<option value="">All Countries</option>';
        countries.forEach(c => {
            const opt = document.createElement("option");
            opt.value = c;
            opt.textContent = c.toUpperCase();
            select.appendChild(opt);
        });
    } catch (err) {
        console.error("Failed to load countries:", err);
    }
}

function getDateRange() {
    const days = currentDateRange;
    const today = new Date();
    if (days <= 3) {
        const dateTo = new Date(today);
        const dateFrom = new Date(today);
        dateFrom.setDate(dateFrom.getDate() - days);
        return {
            date_from: dateFrom.toISOString().split("T")[0],
            date_to: dateTo.toISOString().split("T")[0],
        };
    }
    const dateTo = new Date(today);
    dateTo.setDate(dateTo.getDate() - 3);
    const dateFrom = new Date(dateTo);
    dateFrom.setDate(dateFrom.getDate() - days + 1);
    return {
        date_from: dateFrom.toISOString().split("T")[0],
        date_to: dateTo.toISOString().split("T")[0],
    };
}

function qp() {
    const { date_from, date_to } = getDateRange();
    let q = `store_id=${currentStoreId}&date_from=${date_from}&date_to=${date_to}`;
    if (currentDevice) q += `&device=${currentDevice}`;
    if (currentCountry) q += `&country=${currentCountry}`;
    return q;
}

async function loadDashboardData() {
    loadOverview();
    loadSearchSplit();
    loadTrends();
    loadActiveTab();
}

// Monotonic request ids per loader — a slow older response must never
// overwrite the result of a newer load (store/filter changed mid-flight).
const gscReqIds = { overview: 0, trends: 0, pages: 0, queries: 0, split: 0 };

// ─── Overview Cards ───

async function loadOverview() {
    const reqId = ++gscReqIds.overview;
    try {
        const data = await apiRequest(`/api/gsc/overview?${qp()}`);
        if (reqId !== gscReqIds.overview) return;
        document.getElementById("gsc-total-clicks").textContent = Math.round(data.total_clicks).toLocaleString();
        document.getElementById("gsc-total-impressions").textContent = Math.round(data.total_impressions).toLocaleString();
        document.getElementById("gsc-avg-ctr").textContent = (data.avg_ctr * 100).toFixed(1) + "%";
        document.getElementById("gsc-avg-position").textContent = data.avg_position.toFixed(1);

        const changeIds = ["gsc-clicks-change", "gsc-impressions-change", "gsc-ctr-change", "gsc-position-change"];
        if (data.changes) {
            setChange("gsc-clicks-change", data.changes.clicks, data.changes.clicks_pct, false);
            setChange("gsc-impressions-change", data.changes.impressions, data.changes.impressions_pct, false);
            setChange("gsc-ctr-change", data.changes.ctr * 100, null, false, "%");
            setChange("gsc-position-change", data.changes.position, null, true);
        } else {
            changeIds.forEach(id => { const el = document.getElementById(id); if (el) el.textContent = ""; });
        }
    } catch (err) {
        console.error("Failed to load overview:", err);
    }
}

// Google's own brand split (brandedClassification) and Lens / image-upload
// searches (type webMultimodal). Stored for all devices and countries.
async function loadSearchSplit() {
    const box = document.getElementById("gsc-search-split");
    if (!box) return;
    const reqId = ++gscReqIds.split;
    try {
        const { date_from, date_to } = getDateRange();
        const d = await apiRequest(`/api/gsc/search-split?store_id=${currentStoreId}&date_from=${date_from}&date_to=${date_to}`);
        if (reqId !== gscReqIds.split) return;
        const parts = [];
        if (d.brand) {
            const b = d.brand.branded || { clicks: 0, impressions: 0 };
            const n = d.brand.non_branded || { clicks: 0, impressions: 0 };
            const total = b.clicks + n.clicks;
            const pct = total ? Math.round((b.clicks / total) * 100) : 0;
            const other = d.brand.named_as_other || 0;
            const note = other
                ? ` <span title="Searches like &quot;a2z smoke shop&quot; name your store, but Google files them as non-branded. The app's click-through baselines count them as branded.">· Google files another ${other.toLocaleString()} clicks from searches naming your store under other</span>`
                : "";
            parts.push(`<span title="Google's own label for searches that name your store"><strong>Searches for your store's name:</strong> ${b.clicks.toLocaleString()} clicks (${pct}% of clicks) · <strong>Other searches:</strong> ${n.clicks.toLocaleString()} clicks${note}</span>`);
        }
        if (d.multimodal) {
            const m = d.multimodal;
            const rows = m.pages.map((p) => `<tr><td>${escapeHtml(shortenUrl(p.page))}</td><td>${p.clicks}</td><td>${p.impressions.toLocaleString()}</td><td>${p.position ?? "—"}</td></tr>`).join("");
            parts.push(`<details style="display:inline-block;vertical-align:top;"><summary style="cursor:pointer;"><strong>Google Lens and image uploads:</strong> ${m.clicks.toLocaleString()} clicks, ${m.impressions.toLocaleString()} impressions</summary>
                <p style="margin:6px 0;">People who searched with a photo (Lens, Circle to Search, an uploaded image) and got your page. Not included in the totals above. Clear, well-lit product photos with accurate alt text help here.</p>
                <div class="table-container"><table class="data-table"><thead><tr><th>Page</th><th>Clicks</th><th>Impressions</th><th>Position</th></tr></thead><tbody>${rows}</tbody></table></div></details>`);
        }
        box.innerHTML = parts.length
            ? `<div style="font-size:12.5px;color:var(--on-surface-secondary);display:flex;flex-direction:column;gap:4px;">${parts.map((p) => `<div>${p}</div>`).join("")}<div style="font-size:11px;">All devices and countries; the filters above don't apply to this line.</div></div>`
            : "";
    } catch (err) {
        box.innerHTML = "";
        console.error("Failed to load search split:", err);
    }
}

function setChange(elId, value, pct, invertColor, suffix = "") {
    const el = document.getElementById(elId);
    if (!el) return;
    const isPositive = invertColor ? value < 0 : value > 0;
    const isNegative = invertColor ? value > 0 : value < 0;
    const arrow = value > 0 ? "\u2191" : value < 0 ? "\u2193" : "";
    const color = isPositive ? "var(--success)" : isNegative ? "var(--error)" : "var(--on-surface-secondary)";
    const absVal = Math.abs(value);
    let text = `${arrow} ${absVal.toFixed(1)}${suffix}`;
    if (pct !== null && pct !== undefined) {
        text += ` (${Math.abs(pct).toFixed(1)}%)`;
    }
    el.style.color = color;
    el.textContent = text;
    el.style.fontSize = "12px";
    el.style.marginTop = "4px";
}

// ─── Trend Chart (SVG) ───

async function loadTrends() {
    const reqId = ++gscReqIds.trends;
    const container = document.getElementById("gsc-trend-chart");
    container.style.opacity = "0.4";
    try {
        const data = await apiRequest(`/api/gsc/trends?${qp()}`);
        if (reqId !== gscReqIds.trends) return;
        container.style.opacity = "1";
        renderTrendChart(data);
    } catch (err) {
        if (reqId !== gscReqIds.trends) return;
        container.style.opacity = "1";
        console.error("Failed to load trends:", err);
    }
}

function renderTrendChart(data) {
    const container = document.getElementById("gsc-trend-chart");
    if (!data.length) {
        container.innerHTML = '<p style="text-align: center; color: var(--on-surface-secondary); padding: 40px 0;">No trend data available.</p>';
        return;
    }

    const showClicks = document.getElementById("chart-clicks").checked;
    const showImpressions = document.getElementById("chart-impressions").checked;

    const width = container.clientWidth;
    const height = 260;
    const pad = { top: 20, right: 60, bottom: 36, left: 60 };
    const chartW = width - pad.left - pad.right;
    const chartH = height - pad.top - pad.bottom;

    const dates = data.map(d => d.date);
    const clicks = data.map(d => d.clicks);
    const impressions = data.map(d => d.impressions);

    let datasets = [];
    if (showClicks) datasets.push({ values: clicks, color: "var(--primary)", label: "Clicks", side: "left" });
    if (showImpressions) datasets.push({ values: impressions, color: "var(--url)", label: "Impressions", side: "right" });

    if (!datasets.length) {
        container.innerHTML = '<p style="text-align: center; color: var(--on-surface-secondary); padding: 40px 0;">Select at least one metric.</p>';
        return;
    }

    let svg = `<svg width="${width}" height="${height}" style="display: block;">`;

    // Grid lines
    for (let i = 0; i <= 4; i++) {
        const y = pad.top + (chartH / 4) * i;
        svg += `<line x1="${pad.left}" y1="${y}" x2="${width - pad.right}" y2="${y}" stroke="var(--outline)" stroke-width="1" stroke-dasharray="4,4"/>`;
    }

    // X-axis labels
    const labelStep = Math.max(1, Math.floor(dates.length / 7));
    for (let i = 0; i < dates.length; i += labelStep) {
        const x = pad.left + (i / (dates.length - 1 || 1)) * chartW;
        const label = dates[i].substring(5);
        svg += `<text x="${x}" y="${height - 8}" text-anchor="middle" fill="var(--on-surface-secondary)" font-size="11">${label}</text>`;
    }

    datasets.forEach((ds, di) => {
        const maxVal = Math.max(...ds.values, 1);
        const points = ds.values.map((v, i) => {
            const x = pad.left + (i / (ds.values.length - 1 || 1)) * chartW;
            const y = pad.top + chartH - (v / maxVal) * chartH;
            return `${x},${y}`;
        });

        // Area fill
        const firstX = pad.left;
        const lastX = pad.left + chartW;
        const baseY = pad.top + chartH;
        svg += `<polygon points="${firstX},${baseY} ${points.join(" ")} ${lastX},${baseY}" fill="${ds.color}" opacity="0.08"/>`;
        svg += `<polyline points="${points.join(" ")}" fill="none" stroke="${ds.color}" stroke-width="2" stroke-linejoin="round"/>`;

        // Y-axis labels
        const axisX = ds.side === "left" ? pad.left - 8 : width - pad.right + 8;
        const anchor = ds.side === "left" ? "end" : "start";
        for (let i = 0; i <= 4; i++) {
            const y = pad.top + (chartH / 4) * (4 - i);
            const val = Math.round((maxVal / 4) * i);
            svg += `<text x="${axisX}" y="${y + 4}" text-anchor="${anchor}" fill="${ds.color}" font-size="10">${val.toLocaleString()}</text>`;
        }
    });

    svg += "</svg>";
    container.innerHTML = svg;
}

document.getElementById("chart-clicks").addEventListener("change", loadTrends);
document.getElementById("chart-impressions").addEventListener("change", loadTrends);

// ─── Tabs ───

function initGSCTabs() {
    document.querySelectorAll("[data-gsc-tab]").forEach(tab => {
        tab.addEventListener("click", () => {
            document.querySelectorAll("[data-gsc-tab]").forEach(t => t.classList.remove("active"));
            document.querySelectorAll(".gsc-tab-panel").forEach(p => p.style.display = "none");
            tab.classList.add("active");
            const panel = document.getElementById("gsc-tab-" + tab.dataset.gscTab);
            if (panel) panel.style.display = "";
            const url = new URL(window.location);
            url.searchParams.set("tab", tab.dataset.gscTab);
            history.replaceState(null, "", url);
            loadActiveTab();
        });
    });
    // /gsc?tab=<name> deep link (dashboard queue + keyword-strategy card land here)
    const wanted = new URLSearchParams(window.location.search).get("tab");
    if (wanted) {
        const btn = document.querySelector(`[data-gsc-tab="${CSS.escape(wanted)}"]`);
        if (btn && !btn.classList.contains("active")) btn.click();
    }
}

function loadActiveTab() {
    const active = document.querySelector("[data-gsc-tab].active");
    if (!active) return;
    const tab = active.dataset.gscTab;
    if (tab === "pages") loadTopPages();
    else if (tab === "queries") loadTopQueries();
    else if (tab === "opportunities") loadOpportunities();
    else if (tab === "cannibalization") loadCannibalization();
    else if (tab === "indexing") loadIndexing();
    else if (tab === "pagetwo") loadPageTwoPlan();
    else if (tab === "scores") loadScoresTab();
}

// ─── Nearly visible: pages ranking 4–20, by what moving up could add ───

async function loadPageTwoPlan() {
    const box = document.getElementById("gsc-pagetwo-container");
    if (!currentStoreId) { box.innerHTML = "<p>Select a store.</p>"; return; }
    box.innerHTML = '<div class="loading-spinner"><div class="spinner"></div></div>';
    try {
        const plan = await apiRequest(`/api/gsc/page-two-plan?store_id=${currentStoreId}`);
        const intro = `<p style="font-size:13px;color:var(--on-surface-secondary);margin:0 0 12px;">Searches where one of your pages already ranks between 4 and 20 — close enough to move. Pages on page one aim for the top three; pages on page two aim for page one. Collections come first because they carry the broad terms. Searches for your store's own name are left out.</p>`;
        if (!plan.pages.length) {
            box.innerHTML = intro + "<p>No pages are ranking between 4 and 20 with enough impressions yet.</p>";
            return;
        }
        const typeLabel = { collection: "Collection", product: "Product", article: "Article" };
        const open = (p) => p.item_type === "article" ? `/blog?article=${p.id}` : `/seo?optimize=${p.item_type}&id=${p.id}&store_id=${currentStoreId}`;
        box.innerHTML = intro + `<p style="font-size:14px;"><strong>${plan.pages.length} pages</strong> could add about <strong style="color:var(--success);">+${plan.total_gain} clicks a month</strong> between them.</p>`
            + plan.pages.map((p) => `<details class="card" style="margin-top:10px;">
                <summary style="cursor:pointer;display:flex;gap:10px;align-items:center;">
                    <span class="badge">${typeLabel[p.item_type] || p.item_type}</span>
                    <span style="flex:1;font-weight:500;">${escapeHtml(p.title)}</span>
                    <span style="color:var(--success);font-weight:600;white-space:nowrap;">+${p.est_clicks_gain}/mo</span>
                    <a class="btn btn-secondary btn-sm" href="${open(p)}" onclick="event.stopPropagation()">Open</a>
                </summary>
                <div class="table-container" style="margin-top:10px;"><table class="data-table"><thead><tr><th>Search</th><th>Impressions</th><th>Clicks</th><th>Position now</th><th>Aim for</th><th>Could add</th></tr></thead><tbody>
                ${p.queries.map((q) => `<tr><td>${escapeHtml(q.query)}</td><td>${q.impressions.toLocaleString()}</td><td>${q.clicks}</td><td>${q.position}</td><td>${q.target === 3 ? "top 3" : "page one"}</td><td style="color:var(--success);">+${q.est_clicks_gain}</td></tr>`).join("")}
                </tbody></table></div></details>`).join("");
    } catch (e) {
        box.innerHTML = `<p style="color:var(--error);">${escapeHtml(e.message)}</p>`;
    }
}

// ─── Indexing: which pages Google has actually indexed ───

const INDEX_ITEM_LINK = {
    product: (id) => `/seo?optimize=product&id=${id}&store_id=${currentStoreId || ""}`,
    collection: (id) => `/seo?optimize=collection&id=${id}&store_id=${currentStoreId || ""}`,
    article: (id) => `/blog?article=${id}`,
};

async function loadIndexing() {
    const box = document.getElementById("gsc-indexing-container");
    if (!currentStoreId) { box.innerHTML = "<p>Select a store.</p>"; return; }
    box.innerHTML = '<div class="loading-spinner"><div class="spinner"></div></div>';
    try {
        const data = await apiRequest(`/api/gsc/index-coverage?store_id=${currentStoreId}`);
        const s = data.summary;
        const sm = data.sitemap || {};
        let sitemapLine = "";
        if (sm.error) {
            sitemapLine = `<p style="font-size:12.5px;color:var(--warning);margin:0 0 12px;">${escapeHtml(sm.error)}</p>`;
        } else if ((sm.problems || []).length) {
            sitemapLine = `<p style="font-size:12.5px;color:var(--error);margin:0 0 12px;"><strong>Sitemap:</strong> ${escapeHtml(sm.problems.join(" "))}</p>`;
        } else if ((sm.sitemaps || []).length) {
            const s0 = sm.sitemaps[0];
            sitemapLine = `<p style="font-size:12.5px;color:var(--on-surface-secondary);margin:0 0 12px;"><strong>Sitemap:</strong> ${s0.submitted.toLocaleString()} pages listed; Google last read it ${s0.last_downloaded ? formatRelativeTime(s0.last_downloaded) : "never"}, no errors.</p>`;
        }
        const intro = `<p style="font-size:13px;color:var(--on-surface-secondary);margin:0 0 12px;">A page Google has not indexed cannot appear in search, however good its text is. The app checks a few hundred pages a night; every page is re-checked about every two weeks.</p>` + sitemapLine;
        const checkBtn = `<button class="btn btn-secondary btn-sm" onclick="runIndexingCheck(this)"><span class="material-symbols-rounded">radar</span> Check more pages now</button>`;
        if (!s.checked) {
            box.innerHTML = intro + `<p>No pages checked yet.</p>` + checkBtn;
            return;
        }
        const typeLine = Object.entries(s.by_type).map(([t, v]) => `${t}s ${v.indexed}/${v.checked}`).join(" · ");
        const groups = {};
        data.not_indexed.forEach((r) => (groups[r.coverage_state] = groups[r.coverage_state] || []).push(r));
        const sections = Object.entries(groups).sort((a, b) => b[1].length - a[1].length).map(([state, rows]) => `
            <details class="card" style="margin-top:12px;" ${rows.length <= 15 ? "open" : ""}>
                <summary style="cursor:pointer;font-weight:600;">${escapeHtml(state)} — ${rows.length} page${rows.length === 1 ? "" : "s"}</summary>
                <p style="font-size:13px;margin:8px 0 4px;">${escapeHtml(rows[0].why)}</p>
                <p style="font-size:13px;margin:0 0 8px;"><strong>What to do:</strong> ${escapeHtml(rows[0].action)}</p>
                <div class="table-container"><table class="data-table"><thead><tr><th>Page</th><th>Type</th><th>Google last read it</th><th></th></tr></thead><tbody>
                ${rows.map((r) => `<tr>
                    <td>${escapeHtml(r.title || r.url)}<div style="font-size:11px;color:var(--on-surface-secondary);">${escapeHtml(shortenUrl(r.url))}</div></td>
                    <td>${r.item_type}</td>
                    <td>${r.last_crawl_time ? formatDate(r.last_crawl_time) : "never"}</td>
                    <td style="text-align:right;white-space:nowrap;">${INDEX_ITEM_LINK[r.item_type] ? `<a class="btn btn-secondary btn-sm" href="${INDEX_ITEM_LINK[r.item_type](r.item_id)}">Open</a>` : ""}
                        <a class="btn btn-secondary btn-sm" target="_blank" rel="noopener" href="${escapeAttr(r.url)}">View</a></td>
                </tr>`).join("")}</tbody></table></div>
            </details>`).join("");
        box.innerHTML = intro + `<div class="kpi-strip" style="margin-bottom:8px;">
                <div class="stat-card"><div class="stat-value">${s.indexed}</div><div class="stat-label">Indexed</div></div>
                <div class="stat-card"><div class="stat-value" style="color:${s.not_indexed ? "var(--error)" : "var(--success)"};">${s.not_indexed}</div><div class="stat-label">Not indexed</div></div>
                <div class="stat-card"><div class="stat-value">${s.checked}</div><div class="stat-label">Pages checked</div></div>
            </div>
            <p style="font-size:12px;color:var(--on-surface-secondary);">${escapeHtml(typeLine)} · last checked ${s.last_checked ? formatDate(s.last_checked) : "—"} ${checkBtn}</p>`
            + (sections
                ? `<div id="gsc-unindexed-plan"><div class="loading-spinner"><div class="spinner"></div></div></div>
                   <details style="margin-top:20px;"><summary style="cursor:pointer;font-weight:600;">The same pages grouped by Google's stated reason</summary>${sections}</details>`
                : '<p style="color:var(--success);">Every page checked so far is indexed.</p>');
        if (sections) loadUnindexedPlan();
    } catch (e) {
        box.innerHTML = `<p style="color:var(--error);">${escapeHtml(e.message)}</p>`;
    }
}

// Which unindexed pages are worth the effort, why each is likely skipped, and
// what to do — most-searched first. Links come from the suggestions card.
async function loadUnindexedPlan() {
    const box = document.getElementById("gsc-unindexed-plan");
    if (!box) return;
    try {
        const plan = await apiRequest(`/api/gsc/unindexed-plan?store_id=${currentStoreId}`);
        const typeLabel = { product: "Product", collection: "Collection", article: "Article" };
        const row = (p) => {
            const open = INDEX_ITEM_LINK[p.item_type] ? INDEX_ITEM_LINK[p.item_type](p.item_id) : null;
            return `<tr>
                <td><span class="badge">${typeLabel[p.item_type] || p.item_type}</span> ${escapeHtml(p.title || p.url)}
                    ${p.keyword ? `<div style="font-size:11px;color:var(--on-surface-secondary);">searched as “${escapeHtml(p.keyword)}”</div>` : ""}</td>
                <td>${p.volume ? p.volume.toLocaleString() : "—"}</td>
                <td style="${p.incoming_links < 3 ? "color:var(--error);font-weight:600;" : ""}">${p.incoming_links}</td>
                <td style="font-size:12.5px;">${escapeHtml(p.reason)}</td>
                <td style="font-size:12.5px;">${escapeHtml(p.action)}
                    ${p.item_type !== "article" && p.reason.includes("little that is new") && open ? `<div style="margin-top:3px;"><a href="${open}">Open to rewrite the description</a></div>` : ""}</td>
                <td style="text-align:right;white-space:nowrap;">${open ? `<a class="btn btn-secondary btn-sm" href="${open}">Open</a>` : ""}</td>
            </tr>`;
        };
        const table = (rows) => `<div class="table-container"><table class="data-table"><thead><tr>
            <th>Page</th><th title="Monthly searches for the page's keyword">Searches / mo</th><th title="Pages on your store that link to it">Links in</th><th>Likely reason</th><th>What to do</th><th></th>
            </tr></thead><tbody>${rows.map(row).join("")}</tbody></table></div>`;
        const worth = plan.pages.filter((p) => p.worth_indexing);
        const low = plan.pages.filter((p) => !p.worth_indexing);
        const starved = plan.pages.filter((p) => p.incoming_links < 3).length;
        box.innerHTML = `<div id="gsc-link-suggestions" style="margin-top:12px;"><div class="card"><div class="loading-spinner"><div class="spinner"></div></div></div></div>
            <p style="font-size:13px;margin:14px 0 0;"><strong>${starved} of ${plan.pages.length}</strong> unindexed pages have fewer than three articles or descriptions linking to them — the most common reason Google skips a page.</p>
            <details class="card" style="margin-top:12px;">
                <summary style="cursor:pointer;font-weight:600;">Worth getting indexed — ${worth.length} page${worth.length === 1 ? "" : "s"} people search for</summary>
                ${worth.length ? table(worth) : '<p style="font-size:13px;">None.</p>'}
            </details>
            <details class="card" style="margin-top:12px;">
                <summary style="cursor:pointer;font-weight:600;">Low priority — ${low.length} page${low.length === 1 ? "" : "s"} with little search demand</summary>
                ${low.length ? table(low) : '<p style="font-size:13px;">None.</p>'}
            </details>`;
        loadLinkSuggestions();
    } catch (e) {
        box.innerHTML = `<p style="color:var(--error);">${escapeHtml(e.message)}</p>`;
    }
}

// ─── Links to unindexed pages: suggested every night, added on review or automatically ───

const LINK_TYPE_LABEL = { product: "Product", collection: "Collection", article: "Article" };
const LINK_SOURCE_OPEN = {
    product: (id) => `/seo?optimize=product&id=${id}&store_id=${currentStoreId || ""}`,
    collection: (id) => `/seo?optimize=collection&id=${id}&store_id=${currentStoreId || ""}`,
    article: (id) => `/blog?article=${id}`,
};

// Did the links help? Pages that got links, checked again by Google since.
function linkOutcomeLine(o) {
    if (!o || !o.linked) return "";
    const c = o.counts;
    const parts = [`<strong>${c.indexed} of ${o.checked}</strong> linked page${o.checked === 1 ? "" : "s"} checked since ${o.checked === 1 ? "is" : "are"} now indexed`];
    if (c.waiting) parts.push(`${c.waiting} waiting for Google's re-check (${o.recheck_after_days} days after the first link)`);
    const stuck = o.stuck.map((s) => `<li>${LINK_SOURCE_OPEN[s.target_type] ? `<a href="${LINK_SOURCE_OPEN[s.target_type](s.target_id)}">${escapeHtml(s.title || "—")}</a>` : escapeHtml(s.title || "—")}
        <span style="color:var(--on-surface-secondary);">· ${escapeHtml(s.coverage_state || "")} · linked ${formatRelativeTime(s.linked_at)}</span></li>`).join("");
    return `<div style="font-size:12.5px;margin:10px 0 0;padding:8px 10px;border-radius:8px;background:var(--surface-variant, rgba(0,0,0,.04));">
        <div>Did it help? ${parts.join(" · ")}.</div>
        ${stuck ? `<details style="margin-top:4px;"><summary style="cursor:pointer;">${c.stuck} still not indexed ${o.stuck_after_days}+ days after linking — these need more than links (better content, or merging into a stronger page)</summary><ul style="margin:6px 0 0 18px;">${stuck}</ul></details>` : ""}
    </div>`;
}

async function loadLinkSuggestions() {
    const box = document.getElementById("gsc-link-suggestions");
    if (!box) return;
    let data;
    try {
        data = await apiRequest(`/api/link-suggestions?store_id=${currentStoreId}`);
    } catch (e) {
        box.innerHTML = `<div class="card"><p style="color:var(--error);">Link suggestions: ${escapeHtml(e.message)}</p></div>`;
        return;
    }
    const groups = new Map();
    data.pending.forEach((s) => {
        const key = `${s.target_type}:${s.target_id}`;
        if (!groups.has(key)) groups.set(key, { target: s, items: [] });
        groups.get(key).items.push(s);
    });
    const pageLink = (type, id, title) => LINK_SOURCE_OPEN[type]
        ? `<a href="${LINK_SOURCE_OPEN[type](id)}">${escapeHtml(title || "—")}</a>` : escapeHtml(title || "—");
    const rows = [...groups.values()].map(({ target, items }) => `<tr>
        <td style="vertical-align:top;"><span class="badge">${LINK_TYPE_LABEL[target.target_type]}</span> ${pageLink(target.target_type, target.target_id, target.target_title)}
            <div style="font-size:11px;color:var(--on-surface-secondary);">${escapeHtml(target.coverage_state || "")}</div></td>
        <td>${items.map((s) => `<div style="display:flex;gap:8px;align-items:flex-start;padding:3px 0;" data-suggestion="${s.id}">
                <div style="flex:1;font-size:12.5px;">from <span class="badge">${LINK_TYPE_LABEL[s.source_type]}</span> ${pageLink(s.source_type, s.source_id, s.source_title)}
                    <div style="font-size:11.5px;color:var(--on-surface-secondary);">${s.how === "inline" && s.anchor ? `“${escapeHtml(s.anchor)}” becomes the link` : escapeHtml(s.reason)}</div></div>
                <button class="btn btn-secondary btn-sm" style="padding:1px 8px;font-size:11px;" onclick="applyLinkSuggestion(${s.id}, this)">Add</button>
                <button class="btn btn-secondary btn-sm" style="padding:1px 8px;font-size:11px;" title="Don't suggest this link again" onclick="dismissLinkSuggestion(${s.id}, this)">Dismiss</button>
            </div>`).join("")}</td>
    </tr>`).join("");
    const recent = data.recent.map((s) => `<tr>
        <td style="font-size:12.5px;">${pageLink(s.source_type, s.source_id, s.source_title)} → ${pageLink(s.target_type, s.target_id, s.target_title)}</td>
        <td style="font-size:12px;">${s.status === "applied" ? (s.applied_by === "auto" ? "Added automatically" : "Added") : `<span style="color:var(--error);">Failed: ${escapeHtml(s.error || "")}</span>`}
            · ${s.updated_at ? formatRelativeTime(s.updated_at) : ""}</td>
        <td style="font-size:12px;">${s.target_indexed ? '<span style="color:var(--success);">now indexed</span>' : "not indexed yet"}</td>
        <td style="text-align:right;">${s.undone ? '<span style="font-size:12px;">Undone</span>' : s.status === "applied" && s.event_id ? `<button class="btn btn-secondary btn-sm" style="padding:1px 8px;font-size:11px;" onclick="undoLinkSuggestion(${s.event_id}, this)">Undo</button>` : ""}</td>
    </tr>`).join("");
    const st = data.settings;
    const last = data.last_run;
    box.innerHTML = `<div class="card">
        <div style="display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap;">
            <div style="flex:1;min-width:260px;">
                <h3 style="margin:0 0 4px;">Links to unindexed pages</h3>
                <p style="font-size:13px;margin:0;color:var(--on-surface-secondary);">Every night, after the indexing check, each page Google hasn't indexed gets up to two suggested links from pages Google already indexed: ones that name it, or are about the same brand or topic, best-read first. Where the page already names it, those words become the link; otherwise it joins a short "Related" line. No page gets more than five links this way.</p>
            </div>
            <div style="display:flex;flex-direction:column;gap:6px;align-items:flex-end;">
                <label style="font-size:13px;display:flex;gap:6px;align-items:center;cursor:pointer;">
                    <input type="checkbox" ${st.auto ? "checked" : ""} onchange="saveLinkAutoSettings()" id="link-auto-toggle"> Add them automatically every night</label>
                <label style="font-size:12px;color:var(--on-surface-secondary);display:flex;gap:6px;align-items:center;">up to
                    <input type="number" min="1" max="100" value="${st.per_night}" id="link-auto-per-night" class="form-control" style="width:64px;padding:2px 6px;" onchange="saveLinkAutoSettings()"> a night</label>
            </div>
        </div>
        ${linkOutcomeLine(data.outcomes)}
        ${last ? `<p style="font-size:12px;margin:8px 0 0;color:${last.last_status === "ok" ? "var(--on-surface-secondary)" : "var(--error)"};">Last run ${formatRelativeTime(last.last_run_at)}: ${escapeHtml(last.last_status === "ok" ? (last.last_detail || "done") : (last.last_error || "failed"))}</p>` : ""}
        <div style="display:flex;gap:8px;margin:12px 0 6px;align-items:center;flex-wrap:wrap;">
            ${data.pending.length ? `<button class="btn btn-primary btn-sm" onclick="applyAllLinkSuggestions(${data.pending.length}, this)"><span class="material-symbols-rounded">add_link</span> Add all ${data.pending.length} links</button>` : ""}
            <button class="btn btn-secondary btn-sm" onclick="refreshLinkSuggestions(this)"><span class="material-symbols-rounded">refresh</span> Suggest again now</button>
            <span style="font-size:12px;color:var(--on-surface-secondary);">${data.pending.length} link${data.pending.length === 1 ? "" : "s"} suggested for ${groups.size} page${groups.size === 1 ? "" : "s"}. Each one can be undone.</span>
        </div>
        ${rows ? `<details ${groups.size <= 12 ? "open" : ""}><summary style="cursor:pointer;font-weight:600;font-size:13px;">Suggested links</summary>
            <div class="table-container"><table class="data-table"><thead><tr><th style="width:38%;">Page Google hasn't indexed</th><th>Link to it</th></tr></thead><tbody>${rows}</tbody></table></div></details>`
            : '<p style="font-size:13px;">No links to suggest right now.</p>'}
        ${recent ? `<details style="margin-top:8px;"><summary style="cursor:pointer;font-weight:600;font-size:13px;">Added in the last 30 days (${data.recent.length})</summary>
            <div class="table-container"><table class="data-table"><tbody>${recent}</tbody></table></div></details>` : ""}
    </div>`;
}

async function applyLinkSuggestion(id, btn) {
    btn.disabled = true;
    try {
        const r = await apiRequest(`/api/link-suggestions/${id}/apply`, { method: "POST", body: {} });
        const line = btn.closest("[data-suggestion]");
        line.innerHTML = `<span style="font-size:12.5px;color:var(--success);">${r.status === "applied" ? "Link added ✓" : "The page already links there"}</span>`;
    } catch (e) {
        showToast(`Could not add the link: ${e.message}`, "error");
        btn.disabled = false;
    }
}

async function dismissLinkSuggestion(id, btn) {
    btn.disabled = true;
    try {
        await apiRequest(`/api/link-suggestions/${id}/dismiss`, { method: "POST", body: {} });
        btn.closest("[data-suggestion]").remove();
    } catch (e) {
        showToast(e.message, "error");
        btn.disabled = false;
    }
}

async function undoLinkSuggestion(eventId, btn) {
    btn.disabled = true;
    try {
        await apiRequest(`/api/changes/${eventId}/revert`, { method: "POST", body: {} });
        showToast("Link removed.", "success");
        loadLinkSuggestions();
    } catch (e) {
        showToast(`Undo failed: ${e.message}`, "error");
        btn.disabled = false;
    }
}

async function saveLinkAutoSettings() {
    const auto = document.getElementById("link-auto-toggle").checked;
    const perNight = parseInt(document.getElementById("link-auto-per-night").value) || 15;
    try {
        await apiRequest("/api/link-suggestions/settings", { method: "POST", body: { auto, per_night: perNight } });
        showToast(auto ? `The best ${perNight} suggested links will be added each night.` : "Links will wait for you to add them.", "success");
    } catch (e) {
        showToast(`Could not save: ${e.message}`, "error");
    }
}

async function refreshLinkSuggestions(btn) {
    btn.disabled = true;
    btn.textContent = "Looking for links…";
    try {
        const r = await apiRequest("/api/link-suggestions/refresh", { method: "POST", body: { store_id: currentStoreId } });
        showToast(`${r.suggested} links suggested for ${r.targets_covered} of ${r.targets} unindexed pages.`, "success");
    } catch (e) {
        showToast(`Could not suggest links: ${e.message}`, "error");
    }
    loadLinkSuggestions();
}

async function applyAllLinkSuggestions(count, btn) {
    const ok = await UIConfirm.ask({
        title: `Add ${count} links?`,
        message: `Each suggested link goes live on its page. Where the page already names the target, those words become the link; otherwise it joins a short "Related" line. Every link can be undone here or from the page's History.`,
        confirmLabel: "Add links",
    });
    if (!ok) return;
    btn.disabled = true;
    try {
        const { task_id } = await apiRequest("/api/link-suggestions/apply-all", { method: "POST", body: { store_id: currentStoreId } });
        const task = await pollTaskUntilDone(`/api/tasks/${task_id}`, {
            intervalMs: 3000, timeoutMs: 60 * 60 * 1000,
            onProgress: (t) => { const p = t.result && t.result.progress; if (p) btn.textContent = `Adding… ${p.completed}/${p.total}`; },
        });
        const r = task.result || {};
        showToast(`${r.applied || 0} links added${r.already ? `, ${r.already} already there` : ""}${r.failed ? `, ${r.failed} failed (${(r.errors || [])[0] || ""})` : ""}.`, r.failed ? "warning" : "success");
    } catch (e) {
        showToast(`Adding links failed: ${e.message}`, "error");
    }
    loadLinkSuggestions();
}

async function runIndexingCheck(btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Checking… (a few minutes)';
    try {
        const { task_id } = await apiRequest("/api/gsc/index-coverage/check", { method: "POST", body: { store_id: currentStoreId } });
        const task = await pollTaskUntilDone(`/api/tasks/${task_id}`, { intervalMs: 5000, timeoutMs: 20 * 60 * 1000 });
        const r = task.result || {};
        if (r.stopped_early) {
            showToast(`Checked ${r.checked || 0} of ${r.due || 0} pages, then Search Console stopped answering (${r.stopped_early}). The rest is retried tonight.`, "warning");
        } else if (!r.due) {
            showToast("Every page was checked in the last two weeks — nothing due.", "info");
        } else {
            showToast(`Checked ${r.checked || 0} pages — ${r.not_indexed || 0} not indexed.`, "success");
        }
    } catch (e) {
        showToast(`Indexing check: ${e.message}`, "error");
    }
    loadIndexing();
}

// ─── Sortable Table Helper ───

let gscTableData = { pages: [], queries: [], opportunities: [] };
let gscRenderedRows = { pages: [], queries: [] };
let gscTableSort = { pages: { col: "clicks", dir: "desc" }, queries: { col: "clicks", dir: "desc" }, opportunities: { col: "potential_clicks", dir: "desc" } };

function sortGscTable(table, col) {
    const s = gscTableSort[table];
    if (s.col === col) {
        s.dir = s.dir === "desc" ? "asc" : "desc";
    } else {
        s.col = col;
        s.dir = (col === "page" || col === "query") ? "asc" : "desc";
    }
    if (table === "pages") renderPagesTable();
    else if (table === "queries") renderQueriesTable();
    else if (table === "opportunities") renderOpportunitiesTable();
}

function sortedData(table) {
    const data = [...gscTableData[table]];
    const { col, dir } = gscTableSort[table];
    data.sort((a, b) => {
        let va = a[col], vb = b[col];
        if (typeof va === "string") return dir === "asc" ? va.localeCompare(vb) : vb.localeCompare(va);
        return dir === "asc" ? (va || 0) - (vb || 0) : (vb || 0) - (va || 0);
    });
    return data;
}

function sortHeader(table, col, label) {
    const s = gscTableSort[table];
    const arrow = s.col === col ? (s.dir === "asc" ? " \u2191" : " \u2193") : "";
    const active = s.col === col ? "color:var(--primary);" : "";
    return `<th class="sortable-header" style="cursor:pointer;user-select:none;${active}" onclick="sortGscTable('${table}','${col}')">${label}${arrow}</th>`;
}

// ─── Top Pages ───

async function loadTopPages() {
    const reqId = ++gscReqIds.pages;
    const tbody = document.getElementById("gsc-pages-tbody");
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;"><div class="spinner" style="margin:20px auto;"></div></td></tr>';
    try {
        const rows = await apiRequest(`/api/gsc/top-pages?${qp()}&limit=500`);
        if (reqId !== gscReqIds.pages) return;
        gscTableData.pages = rows;
        if (!gscTableData.pages.length) {
            tbody.innerHTML = "";
            document.getElementById("gsc-pages-empty").style.display = "";
            return;
        }
        document.getElementById("gsc-pages-empty").style.display = "none";
        renderPagesTable();
    } catch (err) {
        if (reqId !== gscReqIds.pages) return;
        tbody.innerHTML = `<tr><td colspan="5" style="color: var(--error);">Failed to load: ${escapeHtml(err.message)}</td></tr>`;
    }
}

function renderPagesTable() {
    const thead = document.querySelector("#gsc-tab-pages thead tr");
    thead.innerHTML = sortHeader("pages", "page", "Page")
        + sortHeader("pages", "clicks", "Clicks")
        + sortHeader("pages", "impressions", "Impressions")
        + sortHeader("pages", "ctr", "CTR")
        + sortHeader("pages", "position", "Position");

    gscRenderedRows.pages = sortedData("pages");
    document.getElementById("gsc-pages-tbody").innerHTML = gscRenderedRows.pages.map((r, i) => `
        <tr class="clickable-row" onclick="showPageDetail(${i})">
            <td style="max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(r.page)}">${shortenUrl(r.page)}</td>
            <td><strong>${Math.round(r.clicks).toLocaleString()}</strong></td>
            <td>${Math.round(r.impressions).toLocaleString()}</td>
            <td>${(r.ctr * 100).toFixed(1)}%</td>
            <td>${positionBadge(r.position)}</td>
        </tr>
    `).join("");
}

// ─── Top Queries ───

async function loadTopQueries() {
    const reqId = ++gscReqIds.queries;
    const tbody = document.getElementById("gsc-queries-tbody");
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;"><div class="spinner" style="margin:20px auto;"></div></td></tr>';
    try {
        const rows = await apiRequest(`/api/gsc/top-queries?${qp()}&limit=500`);
        if (reqId !== gscReqIds.queries) return;
        gscTableData.queries = rows;
        if (!gscTableData.queries.length) {
            tbody.innerHTML = "";
            document.getElementById("gsc-queries-empty").style.display = "";
            return;
        }
        document.getElementById("gsc-queries-empty").style.display = "none";
        renderQueriesTable();
    } catch (err) {
        if (reqId !== gscReqIds.queries) return;
        tbody.innerHTML = `<tr><td colspan="5" style="color: var(--error);">Failed to load: ${escapeHtml(err.message)}</td></tr>`;
    }
}

function renderQueriesTable() {
    const thead = document.querySelector("#gsc-tab-queries thead tr");
    thead.innerHTML = sortHeader("queries", "query", "Query")
        + sortHeader("queries", "clicks", "Clicks")
        + sortHeader("queries", "impressions", "Impressions")
        + sortHeader("queries", "ctr", "CTR")
        + sortHeader("queries", "position", "Position");

    gscRenderedRows.queries = sortedData("queries");
    document.getElementById("gsc-queries-tbody").innerHTML = gscRenderedRows.queries.map((r, i) => `
        <tr class="clickable-row" onclick="showQueryDetail(${i})">
            <td><strong>${escapeHtml(r.query)}</strong></td>
            <td><strong>${Math.round(r.clicks).toLocaleString()}</strong></td>
            <td>${Math.round(r.impressions).toLocaleString()}</td>
            <td>${(r.ctr * 100).toFixed(1)}%</td>
            <td>${positionBadge(r.position)}</td>
        </tr>
    `).join("");
}

// ─── Opportunities ───

async function loadOpportunities() {
    const tbody = document.getElementById("gsc-opportunities-tbody");
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;"><div class="spinner" style="margin:20px auto;"></div></td></tr>';
    try {
        gscTableData.opportunities = await apiRequest(`/api/gsc/opportunities?${qp()}`);
        if (!gscTableData.opportunities.length) {
            tbody.innerHTML = "";
            document.getElementById("gsc-opportunities-empty").style.display = "";
            return;
        }
        document.getElementById("gsc-opportunities-empty").style.display = "none";
        renderOpportunitiesTable();
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="7" style="color: var(--error);">Failed: ${escapeHtml(err.message)}</td></tr>`;
    }
}

function renderOpportunitiesTable() {
    const thead = document.querySelector("#gsc-tab-opportunities thead tr");
    thead.innerHTML = sortHeader("opportunities", "page", "Page")
        + sortHeader("opportunities", "query", "Query")
        + sortHeader("opportunities", "impressions", "Impressions")
        + sortHeader("opportunities", "ctr", "CTR")
        + sortHeader("opportunities", "position", "Position")
        + sortHeader("opportunities", "potential_clicks", "Potential +Clicks")
        + '<th>Action</th>';

    document.getElementById("gsc-opportunities-tbody").innerHTML = sortedData("opportunities").map(r => `
        <tr>
            <td style="max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(r.page)}">${shortenUrl(r.page)}</td>
            <td style="max-width: 190px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(r.query)} ${r.confirmed
                ? '<span class="badge badge-warning" title="On page one with enough impressions to be sure the snippet is underperforming. These are the rows Home queues.">Confirmed</span>'
                : '<span class="badge" title="Early signal: not enough impressions yet, or not on page one, to be sure the snippet is the problem.">Watch</span>'}</td>
            <td>${Math.round(r.impressions).toLocaleString()}</td>
            <td style="color: var(--error);">${(r.ctr * 100).toFixed(1)}%</td>
            <td>${positionBadge(r.position)}</td>
            <td style="color: var(--success); font-weight: 600;">+${Math.round(r.potential_clicks)}</td>
            <td style="font-size: 12px;">${r.matched_item
                ? `<a class="btn btn-secondary btn-sm" href="/seo?optimize=${r.matched_item.item_type}&id=${r.matched_item.id}&action=${encodeURIComponent(r.action)}&store_id=${currentStoreId || ""}" style="white-space:nowrap;">Rewrite title/meta</a>`
                : `<span style="color: var(--on-surface-secondary);">${escapeHtml(r.action)}</span>`}</td>
        </tr>
    `).join("");
}

// ─── Cannibalization ───

let cannibalizationData = [];
let cannibalizationAnalysis = {};
let cannibalizationFixes = {};

let cannibalPageSize = 20;
let cannibalShown = 0;

async function loadCannibalization() {
    const container = document.getElementById("gsc-cannibalization-list");
    container.innerHTML = '<div class="spinner" style="margin: 20px auto;"></div>';
    try {
        const [response, fixes] = await Promise.all([
            apiRequest(`/api/gsc/cannibalization?${qp()}&min_impressions=10`),
            apiRequest(`/api/gsc/cannibalization/fixes?store_id=${currentStoreId}`).catch(() => ({})),
        ]);
        cannibalizationData = response.groups || [];
        cannibalizationFixes = fixes;

        const summary = response.summary || {};
        if (!cannibalizationData.length) {
            container.innerHTML = "";
            document.getElementById("cannibal-summary").style.display = "none";
            document.getElementById("gsc-cannibalization-empty").style.display = "";
            return;
        }

        document.getElementById("gsc-cannibalization-empty").style.display = "none";
        document.getElementById("cannibal-summary").style.display = "";
        document.getElementById("cannibal-total-queries").textContent = summary.total_queries?.toLocaleString() || 0;
        document.getElementById("cannibal-total-pages").textContent = summary.total_pages_affected?.toLocaleString() || 0;
        document.getElementById("cannibal-total-impressions").textContent = summary.total_impressions_at_risk?.toLocaleString() || 0;
        document.getElementById("cannibal-total-volume").textContent = summary.total_semrush_volume?.toLocaleString() || 0;
        document.getElementById("cannibal-high").textContent = `${summary.high_count || 0} high`;
        document.getElementById("cannibal-medium").textContent = `${summary.medium_count || 0} med`;
        document.getElementById("cannibal-low").textContent = `${summary.low_count || 0} low`;

        cannibalShown = 0;
        renderFilteredCannibalization();

        initCannibalControls();
    } catch (err) {
        container.innerHTML = `<p style="color: var(--error);">Failed: ${escapeHtml(err.message)}</p>`;
    }
}

function getFilteredCannibalization() {
    let data = [...cannibalizationData];
    const sevFilter = document.getElementById("cannibal-severity-filter")?.value || "";
    const statusFilter = document.getElementById("cannibal-status-filter")?.value || "";
    const search = (document.getElementById("cannibal-search")?.value || "").toLowerCase().trim();
    const sortBy = document.getElementById("cannibal-sort")?.value || "severity";

    if (sevFilter) data = data.filter(g => g.severity === sevFilter);

    if (statusFilter === "pending") {
        data = data.filter(g => g.pages.some(p => {
            const key = `${g.query}|${p.page}`;
            return cannibalizationFixes[key]?.status === "pending";
        }));
    } else if (statusFilter === "needs_attention") {
        data = data.filter(g => g.pages.some(p => {
            const key = `${g.query}|${p.page}`;
            return cannibalizationFixes[key]?.status === "needs_attention";
        }));
    } else if (statusFilter === "verified") {
        data = data.filter(g => g.pages.some(p => {
            const key = `${g.query}|${p.page}`;
            return cannibalizationFixes[key]?.status === "verified";
        }));
    } else if (statusFilter === "unfixed") {
        data = data.filter(g => !g.pages.some(p => {
            const key = `${g.query}|${p.page}`;
            return cannibalizationFixes[key];
        }));
    }

    if (search) data = data.filter(g => g.query.toLowerCase().includes(search));

    if (sortBy === "volume_desc") data.sort((a, b) => (b.semrush_volume || 0) - (a.semrush_volume || 0));
    else if (sortBy === "impressions_desc") data.sort((a, b) => b.total_impressions - a.total_impressions);
    else if (sortBy === "impressions_asc") data.sort((a, b) => a.total_impressions - b.total_impressions);
    else if (sortBy === "clicks_desc") data.sort((a, b) => b.total_clicks - a.total_clicks);
    else if (sortBy === "pages_desc") data.sort((a, b) => b.page_count - a.page_count);
    else if (sortBy === "pages_asc") data.sort((a, b) => a.page_count - b.page_count);
    else if (sortBy === "query_asc") data.sort((a, b) => a.query.localeCompare(b.query));
    else if (sortBy === "query_desc") data.sort((a, b) => b.query.localeCompare(a.query));
    // default "severity" already sorted from backend

    return data;
}

function renderFilteredCannibalization() {
    const filtered = getFilteredCannibalization();
    cannibalShown = Math.min(cannibalShown + cannibalPageSize, filtered.length);
    const visible = filtered.slice(0, cannibalShown);

    const container = document.getElementById("gsc-cannibalization-list");
    document.getElementById("cannibal-showing").textContent = `Showing ${visible.length} of ${filtered.length}`;

    renderCannibalizationList(container, visible);

    const loadMore = document.getElementById("cannibal-load-more");
    if (cannibalShown < filtered.length) {
        loadMore.style.display = "";
        document.getElementById("cannibal-load-more-btn").textContent = `Load More (${filtered.length - cannibalShown} remaining)`;
    } else {
        loadMore.style.display = "none";
    }
}

function setCannibalWinner(gi, pi) {
    const group = cannibalizationData[gi];
    if (!group) return;
    const winnerPage = group.pages[pi];
    if (!winnerPage) return;
    const pageUrl = winnerPage.page;

    group.pages.forEach(p => {
        p.is_winner = (p.page === pageUrl);
    });
    group._manualWinner = pageUrl;

    showToast(`Winner set to: ${shortenUrl(pageUrl)}. Other pages will be targeted for fixes.`, "success");
    renderFilteredCannibalization();
}

async function verifyCannibalizationFixes() {
    const btn = document.getElementById("cannibal-verify-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Verifying...';
    try {
        const result = await apiRequest(`/api/gsc/cannibalization/verify?store_id=${currentStoreId}`, { method: "POST" });
        const msgs = [];
        if (result.verified) msgs.push(`${result.verified} verified`);
        if (result.needs_attention) msgs.push(`${result.needs_attention} need attention`);
        if (result.still_pending) msgs.push(`${result.still_pending} still pending`);
        showToast(`Verification complete: ${msgs.join(", ")}`, result.needs_attention ? "warning" : "success");
        loadCannibalization();
    } catch (err) {
        showToast("Verification failed: " + err.message, "error");
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded" style="font-size: 14px; vertical-align: middle;">verified</span> Verify Pending Fixes';
    }
}

function sortCannibalInner(gi, sortKey) {
    const group = cannibalizationData[gi];
    if (!group) return;
    if (group._innerSort === sortKey) {
        const dir = sortKey.endsWith("_desc") ? sortKey.replace("_desc", "_asc") : sortKey.replace("_asc", "_desc");
        group._innerSort = dir;
    } else {
        group._innerSort = sortKey;
    }
    renderFilteredCannibalization();
}

let _cannibalControlsInit = false;
function initCannibalControls() {
    if (_cannibalControlsInit) return;
    _cannibalControlsInit = true;

    ["cannibal-severity-filter", "cannibal-status-filter", "cannibal-sort"].forEach(id => {
        document.getElementById(id)?.addEventListener("change", () => { cannibalShown = 0; renderFilteredCannibalization(); });
    });

    let searchTimeout;
    document.getElementById("cannibal-search")?.addEventListener("input", () => {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(() => { cannibalShown = 0; renderFilteredCannibalization(); }, 300);
    });

    document.getElementById("cannibal-load-more-btn")?.addEventListener("click", () => {
        renderFilteredCannibalization();
    });
}

function renderCannibalizationList(container, data) {
    container.innerHTML = data.map((group) => {
        const gi = cannibalizationData.indexOf(group);
        if (gi === -1) return "";
        const sevCls = group.severity === "high" ? "badge-error" : group.severity === "medium" ? "badge-warning" : "badge-info";
        const sevLabel = group.severity.charAt(0).toUpperCase() + group.severity.slice(1);
        const totalImp = Math.round(group.total_impressions || 0).toLocaleString();

        const sortedPages = [...group.pages].sort((a, b) => {
            const sort = group._innerSort || "impressions_desc";
            if (sort === "clicks_desc") return b.clicks - a.clicks;
            if (sort === "clicks_asc") return a.clicks - b.clicks;
            if (sort === "impressions_asc") return a.impressions - b.impressions;
            if (sort === "position_asc") return a.position - b.position;
            if (sort === "position_desc") return b.position - a.position;
            if (sort === "page_asc") return (a.match?.title || a.page).localeCompare(b.match?.title || b.page);
            return b.impressions - a.impressions; // default
        });

        const pagesHtml = sortedPages.map(p => {
            const match = p.match || {};
            const matchLabel = match.title || shortenUrl(p.page);
            const matchType = match.type ? `<span class="badge" style="font-size:10px;padding:1px 5px;margin-left:4px;">${match.type}</span>` : "";
            const winnerBadge = p.is_winner ? '<span class="badge badge-success" style="font-size:10px;padding:1px 5px;margin-left:6px;">Winner</span>' : "";

            const fixKey = `${group.query}|${p.page}`;
            const fix = cannibalizationFixes[fixKey];
            let fixBadge = "";
            if (fix) {
                if (fix.status === "pending") {
                    fixBadge = `<span class="badge badge-warning" style="font-size:10px;padding:1px 5px;margin-left:6px;" title="Applied ${fix.applied_at ? formatDate(fix.applied_at) : ''}">Fix Pending</span>`;
                } else if (fix.status === "needs_attention") {
                    fixBadge = `<span class="badge badge-error" style="font-size:10px;padding:1px 5px;margin-left:6px;" title="14+ days, still cannibalizing">Needs Attention</span>`;
                } else if (fix.status === "verified") {
                    fixBadge = `<span class="badge badge-success" style="font-size:10px;padding:1px 5px;margin-left:6px;" title="Verified ${fix.verified_at ? formatDate(fix.verified_at) : ''}">Verified</span>`;
                }
            }

            const rowStyle = p.is_winner ? "background: var(--success-light);" : fix?.status === "needs_attention" ? "background: var(--error-light);" : fix?.status === "pending" ? "background: var(--warning-light);" : fix?.status === "verified" ? "background: var(--success-light); opacity: 0.7;" : "";
            const viewLink = match.type === "product" || match.type === "collection" ? `<a href="/seo?optimize=${match.type}&id=${match.id}&store_id=${currentStoreId || ""}" title="Open this page in SEO" style="color:var(--primary);"><span class="material-symbols-rounded" style="font-size:16px;">open_in_new</span></a>` : "";
            const setWinnerBtn = !p.is_winner ? `<button class="action-icon" title="Set as winner" onclick="event.stopPropagation();setCannibalWinner(${gi},${group.pages.indexOf(p)})"><span class="material-symbols-rounded" style="font-size:16px;color:var(--success);">emoji_events</span></button>` : "";

            return `<tr style="${rowStyle}">
                <td style="max-width: 280px;">
                    <div style="font-weight:500;">${escapeHtml(matchLabel)}${matchType}${winnerBadge}${fixBadge}</div>
                    <div style="font-size:11px;color:var(--on-surface-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(p.page)}">${shortenUrl(p.page)}</div>
                </td>
                <td><strong>${Math.round(p.clicks).toLocaleString()}</strong></td>
                <td>${Math.round(p.impressions).toLocaleString()}</td>
                <td>${positionBadge(p.position)}</td>
                <td style="white-space:nowrap;">${setWinnerBtn}${viewLink}</td>
            </tr>`;
        }).join("");

        return `
            <div style="border: 1px solid var(--outline); border-radius: var(--radius-sm); margin-bottom: 16px; overflow: hidden;">
                <div style="padding: 12px 16px; background: var(--surface-dim); display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                    <span class="badge ${sevCls}">${sevLabel}</span>
                    <span class="badge" style="font-size:11px;">${group.page_count} pages</span>
                    <strong style="font-size: 14px; flex: 1;">"${escapeHtml(group.query)}"</strong>
                    <span style="font-size: 12px; color: var(--on-surface-secondary);">${totalImp} impressions</span>
                    ${group.semrush_volume ? `<span style="font-size: 12px; color: var(--primary); font-weight: 600;">${group.semrush_volume.toLocaleString()} mo. vol.</span>` : ''}
                    <button class="btn btn-primary btn-sm" onclick="analyzeCannibalization(${gi})" id="cannibal-ai-btn-${gi}">
                        <span class="material-symbols-rounded" style="font-size: 14px; vertical-align: middle;">smart_toy</span>
                        AI Analysis
                    </button>
                    <button class="btn btn-sm" style="background:var(--warning);color:#000;font-size:12px;" onclick="bulkFixCannibalization(${gi})" id="cannibal-bulk-btn-${gi}">
                        <span class="material-symbols-rounded" style="font-size: 14px; vertical-align: middle;">bolt</span>
                        Fix all
                    </button>
                </div>
                <table class="data-table" style="margin: 0;">
                    <thead><tr>
                        <th class="sortable-header" onclick="sortCannibalInner(${gi},'page_asc')" style="cursor:pointer;">Page</th>
                        <th class="sortable-header" onclick="sortCannibalInner(${gi},'clicks_desc')" style="cursor:pointer;">Clicks</th>
                        <th class="sortable-header" onclick="sortCannibalInner(${gi},'impressions_desc')" style="cursor:pointer;">Impressions</th>
                        <th class="sortable-header" onclick="sortCannibalInner(${gi},'position_asc')" style="cursor:pointer;">Position</th>
                        <th></th>
                    </tr></thead>
                    <tbody>${pagesHtml}</tbody>
                </table>
                <div id="cannibal-ai-result-${gi}" style="display:none;"></div>
            </div>
        `;
    }).join("");
}

async function analyzeCannibalization(groupIndex) {
    const group = cannibalizationData[groupIndex];
    if (!group) return;

    const btn = document.getElementById(`cannibal-ai-btn-${groupIndex}`);
    const resultDiv = document.getElementById(`cannibal-ai-result-${groupIndex}`);
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Analyzing...';
    resultDiv.style.display = "";
    resultDiv.innerHTML = '<div style="padding: 16px; text-align: center;"><div class="spinner" style="margin: 0 auto;"></div><p style="margin-top: 8px; font-size: 13px; color: var(--on-surface-secondary);">AI is analyzing this cannibalization issue...</p></div>';

    try {
        const result = await apiRequest("/api/gsc/cannibalization/analyze", {
            method: "POST",
            body: { query: group.query, pages: group.pages, manual_winner: group._manualWinner || null, semrush_volume: group.semrush_volume || null },
        });

        let html = '<div style="padding: 16px; border-top: 1px solid var(--outline); background: var(--primary-light);">';
        html += '<h4 style="font-size: 13px; margin-bottom: 12px;"><span class="material-symbols-rounded" style="vertical-align:middle;font-size:16px;">smart_toy</span> AI Resolution Plan</h4>';

        if (result.winner_page) {
            html += `<div style="margin-bottom: 12px; padding: 10px; background: var(--success-light); border-radius: var(--radius-sm); border: 1px solid var(--success);">
                <strong style="font-size: 12px; color: var(--success);">RECOMMENDED WINNER</strong><br>
                <span style="font-size: 13px;">${escapeHtml(result.winner_reason || result.winner_page)}</span>
            </div>`;
        }

        if (result.severity_reason) {
            html += `<p style="font-size: 13px; color: var(--on-surface-secondary); margin-bottom: 12px;">${escapeHtml(result.severity_reason)}</p>`;
        }

        cannibalizationAnalysis[groupIndex] = result;

        if (result.recommendations && result.recommendations.length) {
            html += '<div style="display: flex; flex-direction: column; gap: 8px;">';
            for (let ri = 0; ri < result.recommendations.length; ri++) {
                const rec = result.recommendations[ri];
                const actionColors = {
                    redirect: { bg: "var(--error-light)", border: "var(--error)", icon: "redo", label: "Redirect" },
                    merge: { bg: "var(--warning-light)", border: "var(--warning)", icon: "merge", label: "Merge Content" },
                    differentiate: { bg: "var(--primary-light)", border: "var(--primary)", icon: "alt_route", label: "Differentiate" },
                    "de-optimize": { bg: "var(--surface-dim)", border: "var(--outline)", icon: "remove_circle_outline", label: "De-optimize" },
                    keep: { bg: "var(--success-light)", border: "var(--success)", icon: "check_circle", label: "Keep" },
                };
                const ac = actionColors[rec.action] || actionColors.keep;
                const title = rec.matched_title || shortenUrl(rec.page);
                const matchedPage = group.pages.find(p => p.page === rec.page);
                const match = matchedPage?.match;
                const canFix = match && (match.type === "product" || match.type === "collection") && rec.action !== "keep" && rec.action !== "redirect";
                const fixBtnId = `cannibal-fix-btn-${groupIndex}-${ri}`;
                const fixResultId = `cannibal-fix-result-${groupIndex}-${ri}`;

                html += `<div style="padding: 10px 12px; background: ${ac.bg}; border: 1px solid ${ac.border}; border-radius: var(--radius-sm);">
                    <div style="display: flex; align-items: center; gap: 6px; margin-bottom: 4px;">
                        <span class="material-symbols-rounded" style="font-size: 16px;">${ac.icon}</span>
                        <strong style="font-size: 12px;">${ac.label}</strong>
                        <span style="font-size: 12px; color: var(--on-surface-secondary);">— ${escapeHtml(title)}</span>
                        ${canFix ? `<button class="btn btn-sm btn-secondary" style="margin-left:auto;font-size:11px;padding:2px 8px;" id="${fixBtnId}" onclick="generateCannibalizationFix(${groupIndex}, ${ri})"><span class="material-symbols-rounded" style="font-size:13px;vertical-align:middle;">edit</span> Generate Fix</button>` : ""}
                    </div>
                    <p style="font-size: 12px; margin: 0; color: var(--on-surface);">${escapeHtml(rec.reason)}</p>
                    ${rec.suggested_keyword ? `<p style="font-size: 12px; margin: 4px 0 0; color: var(--primary);"><strong>Target keyword:</strong> "${escapeHtml(rec.suggested_keyword)}"</p>` : ""}
                    <div id="${fixResultId}"></div>
                </div>`;
            }
            html += '</div>';
        }

        html += '</div>';
        resultDiv.innerHTML = html;
    } catch (err) {
        resultDiv.innerHTML = `<div style="padding: 16px; color: var(--error);">AI analysis failed: ${escapeHtml(err.message)}</div>`;
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded" style="font-size: 14px; vertical-align: middle;">smart_toy</span> AI Analysis';
    }
}

let cannibalizationFixData = {};

async function generateCannibalizationFix(groupIndex, recIndex) {
    const group = cannibalizationData[groupIndex];
    const analysis = cannibalizationAnalysis[groupIndex];
    if (!group || !analysis) return;

    const rec = analysis.recommendations[recIndex];
    const matchedPage = group.pages.find(p => p.page === rec.page);
    const match = matchedPage?.match;
    if (!match || !match.id) { showToast("Cannot fix: page not matched to a product/collection", "error"); return; }

    const btn = document.getElementById(`cannibal-fix-btn-${groupIndex}-${recIndex}`);
    const resultDiv = document.getElementById(`cannibal-fix-result-${groupIndex}-${recIndex}`);
    btn.disabled = true;
    btn.textContent = "Generating...";
    resultDiv.innerHTML = '<div style="padding:8px;text-align:center;"><div class="spinner" style="width:18px;height:18px;border-width:2px;margin:0 auto;"></div></div>';

    try {
        const fix = await apiRequest("/api/gsc/cannibalization/generate-fix", {
            method: "POST",
            body: {
                item_type: match.type,
                item_id: match.id,
                competing_query: group.query,
                action: rec.action,
                reason: rec.reason,
                target_keyword: rec.suggested_keyword || "",
            },
        });

        cannibalizationFixData[`${groupIndex}-${recIndex}`] = fix;
        const applyId = `cannibal-apply-${groupIndex}-${recIndex}`;
        const images = fix.current?.images || [];
        const altSuggestions = fix.image_alt_suggestions || [];

        let imagesHtml = "";
        if (images.length > 0) {
            imagesHtml = `<div style="margin-top: 10px; border-top: 1px solid var(--outline); padding-top: 10px;">
                <h5 style="font-size: 12px; margin-bottom: 8px; color: var(--on-surface-secondary);">
                    <span class="material-symbols-rounded" style="font-size:14px;vertical-align:middle;">image</span>
                    Image Alt Text (${images.length} images)
                </h5>`;
            for (let i = 0; i < images.length; i++) {
                const img = images[i];
                const suggestion = altSuggestions.find(s => s.image_index === i);
                const hasChange = !!suggestion;
                const borderColor = hasChange ? "var(--warning)" : "var(--outline)";
                imagesHtml += `<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 6px; padding: 6px; border: 1px solid ${borderColor}; border-radius: 6px;">
                    <div>
                        <label style="font-size: 10px; color: var(--on-surface-secondary);">Image ${i + 1} — Current Alt</label>
                        <div style="padding: 4px 6px; background: var(--surface-dim); border-radius: 4px; font-size: 11px; word-break: break-word;">${escapeHtml(img.alt || "No alt text")}</div>
                    </div>
                    <div>
                        <label style="font-size: 10px; color: var(--on-surface-secondary);">New Alt ${hasChange ? '<span style="color:var(--warning);">(changed)</span>' : '<span style="color:var(--on-surface-secondary);">(unchanged)</span>'}</label>
                        <input type="text" class="form-control fix-img-alt" data-media-id="${escapeHtml(img.media_id)}" data-index="${i}"
                            style="font-size: 11px; padding: 4px 6px;" value="${escapeHtml(suggestion ? suggestion.new_alt : img.alt || "")}">
                    </div>
                </div>`;
            }
            imagesHtml += "</div>";
        }

        resultDiv.innerHTML = `
            <div style="margin-top: 10px; padding: 12px; background: var(--surface); border: 1px solid var(--outline); border-radius: var(--radius-sm);">
                <h5 style="font-size: 12px; margin-bottom: 8px; color: var(--on-surface-secondary);">Proposed Changes</h5>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 12px;">
                    <div>
                        <label style="font-size: 11px; color: var(--on-surface-secondary);">Current Meta Title</label>
                        <div style="padding: 6px 8px; background: var(--surface-dim); border-radius: 6px; word-break: break-word;">${escapeHtml(fix.current?.meta_title || "Not set")}</div>
                    </div>
                    <div>
                        <label style="font-size: 11px; color: var(--on-surface-secondary);">New Meta Title</label>
                        <input type="text" class="form-control" style="font-size: 12px; padding: 6px 8px;" id="fix-mt-${groupIndex}-${recIndex}" value="${escapeHtml(fix.new_meta_title || "")}">
                    </div>
                    <div>
                        <label style="font-size: 11px; color: var(--on-surface-secondary);">Current Meta Description</label>
                        <div style="padding: 6px 8px; background: var(--surface-dim); border-radius: 6px; word-break: break-word;">${escapeHtml(fix.current?.meta_description || "Not set")}</div>
                    </div>
                    <div>
                        <label style="font-size: 11px; color: var(--on-surface-secondary);">New Meta Description</label>
                        <textarea class="form-control" style="font-size: 12px; padding: 6px 8px; min-height: 60px;" id="fix-md-${groupIndex}-${recIndex}">${escapeHtml(fix.new_meta_description || "")}</textarea>
                    </div>
                    ${fix.new_title ? `
                    <div>
                        <label style="font-size: 11px; color: var(--on-surface-secondary);">Current Title</label>
                        <div style="padding: 6px 8px; background: var(--surface-dim); border-radius: 6px;">${escapeHtml(fix.current?.title || "")}</div>
                    </div>
                    <div>
                        <label style="font-size: 11px; color: var(--on-surface-secondary);">New Title</label>
                        <input type="text" class="form-control" style="font-size: 12px; padding: 6px 8px;" id="fix-title-${groupIndex}-${recIndex}" value="${escapeHtml(fix.new_title || "")}">
                    </div>` : ""}
                </div>
                ${imagesHtml}
                <p style="font-size: 11px; color: var(--on-surface-secondary); margin: 8px 0 0;">${escapeHtml(fix.explanation || "")}</p>
                <div style="margin-top: 10px; display: flex; gap: 8px;">
                    <button class="btn btn-primary btn-sm" id="${applyId}" onclick="applyCannibalizationFix(${groupIndex}, ${recIndex}, '${match.type}', ${match.id})">
                        <span class="material-symbols-rounded" style="font-size:14px;vertical-align:middle;">cloud_upload</span> Apply to Shopify
                    </button>
                    <button class="btn btn-secondary btn-sm" onclick="document.getElementById('cannibal-fix-result-${groupIndex}-${recIndex}').innerHTML=''">Cancel</button>
                </div>
            </div>`;
    } catch (err) {
        resultDiv.innerHTML = `<p style="font-size:12px;color:var(--error);margin-top:6px;">Fix generation failed: ${escapeHtml(err.message)}</p>`;
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:13px;vertical-align:middle;">edit</span> Generate Fix';
    }
}

async function applyCannibalizationFix(groupIndex, recIndex, itemType, itemId) {
    const applyBtn = document.getElementById(`cannibal-apply-${groupIndex}-${recIndex}`);
    applyBtn.disabled = true;
    applyBtn.textContent = "Applying...";

    const newTitle = document.getElementById(`fix-title-${groupIndex}-${recIndex}`)?.value?.trim() || null;
    const newMetaTitle = document.getElementById(`fix-mt-${groupIndex}-${recIndex}`)?.value?.trim() || "";
    const newMetaDesc = document.getElementById(`fix-md-${groupIndex}-${recIndex}`)?.value?.trim() || "";

    const imageAlts = [];
    const resultDiv = document.getElementById(`cannibal-fix-result-${groupIndex}-${recIndex}`);
    const fix = cannibalizationFixData[`${groupIndex}-${recIndex}`];
    const currentImages = fix?.current?.images || [];

    resultDiv.querySelectorAll(".fix-img-alt").forEach(input => {
        const mediaId = input.dataset.mediaId;
        const newAlt = input.value.trim();
        const idx = parseInt(input.dataset.index);
        const originalAlt = currentImages[idx]?.alt || "";
        if (mediaId && newAlt && newAlt !== originalAlt) {
            imageAlts.push({ media_id: mediaId, alt: newAlt });
        }
    });

    const group = cannibalizationData[groupIndex];
    const analysis = cannibalizationAnalysis[groupIndex];
    const rec = analysis?.recommendations?.[recIndex];
    const matchedPage = group?.pages?.find(p => p.page === rec?.page);

    try {
        await apiRequest("/api/gsc/cannibalization/apply-fix", {
            method: "POST",
            body: {
                item_type: itemType,
                item_id: itemId,
                new_title: newTitle,
                new_meta_title: newMetaTitle,
                new_meta_description: newMetaDesc,
                image_alts: imageAlts,
                cannibal_query: group?.query || "",
                cannibal_page: matchedPage?.page || "",
                cannibal_action: rec?.action || "",
                cannibal_target: rec?.suggested_keyword || "",
            },
        });
        const altMsg = imageAlts.length > 0 ? ` + ${imageAlts.length} image alt texts` : "";
        showToast(`Fix applied to Shopify${altMsg}! Local database updated.`, "success");
        resultDiv.innerHTML = `<p style="font-size:12px;color:var(--success);margin-top:6px;"><span class="material-symbols-rounded" style="font-size:14px;vertical-align:middle;">check_circle</span> Applied successfully (meta${altMsg}). Re-analyze in a few days to verify improvement.</p>`;
        loadCannibalization();
    } catch (err) {
        showToast("Apply failed: " + err.message, "error");
        applyBtn.disabled = false;
        applyBtn.innerHTML = '<span class="material-symbols-rounded" style="font-size:14px;vertical-align:middle;">cloud_upload</span> Apply to Shopify';
    }
}

async function bulkFixCannibalization(groupIndex) {
    const group = cannibalizationData[groupIndex];
    if (!group) return;

    const analysis = cannibalizationAnalysis[groupIndex];
    if (!analysis || !analysis.recommendations) {
        showToast("Run AI Analysis first before using Fix all", "error");
        return;
    }

    const actionableItems = [];
    for (const rec of analysis.recommendations) {
        if (rec.action === "keep" || rec.action === "redirect") continue;
        const matchedPage = group.pages.find(p => p.page === rec.page);
        const match = matchedPage?.match;
        if (!match || !match.id || (match.type !== "product" && match.type !== "collection")) continue;
        actionableItems.push({
            item_type: match.type,
            item_id: match.id,
            competing_query: group.query,
            action: rec.action,
            reason: rec.reason,
            target_keyword: rec.suggested_keyword || "",
            page_url: rec.page,
        });
    }

    if (!actionableItems.length) {
        showToast("No actionable items to fix (only products/collections with differentiate or de-optimize)", "error");
        return;
    }

    const confirmed = await UIConfirm.ask({
        title: `Rewrite ${actionableItems.length} page${actionableItems.length === 1 ? "" : "s"}?`,
        message: `New titles and meta tags will be generated and written to your live store for ${actionableItems.length} page${actionableItems.length === 1 ? "" : "s"} competing for “${group.query}”, without a preview of each one.`,
        confirmLabel: "Fix all",
    });
    if (!confirmed) return;

    const btn = document.getElementById(`cannibal-bulk-btn-${groupIndex}`);
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Fixing ${actionableItems.length} items...`;

    try {
        // Runs as a background task — synchronous execution hit the proxy
        // timeout on big batches while writes kept landing behind a 504.
        const start = await apiRequest("/api/gsc/cannibalization/bulk-fix", {
            method: "POST",
            body: { items: actionableItems, store_id: currentStoreId },
        });
        const task = await pollTaskUntilDone(`/api/tasks/${start.task_id}`, {
            intervalMs: 2500,
            timeoutMs: Math.max(15, actionableItems.length * 3) * 60 * 1000,
            onProgress: (t) => {
                const p = t.result && t.result.progress;
                if (p) btn.innerHTML = `<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Fixing ${p.completed}/${p.total}...`;
            },
        });
        const res = task.result || {};
        showToast(`Bulk fix complete: ${res.success || 0} succeeded, ${res.failed || 0} failed`, res.failed ? "warning" : "success");
        loadCannibalization();
    } catch (err) {
        showToast("Bulk fix failed: " + err.message, "error");
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded" style="font-size: 14px; vertical-align: middle;">bolt</span> Fix all';
    }
}

// ─── SEO Scores Tab ───

async function loadScoresTab() {
    loadScoreChart();
    loadImprovements();
}

async function loadScoreChart() {
    const container = document.getElementById("gsc-score-chart");
    try {
        const data = await apiRequest(`/api/dashboard/score-trends?${qp()}`);
        if (!data.length) {
            container.innerHTML = '<p style="text-align: center; color: var(--on-surface-secondary); padding: 40px 0;">No score snapshots yet. Take a snapshot to start tracking.</p>';
            return;
        }
        renderScoreChart(container, data);
    } catch (err) {
        container.innerHTML = `<p style="color: var(--error);">Failed to load score trends.</p>`;
    }
}

function renderScoreChart(container, data) {
    const width = container.clientWidth;
    const height = 220;
    const pad = { top: 20, right: 20, bottom: 36, left: 50 };
    const chartW = width - pad.left - pad.right;
    const chartH = height - pad.top - pad.bottom;

    const maxScore = 100;
    const dates = data.map(d => d.date);
    const scores = data.map(d => d.avg_score);

    let svg = `<svg width="${width}" height="${height}" style="display: block;">`;

    // Grid
    for (let s = 0; s <= 100; s += 25) {
        const y = pad.top + chartH - (s / maxScore) * chartH;
        svg += `<line x1="${pad.left}" y1="${y}" x2="${width - pad.right}" y2="${y}" stroke="var(--outline)" stroke-width="1" stroke-dasharray="4,4"/>`;
        svg += `<text x="${pad.left - 8}" y="${y + 4}" text-anchor="end" fill="var(--on-surface-secondary)" font-size="10">${s}</text>`;
    }

    // Labels
    const step = Math.max(1, Math.floor(dates.length / 7));
    for (let i = 0; i < dates.length; i += step) {
        const x = pad.left + (i / (dates.length - 1 || 1)) * chartW;
        svg += `<text x="${x}" y="${height - 8}" text-anchor="middle" fill="var(--on-surface-secondary)" font-size="11">${dates[i].substring(5)}</text>`;
    }

    const points = scores.map((s, i) => {
        const x = pad.left + (i / (scores.length - 1 || 1)) * chartW;
        const y = pad.top + chartH - (s / maxScore) * chartH;
        return `${x},${y}`;
    });

    const baseY = pad.top + chartH;
    svg += `<polygon points="${pad.left},${baseY} ${points.join(" ")} ${pad.left + chartW},${baseY}" fill="var(--primary)" opacity="0.1"/>`;
    svg += `<polyline points="${points.join(" ")}" fill="none" stroke="var(--primary)" stroke-width="2.5" stroke-linejoin="round"/>`;

    scores.forEach((s, i) => {
        const x = pad.left + (i / (scores.length - 1 || 1)) * chartW;
        const y = pad.top + chartH - (s / maxScore) * chartH;
        svg += `<circle cx="${x}" cy="${y}" r="3" fill="var(--primary)"/>`;
    });

    svg += "</svg>";
    container.innerHTML = svg;
}

async function loadImprovements() {
    const tbody = document.getElementById("gsc-improvements-tbody");
    try {
        const data = await apiRequest(`/api/dashboard/improvements?store_id=${currentStoreId}`);
        if (!data.length) {
            tbody.innerHTML = "";
            document.getElementById("gsc-improvements-empty").style.display = "";
            return;
        }
        document.getElementById("gsc-improvements-empty").style.display = "none";
        tbody.innerHTML = data.map(r => {
            const changeColor = r.score_change > 0 ? "var(--success)" : r.score_change < 0 ? "var(--error)" : "var(--on-surface-secondary)";
            const changeText = r.score_change > 0 ? `+${r.score_change}` : `${r.score_change}`;
            return `
                <tr>
                    <td><span class="badge">${r.item_type}</span></td>
                    <td>${escapeHtml(r.title || "Untitled")}</td>
                    <td>${getScoreBadge(r.score)}</td>
                    <td>${r.prev_score !== null ? getScoreBadge(r.prev_score) : "—"}</td>
                    <td style="color: ${changeColor}; font-weight: 600;">${changeText}</td>
                </tr>
            `;
        }).join("");
    } catch (err) {
        tbody.innerHTML = "";
    }
}

// ─── Detail Modal ───

async function showPageDetail(idx) {
    const row = gscRenderedRows.pages[idx];
    if (!row) return;
    const pageUrl = row.page;
    const modal = document.getElementById("gsc-detail-modal");
    document.getElementById("gsc-detail-title").innerHTML = "Queries for: " + escapeHtml(shortenUrl(pageUrl)) + ' <span id="gsc-detail-actions"></span>';
    apiRequest(`/api/gsc/resolve-page?store_id=${currentStoreId}&url=${encodeURIComponent(pageUrl)}`).then((res) => {
        const item = res && res.item;
        const slot = document.getElementById("gsc-detail-actions");
        if (!item || !slot) return;
        const href = item.item_type === "article" ? `/blog?article=${item.id}` : `/seo?optimize=${item.item_type}&id=${item.id}&store_id=${currentStoreId}`;
        slot.innerHTML = `<a class="btn btn-secondary btn-sm" style="margin-left:8px;" href="${href}">Open this ${item.item_type}</a>`;
    }).catch(() => {});
    document.getElementById("gsc-detail-thead").innerHTML = "<th>Query</th><th>Clicks</th><th>Impressions</th><th>CTR</th><th>Position</th>";
    const tbody = document.getElementById("gsc-detail-tbody");
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;"><div class="spinner" style="margin:20px auto;"></div></td></tr>';
    modal.style.display = "";

    try {
        const { date_from, date_to } = getDateRange();
        const data = await apiRequest(`/api/gsc/page-detail?store_id=${currentStoreId}&page=${encodeURIComponent(pageUrl)}&date_from=${date_from}&date_to=${date_to}`);
        tbody.innerHTML = data.map(r => `
            <tr>
                <td><strong>${escapeHtml(r.query)}</strong></td>
                <td>${Math.round(r.clicks).toLocaleString()}</td>
                <td>${Math.round(r.impressions).toLocaleString()}</td>
                <td>${(r.ctr * 100).toFixed(1)}%</td>
                <td>${positionBadge(r.position)}</td>
            </tr>
        `).join("") || '<tr><td colspan="5">No data</td></tr>';
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" style="color: var(--error);">Failed: ${escapeHtml(err.message)}</td></tr>`;
    }
}

async function showQueryDetail(idx) {
    const row = gscRenderedRows.queries[idx];
    if (!row) return;
    const query = row.query;
    const modal = document.getElementById("gsc-detail-modal");
    const writeParams = new URLSearchParams({ tab: "create", topic: query, keywords: query });
    document.getElementById("gsc-detail-title").innerHTML = 'Pages ranking for: "' + escapeHtml(query) + '"'
        + ` <a class="btn btn-secondary btn-sm" style="margin-left:8px;" href="/blog?${writeParams.toString()}" title="Start a blog post on this search">Write an article</a>`;
    document.getElementById("gsc-detail-thead").innerHTML = "<th>Page</th><th>Clicks</th><th>Impressions</th><th>CTR</th><th>Position</th>";
    const tbody = document.getElementById("gsc-detail-tbody");
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;"><div class="spinner" style="margin:20px auto;"></div></td></tr>';
    modal.style.display = "";

    try {
        const { date_from, date_to } = getDateRange();
        const data = await apiRequest(`/api/gsc/query-detail?store_id=${currentStoreId}&query=${encodeURIComponent(query)}&date_from=${date_from}&date_to=${date_to}`);
        tbody.innerHTML = data.map(r => `
            <tr>
                <td style="max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(r.page)}">${shortenUrl(r.page)}</td>
                <td>${Math.round(r.clicks).toLocaleString()}</td>
                <td>${Math.round(r.impressions).toLocaleString()}</td>
                <td>${(r.ctr * 100).toFixed(1)}%</td>
                <td>${positionBadge(r.position)}</td>
            </tr>
        `).join("") || '<tr><td colspan="5">No data</td></tr>';
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" style="color: var(--error);">Failed: ${escapeHtml(err.message)}</td></tr>`;
    }
}

document.getElementById("gsc-detail-close").addEventListener("click", () => {
    document.getElementById("gsc-detail-modal").style.display = "none";
});
document.getElementById("gsc-detail-modal").addEventListener("click", (e) => {
    if (e.target === e.currentTarget) e.currentTarget.style.display = "none";
});

// ─── Events ───

function initGSCEvents() {
    document.getElementById("gsc-date-range").addEventListener("change", (e) => {
        currentDateRange = parseInt(e.target.value);
        loadDashboardData();
    });

    document.getElementById("gsc-device-filter").addEventListener("change", (e) => {
        currentDevice = e.target.value;
        loadDashboardData();
    });

    document.getElementById("gsc-country-filter").addEventListener("change", (e) => {
        currentCountry = e.target.value;
        loadDashboardData();
    });

    document.getElementById("gsc-refresh-btn").addEventListener("click", async () => {
        const btn = document.getElementById("gsc-refresh-btn");
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span>';
        showToast("Pulling latest data from Google Search Console...", "info");
        try {
            const result = await apiRequest(`/api/gsc/pull-sync?store_id=${currentStoreId}`, { method: "POST", body: { type: "incremental" } });
            showToast(`Pull complete: ${result.rows_pulled} rows fetched.`, "success");
            onStoreSelected(currentStoreId);
        } catch (err) {
            showToast("Pull failed: " + err.message, "error");
        } finally {
            btn.disabled = false;
            btn.innerHTML = '<span class="material-symbols-rounded" style="font-size: 16px;">sync</span>';
        }
    });

    document.getElementById("gsc-disconnect-btn").addEventListener("click", async () => {
        if (!confirm("Disconnect Google Search Console? All stored GSC data will be deleted.")) return;
        try {
            await apiRequest(`/api/gsc/auth?store_id=${currentStoreId}`, { method: "DELETE" });
            showToast("Disconnected", "success");
            onStoreSelected(currentStoreId);
        } catch (err) {
            showToast(err.message, "error");
        }
    });

    document.getElementById("gsc-snapshot-btn").addEventListener("click", async () => {
        const btn = document.getElementById("gsc-snapshot-btn");
        btn.disabled = true;
        btn.textContent = "Taking snapshot...";
        try {
            await apiRequest(`/api/dashboard/snapshot?store_id=${currentStoreId}`, { method: "POST" });
            showToast("Score snapshot started. Results will appear shortly.", "success");
            setTimeout(() => loadScoresTab(), 3000);
        } catch (err) {
            showToast(err.message, "error");
        } finally {
            btn.disabled = false;
            btn.innerHTML = '<span class="material-symbols-rounded" style="font-size: 16px; vertical-align: middle; margin-right: 4px;">photo_camera</span> Take Snapshot';
        }
    });
}

// ─── Helpers ───

function shortenUrl(url) {
    try {
        const u = new URL(url);
        return u.pathname + u.search;
    } catch {
        return url;
    }
}

function positionBadge(pos) {
    const rounded = pos.toFixed(1);
    if (pos <= 3) return `<span class="badge badge-success">${rounded}</span>`;
    if (pos <= 10) return `<span class="badge badge-warning">${rounded}</span>`;
    return `<span class="badge badge-error">${rounded}</span>`;
}
