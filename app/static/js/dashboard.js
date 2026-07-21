let dashStoreId = null;
const dashCharts = {};

document.addEventListener("DOMContentLoaded", () => {
    loadDashboard();
    document.getElementById("store-switcher").addEventListener("change", async (e) => {
        const storeId = parseInt(e.target.value, 10);
        try {
            await apiRequest("/api/session/active-store", {
                method: "POST",
                body: JSON.stringify({ store_id: storeId }),
            });
        } catch {
            showToast("Failed to switch store", "error");
            return;
        }
        dashStoreId = storeId;
        loadDashboard();
    });
});

function loadDashboard() {
    // Reset store-scoped widgets so a store switch never shows stale data
    // from the previous store while the new requests are in flight.
    for (const id of ["trend-card", "queue-card", "ctr-card"]) {
        document.getElementById(id).style.display = "none";
    }
    loadOverview().then(() => {
        // Widgets below depend on the resolved store id from the overview.
        loadTrends();
        loadActionQueue();
        loadCtrOpportunities();
    });
}

function setChart(key, make) {
    if (dashCharts[key]) {
        dashCharts[key].destroy();
        delete dashCharts[key];
    }
    const chart = make();
    if (chart) dashCharts[key] = chart;
}

function renderStoreSwitcher(stores, activeId) {
    const sel = document.getElementById("store-switcher");
    if (!stores || stores.length < 2) {
        sel.style.display = "none";
        return;
    }
    sel.innerHTML = stores
        .map((s) => `<option value="${s.id}" ${s.id === activeId ? "selected" : ""}>${escapeHtml(s.name)}</option>`)
        .join("");
    sel.style.display = "";
}

async function loadOverview() {
    try {
        const url = dashStoreId ? `/api/dashboard/overview?store_id=${dashStoreId}` : "/api/dashboard/overview";
        const stats = await apiRequest(url);

        dashStoreId = stats.store ? stats.store.id : null;
        renderStoreSwitcher(stats.stores, dashStoreId);

        // Store identity + sync freshness + inventory line
        const line = document.getElementById("dash-store-line");
        if (stats.store && stats.store.name) {
            const synced = stats.store.last_synced_at
                ? `Last synced ${formatRelativeTime(stats.store.last_synced_at)}`
                : "Never synced";
            const c = stats.counts || {};
            const inventory = `${(c.products ?? 0).toLocaleString()} products · ${(c.collections ?? 0).toLocaleString()} collections · ${(c.articles ?? 0).toLocaleString()} articles`;
            line.innerHTML = `<strong>${escapeHtml(stats.store.name)}</strong>` +
                (stats.store.domain ? ` · ${escapeHtml(stats.store.domain)}` : "") +
                ` · ${escapeHtml(synced)} · ${escapeHtml(inventory)}`;
            line.style.display = "";
        } else {
            line.style.display = "none";
        }

        renderKpis(stats.kpis || {}, stats);
    } catch (err) {
        showToast("Failed to load dashboard", "error");
    }
}

