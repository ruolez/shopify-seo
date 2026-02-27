(function () {
    let currentStoreId = null;
    let analysisData = null;
    let activeTab = loadFilter('interlinkTab', 'overview');
    let aiRecsHtml = null;

    async function init() {
        await loadStores();
        const sel = document.getElementById('store-select');
        sel.addEventListener('change', () => {
            currentStoreId = sel.value ? parseInt(sel.value) : null;
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
        const underLinkedCount = (data.under_linked || []).length;
        const notInCollectionCount = (data.not_in_collection || []).length;
        const crossLinksCount = (data.missing_crosslinks || []).length;

        let html = `
            <div class="interlink-action-bar" style="display: flex; align-items: center; gap: 12px; margin-bottom: 24px; flex-wrap: wrap;">
                <button class="btn btn-primary" onclick="window._runAnalysis()">
                    <span class="material-symbols-rounded">refresh</span> Re-Analyze
                </button>
                <button class="btn btn-secondary" onclick="window._getRecommendations()">
                    <span class="material-symbols-rounded">auto_awesome</span> AI Recommendations
                </button>
                ${createdAt ? `<span style="font-size: 13px; color: var(--on-surface-secondary); margin-left: auto;">Last analyzed: ${formatDate(createdAt)}</span>` : ''}
            </div>`;

        html += renderSummaryStats(s);

        const tabs = [
            { id: 'overview', label: 'Overview' },
            { id: 'orphan-products', label: 'Orphan Products', count: orphanProductsCount },
            { id: 'orphan-collections', label: 'Orphan Collections', count: orphanCollectionsCount },
            { id: 'under-linked', label: 'Under-Linked', count: underLinkedCount },
            { id: 'not-in-collection', label: 'Not in Collection', count: notInCollectionCount },
            { id: 'cross-links', label: 'Cross-Links', count: crossLinksCount },
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
        html += renderTabPanel('under-linked', renderSectionContent('Under-Linked Pages', 'info', 'Few incoming links (1 only)', data.under_linked, 'mixed'));
        html += renderTabPanel('not-in-collection', renderNotInCollectionContent(data.not_in_collection));
        html += renderTabPanel('cross-links', renderMissingCrossLinksContent(data.missing_crosslinks));
        html += renderAiRecsPanel();

        container.innerHTML = html;
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
            { label: 'Under-Linked Pages', count: (data.under_linked || []).length, tab: 'under-linked', icon: 'trending_down' },
            { label: 'Not in Collection', count: (data.not_in_collection || []).length, tab: 'not-in-collection', icon: 'inventory_2' },
            { label: 'Missing Cross-Links', count: (data.missing_crosslinks || []).length, tab: 'cross-links', icon: 'swap_horiz' },
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
            { icon: 'trending_up', color: 'green', value: s.avg_incoming_links, label: 'Avg Links / Page' },
            { icon: 'score', color: 'blue', value: s.avg_score, label: 'Avg Interlinking Score' },
            { icon: 'inventory_2', color: 'orange', value: s.not_in_collection_count, label: 'Not in Any Collection', tab: 'not-in-collection' },
            { icon: 'swap_horiz', color: 'red', value: s.missing_crosslinks_count, label: 'Missing Cross-Links', tab: 'cross-links' },
            { icon: 'checklist', color: 'green', value: s.score_distribution ? s.score_distribution.excellent : 0, label: 'Well-Linked Pages' },
        ];

        let html = '<div class="stats-grid" style="margin-bottom: 8px;">';
        statCards.slice(0, 4).forEach(c => {
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
        statCards.slice(4).forEach(c => {
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
            const type = item.in_collection !== undefined ? 'product' : (itemType === 'mixed' ? (item.collection_count !== undefined ? 'product' : 'collection') : itemType);
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
        if (!result || result.error) {
            aiRecsHtml = `
                <div class="ai-panel">
                    <h4><span class="material-symbols-rounded">auto_awesome</span> AI Recommendations</h4>
                    <p style="color: var(--error);">Failed to generate recommendations. ${escapeHtml(result?.error || '')}</p>
                </div>`;
        } else {
            let html = '<div class="ai-panel">';
            html += '<h4><span class="material-symbols-rounded">auto_awesome</span> AI Recommendations</h4>';

            if (result.summary) {
                html += `<div class="ai-field"><div class="ai-field-label">Summary</div><div class="ai-field-value">${escapeHtml(result.summary)}</div></div>`;
            }

            if (result.priority_fixes && result.priority_fixes.length > 0) {
                html += '<div class="ai-field"><div class="ai-field-label">Priority Fixes</div>';
                result.priority_fixes.forEach(fix => {
                    html += `
                        <div class="recommendation-card">
                            <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                                <span class="badge badge-info">${escapeHtml(fix.action || 'add_link')}</span>
                                <strong>${escapeHtml(fix.from_title || '')} \u2192 ${escapeHtml(fix.to_title || '')}</strong>
                            </div>
                            ${fix.suggested_anchor ? `<div style="font-size: 13px; color: var(--on-surface-secondary);">Anchor: "<em>${escapeHtml(fix.suggested_anchor)}</em>"</div>` : ''}
                            ${fix.reason ? `<div style="font-size: 13px; color: var(--on-surface-secondary); margin-top: 4px;">${escapeHtml(fix.reason)}</div>` : ''}
                        </div>`;
                });
                html += '</div>';
            }

            if (result.crosslink_suggestions && result.crosslink_suggestions.length > 0) {
                html += '<div class="ai-field"><div class="ai-field-label">Cross-Link Suggestions</div>';
                result.crosslink_suggestions.forEach(s => {
                    html += `
                        <div class="recommendation-card">
                            <strong>${escapeHtml(s.collection_a || '')} \u2194 ${escapeHtml(s.collection_b || '')}</strong>
                            ${s.shared_products ? `<span style="font-size: 13px; margin-left: 8px;">(${s.shared_products} shared products)</span>` : ''}
                            ${s.reason ? `<div style="font-size: 13px; color: var(--on-surface-secondary); margin-top: 4px;">${escapeHtml(s.reason)}</div>` : ''}
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

        showToast('Generating AI recommendations...', 'info');
        try {
            const resp = await apiRequest('/api/interlinking/recommendations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ store_id: currentStoreId }),
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

    document.addEventListener('DOMContentLoaded', init);
})();
