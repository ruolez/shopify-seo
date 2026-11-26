let dashStoreId = null;
let dashDomain = "";
const dashCharts = {};

document.addEventListener("DOMContentLoaded", () => {
    // Every headline number opens the page where you act on it.
    document.querySelectorAll(".kpi[data-href]").forEach((el) => {
        const go = (e) => { if (!e.target.closest("a")) window.location = el.dataset.href; };
        el.addEventListener("click", go);
        el.addEventListener("keydown", (e) => { if (e.key === "Enter") go(e); });
    });
    loadDashboard();
    // Store switching is owned by the global topbar StoreSelector (ui.js).
    document.addEventListener("store:changed", (e) => {
        dashStoreId = e.detail.storeId;
        loadDashboard();
    });
});

function loadDashboard() {
    // Reset store-scoped widgets so a store switch never shows stale data
    // from the previous store while the new requests are in flight.
    for (const id of ["trend-card", "queue-card", "ctr-card", "impact-card", "movers-card", "ops-row", "dash-freshness", "changes-card", "revenue-card", "kpi-revenue-card", "setup-card", "week-card", "goals-card"]) {
        document.getElementById(id).style.display = "none";
    }
    loadOverview().then(() => {
        // Widgets below depend on the resolved store id from the overview.
        loadSetupChecklist();
        loadActionQueue();
        loadMonthlyReport();
        loadChanges();
        loadTrends();
        loadCtrOpportunities();
        loadImpact();
        loadMovers();
        loadOps();
        loadRevenue();
        loadWeek();
        loadGoals();
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
    // Superseded by the global topbar StoreSelector (ui.js); kept as a no-op
    // because loadOverview still calls it.
}

async function loadOverview() {
    try {
        const url = dashStoreId ? `/api/dashboard/overview?store_id=${dashStoreId}` : "/api/dashboard/overview";
        const stats = await apiRequest(url);

        dashStoreId = stats.store ? stats.store.id : null;
        renderStoreSwitcher(stats.stores, dashStoreId);

        // Store identity + sync freshness + inventory line
        const line = document.getElementById("dash-store-line");
        dashDomain = (stats.store && stats.store.domain) || "";
        if (stats.store && stats.store.name) {
            const synced = stats.store.last_synced_at
                ? `Synced ${formatRelativeTime(stats.store.last_synced_at)}`
                : "Never synced";
            const c = stats.counts || {};
            line.innerHTML = `<a href="/stores" class="aside-row"><span>${escapeHtml(synced)}</span><span class="material-symbols-rounded">sync</span></a>
                <a href="/seo?tab=products" class="aside-row"><span>Products</span><b>${(c.products ?? 0).toLocaleString()}</b></a>
                <a href="/seo?tab=collections" class="aside-row"><span>Collections</span><b>${(c.collections ?? 0).toLocaleString()}</b></a>
                <a href="/blog" class="aside-row"><span>Articles</span><b>${(c.articles ?? 0).toLocaleString()}</b></a>`;
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
            '<a href="/ai-seo?tab=visibility" style="color:var(--primary);">Run tracking</a>';
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
    review_draft: "Draft", regression: "Declined", refresh: "Refresh", indexing: "Indexing",
};

// "Is this working?" — 28 days against the 28 before, and what the app did.
async function loadMonthlyReport() {
    const card = document.getElementById("report-card");
    if (!card) return;
    try {
        const r = await apiRequest(`/api/dashboard/monthly-report?store_id=${dashStoreId}`);
        document.getElementById("report-headline").textContent = r.headline;
        const pct = (v) => v == null ? "—" : `<span class="${v >= 0 ? "delta-up" : "delta-down"}">${v >= 0 ? "▲" : "▼"} ${Math.abs(v).toFixed(0)}%</span>`;
        const tool = (t) => (typeof UIHistory !== "undefined" && UIHistory.toolLabel ? UIHistory.toolLabel(t) : t);
        const kinds = r.by_kind.map((k) => `<tr><td>${escapeHtml(k.kind)}</td><td>${k.clicks.toLocaleString()}</td><td>${k.clicks_prev.toLocaleString()}</td><td>${pct(k.clicks_change_pct)}</td><td>${k.impressions.toLocaleString()}</td></tr>`).join("");
        const changes = r.changes.length
            ? r.changes.map((c) => `${escapeHtml(tool(c.tool))} × ${c.n}`).join(" · ")
            : "none in this period";
        const o = r.outcomes;
        const wins = r.win_rates.map((w) => `<tr><td>${escapeHtml(tool(w.change_type))}</td><td>${w.measured}</td><td>${w.improved}</td><td>${w.declined}</td></tr>`).join("");
        document.getElementById("dash-report").innerHTML = `
            <div class="table-container"><table class="data-table"><thead><tr><th>Clicks from Google</th><th>Last 28 days</th><th>28 days before</th><th>Change</th><th>Impressions</th></tr></thead><tbody>${kinds}</tbody></table></div>
            <p style="margin:12px 0 4px;"><strong>By device:</strong> ${(r.by_device || []).map((d) => `${escapeHtml(d.device)} ${d.clicks.toLocaleString()} clicks from ${d.impressions.toLocaleString()} impressions (avg position ${d.position})`).join(" · ")}</p>
            <p style="margin:4px 0;"><strong>Orders from search:</strong> ${r.totals.orders} ($${Math.round(r.totals.revenue).toLocaleString()}) — before: ${r.totals.orders_prev} ($${Math.round(r.totals.revenue_prev).toLocaleString()})</p>
            <p style="margin:4px 0;"><strong>What the app changed:</strong> ${changes}${r.articles_published ? ` · ${r.articles_published} article${r.articles_published === 1 ? "" : "s"} published` : ""}</p>
            <p style="margin:4px 0;"><strong>How changes turned out:</strong> ${o.improved} improved · ${o.declined} declined · ${o.collecting} still being measured</p>
            ${wins ? `<div class="table-container" style="margin-top:8px;"><table class="data-table"><thead><tr><th>Kind of change</th><th>Measured</th><th>Improved</th><th>Declined</th></tr></thead><tbody>${wins}</tbody></table></div>` : ""}
            ${r.indexing.checked ? `<p style="margin:8px 0 0;"><strong>Indexing:</strong> ${r.indexing.not_indexed} of ${r.indexing.checked} checked pages are not indexed by Google. <a href="/gsc?tab=indexing">See which</a></p>` : ""}`;
        card.style.display = "";
    } catch (e) {
        card.style.display = "none";
    }
}

// The weekly plan takes its top fixes from the queue Home already loaded
// (building the queue is the slow part; doing it twice doubled the wait).
let dashQueueResolve = () => {};
let dashQueuePromise = Promise.resolve(null);

async function loadActionQueue() {
    dashQueuePromise = new Promise((resolve) => { dashQueueResolve = resolve; });
    try {
        if (!dashStoreId) { dashQueueResolve(null); return; }
        const data = await apiRequest(`/api/dashboard/action-queue?store_id=${dashStoreId}&limit=15`);
        dashQueueResolve(data);
        const container = document.getElementById("dash-queue");
        const card = document.getElementById("queue-card");

        const totals = data.total_by_source || {};
        const totalCount = Object.values(totals).reduce((a, b) => a + b, 0);
        // total_items is the pre-truncation queue size — the number the
        // sidebar badge shows. Say so when the list is cut short, otherwise
        // the badge looks wrong against a shorter list.
        const queued = data.total_items != null ? data.total_items : data.items.length;
        const shownNote = queued > data.items.length
            ? `showing ${data.items.length} of ${queued} queued · `
            : "";
        const failed = data.failed_sources || [];
        const failedNote = failed.length
            ? ` · could not check ${failed.join(", ")} (see the log), so this list may be incomplete`
            : "";
        document.getElementById("queue-note").textContent = (data.items.length
            ? `${shownNote}${queued} queued from ${totalCount.toLocaleString()} tracked opportunities`
            : "") + failedNote;

        const ms = data.measuring_summary || {};
        const measuringTotal = (ms.products_measuring || 0) + (ms.collections_measuring || 0);
        const measuringNote = measuringTotal
            ? `<div style="padding:10px 12px;margin:6px 0 2px;border:1px solid var(--outline);border-radius:8px;background:var(--surface-dim);font-size:12.5px;color:var(--on-surface-secondary);">
                <span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;color:var(--primary);">schedule</span>
                ${ms.products_measuring || 0} of ${ms.products_total || 0} products and ${ms.collections_measuring || 0} of ${ms.collections_total || 0} collections were applied to Shopify in the last 28 days — Google is re-measuring them.
                ${ms.next_complete_days !== null && ms.next_complete_days !== undefined ? `Oldest re-measure completes in ${ms.next_complete_days} day${ms.next_complete_days === 1 ? "" : "s"}.` : ""}
               </div>`
            : "";

        if (!data.items.length && failed.length) {
            container.innerHTML = `<div style="padding:16px 0;color:var(--on-surface-secondary);font-size:13px;">
                <span class="material-symbols-rounded" style="vertical-align:middle;color:var(--warning);">warning</span>
                The queue could not be built: ${escapeHtml(failed.join(", "))} failed. Reload in a minute; if it persists, check the app log.</div>`;
            card.style.display = "";
            return;
        }
        if (!data.items.length) {
            container.innerHTML = `<div style="padding:16px 0;color:var(--on-surface-secondary);font-size:13px;">
                <span class="material-symbols-rounded" style="vertical-align:middle;color:var(--success);">check_circle</span>
                ${(data.measuring || []).length
                    ? "Nothing to act on right now — recent changes are still being re-measured by Google."
                    : "Nothing in the queue. Next steps that add traffic: review <a href=\"/content-gaps\">content gaps</a>, check <a href=\"/keyword-strategy\">keyword coverage</a>, or <a href=\"/blog?tab=create\">write a post</a>."}</div>`
                + measuringNote + renderMeasuringBand(data.measuring || []);
            card.style.display = "";
            return;
        }

        // Work bands: concrete per-item fixes lead, aggregate reviews follow,
        // content creation last. Quick wins expand by default.
        const BANDS = [
            { key: "attention", label: "Needs you now", verb: "Review", sources: ["review_draft", "regression"], open: true },
            { key: "quick", label: "Quick wins", verb: "Fix", sources: ["priority", "ctr", "refresh"], open: true },
            { key: "review", label: "Needs review", verb: "Review", sources: ["indexing", "cannibalization", "technical", "competitor", "links"], open: false },
            { key: "create", label: "Create content", verb: "Draft", sources: ["gaps"], open: false },
        ];
        const sevBadge = { high: "badge-error", med: "badge-warning", low: "badge-info" };
        const row = (item, verb) => {
            const fix = item.fix || null;
            if (item.source === "refresh") verb = "Refresh";
            else if (item.source === "review_draft") verb = "Review & approve";
            if (fix) {
                if (fix.kind === "ctr_rewrite") verb = item.item_id ? "Fix title" : "Review";
                else if (fix.kind === "striking_distance") verb = "Optimize";
            }
            const kwChip = fix && fix.keyword
                ? `<span class="kw-chip" title="Target keyword">${escapeHtml(fix.keyword)}</span>`
                : "";
            const dismiss = item.action_key
                ? `<button class="icon-btn" title="Snooze 7 days" onclick="event.stopPropagation();dismissQueueItem('${escapeAttr(item.action_key)}', 7, this)"><span class="material-symbols-rounded" style="font-size:16px;">snooze</span></button>
                   <button class="icon-btn" title="Dismiss" onclick="event.stopPropagation();dismissQueueItem('${escapeAttr(item.action_key)}', null, this)"><span class="material-symbols-rounded" style="font-size:16px;">close</span></button>`
                : "";
            const src = `<span class="badge ${sevBadge[item.severity] || "badge-info"}">${QUEUE_SOURCE_LABELS[item.source] || item.source}</span>`;
            const why = `<div class="queue-why">${escapeHtml(item.detail || "")}${kwChip ? " " + kwChip : ""}</div>`;
            const main = item.serp
                ? UISerp.html({ domain: dashDomain, path: item.serp.path, title: item.serp.title,
                    description: item.serp.description, own: item.serp.own_description, href: item.href, lead: src }) + why
                : `<div class="serp-url">${src}</div><a class="queue-title" href="${escapeAttr(item.href)}">${escapeHtml(item.title)}</a>${why}`;
            const gainHtml = item.est_clicks_gain
                ? `<div class="queue-gain">+${item.est_clicks_gain}<small>clicks / mo</small></div>`
                : `<div class="queue-gain queue-gain-none"></div>`;
            return `<div class="queue-row">
                <div class="queue-main">${main}</div>
                <div class="queue-side">
                    ${gainHtml}
                    <div class="queue-actions">${dismiss}<a href="${escapeAttr(item.href)}" class="btn btn-secondary btn-sm">${verb}</a></div>
                </div>
            </div>`;
        };

        let html = "";
        const placed = new Set();
        for (const band of BANDS) {
            const items = data.items.filter((i) => band.sources.includes(i.source));
            items.forEach((i) => placed.add(i));
            if (!items.length) continue;
            html += `<details class="queue-band" ${band.open ? "open" : ""}>
                <summary><span class="material-symbols-rounded">expand_more</span>${band.label}<span class="band-count">${items.length}</span></summary>
                ${items.map((i) => row(i, band.verb)).join("")}
            </details>`;
        }
        const rest = data.items.filter((i) => !placed.has(i));
        if (rest.length) html += rest.map((i) => row(i, "Open")).join("");
        html += measuringNote + renderMeasuringBand(data.measuring || []);
        container.innerHTML = html;
        card.style.display = "";
    } catch {
        dashQueueResolve(null);
        // Queue unavailable — card stays hidden
    }
}

// Applied-to-Shopify pages waiting on fresh GSC data: informational only —
// no action buttons, so the queue never nags about work already done.
function renderMeasuringBand(rows) {
    if (!rows.length) return "";
    const typeB = { product: "badge-success", collection: "badge-warning" };
    return `<details style="margin:4px 0;">
        <summary style="cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;padding:8px 0;font-weight:600;font-size:12.5px;color:var(--on-surface-secondary);">
            <span class="material-symbols-rounded" style="font-size:16px;">expand_more</span>Re-measuring after optimization
            <span class="badge" style="background:var(--surface-dim);color:var(--on-surface);">${rows.length}</span>
        </summary>
        ${rows.map((m) => `<div class="queue-row" style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--outline);opacity:0.75;">
            <span class="badge ${typeB[m.item_type] || "badge-info"}" style="min-width:64px;text-align:center;">${m.item_type}</span>
            <div style="flex:1;min-width:0;">
                <div style="font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(m.title)}</div>
                <div style="font-size:11px;color:var(--on-surface-secondary);">
                    applied ${m.applied_days_ago ?? "?"}d ago · re-measure completes in ${m.days_remaining ?? "?"}d${m.target_keyword ? ` · target: ${escapeHtml(m.target_keyword)}` : ""}</div>
            </div>
            <a href="${escapeAttr(m.href)}" class="btn btn-ghost btn-sm" onclick="event.stopPropagation()">View</a>
        </div>`).join("")}
    </details>`;
}

async function dismissQueueItem(actionKey, snoozeDays, btn) {
    if (btn) btn.disabled = true;
    try {
        await apiRequest(`/api/dashboard/action-queue/dismiss?store_id=${dashStoreId}`, {
            method: "POST",
            body: JSON.stringify({ action_key: actionKey, snooze_days: snoozeDays }),
        });
        const row = btn && btn.closest(".queue-row");
        if (row) row.remove();
        showToast(snoozeDays ? `Snoozed for ${snoozeDays} days` : "Dismissed", "success");
        // The sidebar count is server-cached — without this it keeps showing
        // the pre-dismissal number until the next poll. NavBadges is a
        // top-level const in ui.js, so it is a global binding but NOT a
        // window property — check the identifier, not window.
        if (typeof NavBadges !== "undefined") NavBadges.refresh();
    } catch (err) {
        showToast(`Failed: ${err.message}`, "error");
        if (btn) btn.disabled = false;
    }
}

async function loadSetupChecklist() {
    try {
        const data = await apiRequest(`/api/dashboard/setup-state?store_id=${dashStoreId || ""}`);
        if (!data.steps || !data.steps.length || data.complete) return;
        const html = data.steps
            .map((s) => `<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--outline);">
                <span class="material-symbols-rounded" style="font-size:19px;color:${s.done ? "var(--success)" : "var(--on-surface-secondary)"};">${s.done ? "check_circle" : "radio_button_unchecked"}</span>
                <span style="flex:1;font-size:13px;${s.done ? "text-decoration:line-through;color:var(--on-surface-secondary);" : ""}">${escapeHtml(s.label)}</span>
                ${s.done ? "" : `<a href="${s.href}" class="btn btn-secondary btn-sm">Go</a>`}
            </div>`)
            .join("");
        document.getElementById("dash-setup").innerHTML = html;
        document.getElementById("setup-card").style.display = "";
    } catch {
        // Checklist unavailable — card stays hidden
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
                ? `<a href="/seo?optimize=${c.matched_item.item_type}&id=${c.matched_item.id}&store_id=${dashStoreId || ""}" style="color:var(--primary);">${escapeHtml(c.matched_item.title)}</a>`
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
        const totalGain = data.items.reduce((a, c) => a + (c.est_clicks_gain || 0), 0);
        document.getElementById("ctr-summary-note").textContent =
            `${data.items.length} page${data.items.length !== 1 ? "s" : ""} could gain ~${totalGain} clicks/mo — expand for details`;
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
    visibility_run: ["AI visibility", "/ai-seo?tab=visibility"],
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
            fresh += `<a href="${href}" class="aside-row" title="${label}: ${age}">
                <span><i class="dot" style="background:${dotColor[entry.level] || dotColor.none}"></i>${label}</span><span>${age}</span></a>`;
        }
        // Background jobs that failed or stopped running. Quiet when healthy.
        const jobColor = { failed: "var(--error)", warning: "var(--warning)", overdue: "var(--warning)" };
        const jobWord = { failed: "failed", warning: "had problems", overdue: "last worked" };
        let jobs = "";
        for (const j of data.jobs || []) {
            const when = j.at ? formatRelativeTime(j.at) : "";
            const detail = j.error ? `<div class="job-error">${escapeHtml(j.error)}</div>` : "";
            jobs += `<div class="job-row" title="${escapeHtml(j.error || "")}">
                <div class="aside-row"><span><i class="dot" style="background:${jobColor[j.kind]}"></i>${escapeHtml(j.label)}</span>
                <span>${jobWord[j.kind]} ${when}</span></div>${detail}</div>`;
        }
        const freshEl = document.getElementById("dash-freshness");
        freshEl.innerHTML = jobs + fresh;
        freshEl.style.display = "";

        // Content pipeline
        const p = data.pipeline || {};
        const pipelineRow = (label, value, href) =>
            `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--outline);">
                <a href="${href}" style="color:inherit;">${label}</a><strong>${value}</strong></div>`;
        document.getElementById("dash-pipeline").innerHTML =
            pipelineRow("Scheduled news", p.news_scheduled ?? 0, "/news") +
            pipelineRow("Drafts in progress", p.news_drafts ?? 0, "/news") +
            pipelineRow("Topic ideas waiting", p.topics_accepted ?? 0, "/blog?tab=create") +
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
            bar("Schema pushed", c.schema_pushed_pct, "/seo?tab=schema") +
            bar("Keywords mapped", c.keyword_mapped_pct, "/keyword-strategy") +
            `<div style="padding:5px 0;font-size:12px;display:flex;justify-content:space-between;"><a href="/ai-seo?tab=assets" style="color:inherit;">AEO assets</a><span>${aeo.published ?? 0}/${aeo.total ?? 0} published</span></div>`;

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

        const rowHtml = (item) => {
            const [icon, color, label] = CHANGE_EVENT_META[item.event_type] || ["info", "var(--on-surface-secondary)", item.event_type];
            const detail = changeDetail(item);
            const subject = (item.subject || "").replace(/^https?:\/\/[^/]+/, "");
            return `<div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--outline);">
                <span class="material-symbols-rounded" style="font-size:18px;color:${color};">${icon}</span>
                <div style="flex:1;min-width:0;">
                    <div style="font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"><strong>${label}:</strong> ${escapeHtml(subject)}</div>
                    ${detail ? `<div style="font-size:11px;color:var(--on-surface-secondary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(detail)}</div>` : ""}
                </div>
                <span style="font-size:11px;color:var(--on-surface-secondary);white-space:nowrap;">${formatRelativeTime(item.created_at)}</span>
            </div>`;
        };
        // Top 5 visible; the full feed stays one click away behind the expander.
        let html = data.items.slice(0, 5).map(rowHtml).join("");
        if (data.items.length > 5) {
            html += `<details style="margin-top:4px;">
                <summary style="cursor:pointer;list-style:none;font-size:12px;color:var(--primary);padding:6px 0;">View all ${data.items.length} events</summary>
                ${data.items.slice(5).map(rowHtml).join("")}
            </details>`;
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

async function loadRevenue() {
    try {
        if (!dashStoreId) return;
        const data = await apiRequest(`/api/dashboard/revenue?store_id=${dashStoreId}&days=28`);
        const kpiCard = document.getElementById("kpi-revenue-card");

        if (!data.available) {
            if (data.scope_missing) {
                kpiCard.style.display = "";
                document.getElementById("kpi-revenue").textContent = "—";
                document.getElementById("kpi-revenue-sub").innerHTML =
                    'Token needs <code>read_orders</code> scope';
            }
            return;
        }

        // KPI card
        kpiCard.style.display = "";
        document.getElementById("kpi-revenue").textContent =
            `$${Math.round(data.organic_revenue).toLocaleString()}`;
        let sub = `${data.organic_orders} orders${data.aov ? ` · $${data.aov.toFixed(0)} AOV` : ""}`;
        if (data.pct_change !== null && data.pct_change !== undefined) {
            const up = data.pct_change >= 0;
            sub += ` <span class="${up ? "delta-up" : "delta-down"}">${up ? "▲" : "▼"} ${Math.abs(data.pct_change).toFixed(1)}%</span>`;
        }
        document.getElementById("kpi-revenue-sub").innerHTML = sub;

        if (!data.trend || !data.trend.length) return;

        // Widget: trend chart + top pages + attribution split
        const colors = chartColors();
        setChart("revenue", () => makeTimeSeriesChart(document.getElementById("revenue-chart"), {
            labels: data.trend.map((d) => d.date.slice(5)),
            datasets: [{
                label: "Organic revenue",
                data: data.trend.map((d) => d.revenue),
                borderColor: colors.success,
                backgroundColor: hexToRgba(colors.success, 0.12),
                fill: true, borderWidth: 1.5, pointRadius: 0, tension: 0.3,
            }],
        }));

        const split = data.attribution_split || {};
        const splitOrder = ["organic", "paid", "referral", "direct", "ai", "unknown"];
        document.getElementById("revenue-split").textContent = splitOrder
            .filter((k) => split[k])
            .map((k) => `${k}: $${Math.round(split[k].revenue).toLocaleString()}`)
            .join(" · ");

        let pages = '<div style="font-size:11px;font-weight:600;color:var(--on-surface-secondary);padding:6px 0;">TOP LANDING PAGES</div>';
        for (const pg of data.top_pages || []) {
            pages += `<div style="display:flex;justify-content:space-between;gap:8px;padding:4px 0;border-bottom:1px solid var(--outline);">
                <span style="min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="/${escapeHtml(pg.landing_path)}">${escapeHtml(pg.title || "/" + pg.landing_path)}</span>
                <span style="white-space:nowrap;color:var(--success);">$${Math.round(pg.revenue).toLocaleString()}</span>
            </div>`;
        }
        document.getElementById("revenue-top-pages").innerHTML = pages;
        document.getElementById("revenue-card").style.display = "";
    } catch {
        // Revenue not available — cards stay hidden
    }
}


// ─── This week: one short list from the queue, calendar, links, duplicates ───

async function loadWeek() {
    const card = document.getElementById("week-card");
    try {
        const [d, queue] = await Promise.all([
            apiRequest(`/api/dashboard/week?store_id=${dashStoreId || ""}&fixes=0`),
            dashQueuePromise,
        ]);
        const fixes = ((queue && queue.items) || []).slice(0, 5).map((it) => ({
            kind: "fix", text: it.title || "Fix", href: it.href, count: it.est_clicks_gain }));
        d.lines = fixes.concat(d.lines || []);
        if (queue && queue.failed_sources && queue.failed_sources.length) d.queue_failed = queue.failed_sources;
        const fmt = (iso) => new Date(iso + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
        document.getElementById("week-note").textContent = d.week ? `${fmt(d.week.from)} – ${fmt(d.week.to)}` : "";
        const icon = { fix: "build", approve: "task_alt", publish: "event", links: "add_link", duplicates: "content_copy" };
        const lines = (d.lines || []).map((l) => `<li style="display:flex;gap:8px;align-items:flex-start;padding:4px 0;">
            <span class="material-symbols-rounded" style="font-size:16px;color:var(--on-surface-secondary);margin-top:1px;">${icon[l.kind] || "chevron_right"}</span>
            <span style="flex:1;font-size:13px;">${l.href ? `<a href="${escapeHtml(l.href)}">${escapeHtml(l.text)}</a>` : escapeHtml(l.text)}
                ${l.kind === "fix" && l.count ? `<span style="color:var(--on-surface-secondary);font-size:12px;"> · ~${l.count} clicks/mo</span>` : ""}</span></li>`).join("");
        const failed = (d.queue_failed || []).length ? `<p style="font-size:12px;color:var(--error);margin:4px 0 0;">Couldn't check: ${escapeHtml(d.queue_failed.join(", "))}</p>` : "";
        document.getElementById("dash-week").innerHTML = (lines ? `<ul style="list-style:none;margin:0;padding:0;">${lines}</ul>` : '<p class="aside-text">Nothing planned — the queue is clear.</p>')
            + failed + `<p style="font-size:12px;color:var(--on-surface-secondary);margin:6px 0 0;">${d.changed_this_week || 0} change${d.changed_this_week === 1 ? "" : "s"} made since Monday.</p>`;
        card.style.display = "";
    } catch (err) {
        console.error("Weekly plan failed:", err);
    }
}

// ─── Goals: keyword families the store is trying to win ───

function goalDelta(v, invert = false, suffix = "") {
    if (v === null || v === undefined || v === 0) return "";
    const good = invert ? v < 0 : v > 0;
    return ` <span style="font-size:11.5px;color:${good ? "var(--success)" : "var(--error)"};">${v > 0 ? "+" : ""}${v}${suffix}</span>`;
}

async function loadGoals() {
    const card = document.getElementById("goals-card");
    const box = document.getElementById("dash-goals");
    try {
        const d = await apiRequest(`/api/dashboard/goals?store_id=${dashStoreId || ""}`);
        if (!d.goals.length) {
            box.innerHTML = `<p class="aside-text">Pick up to ${d.max || 10} keyword families you want to win (for example a brand), and Home tracks their clicks, position and top-10 keywords.</p>`;
        } else {
            box.innerHTML = `<div style="font-size:11.5px;color:var(--on-surface-secondary);margin-bottom:4px;">Last ${d.window_days} days vs the ${d.window_days} before</div>`
                + d.goals.map((g) => `<div style="padding:5px 0;border-top:1px solid var(--outline);font-size:13px;">
                    <a href="/keyword-strategy?tab=explorer&family=${g.cluster_id}" style="font-weight:600;">${escapeHtml(g.name)}</a>
                    <div style="font-size:12px;color:var(--on-surface-secondary);">${g.now.clicks.toLocaleString()} clicks${goalDelta(g.change.clicks)}
                        · pos ${g.now.position ?? "—"}${goalDelta(g.change.position, true)}
                        · ${g.now.top10} in top 10${goalDelta(g.change.top10)}</div></div>`).join("");
        }
        card.style.display = "";
    } catch (err) {
        console.error("Goals failed:", err);
    }
}

async function openGoalPicker() {
    let d;
    try {
        d = await apiRequest(`/api/dashboard/goals/options?store_id=${dashStoreId || ""}`);
    } catch (err) {
        showToast(err.message, "error");
        return;
    }
    if (!d.families.length) {
        showToast("No keyword families yet — build them in Keyword Strategy first", "info");
        return;
    }
    const rows = d.families.map((f) => `<label style="display:flex;gap:8px;align-items:center;padding:4px 0;font-size:13px;cursor:pointer;">
        <input type="checkbox" value="${f.id}" ${f.chosen ? "checked" : ""}> <span style="flex:1;">${escapeHtml(f.name)}</span>
        <span style="color:var(--on-surface-secondary);font-size:12px;">${(f.total_volume || 0).toLocaleString()} searches/mo · ${f.keyword_count} keywords</span></label>`).join("");
    const modal = UIModal.open({
        title: "Goals",
        bodyHtml: `<p style="font-size:13px;margin:0 0 8px;">Choose up to ${d.max} keyword families to track on Home.</p>
            <div style="max-height:50vh;overflow:auto;">${rows}</div>`,
        footerHtml: `<button class="btn btn-secondary" data-ui-cancel>Cancel</button><button class="btn btn-primary" data-save>Save</button>`,
    });
    const boxes = () => [...modal.el.querySelectorAll("input[type=checkbox]")];
    boxes().forEach((b) => b.addEventListener("change", () => {
        if (boxes().filter((x) => x.checked).length > d.max) { b.checked = false; showToast(`Up to ${d.max} goals`, "info"); }
    }));
    modal.el.querySelector("[data-ui-cancel]").addEventListener("click", () => modal.close());
    modal.el.querySelector("[data-save]").addEventListener("click", async () => {
        const ids = boxes().filter((x) => x.checked).map((x) => parseInt(x.value, 10));
        try {
            await apiRequest("/api/dashboard/goals", { method: "POST", body: JSON.stringify({ store_id: dashStoreId, cluster_ids: ids }) });
            modal.close();
            loadGoals();
        } catch (err) {
            showToast(err.message, "error");
        }
    });
}