function renderKpis(kpis, stats) {
    const colors = chartColors();

    // Organic clicks
    const clicksEl = document.getElementById("kpi-clicks");
    if (kpis.clicks_28d) {
        clicksEl.textContent = kpis.clicks_28d.value.toLocaleString();
        renderDelta(document.getElementById("kpi-clicks-delta"), kpis.clicks_28d.pct_change);
        setChart("kpi-clicks", () => makeSparkline(document.getElementById("kpi-clicks-spark"), kpis.clicks_28d.spark));
    } else {
        clicksEl.textContent = "—";
        document.getElementById("kpi-clicks-delta").innerHTML =
            '<a href="/gsc" style="color:var(--primary);">Connect GSC</a>';
    }

    // Avg position (lower = better)
    const posEl = document.getElementById("kpi-position");
    if (kpis.avg_position_28d && kpis.avg_position_28d.value !== null) {
        posEl.textContent = kpis.avg_position_28d.value.toFixed(1);
        const ch = kpis.avg_position_28d.change;
        const deltaEl = document.getElementById("kpi-position-delta");
        if (ch !== null && ch !== undefined) {
            const improved = ch <= 0;
            deltaEl.innerHTML = `<span class="${improved ? "delta-up" : "delta-down"}">${improved ? "▲" : "▼"} ${Math.abs(ch).toFixed(1)} vs prev period</span>`;
        } else {
            deltaEl.textContent = "";
        }
        setChart("kpi-position", () => makeSparkline(
            document.getElementById("kpi-position-spark"),
            kpis.avg_position_28d.spark.map((v) => (v === null ? null : -v)),
            { color: colors.warning }
        ));
    } else {
        posEl.textContent = "—";
    }

    // SEO score
    const scoreEl = document.getElementById("kpi-score");
    if (stats.score_pending) {
        scoreEl.textContent = "…";
        document.getElementById("kpi-score-sub").textContent = "Pending nightly snapshot";
    } else if (kpis.seo_score) {
        scoreEl.textContent = kpis.seo_score.value;
        const sub = document.getElementById("kpi-score-sub");
        const dist = stats.score_distribution;
        let html = dist ? `${dist.good} good · ${dist.fair} fair · ${dist.poor} poor` : "";
        const ch = kpis.seo_score.change_30d;
        if (ch !== null && ch !== undefined && ch !== 0) {
            const up = ch > 0;
            html += ` <span class="${up ? "delta-up" : "delta-down"}">${up ? "▲" : "▼"} ${Math.abs(ch).toFixed(1)} (30d)</span>`;
        }
        sub.innerHTML = html;
        setChart("kpi-score", () => makeSparkline(document.getElementById("kpi-score-spark"), kpis.seo_score.spark, { color: colors.success }));
    } else if (stats.avg_seo_score) {
        scoreEl.textContent = stats.avg_seo_score;
    }

    // Keywords
    const kwEl = document.getElementById("kpi-keywords");
    if (kpis.keywords) {
        kwEl.textContent = kpis.keywords.total.toLocaleString();
        const delta = kpis.keywords.striking_change;
        const deltaHtml = delta
            ? ` <span class="${delta > 0 ? "delta-up" : "delta-down"}">${delta > 0 ? "+" : ""}${delta}</span>`
            : "";
        document.getElementById("kpi-keywords-sub").innerHTML =
            `${kpis.keywords.top10} top 10 · ${kpis.keywords.striking} striking${deltaHtml}`;
    } else {
        kwEl.textContent = "—";
        document.getElementById("kpi-keywords-sub").innerHTML =
            '<a href="/semrush" style="color:var(--primary);">Import keywords</a>';
    }

    // AI visibility
    const visEl = document.getElementById("kpi-visibility");
    if (kpis.ai_visibility) {
        const v = kpis.ai_visibility;
        visEl.textContent = `${Math.round(v.mention_rate * 100)}%`;
        let sub = `${Math.round(v.share_of_voice * 100)}% share of voice`;
        if (v.delta_vs_prev_run !== null && v.delta_vs_prev_run !== undefined && v.delta_vs_prev_run !== 0) {
            const up = v.delta_vs_prev_run > 0;
            const pts = Math.abs(Math.round(v.delta_vs_prev_run * 100));
            sub += ` <span class="${up ? "delta-up" : "delta-down"}">${up ? "▲" : "▼"} ${pts}pp</span>`;
        }
        if (v.last_run_at) sub += ` · ${formatRelativeTime(v.last_run_at)}`;
        document.getElementById("kpi-visibility-sub").innerHTML = sub;
    } else {
        visEl.textContent = "—";
        document.getElementById("kpi-visibility-sub").innerHTML =
            '<a href="/ai-seo" style="color:var(--primary);">Run tracking</a>';
    }
}

async function loadTrends() {
    try {
        if (!dashStoreId) return;
        const data = await apiRequest(`/api/dashboard/trends?store_id=${dashStoreId}&days=90`);
        if (!data.gsc_daily || !data.gsc_daily.length) return;

        const colors = chartColors();
        const labels = data.gsc_daily.map((d) => d.date.slice(5)); // MM-DD
        const dateIndex = {};
        data.gsc_daily.forEach((d, i) => { dateIndex[d.date] = i; });
        const markers = (data.events || [])
            .filter((e) => dateIndex[e.date] !== undefined)
            .map((e) => ({ index: dateIndex[e.date], label: `${e.count} optimized` }));

        const totalOptimized = (data.events || []).reduce((n, e) => n + e.count, 0);
        document.getElementById("trend-note").textContent =
            `Last 90 days${totalOptimized ? ` · ${totalOptimized} optimizations applied (dashed lines)` : ""}`;

        setChart("trend", () => makeTimeSeriesChart(document.getElementById("trend-chart"), {
            labels,
            markers,
            datasets: [
                {
                    label: "Clicks",
                    data: data.gsc_daily.map((d) => d.clicks),
                    borderColor: colors.primary,
                    backgroundColor: hexToRgba(colors.primary, 0.10),
                    fill: true, borderWidth: 1.5, pointRadius: 0, tension: 0.3, yAxisID: "y",
                },
                {
                    label: "Impressions",
                    data: data.gsc_daily.map((d) => d.impressions),
                    borderColor: colors.text,
                    borderWidth: 1, pointRadius: 0, tension: 0.3, yAxisID: "y1", hidden: true,
                },
            ],
        }));
        // Secondary axis for impressions when toggled on via legend
        const chart = dashCharts["trend"];
        chart.options.scales.y1 = { position: "right", display: "auto", ticks: { color: colors.text, maxTicksLimit: 5 }, grid: { display: false } };
        chart.update();

        document.getElementById("trend-card").style.display = "";
    } catch {
        // GSC not connected or no data — card stays hidden
    }
}

