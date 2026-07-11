let dashStoreId = null;

document.addEventListener("DOMContentLoaded", async () => {
    try {
        const stores = await apiRequest("/api/stores");
        if (stores.length) dashStoreId = stores[0].id;
    } catch {}
    loadStats();
    loadGSCSummary();
    loadTopPriorities();
});

async function loadStats() {
    try {
        const url = dashStoreId ? `/api/dashboard/stats?store_id=${dashStoreId}` : "/api/dashboard/stats";
        const stats = await apiRequest(url);

        // Store identity + sync freshness
        if (stats.store && stats.store.name) {
            const line = document.getElementById("dash-store-line");
            const synced = stats.store.last_synced_at
                ? `Last synced ${formatRelativeTime(stats.store.last_synced_at)}`
                : "Never synced";
            line.innerHTML = `<strong>${escapeHtml(stats.store.name)}</strong>` +
                (stats.store.domain ? ` · ${escapeHtml(stats.store.domain)}` : "") +
                ` · ${escapeHtml(synced)}`;
            line.style.display = "";
        }

        document.getElementById("stat-products").textContent = stats.products.toLocaleString();
        if (stats.active_products !== undefined && stats.active_products !== stats.products) {
            document.getElementById("stat-products-sub").textContent = `${stats.active_products.toLocaleString()} active`;
        }
        document.getElementById("stat-collections").textContent = stats.collections.toLocaleString();
        document.getElementById("stat-articles").textContent = (stats.articles ?? 0).toLocaleString();

        document.getElementById("stat-seo").textContent = stats.avg_seo_score;
        const dist = stats.score_distribution;
        if (dist) {
            document.getElementById("stat-seo-sub").textContent =
                `${dist.good} good · ${dist.fair} fair · ${dist.poor} poor`;
        }

        document.getElementById("stat-tasks").textContent = (stats.ai_tasks_30d ?? 0).toLocaleString();
        if (stats.tasks_running > 0) {
            document.getElementById("stat-tasks-sub").innerHTML =
                `<span class="delta-up">${stats.tasks_running} running now</span>`;
        }

        if (stats.insights) {
            renderInsights(stats.insights);
        }
    } catch (err) {
        showToast("Failed to load dashboard stats", "error");
    }
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

function renderDelta(el, pct, invert = false) {
    if (pct === null || pct === undefined || !isFinite(pct)) { el.textContent = ""; return; }
    const up = pct >= 0;
    const good = invert ? !up : up;
    const arrow = up ? "▲" : "▼";
    el.innerHTML = `<span class="${good ? "delta-up" : "delta-down"}">${arrow} ${Math.abs(pct).toFixed(1)}% vs prev period</span>`;
}

async function loadGSCSummary() {
    try {
        if (!dashStoreId) return;
        const status = await apiRequest(`/api/gsc/status?store_id=${dashStoreId}`);
        if (!status.connected || !status.property) return;

        const today = new Date();
        const dateTo = new Date(today);
        dateTo.setDate(dateTo.getDate() - 3);
        const dateFrom = new Date(dateTo);
        dateFrom.setDate(dateFrom.getDate() - 27); // 28-day window inclusive
        const df = dateFrom.toISOString().split("T")[0];
        const dt = dateTo.toISOString().split("T")[0];

        const data = await apiRequest(`/api/gsc/overview?store_id=${dashStoreId}&date_from=${df}&date_to=${dt}`);
        document.getElementById("dash-gsc-window").textContent = `${df} → ${dt}`;
        document.getElementById("dash-gsc-clicks").textContent = Math.round(data.total_clicks).toLocaleString();
        document.getElementById("dash-gsc-impressions").textContent = Math.round(data.total_impressions).toLocaleString();
        document.getElementById("dash-gsc-ctr").textContent = (data.avg_ctr * 100).toFixed(1) + "%";
        document.getElementById("dash-gsc-position").textContent = data.avg_position.toFixed(1);

        if (data.changes) {
            renderDelta(document.getElementById("dash-gsc-clicks-delta"), data.changes.clicks_pct);
            renderDelta(document.getElementById("dash-gsc-impressions-delta"), data.changes.impressions_pct);
            const ctrEl = document.getElementById("dash-gsc-ctr-delta");
            if (data.changes.ctr !== null && data.changes.ctr !== undefined) {
                const up = data.changes.ctr >= 0;
                ctrEl.innerHTML = `<span class="${up ? "delta-up" : "delta-down"}">${up ? "▲" : "▼"} ${(Math.abs(data.changes.ctr) * 100).toFixed(2)}pp</span>`;
            }
            const posEl = document.getElementById("dash-gsc-position-delta");
            if (data.changes.position !== null && data.changes.position !== undefined) {
                const improved = data.changes.position <= 0; // lower position = better
                posEl.innerHTML = `<span class="${improved ? "delta-up" : "delta-down"}">${improved ? "▲" : "▼"} ${Math.abs(data.changes.position).toFixed(1)}</span>`;
            }
        }
        document.getElementById("gsc-summary").style.display = "";
    } catch {
        // GSC not connected or error — leave hidden
    }
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
