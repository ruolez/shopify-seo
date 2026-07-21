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
    document.getElementById("trend-card").style.display = "none";
    document.getElementById("dash-insights").style.display = "none";
    const prio = document.getElementById("dash-priorities");
    if (prio) {
        prio.innerHTML = "";
        prio.closest(".card").style.display = "none";
    }
    loadOverview().then(() => {
        // Widgets below depend on the resolved store id from the overview.
        loadTrends();
        loadTopPriorities();
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

        if (stats.insights) {
            renderInsights(stats.insights);
        }
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

function renderInsights(insights) {
    const container = document.getElementById("dash-insights");
    if (!container) return;

    let html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;">';

    // AI visibility (latest tracking run)
    if (insights.ai_visibility) {
        const v = insights.ai_visibility;
        const rate = Math.round((v.brand_mention_rate || 0) * 100);
        const sov = Math.round((v.share_of_voice || 0) * 100);
        const rank = v.avg_rank !== null && v.avg_rank !== undefined ? `Avg rank ${v.avg_rank.toFixed(1)}` : "Not yet ranked";
        const when = v.completed_at ? formatRelativeTime(v.completed_at) : "";
        html += `<a href="/ai-seo" class="insight-card insight-clickable">
            <div class="insight-icon" style="color:var(--primary);"><span class="material-symbols-rounded">psychology</span></div>
            <div class="insight-label">AI Visibility</div>
            <div class="insight-value">${rate}% mentions</div>
            <div class="insight-detail">${sov}% share of voice · ${escapeHtml(rank)}${when ? ` · ${escapeHtml(when)}` : ""}</div>
        </a>`;
    }

    // Semrush summary
    if (insights.semrush) {
        const sr = insights.semrush;
        html += `<a href="/semrush" class="insight-card insight-clickable">
            <div class="insight-icon" style="color:var(--primary);"><span class="material-symbols-rounded">search</span></div>
            <div class="insight-label">Semrush Keywords</div>
            <div class="insight-value">${sr.total_keywords.toLocaleString()}</div>
            <div class="insight-detail">Avg pos: ${sr.avg_position} · Striking distance: ${sr.striking_count}</div>
        </a>`;
    }

    // Keyword strategy corpus
    if (insights.keywords) {
        html += `<a href="/keyword-strategy" class="insight-card insight-clickable">
            <div class="insight-icon" style="color:var(--primary);"><span class="material-symbols-rounded">label</span></div>
            <div class="insight-label">Master Keywords</div>
            <div class="insight-value">${insights.keywords.total.toLocaleString()}</div>
            <div class="insight-detail">${insights.keywords.manual} manually curated</div>
        </a>`;
    }

    // Cannibalization — the fixer UI lives on the Search Console page
    if (insights.cannibalization_count > 0) {
        html += `<a href="/gsc" class="insight-card insight-clickable" style="border-color:var(--error);">
            <div class="insight-icon" style="color:var(--error);"><span class="material-symbols-rounded">warning</span></div>
            <div class="insight-label">Cannibalization Issues</div>
            <div class="insight-value" style="color:var(--error);">${insights.cannibalization_count}</div>
            <div class="insight-detail">Keywords ranking on multiple pages</div>
        </a>`;
    }

    // Competitor alerts
    if (insights.unread_competitor_alerts > 0) {
        html += `<a href="/competitors" class="insight-card insight-clickable" style="border-color:var(--warning);">
            <div class="insight-icon" style="color:var(--warning);"><span class="material-symbols-rounded">groups</span></div>
            <div class="insight-label">Competitor Alerts</div>
            <div class="insight-value" style="color:var(--warning);">${insights.unread_competitor_alerts}</div>
            <div class="insight-detail">Unread ranking changes</div>
        </a>`;
    }

    // Content gaps
    if (insights.content_gaps && insights.content_gaps.new > 0) {
        html += `<a href="/content-gaps" class="insight-card insight-clickable">
            <div class="insight-icon" style="color:var(--primary);"><span class="material-symbols-rounded">lightbulb</span></div>
            <div class="insight-label">Content Gaps</div>
            <div class="insight-value">${insights.content_gaps.new}</div>
            <div class="insight-detail">${insights.content_gaps.total} total opportunities</div>
        </a>`;
    }

    // Technical SEO
    if (insights.technical_seo && insights.technical_seo.total_issues > 0) {
        html += `<a href="/technical-seo" class="insight-card insight-clickable">
            <div class="insight-icon" style="color:var(--warning);"><span class="material-symbols-rounded">build</span></div>
            <div class="insight-label">Technical Issues</div>
            <div class="insight-value">${insights.technical_seo.total_issues}</div>
            <div class="insight-detail">${insights.technical_seo.fixable_issues} auto-fixable</div>
        </a>`;
    }

    // Interlinking
    if (insights.interlinking) {
        const il = insights.interlinking;
        const orphans = il.orphan_products + il.orphan_collections;
        if (orphans > 0 || il.broken_links > 0) {
            html += `<a href="/interlinking" class="insight-card insight-clickable">
                <div class="insight-icon" style="color:var(--on-surface-secondary);"><span class="material-symbols-rounded">link</span></div>
                <div class="insight-label">Link Health</div>
                <div class="insight-value">${orphans} orphans</div>
                <div class="insight-detail">${il.under_linked} under-linked · ${il.broken_links} broken</div>
            </a>`;
        }
    }

    // Recent optimizations
    if (insights.recent_optimizations > 0) {
        html += `<a href="/seo?tab=priorities" class="insight-card insight-clickable">
            <div class="insight-icon" style="color:var(--success);"><span class="material-symbols-rounded">auto_fix_high</span></div>
            <div class="insight-label">Recent Optimizations</div>
            <div class="insight-value">${insights.recent_optimizations}</div>
            <div class="insight-detail">Last 30 days</div>
        </a>`;
    }

    html += '</div>';
    container.innerHTML = html;
    container.style.display = "";
}

async function loadTopPriorities() {
    try {
        if (!dashStoreId) return;

        const items = await apiRequest(`/api/seo/priorities?store_id=${dashStoreId}&type=product&limit=5`);
        const container = document.getElementById("dash-priorities");
        if (!container || !items.length) return;

        let html = "";
        for (const item of items) {
            const prioClass = item.priority_score >= 80 ? "badge-error" : item.priority_score >= 60 ? "badge-warning" : "badge-info";
            const actionParam = encodeURIComponent(item.recommended_action || '');
            const optimizeUrl = `/seo?optimize=${item.item_type}&id=${item.id}&action=${actionParam}`;
            const cannibalIcon = item.cannibalization_warning ? '<span class="material-symbols-rounded" style="font-size:14px;color:var(--error);vertical-align:middle;" title="Cannibalization risk">warning</span> ' : '';
            html += `<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--outline);cursor:pointer;" onclick="window.location='${optimizeUrl}'">
                <span class="badge ${prioClass}" style="min-width:32px;text-align:center;">${item.priority_score}</span>
                <div style="flex:1;min-width:0;">
                    <div style="font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${cannibalIcon}${escapeHtml(item.title)}</div>
                    <div style="font-size:11px;color:var(--on-surface-secondary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(item.recommended_action || item.top_opportunity || '')}</div>
                </div>
                <a href="${optimizeUrl}" class="action-icon" title="Optimize" onclick="event.stopPropagation()"><span class="material-symbols-rounded" style="font-size:18px;">arrow_forward</span></a>
            </div>`;
        }

        container.innerHTML = html;
        container.closest(".card").style.display = "";
    } catch {
        // No priorities available
    }
}