function renderDelta(el, pct, invert = false) {
    if (pct === null || pct === undefined || !isFinite(pct)) { el.textContent = ""; return; }
    const up = pct >= 0;
    const good = invert ? !up : up;
    const arrow = up ? "▲" : "▼";
    el.innerHTML = `<span class="${good ? "delta-up" : "delta-down"}">${arrow} ${Math.abs(pct).toFixed(1)}% vs prev period</span>`;
}

const QUEUE_SOURCE_LABELS = {
    priority: "SEO", ctr: "CTR", cannibalization: "Cannibal", technical: "Technical",
    gaps: "Gaps", competitor: "Competitor", links: "Links",
};

async function loadActionQueue() {
    try {
        if (!dashStoreId) return;
        const data = await apiRequest(`/api/dashboard/action-queue?store_id=${dashStoreId}&limit=15`);
        const container = document.getElementById("dash-queue");
        const card = document.getElementById("queue-card");

        const totals = data.total_by_source || {};
        const totalCount = Object.values(totals).reduce((a, b) => a + b, 0);
        document.getElementById("queue-note").textContent = totalCount ? `${totalCount} open items` : "";

        if (!data.items.length) {
            container.innerHTML = `<div style="padding:16px 0;color:var(--on-surface-secondary);font-size:13px;">
                <span class="material-symbols-rounded" style="vertical-align:middle;color:var(--success);">check_circle</span>
                All clear — no outstanding actions.</div>`;
            card.style.display = "";
            return;
        }

        const sevBadge = { high: "badge-error", med: "badge-warning", low: "badge-info" };
        let html = "";
        for (const item of data.items) {
            const gain = item.est_clicks_gain
                ? `<span style="font-size:11px;color:var(--success);white-space:nowrap;">+${item.est_clicks_gain} clicks/mo</span>`
                : "";
            html += `<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--outline);cursor:pointer;" onclick="window.location='${item.href}'">
                <span class="badge ${sevBadge[item.severity] || "badge-info"}" style="min-width:64px;text-align:center;">${QUEUE_SOURCE_LABELS[item.source] || item.source}</span>
                <div style="flex:1;min-width:0;">
                    <div style="font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(item.title)}</div>
                    <div style="font-size:11px;color:var(--on-surface-secondary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(item.detail || "")}</div>
                </div>
                ${gain}
                <a href="${item.href}" class="action-icon" title="Open" onclick="event.stopPropagation()"><span class="material-symbols-rounded" style="font-size:18px;">arrow_forward</span></a>
            </div>`;
        }
        container.innerHTML = html;
        card.style.display = "";
    } catch {
        // Queue unavailable — card stays hidden
    }
}

async function loadCtrOpportunities() {
    try {
        if (!dashStoreId) return;
        const data = await apiRequest(`/api/dashboard/ctr-opportunities?store_id=${dashStoreId}&limit=10`);
        if (!data.items || !data.items.length) return;

        let html = "";
        for (const c of data.items) {
            const target = c.matched_item
                ? `<a href="/seo?optimize=${c.matched_item.item_type}&id=${c.matched_item.id}" style="color:var(--primary);">${escapeHtml(c.matched_item.title)}</a>`
                : `<span style="color:var(--on-surface-secondary);">${escapeHtml(c.page.replace(/^https?:\/\/[^/]+/, ""))}</span>`;
            html += `<tr style="border-top:1px solid var(--outline);">
                <td style="padding:6px 8px;font-weight:500;">${escapeHtml(c.query)}</td>
                <td style="padding:6px 8px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${target}</td>
                <td style="padding:6px 8px;text-align:right;">${c.position}</td>
                <td style="padding:6px 8px;text-align:right;">${c.impressions.toLocaleString()}</td>
                <td style="padding:6px 8px;text-align:right;color:var(--error);">${(c.actual_ctr * 100).toFixed(1)}%</td>
                <td style="padding:6px 8px;text-align:right;">${(c.expected_ctr * 100).toFixed(1)}%</td>
                <td style="padding:6px 8px;text-align:right;color:var(--success);">+${c.est_clicks_gain}/mo</td>
            </tr>`;
        }
        document.getElementById("dash-ctr-rows").innerHTML = html;
        document.getElementById("ctr-card").style.display = "";
    } catch {
        // No CTR data — card stays hidden
    }
}
