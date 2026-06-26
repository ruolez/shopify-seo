(function () {
    let currentStoreId = null;
    let analysisData = null;
    let activeTab = loadFilter('interlinkTab', 'overview');
    let aiRecsHtml = null;
    let currentFixes = [];
    let currentCrosslinks = [];
    let appliedFixes = new Set();
    let brokenLinkGroups = [];
    let fixedBrokenGroups = new Set();
    let availableTargets = null;

    async function init() {
        await loadStores();
        const sel = document.getElementById('store-select');
        sel.addEventListener('change', () => {
            currentStoreId = sel.value ? parseInt(sel.value) : null;
            availableTargets = null;
            saveFilter('interlinkStore', currentStoreId);
            if (currentStoreId) loadAnalysis();
            else renderEmpty();
        });
    }

    async function loadStores() {
        try {
            const stores = await apiRequest('/api/stores');
            const sel = document.getElementById('store-select');
            stores.forEach(s => {
                const opt = document.createElement('option');
                opt.value = s.id;
                opt.textContent = s.name || s.domain;
                sel.appendChild(opt);
            });
            const savedStore = loadFilter('interlinkStore', null);
            const matchedStore = savedStore && stores.find(s => s.id === savedStore);
            if (matchedStore) {
                sel.value = matchedStore.id;
                currentStoreId = matchedStore.id;
                loadAnalysis();
            } else if (stores.length === 1) {
                sel.value = stores[0].id;
                currentStoreId = stores[0].id;
                loadAnalysis();
            }
        } catch (e) {
            showToast('Failed to load stores', 'error');
        }
    }

    async function loadAnalysis() {
        const container = document.getElementById('interlinking-content');
        container.innerHTML = '<div class="loading-spinner"><div class="spinner"></div><p>Loading analysis...</p></div>';

        try {
            const resp = await apiRequest(`/api/interlinking/analysis?store_id=${currentStoreId}`);
            analysisData = resp.analysis;
            if (analysisData) {
                renderDashboard(analysisData, resp.created_at);
            } else {
                renderNoAnalysis();
            }
        } catch (e) {
            container.innerHTML = '<div class="empty-state"><span class="material-symbols-rounded">error</span><p>Failed to load analysis.</p></div>';
        }
    }

    function renderEmpty() {
        document.getElementById('interlinking-content').innerHTML =
            '<div class="empty-state"><span class="material-symbols-rounded">link</span><p>Select a store and run analysis to view interlinking data.</p></div>';
    }

    function renderNoAnalysis() {
        document.getElementById('interlinking-content').innerHTML = `
            <div class="empty-state">
                <span class="material-symbols-rounded">link_off</span>
                <p>No analysis data yet. Click "Analyze" to scan your store's internal links.</p>
                <button class="btn btn-primary" onclick="window._runAnalysis()">
                    <span class="material-symbols-rounded">search</span> Analyze Interlinking
                </button>
            </div>`;
    }

    function renderDashboard(data, createdAt) {
        const s = data.summary;
        const container = document.getElementById('interlinking-content');

        const orphanProductsCount = (data.orphan_products || []).length;
        const orphanCollectionsCount = (data.orphan_collections || []).length;
        const orphanArticlesCount = (data.orphan_articles || []).length;
        const underLinkedCount = (data.under_linked || []).length;
        const notInCollectionCount = (data.not_in_collection || []).length;
        const crossLinksCount = (data.missing_crosslinks || []).length;
        const brokenLinksCount = (data.broken_links || []).length;
        currentBrokenLinks = data.broken_links || [];
        fixedBrokenLinks = new Set();

        let html = `
            <div class="interlink-action-bar" style="display: flex; align-items: center; gap: 12px; margin-bottom: 24px; flex-wrap: wrap;">
                <button class="btn btn-primary" onclick="window._runAnalysis()">
                    <span class="material-symbols-rounded">refresh</span> Re-Analyze
                </button>
                <button class="btn btn-secondary" onclick="window._getRecommendations()">
                    <span class="material-symbols-rounded">auto_awesome</span> AI Recommendations
                </button>
                <select id="batch-size-select" style="padding: 8px 12px; border-radius: var(--radius-sm); border: 1px solid var(--outline); background: var(--surface); font-size: 13px; color: var(--on-surface);" title="Number of suggestions to generate">
                    <option value="10">10 suggestions</option>
                    <option value="20">20 suggestions</option>
                    <option value="30">30 suggestions</option>
                    <option value="50">50 suggestions</option>
                    <option value="100">100 suggestions</option>
                    <option value="200">200 suggestions</option>
                </select>
                ${createdAt ? `<span style="font-size: 13px; color: var(--on-surface-secondary); margin-left: auto;">Last analyzed: ${formatDate(createdAt)}</span>` : ''}
            </div>`;

        html += renderSummaryStats(s);

        const tabs = [
            { id: 'overview', label: 'Overview' },
            { id: 'orphan-products', label: 'Orphan Products', count: orphanProductsCount },
            { id: 'orphan-collections', label: 'Orphan Collections', count: orphanCollectionsCount },
            { id: 'orphan-articles', label: 'Orphan Articles', count: orphanArticlesCount },
            { id: 'under-linked', label: 'Under-Linked', count: underLinkedCount },
            { id: 'not-in-collection', label: 'Not in Collection', count: notInCollectionCount },
            { id: 'cross-links', label: 'Cross-Links', count: crossLinksCount },
            { id: 'broken-links', label: 'Broken Links', count: brokenLinksCount },
            { id: 'ai-recs', label: 'AI Recs' },
        ];

        html += '<div class="interlink-tabs">';
        tabs.forEach(t => {
            const isActive = t.id === activeTab ? ' active' : '';
            const countBadge = t.count !== undefined ? ` (${t.count})` : '';
            html += `<button class="tab${isActive}" data-tab="${t.id}" onclick="window._switchInterlinkTab('${t.id}')">${escapeHtml(t.label)}${countBadge}</button>`;
        });
        html += '</div>';

        html += renderOverviewPanel(s, data);
        html += renderTabPanel('orphan-products', renderSectionContent('Orphan Products', 'warning', 'No incoming links and not in any collection', data.orphan_products, 'product'));
        html += renderTabPanel('orphan-collections', renderSectionContent('Orphan Collections', 'warning', 'No incoming links from other pages', data.orphan_collections, 'collection'));
        html += renderTabPanel('orphan-articles', renderSectionContent('Orphan Articles', 'warning', 'No incoming links from any page', data.orphan_articles, 'article'));
        html += renderTabPanel('under-linked', renderSectionContent('Under-Linked Pages', 'info', 'Few incoming links (1 only)', data.under_linked, 'mixed'));
        html += renderTabPanel('not-in-collection', renderNotInCollectionContent(data.not_in_collection));
        html += renderTabPanel('cross-links', renderMissingCrossLinksContent(data.missing_crosslinks));
        html += renderTabPanel('broken-links', renderBrokenLinksContent(data.broken_links));
        html += renderAiRecsPanel();

        container.innerHTML = html;

        if (brokenLinkGroups.length > 0) {
            _loadAvailableTargets();
        }
    }

    function renderTabPanel(id, content) {
        const isActive = id === activeTab ? ' active' : '';
        return `<div class="tab-panel${isActive}" id="interlink-tab-${id}">${content}</div>`;
    }

    function renderOverviewPanel(s, data) {
        const isActive = activeTab === 'overview' ? ' active' : '';
        const dist = s.score_distribution || {};
        const total = (dist.excellent || 0) + (dist.good || 0) + (dist.fair || 0) + (dist.poor || 0);

        let html = `<div class="tab-panel${isActive}" id="interlink-tab-overview">`;
        html += '<div class="card">';
        html += '<div class="card-header"><h3>Health Summary</h3></div>';

        if (total > 0) {
            const bars = [
                { label: 'Excellent (80-100)', count: dist.excellent || 0, cls: 'high' },
                { label: 'Good (60-79)', count: dist.good || 0, cls: 'high' },
                { label: 'Fair (40-59)', count: dist.fair || 0, cls: 'medium' },
                { label: 'Poor (0-39)', count: dist.poor || 0, cls: 'low' },
            ];
            bars.forEach(b => {
                const pct = Math.round((b.count / total) * 100);
                html += `
                    <div class="score-row">
                        <div class="score-label">${b.label}</div>
                        <div class="score-bar"><div class="score-bar-fill ${b.cls}" style="width: ${pct}%"></div></div>
                        <div class="score-value">${b.count}</div>
                    </div>`;
            });
        } else {
            html += '<p style="color: var(--on-surface-secondary); font-size: 14px;">No score distribution data available.</p>';
        }

        html += '<div style="margin-top: 24px; display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">';
        const issues = [
            { label: 'Orphan Products', count: (data.orphan_products || []).length, tab: 'orphan-products', icon: 'link_off' },
            { label: 'Orphan Collections', count: (data.orphan_collections || []).length, tab: 'orphan-collections', icon: 'folder_off' },
            { label: 'Orphan Articles', count: (data.orphan_articles || []).length, tab: 'orphan-articles', icon: 'article' },
            { label: 'Under-Linked Pages', count: (data.under_linked || []).length, tab: 'under-linked', icon: 'trending_down' },
            { label: 'Not in Collection', count: (data.not_in_collection || []).length, tab: 'not-in-collection', icon: 'inventory_2' },
            { label: 'Missing Cross-Links', count: (data.missing_crosslinks || []).length, tab: 'cross-links', icon: 'swap_horiz' },
            { label: 'Broken Links', count: (data.broken_links || []).length, tab: 'broken-links', icon: 'broken_image' },
        ];
        issues.forEach(iss => {
            const badgeCls = iss.count === 0 ? 'badge-success' : 'badge-warning';
            html += `
                <div style="display: flex; align-items: center; gap: 10px; padding: 10px 14px; background: var(--surface-dim); border-radius: var(--radius-sm); cursor: pointer;" onclick="window._switchInterlinkTab('${iss.tab}')">
                    <span class="material-symbols-rounded" style="font-size: 20px; color: var(--on-surface-secondary);">${iss.icon}</span>
                    <span style="flex: 1; font-size: 14px;">${iss.label}</span>
                    <span class="badge ${badgeCls}">${iss.count}</span>
                </div>`;
        });
        html += '</div>';

        html += '</div></div>';
        return html;
    }

    function renderSummaryStats(s) {
        const statCards = [
            { icon: 'link', color: 'blue', value: s.total_links, label: 'Total Internal Links' },
            { icon: 'link_off', color: 'red', value: s.orphan_products_count, label: 'Orphan Products', tab: 'orphan-products' },
            { icon: 'folder_off', color: 'orange', value: s.orphan_collections_count, label: 'Orphan Collections', tab: 'orphan-collections' },
            { icon: 'article', color: 'orange', value: s.orphan_articles_count || 0, label: 'Orphan Articles', tab: 'orphan-articles' },
            { icon: 'trending_up', color: 'green', value: s.avg_incoming_links, label: 'Avg Links / Page' },
            { icon: 'score', color: 'blue', value: s.avg_score, label: 'Avg Interlinking Score' },
            { icon: 'inventory_2', color: 'orange', value: s.not_in_collection_count, label: 'Not in Any Collection', tab: 'not-in-collection' },
            { icon: 'swap_horiz', color: 'red', value: s.missing_crosslinks_count, label: 'Missing Cross-Links', tab: 'cross-links' },
            { icon: 'broken_image', color: 'red', value: s.broken_links_count || 0, label: 'Broken Links', tab: 'broken-links' },
            { icon: 'newspaper', color: 'blue', value: s.total_articles || 0, label: 'Total Articles' },
            { icon: 'checklist', color: 'green', value: s.score_distribution ? s.score_distribution.excellent : 0, label: 'Well-Linked Pages' },
        ];

        let html = '<div class="stats-grid" style="margin-bottom: 8px;">';
        statCards.slice(0, 5).forEach(c => {
            const clickAttr = c.tab ? ` onclick="window._switchInterlinkTab('${c.tab}')"` : '';
            const clickClass = c.tab ? ' stat-card-clickable' : '';
            html += `
                <div class="stat-card${clickClass}"${clickAttr}>
                    <div class="stat-icon ${c.color}"><span class="material-symbols-rounded">${c.icon}</span></div>
                    <div class="stat-value">${c.value}</div>
                    <div class="stat-label">${c.label}</div>
                </div>`;
        });
        html += '</div><div class="stats-grid" style="margin-bottom: 24px;">';
        statCards.slice(5).forEach(c => {
            const clickAttr = c.tab ? ` onclick="window._switchInterlinkTab('${c.tab}')"` : '';
            const clickClass = c.tab ? ' stat-card-clickable' : '';
            html += `
                <div class="stat-card${clickClass}"${clickAttr}>
                    <div class="stat-icon ${c.color}"><span class="material-symbols-rounded">${c.icon}</span></div>
                    <div class="stat-value">${c.value}</div>
                    <div class="stat-label">${c.label}</div>
                </div>`;
        });
        html += '</div>';
        return html;
    }

    function renderSectionContent(title, badgeType, subtitle, items, itemType) {
        if (!items || items.length === 0) {
            return `
                <div class="card">
                    <div class="card-header">
                        <h3>${escapeHtml(title)}</h3>
                        <span class="badge badge-success">0 items</span>
                    </div>
                    <p style="color: var(--on-surface-secondary); font-size: 14px;">No issues found.</p>
                </div>`;
        }

        let rows = items.map(item => {
            let type;
            if (item.item_type === 'article') {
                type = 'article';
            } else if (item.in_collection !== undefined) {
                type = 'product';
            } else if (itemType === 'mixed') {
                type = item.collection_count !== undefined ? 'product' : 'collection';
            } else {
                type = itemType;
            }
            return `
                <tr style="cursor: pointer;" onclick="window._showItemDetail('${type}', ${item.id})">
                    <td>${escapeHtml(item.title)}</td>
                    <td><code>${escapeHtml(item.handle)}</code></td>
                    <td>${item.incoming_count}</td>
                    <td>${item.outgoing_count}</td>
                    <td>${getScoreBadge(item.score)}</td>
                </tr>`;
        }).join('');

        return `
            <div class="card">
                <div class="card-header">
                    <h3>${escapeHtml(title)}</h3>
                    <span class="badge badge-${badgeType}">${items.length} item${items.length !== 1 ? 's' : ''}</span>
                </div>
                <p style="color: var(--on-surface-secondary); font-size: 13px; margin-bottom: 16px;">${escapeHtml(subtitle)}</p>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Title</th>
                                <th>Handle</th>
                                <th>Incoming</th>
                                <th>Outgoing</th>
                                <th>Score</th>
                            </tr>
                        </thead>
                        <tbody>${rows}</tbody>
                    </table>
                </div>
            </div>`;
    }

    function renderNotInCollectionContent(items) {
        if (!items || items.length === 0) {
            return `
                <div class="card">
                    <div class="card-header">
                        <h3>Products Not in Any Collection</h3>
                        <span class="badge badge-success">0 items</span>
                    </div>
                    <p style="color: var(--on-surface-secondary); font-size: 14px;">All products belong to at least one collection.</p>
                </div>`;
        }

        let rows = items.map(item => `
            <tr style="cursor: pointer;" onclick="window._showItemDetail('product', ${item.id})">
                <td>${escapeHtml(item.title)}</td>
                <td><code>${escapeHtml(item.handle)}</code></td>
                <td>${item.incoming_count}</td>
                <td>${item.outgoing_count}</td>
                <td>${getScoreBadge(item.score)}</td>
            </tr>`).join('');

        return `
            <div class="card">
                <div class="card-header">
                    <h3>Products Not in Any Collection</h3>
                    <span class="badge badge-warning">${items.length} item${items.length !== 1 ? 's' : ''}</span>
                </div>
                <p style="color: var(--on-surface-secondary); font-size: 13px; margin-bottom: 16px;">These products are not assigned to any collection</p>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Title</th>
                                <th>Handle</th>
                                <th>Incoming</th>
                                <th>Outgoing</th>
                                <th>Score</th>
                            </tr>
                        </thead>
                        <tbody>${rows}</tbody>
                    </table>
                </div>
            </div>`;
    }

    function renderMissingCrossLinksContent(items) {
        if (!items || items.length === 0) {
            return `
                <div class="card">
                    <div class="card-header">
                        <h3>Missing Cross-Links</h3>
                        <span class="badge badge-success">0 pairs</span>
                    </div>
                    <p style="color: var(--on-surface-secondary); font-size: 14px;">No missing cross-links detected between collections.</p>
                </div>`;
        }

        let rows = items.map(item => `
            <tr>
                <td>${escapeHtml(item.collection_a_title)}</td>
                <td>${escapeHtml(item.collection_b_title)}</td>
                <td>${item.shared_products}</td>
                <td>${item.shared_tags}</td>
                <td>
                    ${item.a_links_to_b ? '<span class="badge badge-success">A\u2192B</span>' : '<span class="badge badge-error">Missing A\u2192B</span>'}
                    ${item.b_links_to_a ? '<span class="badge badge-success">B\u2192A</span>' : '<span class="badge badge-error">Missing B\u2192A</span>'}
                </td>
            </tr>`).join('');

        return `
            <div class="card">
                <div class="card-header">
                    <h3>Missing Cross-Links</h3>
                    <span class="badge badge-error">${items.length} pair${items.length !== 1 ? 's' : ''}</span>
                </div>
                <p style="color: var(--on-surface-secondary); font-size: 13px; margin-bottom: 16px;">Collection pairs sharing products/tags but not linking to each other</p>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Collection A</th>
                                <th>Collection B</th>
                                <th>Shared Products</th>
                                <th>Shared Tags</th>
                                <th>Link Status</th>
                            </tr>
                        </thead>
                        <tbody>${rows}</tbody>
                    </table>
                </div>
            </div>`;
    }

    function groupBrokenLinks(items) {
        const groups = {};
        (items || []).forEach(item => {
            const key = `${item.to_type}:${item.to_handle}`;
            if (!groups[key]) {
                groups[key] = {
                    to_type: item.to_type,
                    to_handle: item.to_handle,
                    to_href: item.to_href || '',
                    sources: [],
                    replacement: null,
                };
            }
            groups[key].sources.push({
                from_type: item.from_type,
                from_handle: item.from_handle,
                from_title: item.from_title,
                from_blog_handle: item.from_blog_handle || null,
                anchor_text: item.anchor_text,
            });
        });
        return Object.values(groups);
    }

    function renderBrokenLinksContent(items) {
        brokenLinkGroups = groupBrokenLinks(items);
        fixedBrokenGroups = new Set();

        if (brokenLinkGroups.length === 0) {
            return `
                <div class="card">
                    <div class="card-header">
                        <h3>Broken Links</h3>
                        <span class="badge badge-success">0 items</span>
                    </div>
                    <p style="color: var(--on-surface-secondary); font-size: 14px;">No broken links detected.</p>
                </div>`;
        }

        const totalLinks = (items || []).length;

        let rows = brokenLinkGroups.map((group, i) => {
            const toType = escapeHtml(group.to_type || 'unknown');
            const toHandle = escapeHtml(group.to_handle || '');
            const sourceCount = group.sources.length;
            const sourceTypes = [...new Set(group.sources.map(s => s.from_type))].map(t => `<span class="badge badge-info">${escapeHtml(t)}</span>`).join(' ');
            return `
                <tr>
                    <td><span class="badge badge-error">${toType}</span> <code>${toHandle}</code></td>
                    <td>${sourceCount}</td>
                    <td>${sourceTypes}</td>
                    <td>
                        <div style="position: relative;">
                            <input type="text" id="broken-replacement-${i}" class="form-control" style="font-size: 12px; padding: 4px 8px; min-width: 240px;" placeholder="Search to replace or leave empty to remove..." autocomplete="off" oninput="window._onReplacementInput(${i}, this)" onfocus="window._onReplacementFocus(${i}, this)" />
                            <div id="broken-dropdown-${i}" class="broken-link-dropdown" style="display:none; position:absolute; top:100%; left:0; right:0; max-height:220px; overflow-y:auto; background:var(--surface); border:1px solid var(--outline); border-radius:var(--radius-sm); z-index:100; box-shadow:0 4px 12px rgba(0,0,0,.15);"></div>
                        </div>
                    </td>
                    <td>
                        <button class="btn btn-primary btn-sm" id="fix-broken-btn-${i}" onclick="window._fixBrokenGroup(${i})" style="font-size: 12px; padding: 4px 12px;">Fix</button>
                    </td>
                </tr>`;
        }).join('');

        return `
            <div class="card">
                <div class="card-header">
                    <h3>Broken Links</h3>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="badge badge-error">${brokenLinkGroups.length} target${brokenLinkGroups.length !== 1 ? 's' : ''} (${totalLinks} link${totalLinks !== 1 ? 's' : ''})</span>
                        <button class="btn btn-primary btn-sm" id="fix-all-broken-btn" onclick="window._fixAllBrokenGroups()" style="font-size: 12px; padding: 4px 12px;">Fix All (${brokenLinkGroups.length})</button>
                    </div>
                </div>
                <p style="color: var(--on-surface-secondary); font-size: 13px; margin-bottom: 16px;">Grouped by broken target — fix replaces or removes the link in all sources</p>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Broken Target</th>
                                <th>Sources</th>
                                <th>Source Types</th>
                                <th>Replacement</th>
                                <th>Action</th>
                            </tr>
                        </thead>
                        <tbody>${rows}</tbody>
                    </table>
                </div>
            </div>`;
    }

    function renderAiRecsPanel() {
        const isActive = activeTab === 'ai-recs' ? ' active' : '';
        let content;
        if (aiRecsHtml) {
            content = aiRecsHtml;
        } else {
            content = `
                <div class="empty-state">
                    <span class="material-symbols-rounded">auto_awesome</span>
                    <p>Click "AI Recommendations" above to generate link suggestions.</p>
                </div>`;
        }
        return `<div class="tab-panel${isActive}" id="interlink-tab-ai-recs">${content}</div>`;
    }

    function renderRecommendations(result) {
        currentFixes = [];
        currentCrosslinks = [];
        appliedFixes = new Set();

        if (!result || result.error) {
            aiRecsHtml = `
                <div class="ai-panel">
                    <h4><span class="material-symbols-rounded">auto_awesome</span> AI Recommendations</h4>
                    <p style="color: var(--error);">Failed to generate recommendations. ${escapeHtml(result?.error || '')}</p>
                </div>`;
        } else {
            currentFixes = result.priority_fixes || [];
            currentCrosslinks = result.crosslink_suggestions || [];

            let html = '<div class="ai-panel">';
            html += '<div style="display: flex; align-items: center; gap: 12px; margin-bottom: 16px;">';
            html += '<h4 style="margin: 0;"><span class="material-symbols-rounded">auto_awesome</span> AI Recommendations</h4>';
            const totalApplyable = currentFixes.length + currentCrosslinks.filter(c => c.collection_a_handle && c.collection_b_handle).length;
            if (totalApplyable > 0) {
                html += `<button class="btn btn-primary btn-sm" id="apply-all-recs-btn" onclick="window._applyAllRecommendations()" style="font-size: 12px; padding: 4px 12px;">Apply All Recommendations (${totalApplyable})</button>`;
            }
            html += '</div>';

            if (result.summary) {
                html += `<div class="ai-field"><div class="ai-field-label">Summary</div><div class="ai-field-value">${escapeHtml(result.summary)}</div></div>`;
            }

            if (currentFixes.length > 0) {
                html += '<div class="ai-field"><div class="ai-field-label" style="display: flex; align-items: center; gap: 12px;">Priority Fixes';
                html += `<button class="btn btn-primary btn-sm" id="apply-all-fixes-btn" onclick="window._applyAllFixes()" style="font-size: 12px; padding: 4px 12px;">Apply All (${currentFixes.length})</button>`;
                html += '</div>';
                currentFixes.forEach((fix, i) => {
                    html += `
                        <div class="recommendation-card" style="display: flex; align-items: flex-start; gap: 12px;">
                            <div style="flex: 1;">
                                <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                                    <span class="badge badge-info">${escapeHtml(fix.action || 'add_link')}</span>
                                    <strong>${escapeHtml(fix.from_title || '')} \u2192 ${escapeHtml(fix.to_title || '')}</strong>
                                </div>
                                ${fix.suggested_anchor ? `<div style="font-size: 13px; color: var(--on-surface-secondary);">Anchor: "<em>${escapeHtml(fix.suggested_anchor)}</em>"</div>` : ''}
                                ${fix.reason ? `<div style="font-size: 13px; color: var(--on-surface-secondary); margin-top: 4px;">${escapeHtml(fix.reason)}</div>` : ''}
                            </div>
                            <button class="btn btn-primary btn-sm" id="apply-fix-btn-${i}" onclick="window._applyFix(${i})" style="white-space: nowrap; font-size: 12px; padding: 4px 12px;">Apply</button>
                        </div>`;
                });
                html += '</div>';
            }

            if (currentCrosslinks.length > 0) {
                const applyableCrosslinks = currentCrosslinks.filter(c => c.collection_a_handle && c.collection_b_handle).length;
                html += '<div class="ai-field"><div class="ai-field-label" style="display: flex; align-items: center; gap: 12px;">Cross-Link Suggestions';
                if (applyableCrosslinks > 0) {
                    html += `<button class="btn btn-primary btn-sm" id="apply-all-crosslinks-btn" onclick="window._applyAllCrosslinks()" style="font-size: 12px; padding: 4px 12px;">Apply All (${applyableCrosslinks})</button>`;
                }
                html += '</div>';
                currentCrosslinks.forEach((s, i) => {
                    const hasHandles = s.collection_a_handle && s.collection_b_handle;
                    html += `
                        <div class="recommendation-card" style="display: flex; align-items: flex-start; gap: 12px;">
                            <div style="flex: 1;">
                                <strong>${escapeHtml(s.collection_a || '')} \u2194 ${escapeHtml(s.collection_b || '')}</strong>
                                ${s.shared_products ? `<span style="font-size: 13px; margin-left: 8px;">(${s.shared_products} shared products)</span>` : ''}
                                ${s.reason ? `<div style="font-size: 13px; color: var(--on-surface-secondary); margin-top: 4px;">${escapeHtml(s.reason)}</div>` : ''}
                            </div>
                            ${hasHandles ? `<button class="btn btn-primary btn-sm" id="apply-crosslink-btn-${i}" onclick="window._applyCrosslink(${i})" style="white-space: nowrap; font-size: 12px; padding: 4px 12px;">Apply</button>` : ''}
                        </div>`;
                });
                html += '</div>';
            }

            html += '</div>';
            aiRecsHtml = html;
        }

        const panel = document.getElementById('interlink-tab-ai-recs');
        if (panel) panel.innerHTML = aiRecsHtml;

        window._switchInterlinkTab('ai-recs');
    }

    window._switchInterlinkTab = function (tabId) {
        activeTab = tabId;
        saveFilter('interlinkTab', activeTab);
        document.querySelectorAll('.interlink-tabs .tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.interlink-tabs ~ .tab-panel').forEach(p => p.classList.remove('active'));

        const activeBtn = document.querySelector(`.interlink-tabs .tab[data-tab="${tabId}"]`);
        if (activeBtn) activeBtn.classList.add('active');

        const activePanel = document.getElementById('interlink-tab-' + tabId);
        if (activePanel) activePanel.classList.add('active');
    };

    window._runAnalysis = async function () {
        if (!currentStoreId) {
            showToast('Select a store first', 'warning');
            return;
        }

        showToast('Analysis started...', 'info');
        try {
            await apiRequest('/api/interlinking/analyze', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ store_id: currentStoreId }),
            });

            const poll = setInterval(async () => {
                try {
                    const status = await apiRequest(`/api/interlinking/status?store_id=${currentStoreId}`);
                    if (!status.running) {
                        clearInterval(poll);
                        showToast('Analysis complete!', 'success');
                        activeTab = 'overview';
                        aiRecsHtml = null;
                        loadAnalysis();
                    }
                } catch (e) {
                    clearInterval(poll);
                }
            }, 2000);
        } catch (e) {
            showToast('Failed to start analysis', 'error');
        }
    };

    window._getRecommendations = async function () {
        if (!currentStoreId) {
            showToast('Select a store first', 'warning');
            return;
        }

        const batchSel = document.getElementById('batch-size-select');
        const batchSize = batchSel ? parseInt(batchSel.value) || 10 : 10;
        const maxCrosslinks = Math.max(5, Math.round(batchSize / 2));

        showToast(`Generating ${batchSize} AI recommendations...`, 'info');
        try {
            const resp = await apiRequest('/api/interlinking/recommendations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ store_id: currentStoreId, max_fixes: batchSize, max_crosslinks: maxCrosslinks }),
            });

            const taskId = resp.task_id;
            const poll = setInterval(async () => {
                try {
                    const task = await apiRequest(`/api/interlinking/recommendations/${taskId}`);
                    if (task.status === 'completed') {
                        clearInterval(poll);
                        showToast('Recommendations ready!', 'success');
                        renderRecommendations(task.result);
                    } else if (task.status === 'failed') {
                        clearInterval(poll);
                        showToast('Recommendation generation failed: ' + (task.error || ''), 'error');
                        renderRecommendations({ error: task.error });
                    }
                } catch (e) {
                    clearInterval(poll);
                }
            }, 3000);
        } catch (e) {
            showToast('Failed to start recommendations', 'error');
        }
    };

    window._showItemDetail = async function (type, id) {
        const modal = document.getElementById('item-detail-modal');
        const title = document.getElementById('item-detail-title');
        const content = document.getElementById('item-detail-content');

        title.textContent = 'Loading...';
        content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div></div>';
        modal.classList.add('active');

        try {
            const data = await apiRequest(`/api/interlinking/item/${type}/${id}`);
            title.textContent = data.item.title;

            let html = '';

            html += '<div class="ai-field"><div class="ai-field-label">Outgoing Links</div>';
            if (data.outgoing_links.length === 0) {
                html += '<div class="ai-field-value">No outgoing internal links in description</div>';
            } else {
                html += '<div class="table-container"><table><thead><tr><th>Target</th><th>Type</th><th>Anchor Text</th></tr></thead><tbody>';
                data.outgoing_links.forEach(l => {
                    html += `<tr><td><code>${escapeHtml(l.handle)}</code></td><td><span class="badge badge-info">${escapeHtml(l.link_type)}</span></td><td>${escapeHtml(l.anchor_text)}</td></tr>`;
                });
                html += '</tbody></table></div>';
            }
            html += '</div>';

            html += '<div class="ai-field" style="margin-top: 16px;"><div class="ai-field-label">Incoming Links</div>';
            if (data.incoming_links.length === 0) {
                html += '<div class="ai-field-value">No incoming internal links found</div>';
            } else {
                html += '<div class="table-container"><table><thead><tr><th>From</th><th>Type</th><th>Anchor Text</th></tr></thead><tbody>';
                data.incoming_links.forEach(l => {
                    html += `<tr><td>${escapeHtml(l.from_title)} <code>(${escapeHtml(l.from_handle)})</code></td><td><span class="badge badge-info">${escapeHtml(l.from_type)}</span></td><td>${escapeHtml(l.anchor_text)}</td></tr>`;
                });
                html += '</tbody></table></div>';
            }
            html += '</div>';

            content.innerHTML = html;
        } catch (e) {
            content.innerHTML = '<p style="color: var(--error);">Failed to load item details.</p>';
        }
    };

    window.closeItemModal = function () {
        document.getElementById('item-detail-modal').classList.remove('active');
    };

    const itemModal = document.getElementById('item-detail-modal');
    if (itemModal) {
        itemModal.addEventListener('click', (e) => {
            if (e.target === itemModal) window.closeItemModal();
        });
    }

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && itemModal && itemModal.classList.contains('active')) {
            window.closeItemModal();
        }
    });

    window._applyFix = async function (index) {
        const fix = currentFixes[index];
        if (!fix || !currentStoreId) return;

        const btn = document.getElementById(`apply-fix-btn-${index}`);
        if (!btn || btn.disabled) return;
        btn.disabled = true;
        btn.textContent = 'Applying...';

        try {
            const resp = await apiRequest('/api/interlinking/apply-fix', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    store_id: currentStoreId,
                    from_type: fix.from_type,
                    from_handle: fix.from_handle,
                    from_blog_handle: fix.from_blog_handle || undefined,
                    to_type: fix.to_type,
                    to_handle: fix.to_handle,
                    to_blog_handle: fix.to_blog_handle || undefined,
                    to_title: fix.to_title,
                    suggested_anchor: fix.suggested_anchor || fix.to_title || fix.to_handle,
                }),
            });

            if (resp.skipped) {
                btn.textContent = 'Already Linked';
                btn.classList.remove('btn-primary');
                btn.classList.add('btn-secondary');
                showToast(`Link already exists: ${fix.from_handle} → ${fix.to_handle}`, 'info');
            } else {
                btn.textContent = 'Applied ✓';
                btn.style.background = 'var(--success)';
                btn.style.borderColor = 'var(--success)';
                showToast(`Link applied: ${fix.from_handle} → ${fix.to_handle}`, 'success');
            }
            appliedFixes.add(index);
        } catch (e) {
            btn.disabled = false;
            btn.textContent = 'Apply';
            showToast(`Failed to apply: ${e.message || e}`, 'error');
        }

        _updateMasterApplyBtn();
    };

    window._applyAllFixes = async function (skipConfirm) {
        if (!currentStoreId || currentFixes.length === 0) return;

        const pendingIndices = currentFixes.map((_, i) => i).filter(i => !appliedFixes.has(i));
        if (pendingIndices.length === 0) {
            showToast('All fixes already applied', 'info');
            return;
        }

        if (!skipConfirm && !confirm(`Apply ${pendingIndices.length} link recommendation${pendingIndices.length !== 1 ? 's' : ''}?`)) return;

        const btn = document.getElementById('apply-all-fixes-btn');
        if (btn) {
            btn.disabled = true;
            btn.textContent = `Applying... (0/${pendingIndices.length})`;
        }

        let applied = 0, skipped = 0, errors = 0;

        for (const idx of pendingIndices) {
            const fix = currentFixes[idx];
            const fixBtn = document.getElementById(`apply-fix-btn-${idx}`);
            if (fixBtn) {
                fixBtn.disabled = true;
                fixBtn.textContent = 'Applying...';
            }

            try {
                const resp = await apiRequest('/api/interlinking/apply-fix', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        store_id: currentStoreId,
                        from_type: fix.from_type,
                        from_handle: fix.from_handle,
                        from_blog_handle: fix.from_blog_handle || undefined,
                        to_type: fix.to_type,
                        to_handle: fix.to_handle,
                        to_blog_handle: fix.to_blog_handle || undefined,
                        to_title: fix.to_title,
                        suggested_anchor: fix.suggested_anchor || fix.to_title || fix.to_handle,
                    }),
                });

                if (resp.skipped) {
                    skipped++;
                    appliedFixes.add(idx);
                    if (fixBtn) {
                        fixBtn.textContent = 'Already Linked';
                        fixBtn.classList.remove('btn-primary');
                        fixBtn.classList.add('btn-secondary');
                    }
                } else {
                    applied++;
                    appliedFixes.add(idx);
                    if (fixBtn) {
                        fixBtn.textContent = 'Applied \u2713';
                        fixBtn.style.background = 'var(--success)';
                        fixBtn.style.borderColor = 'var(--success)';
                    }
                }
            } catch (e) {
                errors++;
                if (fixBtn) {
                    fixBtn.disabled = false;
                    fixBtn.textContent = 'Apply';
                }
            }

            if (btn) btn.textContent = `Applying... (${applied + skipped + errors}/${pendingIndices.length})`;
        }

        showToast(`Done: ${applied} applied, ${skipped} skipped, ${errors} errors`, applied > 0 ? 'success' : 'info');

        if (btn) {
            const remaining = currentFixes.filter((_, i) => !appliedFixes.has(i)).length;
            btn.textContent = remaining > 0 ? `Apply All (${remaining})` : 'All Applied \u2713';
            btn.disabled = remaining === 0;
        }

        _updateMasterApplyBtn();
    };

    window._applyCrosslink = async function (index) {
        const cl = currentCrosslinks[index];
        if (!cl || !currentStoreId) return;
        if (!cl.collection_a_handle || !cl.collection_b_handle) return;

        const btn = document.getElementById(`apply-crosslink-btn-${index}`);
        if (!btn || btn.disabled) return;
        btn.disabled = true;
        btn.textContent = 'Applying...';

        const fixes = [
            {
                from_type: 'collection',
                from_handle: cl.collection_a_handle,
                to_type: 'collection',
                to_handle: cl.collection_b_handle,
                to_title: cl.collection_b,
                suggested_anchor: cl.collection_b || cl.collection_b_handle,
            },
            {
                from_type: 'collection',
                from_handle: cl.collection_b_handle,
                to_type: 'collection',
                to_handle: cl.collection_a_handle,
                to_title: cl.collection_a,
                suggested_anchor: cl.collection_a || cl.collection_a_handle,
            },
        ];

        try {
            const resp = await apiRequest('/api/interlinking/apply-all', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ store_id: currentStoreId, fixes }),
            });

            const applied = (resp.results || []).filter(r => r.status === 'applied').length;
            const skipped = (resp.results || []).filter(r => r.status === 'skipped').length;

            if (applied > 0) {
                btn.textContent = 'Applied ✓';
                btn.style.background = 'var(--success)';
                btn.style.borderColor = 'var(--success)';
                showToast(`Cross-links applied (${applied} added, ${skipped} skipped)`, 'success');
            } else if (skipped > 0) {
                btn.textContent = 'Already Linked';
                btn.classList.remove('btn-primary');
                btn.classList.add('btn-secondary');
                showToast('Both cross-links already exist', 'info');
            }
        } catch (e) {
            btn.disabled = false;
            btn.textContent = 'Apply';
            showToast(`Failed to apply cross-links: ${e.message || e}`, 'error');
        }

        _updateMasterApplyBtn();
    };

    window._applyAllCrosslinks = async function (skipConfirm) {
        if (!currentStoreId || currentCrosslinks.length === 0) return;

        const pending = currentCrosslinks.filter((cl, i) => {
            if (!cl.collection_a_handle || !cl.collection_b_handle) return false;
            const btn = document.getElementById(`apply-crosslink-btn-${i}`);
            return btn && !btn.disabled;
        });

        if (pending.length === 0) {
            showToast('All cross-links already applied', 'info');
            return;
        }

        if (!skipConfirm && !confirm(`Apply ${pending.length} cross-link suggestion${pending.length !== 1 ? 's' : ''}? Each creates two bidirectional links.`)) return;

        const btn = document.getElementById('apply-all-crosslinks-btn');
        if (btn) {
            btn.disabled = true;
            btn.textContent = `Applying... (0/${pending.length})`;
        }

        let applied = 0, skipped = 0, errors = 0;

        for (let idx = 0; idx < currentCrosslinks.length; idx++) {
            const cl = currentCrosslinks[idx];
            if (!cl.collection_a_handle || !cl.collection_b_handle) continue;
            const clBtn = document.getElementById(`apply-crosslink-btn-${idx}`);
            if (!clBtn || clBtn.disabled) continue;

            clBtn.disabled = true;
            clBtn.textContent = 'Applying...';

            const fixes = [
                {
                    from_type: 'collection',
                    from_handle: cl.collection_a_handle,
                    to_type: 'collection',
                    to_handle: cl.collection_b_handle,
                    to_title: cl.collection_b,
                    suggested_anchor: cl.collection_b || cl.collection_b_handle,
                },
                {
                    from_type: 'collection',
                    from_handle: cl.collection_b_handle,
                    to_type: 'collection',
                    to_handle: cl.collection_a_handle,
                    to_title: cl.collection_a,
                    suggested_anchor: cl.collection_a || cl.collection_a_handle,
                },
            ];

            try {
                const resp = await apiRequest('/api/interlinking/apply-all', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ store_id: currentStoreId, fixes }),
                });

                const clApplied = (resp.results || []).filter(r => r.status === 'applied').length;
                const clSkipped = (resp.results || []).filter(r => r.status === 'skipped').length;

                if (clApplied > 0) {
                    applied++;
                    clBtn.textContent = 'Applied \u2713';
                    clBtn.style.background = 'var(--success)';
                    clBtn.style.borderColor = 'var(--success)';
                } else if (clSkipped > 0) {
                    skipped++;
                    clBtn.textContent = 'Already Linked';
                    clBtn.classList.remove('btn-primary');
                    clBtn.classList.add('btn-secondary');
                }
            } catch (e) {
                errors++;
                clBtn.disabled = false;
                clBtn.textContent = 'Apply';
            }

            if (btn) btn.textContent = `Applying... (${applied + skipped + errors}/${pending.length})`;
        }

        showToast(`Cross-links: ${applied} applied, ${skipped} skipped, ${errors} errors`, applied > 0 ? 'success' : 'info');

        if (btn) {
            btn.disabled = applied + skipped + errors >= pending.length;
            btn.textContent = btn.disabled ? 'All Applied \u2713' : `Apply All (${pending.length - applied - skipped - errors})`;
        }

        _updateMasterApplyBtn();
    };

    window._applyAllRecommendations = async function () {
        if (!currentStoreId) return;

        const pendingFixes = currentFixes.filter((_, i) => !appliedFixes.has(i));
        const pendingCrosslinks = currentCrosslinks.filter((cl, i) => {
            if (!cl.collection_a_handle || !cl.collection_b_handle) return false;
            const btn = document.getElementById(`apply-crosslink-btn-${i}`);
            return btn && !btn.disabled;
        });
        const totalPending = pendingFixes.length + pendingCrosslinks.length;

        if (totalPending === 0) {
            showToast('All recommendations already applied', 'info');
            return;
        }

        if (!confirm(`Apply all ${totalPending} recommendations? (${pendingFixes.length} link fixes + ${pendingCrosslinks.length} cross-links)`)) return;

        const masterBtn = document.getElementById('apply-all-recs-btn');
        if (masterBtn) {
            masterBtn.disabled = true;
            masterBtn.textContent = 'Applying all...';
        }

        if (pendingFixes.length > 0) {
            await window._applyAllFixes(true);
        }
        if (pendingCrosslinks.length > 0) {
            await window._applyAllCrosslinks(true);
        }

        _updateMasterApplyBtn();
    };

    function _updateMasterApplyBtn() {
        const masterBtn = document.getElementById('apply-all-recs-btn');
        if (!masterBtn) return;

        const remainingFixes = currentFixes.filter((_, i) => !appliedFixes.has(i)).length;
        const remainingCrosslinks = currentCrosslinks.filter((cl, i) => {
            if (!cl.collection_a_handle || !cl.collection_b_handle) return false;
            const btn = document.getElementById(`apply-crosslink-btn-${i}`);
            return btn && !btn.disabled;
        }).length;
        const remaining = remainingFixes + remainingCrosslinks;

        if (remaining === 0) {
            masterBtn.disabled = true;
            masterBtn.textContent = 'All Applied \u2713';
        } else {
            masterBtn.disabled = false;
            masterBtn.textContent = `Apply All Recommendations (${remaining})`;
        }
    }

    // ── Broken link target loading + autocomplete ──────────────

    async function _loadAvailableTargets() {
        if (availableTargets) return;
        try {
            availableTargets = await apiRequest(`/api/interlinking/available-targets?store_id=${currentStoreId}`);
        } catch (e) {
            availableTargets = [];
        }
    }

    function _filterTargets(query) {
        if (!availableTargets) return [];
        const q = query.toLowerCase().trim();
        if (!q) return [];

        const slug = q.replace(/\s+/g, '-');

        const scored = [];
        for (const t of availableTargets) {
            const handle = t.handle.toLowerCase();
            let score = 0;

            if (handle === slug) score = 1000;
            else if (handle.startsWith(slug)) score = 500;
            else if (handle.includes(slug)) score = 200;
            else continue;

            scored.push({ target: t, score });
        }

        scored.sort((a, b) => b.score - a.score);
        return scored.slice(0, 20).map(s => s.target);
    }

    function _renderDropdown(index, items) {
        const dd = document.getElementById(`broken-dropdown-${index}`);
        if (!dd) return;
        if (items.length === 0) {
            dd.style.display = 'none';
            return;
        }
        dd.innerHTML = items.map((t, j) => {
            const typeBadge = `<span class="badge badge-info" style="font-size:10px;margin-right:4px;">${escapeHtml(t.type)}</span>`;
            return `<div class="broken-link-option" data-index="${index}" data-item='${JSON.stringify({ type: t.type, handle: t.handle, blog_handle: t.blog_handle || null }).replace(/'/g, "&#39;")}' style="padding:6px 10px;cursor:pointer;font-size:12px;border-bottom:1px solid var(--outline);" onmousedown="window._selectReplacement(${index}, this)">${typeBadge}<strong>${escapeHtml(t.title)}</strong> <span style="color:var(--on-surface-secondary);">${escapeHtml(t.handle)}</span></div>`;
        }).join('');
        dd.style.display = 'block';
    }

    let _dropdownDebounce = null;

    window._onReplacementInput = function (index, input) {
        brokenLinkGroups[index].replacement = null;
        clearTimeout(_dropdownDebounce);
        _dropdownDebounce = setTimeout(async () => {
            await _loadAvailableTargets();
            const q = input.value.trim();
            if (!q) {
                const dd = document.getElementById(`broken-dropdown-${index}`);
                if (dd) dd.style.display = 'none';
                return;
            }
            _renderDropdown(index, _filterTargets(q));
        }, 150);
    };

    window._onReplacementFocus = function (index, input) {
        const q = input.value.trim();
        if (q) {
            window._onReplacementInput(index, input);
        }
    };

    window._selectReplacement = function (index, el) {
        const data = JSON.parse(el.dataset.item);
        brokenLinkGroups[index].replacement = data;
        const input = document.getElementById(`broken-replacement-${index}`);
        if (input) {
            const label = el.querySelector('strong');
            input.value = `[${data.type}] ${label ? label.textContent : data.handle}`;
        }
        const dd = document.getElementById(`broken-dropdown-${index}`);
        if (dd) dd.style.display = 'none';
    };

    document.addEventListener('click', (e) => {
        if (!e.target.closest('.broken-link-dropdown') && !e.target.matches('[id^="broken-replacement-"]')) {
            document.querySelectorAll('.broken-link-dropdown').forEach(dd => dd.style.display = 'none');
        }
    });

    // ── Broken link fix handlers ─────────────────────────────────

    async function _fixOneSource(group, source) {
        const payload = {
            store_id: currentStoreId,
            to_type: group.to_type,
            to_handle: group.to_handle,
            to_href: group.to_href,
            from_type: source.from_type,
            from_handle: source.from_handle,
            from_blog_handle: source.from_blog_handle || undefined,
        };
        if (group.replacement) {
            payload.replacement_type = group.replacement.type;
            payload.replacement_handle = group.replacement.handle;
            payload.replacement_blog_handle = group.replacement.blog_handle || undefined;
        }
        return await apiRequest('/api/interlinking/fix-broken-link', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
    }

    window._fixBrokenGroup = async function (index) {
        const group = brokenLinkGroups[index];
        if (!group || !currentStoreId) return;

        const btn = document.getElementById(`fix-broken-btn-${index}`);
        if (!btn || btn.disabled) return;
        btn.disabled = true;

        const total = group.sources.length;
        let fixed = 0, skipped = 0, errors = 0;

        for (let s = 0; s < total; s++) {
            btn.textContent = `Fixing... (${s + 1}/${total})`;
            try {
                const resp = await _fixOneSource(group, group.sources[s]);
                if (resp.status === 'fixed') fixed++;
                else skipped++;
            } catch (e) {
                errors++;
            }
        }

        fixedBrokenGroups.add(index);
        if (fixed > 0) {
            btn.textContent = `Fixed \u2713 (${fixed})`;
            btn.style.background = 'var(--success)';
            btn.style.borderColor = 'var(--success)';
        } else {
            btn.textContent = 'Skipped';
            btn.style.background = 'var(--warning)';
            btn.style.borderColor = 'var(--warning)';
        }

        const action = group.replacement ? `replaced → ${group.replacement.handle}` : 'links removed';
        showToast(`${group.to_handle}: ${action} — ${fixed} fixed, ${skipped} skipped, ${errors} errors`, fixed > 0 ? 'success' : 'info');
        _updateFixAllBrokenBtn();
    };

    window._fixAllBrokenGroups = async function () {
        if (!currentStoreId || brokenLinkGroups.length === 0) return;

        const pendingIndices = brokenLinkGroups.map((_, i) => i).filter(i => !fixedBrokenGroups.has(i));
        if (pendingIndices.length === 0) {
            showToast('All broken link groups already fixed', 'info');
            return;
        }

        if (!confirm(`Fix ${pendingIndices.length} broken target${pendingIndices.length !== 1 ? 's' : ''}? This will update all affected source pages.`)) return;

        const masterBtn = document.getElementById('fix-all-broken-btn');
        if (masterBtn) {
            masterBtn.disabled = true;
            masterBtn.textContent = `Fixing... (0/${pendingIndices.length})`;
        }

        let groupsDone = 0, groupErrors = 0;

        for (const idx of pendingIndices) {
            const group = brokenLinkGroups[idx];
            const fixBtn = document.getElementById(`fix-broken-btn-${idx}`);

            if (fixBtn) fixBtn.disabled = true;

            let fixed = 0, skipped = 0, errors = 0;
            for (let s = 0; s < group.sources.length; s++) {
                if (fixBtn) fixBtn.textContent = `Fixing... (${s + 1}/${group.sources.length})`;
                try {
                    const resp = await _fixOneSource(group, group.sources[s]);
                    if (resp.status === 'fixed') fixed++;
                    else skipped++;
                } catch (e) {
                    errors++;
                }
            }

            fixedBrokenGroups.add(idx);
            if (fixBtn) {
                if (fixed > 0) {
                    fixBtn.textContent = `Fixed \u2713 (${fixed})`;
                    fixBtn.style.background = 'var(--success)';
                    fixBtn.style.borderColor = 'var(--success)';
                } else {
                    fixBtn.textContent = 'Skipped';
                    fixBtn.style.background = 'var(--warning)';
                    fixBtn.style.borderColor = 'var(--warning)';
                }
            }

            if (fixed > 0) groupsDone++;
            else groupErrors++;

            if (masterBtn) masterBtn.textContent = `Fixing... (${groupsDone + groupErrors}/${pendingIndices.length})`;
        }

        showToast(`Done: ${groupsDone} targets fixed, ${groupErrors} skipped/errors`, groupsDone > 0 ? 'success' : 'info');
        _updateFixAllBrokenBtn();
    };

    function _updateFixAllBrokenBtn() {
        const btn = document.getElementById('fix-all-broken-btn');
        if (!btn) return;

        const remaining = brokenLinkGroups.filter((_, i) => !fixedBrokenGroups.has(i)).length;
        if (remaining === 0) {
            btn.disabled = true;
            btn.textContent = 'All Fixed \u2713';
        } else {
            btn.disabled = false;
            btn.textContent = `Fix All (${remaining})`;
        }
    }

    window._closeBrokenLinkModal = function () {
        document.getElementById('broken-link-fix-modal').classList.remove('active');
    };

    window._confirmBrokenLinkFix = function () {
        window._closeBrokenLinkModal();
    };

    const brokenModal = document.getElementById('broken-link-fix-modal');
    if (brokenModal) {
        brokenModal.addEventListener('click', (e) => {
            if (e.target === brokenModal) window._closeBrokenLinkModal();
        });
    }

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && brokenModal && brokenModal.classList.contains('active')) {
            window._closeBrokenLinkModal();
        }
    });

    document.addEventListener('DOMContentLoaded', init);
})();
