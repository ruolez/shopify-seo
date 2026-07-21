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
    for (const id of ["trend-card", "queue-card", "ctr-card", "impact-card", "movers-card", "ops-row", "dash-freshness", "changes-card"]) {
        document.getElementById(id).style.display = "none";
    }
    loadOverview().then(() => {
        // Widgets below depend on the resolved store id from the overview.
        loadTrends();
        loadActionQueue();
        loadCtrOpportunities();
        loadImpact();
        loadMovers();
        loadOps();
        loadChanges();
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

const IMPACT_BADGES = {
    improved: ["badge-success", "Improved"],
    declined: ["badge-error", "Declined"],
    flat: ["badge-info", "Flat"],
    collecting: ["badge-warning", "Collecting"],
    insufficient: ["badge-info", "Low data"],
};

async function loadImpact() {
    try {
        if (!dashStoreId) return;
        const data = await apiRequest(`/api/dashboard/impact?store_id=${dashStoreId}&limit=8`);
        if (!data.items || !data.items.length) return;

        const s = data.summary || {};
        document.getElementById("impact-summary").textContent =
            `${s.events_30d ?? 0} changes (30d) · ${s.improved ?? 0} improved · ${s.declined ?? 0} declined`;

        let html = "";
        for (const item of data.items) {
            const [badgeClass, badgeText] = IMPACT_BADGES[item.status] || ["badge-info", item.status];
            let detail;
            if (item.status === "collecting") {
                detail = `${item.post_days_available} of 7 days of post-change data`;
            } else if (item.status === "insufficient") {
                detail = "Too little search data before the change";
            } else {
                const clicks = item.clicks_change_pct !== null
                    ? `clicks/day ${item.pre.clicks_per_day} → ${item.post.clicks_per_day} (${item.clicks_change_pct > 0 ? "+" : ""}${item.clicks_change_pct}%)`
                    : `clicks/day ${item.pre.clicks_per_day} → ${item.post.clicks_per_day}`;
                const pos = item.position_change !== null
                    ? ` · pos ${item.pre.position} → ${item.post.position}`
                    : "";
                detail = clicks + pos;
            }
            html += `<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--outline);">
                <span class="badge ${badgeClass}" style="min-width:74px;text-align:center;">${badgeText}</span>
                <div style="flex:1;min-width:0;">
                    <div style="font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(item.title)}</div>
                    <div style="font-size:11px;color:var(--on-surface-secondary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(item.change_type || "optimized")} · ${formatRelativeTime(item.changed_at)} · ${escapeHtml(detail)}</div>
                </div>
            </div>`;
        }
        document.getElementById("dash-impact").innerHTML = html;
        document.getElementById("impact-card").style.display = "";
    } catch {
        // No impact data — card stays hidden
    }
}

async function loadMovers() {
    try {
        if (!dashStoreId) return;
        const data = await apiRequest(`/api/dashboard/movers?store_id=${dashStoreId}&limit=5`);
        const hasData = (data.up && data.up.length) || (data.down && data.down.length) || (data.competitor_gains && data.competitor_gains.length);
        if (!hasData) return;

        const row = (m, up) => `<div style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:12px;">
            <span class="${up ? "delta-up" : "delta-down"}" style="min-width:34px;">${up ? "▲" : "▼"} ${Math.abs(m.change)}</span>
            <span style="flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${escapeHtml(m.keyword)}">${escapeHtml(m.keyword)}</span>
            <span style="color:var(--on-surface-secondary);white-space:nowrap;">${m.previous_position} → ${m.position}${m.volume ? ` · ${m.volume.toLocaleString()}/mo` : ""}</span>
        </div>`;

        let html = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">';
        html += `<div><div style="font-size:11px;font-weight:600;color:var(--on-surface-secondary);padding:6px 0;">WINNERS</div>${(data.up || []).map((m) => row(m, true)).join("") || '<div style="font-size:12px;color:var(--on-surface-secondary);">None</div>'}</div>`;
        html += `<div><div style="font-size:11px;font-weight:600;color:var(--on-surface-secondary);padding:6px 0;">LOSERS</div>${(data.down || []).map((m) => row(m, false)).join("") || '<div style="font-size:12px;color:var(--on-surface-secondary);">None</div>'}</div>`;
        html += "</div>";

        if (data.competitor_gains && data.competitor_gains.length) {
            html += '<div style="font-size:11px;font-weight:600;color:var(--on-surface-secondary);padding:10px 0 4px;">COMPETITOR ALERTS</div>';
            for (const g of data.competitor_gains.slice(0, 4)) {
                html += `<div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px;">
                    <span style="flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(g.keyword)}</span>
                    <span style="color:var(--on-surface-secondary);white-space:nowrap;">${escapeHtml(g.competitor || "")} #${g.their_position ?? "?"} vs us #${g.our_position ?? "—"}</span>
                </div>`;
            }
        }

        document.getElementById("dash-movers").innerHTML = html;
        document.getElementById("movers-card").style.display = "";
    } catch {
        // No movers data — card stays hidden
    }
}

const FRESHNESS_LABELS = {
    sync: ["Store sync", "/stores"],
    gsc_pull: ["GSC pull", "/gsc"],
    semrush_import: ["Semrush", "/semrush"],
    visibility_run: ["AI visibility", "/ai-seo"],
    competitor_check: ["Competitors", "/competitors"],
};

async function loadOps() {
    try {
        if (!dashStoreId) return;
        const data = await apiRequest(`/api/dashboard/ops?store_id=${dashStoreId}`);
        if (!data || !data.freshness) return;

        // Freshness strip
        const dotColor = { ok: "var(--success)", stale: "var(--warning)", old: "var(--error)", none: "var(--outline)" };
        let fresh = "";
        for (const [key, entry] of Object.entries(data.freshness)) {
            const [label, href] = FRESHNESS_LABELS[key] || [key, "#"];
            const age = entry.at ? formatRelativeTime(entry.at) : "never";
            fresh += `<a href="${href}" style="display:inline-flex;align-items:center;gap:4px;color:inherit;white-space:nowrap;" title="${label}: ${age}">
                <span style="width:7px;height:7px;border-radius:50%;background:${dotColor[entry.level] || dotColor.none};display:inline-block;"></span>
                ${label} ${age}</a>`;
        }
        const freshEl = document.getElementById("dash-freshness");
        freshEl.innerHTML = fresh;
        freshEl.style.display = "flex";

        // Content pipeline
        const p = data.pipeline || {};
        const pipelineRow = (label, value, href) =>
            `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--outline);">
                <a href="${href}" style="color:inherit;">${label}</a><strong>${value}</strong></div>`;
        document.getElementById("dash-pipeline").innerHTML =
            pipelineRow("Scheduled news", p.news_scheduled ?? 0, "/news") +
            pipelineRow("Drafts in progress", p.news_drafts ?? 0, "/news") +
            pipelineRow("Accepted topics waiting", p.topics_accepted ?? 0, "/blog") +
            pipelineRow("Published this month", p.published_this_month ?? 0, "/blog") +
            pipelineRow("Gaps closed this month", p.gaps_closed_this_month ?? 0, "/content-gaps");

        // Coverage bars
        const c = data.coverage || {};
        const bar = (label, pct, href) => {
            if (pct === null || pct === undefined) {
                return `<div style="padding:5px 0;"><a href="${href}" style="color:var(--on-surface-secondary);">${label}: no data</a></div>`;
            }
            const color = pct >= 90 ? "var(--success)" : pct >= 60 ? "var(--warning)" : "var(--error)";
            return `<div style="padding:5px 0;">
                <div style="display:flex;justify-content:space-between;font-size:12px;"><a href="${href}" style="color:inherit;">${label}</a><span>${pct}%</span></div>
                <div style="height:5px;background:var(--outline);border-radius:3px;margin-top:3px;"><div style="height:5px;width:${Math.min(pct, 100)}%;background:${color};border-radius:3px;"></div></div>
            </div>`;
        };
        const aeo = c.aeo_assets || {};
        document.getElementById("dash-coverage").innerHTML =
            bar("Meta titles", c.meta_title_pct, "/seo") +
            bar("Meta descriptions", c.meta_desc_pct, "/seo") +
            bar("Image alt text", c.alt_text_pct, "/seo") +
            bar("Schema pushed", c.schema_pushed_pct, "/ai-seo") +
            bar("Keywords mapped", c.keyword_mapped_pct, "/keyword-strategy") +
            `<div style="padding:5px 0;font-size:12px;display:flex;justify-content:space-between;"><a href="/ai-seo" style="color:inherit;">AEO assets</a><span>${aeo.published ?? 0}/${aeo.total ?? 0} published</span></div>`;

        // AI operations
        const a = data.ai_ops || {};
        const successHtml = a.success_rate_7d !== null && a.success_rate_7d !== undefined
            ? `<span style="color:${a.success_rate_7d >= 90 ? "var(--success)" : "var(--warning)"};">${a.success_rate_7d}%</span>`
            : "—";
        const failedHtml = a.failed_7d
            ? `<a href="/settings" style="color:var(--error);font-weight:600;">${a.failed_7d}</a>`
            : "0";
        const spendHtml = a.daily_budget
            ? `$${(a.spend_today ?? 0).toFixed(2)} / $${a.daily_budget.toFixed(0)} <span style="color:var(--on-surface-secondary);font-size:11px;">(visibility runs)</span>`
            : `$${(a.spend_today ?? 0).toFixed(2)}`;
        const aiRow = (label, html) =>
            `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--outline);"><span>${label}</span><strong>${html}</strong></div>`;
        document.getElementById("dash-aiops").innerHTML =
            aiRow("Tasks (7 days)", (a.tasks_7d ?? 0).toLocaleString()) +
            aiRow("Success rate", successHtml) +
            aiRow("Failed (7 days)", failedHtml) +
            aiRow("Running now", a.running ?? 0) +
            aiRow("AI spend today", spendHtml);

        document.getElementById("ops-row").style.display = "grid";
    } catch {
        // Ops data unavailable — row stays hidden
    }
}

const CHANGE_EVENT_META = {
    anomaly_clicks_drop: ["trending_down", "var(--error)", "Traffic drop"],
    anomaly_keyword_dropout: ["keyboard_double_arrow_down", "var(--warning)", "Keyword dropout"],
    gap_closed: ["task_alt", "var(--success)", "Gap closed"],
    seo_applied: ["auto_fix_high", "var(--primary)", "Optimization applied"],
    article_published: ["article", "var(--primary)", "Article published"],
    visibility_run: ["psychology", "var(--primary)", "AI visibility"],
    competitor_check: ["groups", "var(--warning)", "Competitors"],
};

function changeDetail(item) {
    const p = item.payload || {};
    switch (item.event_type) {
        case "anomaly_clicks_drop":
            return `clicks ${p.prev_clicks} → ${p.cur_clicks} (−${p.drop_pct}%) week over week`;
        case "anomaly_keyword_dropout":
            return `position ${p.previous_position} → ${p.position ?? "lost"}${p.volume ? ` · ${p.volume.toLocaleString()}/mo` : ""}${p.impressions ? ` · ${p.impressions.toLocaleString()} impressions` : ""}`;
        case "gap_closed":
            return `now ranking at position ${p.position}`;
        case "seo_applied":
            return p.change_type || "changes applied";
        case "visibility_run":
            return `${Math.round((p.share_of_voice || 0) * 100)}% share of voice`;
        case "competitor_check":
            return `${p.alerts} new ranking alerts`;
        default:
            return "";
    }
}

async function loadChanges() {
    try {
        if (!dashStoreId) return;
        const data = await apiRequest(`/api/dashboard/changes?store_id=${dashStoreId}`);
        if (!data.items || !data.items.length) return;

        document.getElementById("changes-note").textContent =
            data.since ? `since ${formatRelativeTime(data.since)}` : "";

        let html = "";
        for (const item of data.items) {
            const [icon, color, label] = CHANGE_EVENT_META[item.event_type] || ["info", "var(--on-surface-secondary)", item.event_type];
            const detail = changeDetail(item);
            const subject = (item.subject || "").replace(/^https?:\/\/[^/]+/, "");
            html += `<div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--outline);">
                <span class="material-symbols-rounded" style="font-size:18px;color:${color};">${icon}</span>
                <div style="flex:1;min-width:0;">
                    <div style="font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"><strong>${label}:</strong> ${escapeHtml(subject)}</div>
                    ${detail ? `<div style="font-size:11px;color:var(--on-surface-secondary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(detail)}</div>` : ""}
                </div>
                <span style="font-size:11px;color:var(--on-surface-secondary);white-space:nowrap;">${formatRelativeTime(item.created_at)}</span>
            </div>`;
        }
        document.getElementById("dash-changes").innerHTML = html;
        document.getElementById("changes-card").style.display = "";

        // Advance the marker only after the feed rendered — a failed load
        // must not swallow unseen events.
        apiRequest(`/api/dashboard/changes/seen?store_id=${dashStoreId}`, { method: "POST", body: "{}" }).catch(() => {});
    } catch {
        // Feed unavailable — card stays hidden
    }
}
