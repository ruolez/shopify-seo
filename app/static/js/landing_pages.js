(function () {
    'use strict';

    let currentStoreId = null;
    let currentBuilderPageId = null;
    let currentSections = [];
    let currentSeoPageId = null;
    let currentSeoPageType = null;
    let currentAiResult = null;
    let editingSectionId = null;
    let confirmCallback = null;

    const SECTION_LABELS = {
        hero: 'Hero',
        features: 'Features',
        product_showcase: 'Product Showcase',
        testimonials: 'Testimonials',
        faq: 'FAQ',
        cta_banner: 'CTA Banner',
        trust_badges: 'Trust Badges',
        rich_text: 'Rich Text',
        image_gallery: 'Image Gallery',
        statistics: 'Statistics',
    };

    const SECTION_ICONS = {
        hero: 'image',
        features: 'star',
        product_showcase: 'storefront',
        testimonials: 'format_quote',
        faq: 'help',
        cta_banner: 'ads_click',
        trust_badges: 'verified',
        rich_text: 'article',
        image_gallery: 'photo_library',
        statistics: 'bar_chart',
    };

    // ── Init ──────────────────────────────────────────────────────

    async function init() {
        await loadStores();
        setupTabs();
        setupCharCounters();
    }

    async function loadStores() {
        try {
            const stores = await apiRequest('/api/stores');
            const sel = document.getElementById('lp-store-select');
            if (!sel) return;
            sel.innerHTML = '<option value="">Select store...</option>';
            stores.forEach(s => {
                sel.innerHTML += `<option value="${s.id}">${s.name} (${s.domain})</option>`;
            });
            sel.addEventListener('change', () => {
                currentStoreId = sel.value ? parseInt(sel.value) : null;
                if (currentStoreId) loadSyncedPages();
            });
            if (stores.length === 1) {
                sel.value = stores[0].id;
                currentStoreId = stores[0].id;
                loadSyncedPages();
            }
        } catch (e) {
            showToast('Failed to load stores: ' + e.message, 'error');
        }
    }

    function setupTabs() {
        document.querySelectorAll('.tabs .tab').forEach(tab => {
            tab.addEventListener('click', () => {
                document.querySelectorAll('.tabs .tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
                tab.classList.add('active');
                const panel = document.getElementById('tab-' + tab.dataset.tab);
                if (panel) panel.classList.add('active');
            });
        });
    }

    function setupCharCounters() {
        const metaTitle = document.getElementById('bp-meta-title');
        const metaDesc = document.getElementById('bp-meta-desc');
        const titleCounter = document.getElementById('bp-meta-title-counter');
        const descCounter = document.getElementById('bp-meta-desc-counter');

        if (metaTitle && titleCounter) {
            metaTitle.addEventListener('input', () => {
                updateCharCounter(metaTitle.value.length, 60, titleCounter);
            });
        }
        if (metaDesc && descCounter) {
            metaDesc.addEventListener('input', () => {
                updateCharCounter(metaDesc.value.length, 160, descCounter);
            });
        }
    }

    function updateCharCounter(len, target, el) {
        el.textContent = `${len}/${target} characters`;
        if (len === 0) {
            el.className = 'char-counter';
        } else if (target === 60) {
            el.className = 'char-counter' + (len >= 50 && len <= 60 ? ' good' : len >= 35 && len <= 70 ? ' warn' : ' bad');
        } else {
            el.className = 'char-counter' + (len >= 140 && len <= 160 ? ' good' : len >= 100 && len <= 200 ? ' warn' : ' bad');
        }
    }

    function switchTab(tabName) {
        document.querySelectorAll('.tabs .tab').forEach(t => {
            t.classList.toggle('active', t.dataset.tab === tabName);
        });
        document.querySelectorAll('.tab-panel').forEach(p => {
            p.classList.toggle('active', p.id === 'tab-' + tabName);
        });
    }

    // ── Synced Pages ──────────────────────────────────────────────

    async function syncPages() {
        if (!currentStoreId) { showToast('Select a store first', 'error'); return; }
        const btn = document.querySelector('[onclick="LandingPages.syncPages()"]');
        const origHtml = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="material-symbols-rounded spin">progress_activity</span> Syncing...'; }
        try {
            await apiRequest('/api/landing-pages/sync', { method: 'POST', body: JSON.stringify({ store_id: currentStoreId }) });
            setTimeout(() => loadSyncedPages(), 3000);
            showToast('Page sync started');
        } catch (e) {
            showToast('Sync failed: ' + e.message, 'error');
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = origHtml; }
        }
    }

    async function loadSyncedPages() {
        if (!currentStoreId) return;
        try {
            const pages = await apiRequest(`/api/landing-pages/synced?store_id=${currentStoreId}`);
            renderSyncedPagesTable(pages);
        } catch (e) {
            showToast('Failed to load pages: ' + e.message, 'error');
        }
    }

    function renderSyncedPagesTable(pages) {
        const container = document.getElementById('synced-pages-container');
        if (!pages || pages.length === 0) {
            container.innerHTML = '<div class="empty-state">No pages synced yet. Click "Sync Pages" to import from Shopify.</div>';
            return;
        }

        let rows = '';
        for (const page of pages) {
            const scoreColor = page.seo_score >= 70 ? 'var(--success)' : page.seo_score >= 40 ? 'var(--warning)' : 'var(--error)';
            const badge = page.is_homepage ? '<span class="badge badge-primary" style="margin-left: 8px;">Homepage</span>' : '';
            const statusBadge = page.is_published
                ? '<span class="badge badge-success">Published</span>'
                : '<span class="badge badge-secondary">Draft</span>';

            rows += `<tr>
                <td>
                    <strong>${escapeHtml(page.title || 'Untitled')}</strong>${badge}
                    <div style="font-size: 12px; color: var(--on-surface-secondary);">/${escapeHtml(page.handle || '')}</div>
                </td>
                <td>
                    <div class="seo-score-mini" style="--color: ${scoreColor};">
                        <svg width="36" height="36" viewBox="0 0 36 36">
                            <circle cx="18" cy="18" r="15.9" fill="none" stroke="var(--outline)" stroke-width="3"/>
                            <circle cx="18" cy="18" r="15.9" fill="none" stroke="${scoreColor}" stroke-width="3"
                                stroke-dasharray="${page.seo_score} ${100 - page.seo_score}"
                                stroke-dashoffset="25" stroke-linecap="round"/>
                        </svg>
                        <span>${page.seo_score}</span>
                    </div>
                </td>
                <td>${statusBadge}</td>
                <td>
                    <div style="display: flex; gap: 4px;">
                        <button class="btn btn-secondary btn-sm" onclick="LandingPages.openSeoAnalysis(${page.id}, 'synced')" title="SEO Analysis">
                            <span class="material-symbols-rounded" style="font-size: 16px;">search</span>
                        </button>
                        <button class="btn btn-secondary btn-sm" onclick="LandingPages.aiOptimizeSynced(${page.id})" title="AI Optimize">
                            <span class="material-symbols-rounded" style="font-size: 16px;">auto_awesome</span>
                        </button>
                        <button class="btn btn-secondary btn-sm" onclick="LandingPages.importToBuilder(${page.id}, this)" title="Open in Builder">
                            <span class="material-symbols-rounded" style="font-size: 16px;">edit</span>
                        </button>
                    </div>
                </td>
            </tr>`;
        }

        container.innerHTML = `
            <div class="card">
                <table class="data-table">
                    <thead><tr><th>Page</th><th>SEO Score</th><th>Status</th><th>Actions</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>`;
    }

    // ── SEO Analysis ──────────────────────────────────────────────

    async function openSeoAnalysis(pageId, pageType) {
        currentSeoPageId = pageId;
        currentSeoPageType = pageType;
        switchTab('seo-analysis');

        document.getElementById('seo-empty').style.display = 'none';
        document.getElementById('seo-detail').style.display = 'block';
        document.getElementById('ai-recommendations-card').style.display = 'none';
        document.getElementById('schema-card').style.display = 'none';

        try {
            const url = pageType === 'synced'
                ? `/api/landing-pages/synced/${pageId}/seo`
                : `/api/landing-pages/${pageId}/seo`;
            const result = await apiRequest(url);
            renderSeoBreakdown(result, pageId, pageType);

            if (pageType === 'builder') {
                loadSchemaCard(pageId);
            }
        } catch (e) {
            showToast('Failed to load SEO analysis', 'error');
        }
    }

    async function loadSchemaCard(pageId) {
        try {
            const { schemas } = await apiRequest(`/api/landing-pages/${pageId}/generate-schema`, { method: 'POST' });
            if (schemas && (Array.isArray(schemas) ? schemas.length > 0 : Object.keys(schemas).length > 0)) {
                document.getElementById('schema-content').textContent = JSON.stringify(schemas, null, 2);
                document.getElementById('schema-card').style.display = 'block';
            }
        } catch (e) {
            // Schema generation is optional, don't block
        }
    }

    async function renderSeoBreakdown(result, pageId, pageType) {
        const scoreEl = document.getElementById('seo-score-value');
        const ringEl = document.getElementById('seo-score-ring');
        scoreEl.textContent = result.score;
        const color = result.score >= 70 ? '#16a34a' : result.score >= 40 ? '#d97706' : '#dc2626';
        ringEl.style.setProperty('--score', result.score);
        ringEl.style.setProperty('--color', color);

        try {
            const url = pageType === 'synced'
                ? `/api/landing-pages/synced?store_id=${currentStoreId}`
                : `/api/landing-pages?store_id=${currentStoreId}`;
            const pages = await apiRequest(url);
            const page = pages.find(p => p.id === pageId);
            if (page) {
                document.getElementById('seo-page-title').textContent = page.title || 'Untitled';
                document.getElementById('seo-page-handle').textContent = '/' + (page.handle || '');
            }
        } catch (e) { /* ignore */ }

        const tbody = document.querySelector('#seo-breakdown-table tbody');
        tbody.innerHTML = '';
        const breakdown = result.breakdown || {};
        for (const [check, data] of Object.entries(breakdown)) {
            const status = data.score >= 70 ? '✓' : data.score >= 40 ? '~' : '✗';
            const statusColor = data.score >= 70 ? 'var(--success)' : data.score >= 40 ? 'var(--warning)' : 'var(--error)';
            tbody.innerHTML += `<tr>
                <td>${check.replace(/_/g, ' ')}</td>
                <td>${data.score}</td>
                <td style="font-size: 13px;">${escapeHtml(data.detail)}</td>
                <td style="color: ${statusColor}; font-weight: 600;">${status}</td>
            </tr>`;
        }
    }

    async function runAiOptimize() {
        if (!currentSeoPageId) return;
        const btn = document.getElementById('seo-ai-btn');
        btn.disabled = true;
        btn.innerHTML = '<span class="material-symbols-rounded spin">progress_activity</span> Analyzing...';

        try {
            const url = currentSeoPageType === 'synced'
                ? `/api/landing-pages/synced/${currentSeoPageId}/ai-optimize`
                : `/api/landing-pages/${currentSeoPageId}/ai-optimize`;
            const { task_id } = await apiRequest(url, { method: 'POST' });
            await pollTask(task_id, (result) => {
                currentAiResult = result;
                renderAiRecommendations(result);
            });
        } catch (e) {
            showToast('AI optimization failed: ' + e.message, 'error');
        } finally {
            btn.disabled = false;
            btn.innerHTML = '<span class="material-symbols-rounded">auto_awesome</span> AI Optimize';
        }
    }

    function renderAiRecommendations(result) {
        const card = document.getElementById('ai-recommendations-card');
        const content = document.getElementById('ai-recommendations-content');
        card.style.display = 'block';

        let html = '';
        if (result.optimized_meta_title) {
            html += `<div class="form-group"><label>Optimized Meta Title</label>
                <div class="recommendation-value">${escapeHtml(result.optimized_meta_title)}</div></div>`;
        }
        if (result.optimized_meta_description) {
            html += `<div class="form-group"><label>Optimized Meta Description</label>
                <div class="recommendation-value">${escapeHtml(result.optimized_meta_description)}</div></div>`;
        }
        if (result.content_recommendations && result.content_recommendations.length) {
            html += '<div class="form-group"><label>Content Recommendations</label><ul>';
            result.content_recommendations.forEach(r => { html += `<li>${escapeHtml(r)}</li>`; });
            html += '</ul></div>';
        }
        if (result.missing_elements && result.missing_elements.length) {
            html += '<div class="form-group"><label>Missing Elements</label><ul>';
            result.missing_elements.forEach(r => { html += `<li>${escapeHtml(r)}</li>`; });
            html += '</ul></div>';
        }
        if (result.summary) {
            html += `<div class="form-group"><label>Summary</label><p style="font-size: 13px; color: var(--on-surface-secondary);">${escapeHtml(result.summary)}</p></div>`;
        }
        content.innerHTML = html || '<p>No recommendations available.</p>';
    }

    async function applyRecommendations() {
        if (!currentAiResult || !currentSeoPageId) return;
        const btn = document.getElementById('apply-recs-btn');
        const origHtml = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="material-symbols-rounded spin">progress_activity</span> Applying...'; }

        const data = {};
        if (currentAiResult.optimized_meta_title) data.meta_title = currentAiResult.optimized_meta_title;
        if (currentAiResult.optimized_meta_description) data.meta_description = currentAiResult.optimized_meta_description;

        try {
            if (currentSeoPageType === 'synced') {
                await apiRequest(`/api/landing-pages/synced/${currentSeoPageId}/ai-apply`, {
                    method: 'POST', body: JSON.stringify(data),
                });
            } else {
                await apiRequest(`/api/landing-pages/${currentSeoPageId}`, {
                    method: 'PUT', body: JSON.stringify(data),
                });
            }
            showToast('Recommendations applied');
            openSeoAnalysis(currentSeoPageId, currentSeoPageType);
            if (currentSeoPageType === 'synced') loadSyncedPages();
        } catch (e) {
            showToast('Failed to apply: ' + e.message, 'error');
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = origHtml; }
        }
    }

    async function aiOptimizeSynced(pageId) {
        openSeoAnalysis(pageId, 'synced');
        setTimeout(() => runAiOptimize(), 500);
    }

    function toggleSchema() {
        const el = document.getElementById('schema-content');
        el.style.display = el.style.display === 'none' ? 'block' : 'none';
    }

    // ── Builder ───────────────────────────────────────────────────

    async function loadPageBuilder(pageId) {
        currentBuilderPageId = pageId;
        switchTab('builder');
        document.getElementById('builder-empty').style.display = 'none';
        document.getElementById('builder-workspace').style.display = 'block';

        try {
            const page = await apiRequest(`/api/landing-pages/${pageId}`);
            document.getElementById('builder-page-title').textContent = page.title;
            const publishedBadge = document.getElementById('builder-published-badge');
            if (publishedBadge) {
                publishedBadge.style.display = page.is_published ? 'inline-flex' : 'none';
            }
            document.getElementById('bp-title').value = page.title || '';
            document.getElementById('bp-handle').value = page.handle || '';
            document.getElementById('bp-meta-title').value = page.meta_title || '';
            document.getElementById('bp-meta-desc').value = page.meta_description || '';

            const titleCounter = document.getElementById('bp-meta-title-counter');
            const descCounter = document.getElementById('bp-meta-desc-counter');
            if (titleCounter) updateCharCounter((page.meta_title || '').length, 60, titleCounter);
            if (descCounter) updateCharCounter((page.meta_description || '').length, 160, descCounter);

            currentSections = page.sections || [];
            renderSectionsList();
            updatePreview();
        } catch (e) {
            showToast('Failed to load page', 'error');
        }
    }

    function renderSectionsList() {
        const container = document.getElementById('sections-list');
        if (!currentSections.length) {
            container.innerHTML = '<div class="empty-state" style="padding: 20px 0;">No sections yet. Add one above.</div>';
            return;
        }

        let html = '';
        currentSections.sort((a, b) => (a.order || 0) - (b.order || 0));
        currentSections.forEach((section, i) => {
            const label = SECTION_LABELS[section.type] || section.type;
            const icon = SECTION_ICONS[section.type] || 'widgets';
            html += `
                <div class="section-card" data-id="${section.id}">
                    <div style="display: flex; align-items: center; gap: 8px; flex: 1;">
                        <span class="material-symbols-rounded" style="font-size: 20px; color: var(--primary);">${icon}</span>
                        <span style="font-weight: 500;">${label}</span>
                    </div>
                    <div style="display: flex; gap: 4px;">
                        <button class="icon-btn" onclick="LandingPages.moveSection(${i}, -1)" title="Move up" ${i === 0 ? 'disabled' : ''}>
                            <span class="material-symbols-rounded" style="font-size: 18px;">arrow_upward</span>
                        </button>
                        <button class="icon-btn" onclick="LandingPages.moveSection(${i}, 1)" title="Move down" ${i === currentSections.length - 1 ? 'disabled' : ''}>
                            <span class="material-symbols-rounded" style="font-size: 18px;">arrow_downward</span>
                        </button>
                        <button class="icon-btn" onclick="LandingPages.editSection('${section.id}')" title="Edit">
                            <span class="material-symbols-rounded" style="font-size: 18px;">edit</span>
                        </button>
                        <button class="icon-btn" onclick="LandingPages.deleteSection('${section.id}')" title="Delete">
                            <span class="material-symbols-rounded" style="font-size: 18px;">delete</span>
                        </button>
                    </div>
                </div>`;
        });
        container.innerHTML = html;
    }

    async function moveSection(index, direction) {
        const newIndex = index + direction;
        if (newIndex < 0 || newIndex >= currentSections.length) return;
        [currentSections[index], currentSections[newIndex]] = [currentSections[newIndex], currentSections[index]];
        currentSections.forEach((s, i) => s.order = i);
        renderSectionsList();
        updatePreview();

        if (currentBuilderPageId) {
            try {
                await apiRequest(`/api/landing-pages/${currentBuilderPageId}/sections/reorder`, {
                    method: 'POST',
                    body: JSON.stringify({ order: currentSections.map(s => s.id) }),
                });
            } catch (e) {
                showToast('Failed to save section order', 'error');
            }
        }
    }

    async function addSection(type) {
        if (!currentBuilderPageId) return;
        toggleAddMenu();
        const defaultData = getDefaultSectionData(type);
        try {
            const section = await apiRequest(`/api/landing-pages/${currentBuilderPageId}/sections`, {
                method: 'POST',
                body: JSON.stringify({ type, data: defaultData }),
            });
            currentSections.push(section);
            renderSectionsList();
            updatePreview();
            editSection(section.id);
        } catch (e) {
            showToast('Failed to add section', 'error');
        }
    }

    function getDefaultSectionData(type) {
        const defaults = {
            hero: { slides: [{ headline: 'Welcome', subheadline: '', cta_text: 'Shop Now', cta_url: '/collections/all', image_url: '', image_alt: '' }], categories: [] },
            features: { heading: 'Features', items: [{ icon: '⭐', title: 'Feature 1', description: 'Description here' }] },
            product_showcase: { heading: 'Featured Products', products: [] },
            testimonials: { heading: 'What Our Customers Say', items: [{ quote: '', author: '', role: '' }] },
            faq: { heading: 'Frequently Asked Questions', items: [{ question: 'Question here?', answer: 'Answer here.' }] },
            cta_banner: { heading: 'Ready to Get Started?', subtext: '', cta_text: 'Shop Now', cta_url: '/collections/all' },
            trust_badges: { heading: 'Why Choose Us', items: [{ icon: '✓', text: 'Free Shipping' }] },
            rich_text: { heading: '', content: '<p>Enter your content here...</p>' },
            image_gallery: { heading: 'Gallery', images: [] },
            statistics: { heading: '', items: [{ value: '', label: '' }] },
        };
        return defaults[type] || {};
    }

    async function deleteSection(sectionId) {
        if (!currentBuilderPageId) return;
        showConfirmModal('Delete Section', 'Are you sure you want to delete this section?', async () => {
            try {
                await apiRequest(`/api/landing-pages/${currentBuilderPageId}/sections/${sectionId}`, { method: 'DELETE' });
                currentSections = currentSections.filter(s => s.id !== sectionId);
                currentSections.forEach((s, i) => s.order = i);
                renderSectionsList();
                updatePreview();
            } catch (e) {
                showToast('Failed to delete section', 'error');
            }
        });
    }

    function editSection(sectionId) {
        const section = currentSections.find(s => s.id === sectionId);
        if (!section) return;
        editingSectionId = sectionId;

        const label = SECTION_LABELS[section.type] || section.type;
        document.getElementById('section-edit-title').textContent = `Edit ${label}`;
        document.getElementById('section-edit-form').innerHTML = buildSectionForm(section);
        document.getElementById('section-edit-modal').classList.add('active');
    }

    function buildSectionForm(section) {
        const d = section.data || {};
        switch (section.type) {
            case 'hero':
                return buildHeroForm(d);
            case 'features':
            case 'trust_badges':
                return buildRepeatableForm(d, section.type === 'features'
                    ? { heading: 'heading', fields: [{ key: 'icon', label: 'Icon' }, { key: 'title', label: 'Title' }, { key: 'description', label: 'Description' }] }
                    : { heading: 'heading', fields: [{ key: 'icon', label: 'Icon' }, { key: 'text', label: 'Text' }] });
            case 'testimonials':
                return buildRepeatableForm(d, { heading: 'heading', fields: [{ key: 'quote', label: 'Quote' }, { key: 'author', label: 'Author' }, { key: 'role', label: 'Role' }] });
            case 'faq':
                return buildRepeatableForm(d, { heading: 'heading', fields: [{ key: 'question', label: 'Question' }, { key: 'answer', label: 'Answer' }] });
            case 'statistics':
                return buildRepeatableForm(d, { heading: 'heading', fields: [{ key: 'value', label: 'Value' }, { key: 'label', label: 'Label' }] });
            case 'cta_banner':
                return `
                    <div class="form-group"><label>Heading</label><input type="text" class="form-control" id="se-heading" value="${escapeAttr(d.heading || '')}"></div>
                    <div class="form-group"><label>Subtext</label><input type="text" class="form-control" id="se-subtext" value="${escapeAttr(d.subtext || '')}"></div>
                    <div class="form-group"><label>CTA Text</label><input type="text" class="form-control" id="se-cta-text" value="${escapeAttr(d.cta_text || '')}"></div>
                    <div class="form-group"><label>CTA URL</label><input type="text" class="form-control" id="se-cta-url" value="${escapeAttr(d.cta_url || '')}"></div>`;
            case 'rich_text':
                return `
                    <div class="form-group"><label>Heading</label><input type="text" class="form-control" id="se-heading" value="${escapeAttr(d.heading || '')}"></div>
                    <div class="form-group"><label>Content (HTML)</label><textarea class="form-control" id="se-content" rows="8">${escapeHtml(d.content || '')}</textarea></div>`;
            case 'product_showcase':
                return `
                    <div class="form-group"><label>Heading</label><input type="text" class="form-control" id="se-heading" value="${escapeAttr(d.heading || '')}"></div>
                    <div class="form-group"><label>Products (JSON array)</label>
                    <textarea class="form-control" id="se-products" rows="6">${JSON.stringify(d.products || [], null, 2)}</textarea>
                    <small style="color: var(--on-surface-secondary);">Format: [{"title": "...", "handle": "...", "image_url": "...", "price": "..."}]</small></div>`;
            case 'image_gallery':
                return `
                    <div class="form-group"><label>Heading</label><input type="text" class="form-control" id="se-heading" value="${escapeAttr(d.heading || '')}"></div>
                    <div class="form-group"><label>Images (JSON array)</label>
                    <textarea class="form-control" id="se-images" rows="6">${JSON.stringify(d.images || [], null, 2)}</textarea>
                    <small style="color: var(--on-surface-secondary);">Format: [{"url": "...", "alt": "..."}]</small></div>`;
            default:
                return '<p>No editor available for this section type.</p>';
        }
    }

    function buildHeroForm(d) {
        // Normalize: support old flat format and new slides format
        let slides = d.slides || [];
        if (!slides.length) {
            slides = [{
                headline: d.headline || '', subheadline: d.subheadline || '',
                cta_text: d.cta_text || '', cta_url: d.cta_url || '',
                image_url: d.image_url || '', image_alt: d.image_alt || '',
            }];
        }
        const categories = d.categories || [];

        let html = '<h4 style="margin: 0 0 12px;">Slides</h4>';
        html += '<div id="se-hero-slides">';
        slides.forEach((slide, i) => {
            html += buildHeroSlideHtml(slide, i);
        });
        html += '</div>';
        html += `<button type="button" class="btn btn-secondary btn-sm" onclick="LandingPages.addHeroSlide()" style="margin-top: 8px;">
            <span class="material-symbols-rounded">add</span> Add Slide</button>`;

        html += '<hr style="margin: 16px 0; border-color: var(--outline);">';
        html += '<h4 style="margin: 0 0 12px;">Category Badges</h4>';
        html += '<div id="se-hero-categories">';
        categories.forEach((cat, i) => {
            html += `<div class="repeatable-item" style="border: 1px solid var(--outline); border-radius: 8px; padding: 12px; margin-bottom: 8px;">
                <div style="display: flex; justify-content: space-between; margin-bottom: 8px;"><strong>Category ${i + 1}</strong><button type="button" class="icon-btn" onclick="this.closest('.repeatable-item').remove()"><span class="material-symbols-rounded" style="font-size: 16px;">close</span></button></div>
                <div class="form-group"><label>Name</label><input type="text" class="form-control se-cat-name" value="${escapeAttr(cat.name || '')}"></div>
                <div class="form-group"><label>URL</label><input type="text" class="form-control se-cat-url" value="${escapeAttr(cat.url || '')}"></div>
            </div>`;
        });
        html += '</div>';
        html += `<button type="button" class="btn btn-secondary btn-sm" onclick="LandingPages.addHeroCategory()" style="margin-top: 8px;">
            <span class="material-symbols-rounded">add</span> Add Category</button>`;

        html += '<hr style="margin: 16px 0; border-color: var(--outline);">';
        html += `<button type="button" class="btn btn-secondary btn-sm" id="se-generate-hero-btn" onclick="LandingPages.generateHeroFromEditor()" style="margin-top: 4px;">
            <span class="material-symbols-rounded">auto_awesome</span> Generate with AI
        </button>`;
        html += '<div id="se-hero-preview" style="margin-top: 8px;"></div>';
        return html;
    }

    function buildHeroSlideHtml(slide, index) {
        return `<div class="hero-slide-item" style="border: 1px solid var(--outline); border-radius: 8px; padding: 12px; margin-bottom: 8px;">
            <div style="display: flex; justify-content: space-between; margin-bottom: 8px;"><strong>Slide ${index + 1}</strong><button type="button" class="icon-btn" onclick="this.closest('.hero-slide-item').remove()"><span class="material-symbols-rounded" style="font-size: 16px;">close</span></button></div>
            <div class="form-group"><label>Headline</label><input type="text" class="form-control se-slide-headline" value="${escapeAttr(slide.headline || '')}"></div>
            <div class="form-group"><label>Subheadline</label><input type="text" class="form-control se-slide-subheadline" value="${escapeAttr(slide.subheadline || '')}"></div>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                <div class="form-group"><label>CTA Text</label><input type="text" class="form-control se-slide-cta-text" value="${escapeAttr(slide.cta_text || '')}"></div>
                <div class="form-group"><label>CTA URL</label><input type="text" class="form-control se-slide-cta-url" value="${escapeAttr(slide.cta_url || '')}"></div>
            </div>
            <div class="form-group"><label>Image URL</label><input type="text" class="form-control se-slide-image-url" value="${escapeAttr(slide.image_url || '')}"></div>
            <div class="form-group"><label>Image Alt</label><input type="text" class="form-control se-slide-image-alt" value="${escapeAttr(slide.image_alt || '')}"></div>
        </div>`;
    }

    function addHeroSlide() {
        const container = document.getElementById('se-hero-slides');
        if (!container) return;
        const idx = container.children.length;
        container.insertAdjacentHTML('beforeend', buildHeroSlideHtml({}, idx));
    }

    function addHeroCategory() {
        const container = document.getElementById('se-hero-categories');
        if (!container) return;
        const idx = container.children.length;
        container.insertAdjacentHTML('beforeend', `<div class="repeatable-item" style="border: 1px solid var(--outline); border-radius: 8px; padding: 12px; margin-bottom: 8px;">
            <div style="display: flex; justify-content: space-between; margin-bottom: 8px;"><strong>Category ${idx + 1}</strong><button type="button" class="icon-btn" onclick="this.closest('.repeatable-item').remove()"><span class="material-symbols-rounded" style="font-size: 16px;">close</span></button></div>
            <div class="form-group"><label>Name</label><input type="text" class="form-control se-cat-name" value=""></div>
            <div class="form-group"><label>URL</label><input type="text" class="form-control se-cat-url" value=""></div>
        </div>`);
    }

    function buildRepeatableForm(data, config) {
        const items = data.items || [];
        let html = `<div class="form-group"><label>Heading</label><input type="text" class="form-control" id="se-heading" value="${escapeAttr(data[config.heading] || data.heading || '')}"></div>`;
        html += '<div id="se-repeatable-items">';
        items.forEach((item, i) => {
            html += `<div class="repeatable-item" style="border: 1px solid var(--outline); border-radius: 8px; padding: 12px; margin-bottom: 8px;">`;
            html += `<div style="display: flex; justify-content: space-between; margin-bottom: 8px;"><strong>#${i + 1}</strong><button type="button" class="icon-btn" onclick="this.closest('.repeatable-item').remove()"><span class="material-symbols-rounded" style="font-size: 16px;">close</span></button></div>`;
            config.fields.forEach(f => {
                html += `<div class="form-group"><label>${f.label}</label><input type="text" class="form-control se-item-field" data-key="${f.key}" data-index="${i}" value="${escapeAttr(item[f.key] || '')}"></div>`;
            });
            html += '</div>';
        });
        html += '</div>';
        html += `<button type="button" class="btn btn-secondary btn-sm" onclick="LandingPages.addRepeatableItem()" style="margin-top: 8px;">
            <span class="material-symbols-rounded">add</span> Add Item</button>`;
        return html;
    }

    function addRepeatableItem() {
        const container = document.getElementById('se-repeatable-items');
        if (!container) return;
        const section = currentSections.find(s => s.id === editingSectionId);
        if (!section) return;

        const typeConfigs = {
            features: [{ key: 'icon', label: 'Icon' }, { key: 'title', label: 'Title' }, { key: 'description', label: 'Description' }],
            trust_badges: [{ key: 'icon', label: 'Icon' }, { key: 'text', label: 'Text' }],
            testimonials: [{ key: 'quote', label: 'Quote' }, { key: 'author', label: 'Author' }, { key: 'role', label: 'Role' }],
            faq: [{ key: 'question', label: 'Question' }, { key: 'answer', label: 'Answer' }],
            statistics: [{ key: 'value', label: 'Value' }, { key: 'label', label: 'Label' }],
        };
        const fields = typeConfigs[section.type] || [];
        const idx = container.children.length;

        let html = `<div class="repeatable-item" style="border: 1px solid var(--outline); border-radius: 8px; padding: 12px; margin-bottom: 8px;">`;
        html += `<div style="display: flex; justify-content: space-between; margin-bottom: 8px;"><strong>#${idx + 1}</strong><button type="button" class="icon-btn" onclick="this.closest('.repeatable-item').remove()"><span class="material-symbols-rounded" style="font-size: 16px;">close</span></button></div>`;
        fields.forEach(f => {
            html += `<div class="form-group"><label>${f.label}</label><input type="text" class="form-control se-item-field" data-key="${f.key}" data-index="${idx}" value=""></div>`;
        });
        html += '</div>';
        container.insertAdjacentHTML('beforeend', html);
    }

    async function saveSectionEdit() {
        const section = currentSections.find(s => s.id === editingSectionId);
        if (!section || !currentBuilderPageId) return;

        const newData = extractSectionData(section.type);
        section.data = newData;

        try {
            await apiRequest(`/api/landing-pages/${currentBuilderPageId}/sections/${editingSectionId}`, {
                method: 'PUT', body: JSON.stringify({ data: newData }),
            });
            closeModal('section-edit-modal');
            renderSectionsList();
            updatePreview();
        } catch (e) {
            showToast('Failed to save section', 'error');
        }
    }

    function extractSectionData(type) {
        const val = id => { const el = document.getElementById(id); return el ? el.value : ''; };

        switch (type) {
            case 'hero': {
                const slides = [];
                document.querySelectorAll('#se-hero-slides .hero-slide-item').forEach(el => {
                    slides.push({
                        headline: el.querySelector('.se-slide-headline')?.value || '',
                        subheadline: el.querySelector('.se-slide-subheadline')?.value || '',
                        cta_text: el.querySelector('.se-slide-cta-text')?.value || '',
                        cta_url: el.querySelector('.se-slide-cta-url')?.value || '',
                        image_url: el.querySelector('.se-slide-image-url')?.value || '',
                        image_alt: el.querySelector('.se-slide-image-alt')?.value || '',
                    });
                });
                const categories = [];
                document.querySelectorAll('#se-hero-categories .repeatable-item').forEach(el => {
                    const name = el.querySelector('.se-cat-name')?.value || '';
                    const url = el.querySelector('.se-cat-url')?.value || '';
                    if (name) categories.push({ name, url });
                });
                return { slides, categories };
            }
            case 'cta_banner':
                return { heading: val('se-heading'), subtext: val('se-subtext'), cta_text: val('se-cta-text'), cta_url: val('se-cta-url') };
            case 'rich_text':
                return { heading: val('se-heading'), content: val('se-content') };
            case 'product_showcase':
                try {
                    return { heading: val('se-heading'), products: JSON.parse(val('se-products')) };
                } catch (e) {
                    showToast('Invalid JSON in products field, using empty array', 'error');
                    return { heading: val('se-heading'), products: [] };
                }
            case 'image_gallery':
                try {
                    return { heading: val('se-heading'), images: JSON.parse(val('se-images')) };
                } catch (e) {
                    showToast('Invalid JSON in images field, using empty array', 'error');
                    return { heading: val('se-heading'), images: [] };
                }
            default:
                return extractRepeatableData();
        }
    }

    function extractRepeatableData() {
        const heading = document.getElementById('se-heading')?.value || '';
        const items = [];
        document.querySelectorAll('#se-repeatable-items .repeatable-item').forEach(itemEl => {
            const item = {};
            itemEl.querySelectorAll('.se-item-field').forEach(field => {
                item[field.dataset.key] = field.value;
            });
            if (Object.values(item).some(v => v)) items.push(item);
        });
        return { heading, items };
    }

    // Returns true on success so callers (publish, manual version) can abort
    // instead of proceeding with stale server-side content.
    async function saveBuilderPage() {
        if (!currentBuilderPageId) return false;
        const btn = document.querySelector('[onclick="LandingPages.saveBuilderPage()"]');
        const origHtml = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="material-symbols-rounded spin">progress_activity</span> Saving...'; }

        const data = {
            title: document.getElementById('bp-title').value,
            handle: document.getElementById('bp-handle').value,
            meta_title: document.getElementById('bp-meta-title').value,
            meta_description: document.getElementById('bp-meta-desc').value,
            sections: currentSections,
        };
        try {
            await apiRequest(`/api/landing-pages/${currentBuilderPageId}`, {
                method: 'PUT', body: JSON.stringify(data),
            });
            showToast('Page saved');
            document.getElementById('builder-page-title').textContent = data.title;
            return true;
        } catch (e) {
            showToast('Failed to save: ' + e.message, 'error');
            return false;
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = origHtml; }
        }
    }

    async function updatePreview() {
        if (!currentBuilderPageId) return;
        try {
            const { html } = await apiRequest(`/api/landing-pages/${currentBuilderPageId}/preview`);
            const container = document.getElementById('preview-container');
            container.innerHTML = '';
            const iframe = document.createElement('iframe');
            iframe.style.cssText = 'width:100%;border:none;min-height:300px;background:#fff;border-radius:8px;';
            iframe.sandbox = 'allow-same-origin';
            container.appendChild(iframe);
            iframe.srcdoc = html;
            iframe.onload = function() {
                try {
                    const h = iframe.contentDocument.documentElement.scrollHeight;
                    iframe.style.height = Math.min(h, 600) + 'px';
                } catch(e) {}
            };
        } catch (e) {
            showToast('Preview failed: ' + e.message, 'error');
        }
    }

    async function publishPage() {
        if (!currentBuilderPageId) return;
        const btn = document.querySelector('[onclick="LandingPages.publishPage()"]');
        const origHtml = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="material-symbols-rounded spin">progress_activity</span> Publishing...'; }

        try {
            const saved = await saveBuilderPage();
            if (!saved) return;
            await apiRequest(`/api/landing-pages/${currentBuilderPageId}/publish`, { method: 'POST' });
            showToast('Page published to Shopify!');
            loadPageBuilder(currentBuilderPageId);
        } catch (e) {
            showToast('Publish failed: ' + e.message, 'error');
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = origHtml; }
        }
    }

    async function importToBuilder(syncedPageId, btnEl) {
        const origHtml = btnEl ? btnEl.innerHTML : '';
        if (btnEl) { btnEl.disabled = true; btnEl.innerHTML = '<span class="material-symbols-rounded spin" style="font-size: 16px;">progress_activity</span>'; }
        try {
            const result = await apiRequest('/api/landing-pages/import', {
                method: 'POST', body: JSON.stringify({ synced_page_id: syncedPageId }),
            });
            loadPageBuilder(result.id);
            showToast('Imported to builder');
        } catch (e) {
            showToast('Import failed: ' + e.message, 'error');
        } finally {
            if (btnEl) { btnEl.disabled = false; btnEl.innerHTML = origHtml; }
        }
    }

    // ── Hero Image Generation from Editor ─────────────────────────

    async function generateHeroFromEditor() {
        if (!currentBuilderPageId) return;
        const btn = document.getElementById('se-generate-hero-btn');
        const origHtml = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="material-symbols-rounded spin">progress_activity</span> Generating...'; }

        try {
            const { task_id } = await apiRequest(`/api/landing-pages/${currentBuilderPageId}/generate-hero`, { method: 'POST' });
            await pollTask(task_id, (result) => {
                if (result.image_url) {
                    // Set image on first slide
                    const firstSlide = document.querySelector('#se-hero-slides .hero-slide-item');
                    if (firstSlide) {
                        const urlInput = firstSlide.querySelector('.se-slide-image-url');
                        const altInput = firstSlide.querySelector('.se-slide-image-alt');
                        if (urlInput) urlInput.value = result.image_url;
                        if (altInput && result.alt_text) altInput.value = result.alt_text;
                    }
                    const preview = document.getElementById('se-hero-preview');
                    if (preview) {
                        // Show a styled hero preview with text overlay
                        const firstSlide = document.querySelector('#se-hero-slides .hero-slide-item');
                        const headline = firstSlide?.querySelector('.se-slide-headline')?.value || '';
                        const subheadline = firstSlide?.querySelector('.se-slide-subheadline')?.value || '';
                        const ctaText = firstSlide?.querySelector('.se-slide-cta-text')?.value || '';
                        preview.innerHTML = `<div style="position:relative;border-radius:8px;overflow:hidden;min-height:200px;">
                            <img src="${escapeAttr(result.image_url)}" alt="${escapeAttr(result.alt_text || '')}" style="width:100%;display:block;">
                            <div style="position:absolute;inset:0;background:linear-gradient(180deg,rgba(0,0,0,0.3) 0%,rgba(0,0,0,0.5) 50%,rgba(0,0,0,0.65) 100%);"></div>
                            <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;width:85%;z-index:2;">
                                <div style="color:#fff;font-size:20px;font-weight:800;text-shadow:0 2px 8px rgba(0,0,0,0.5);margin-bottom:6px;">${escapeHtml(headline)}</div>
                                ${subheadline ? `<div style="color:rgba(255,255,255,0.9);font-size:12px;margin-bottom:10px;text-shadow:0 1px 4px rgba(0,0,0,0.3);">${escapeHtml(subheadline)}</div>` : ''}
                                ${ctaText ? `<span style="display:inline-block;padding:6px 18px;background:#fff;color:#111;border-radius:6px;font-size:11px;font-weight:700;text-transform:uppercase;">${escapeHtml(ctaText)}</span>` : ''}
                            </div>
                        </div>`;
                    }
                    showToast('Hero image generated!');
                } else {
                    showToast('Image generation returned no URL', 'error');
                }
            });
        } catch (e) {
            showToast('Hero image generation failed: ' + e.message, 'error');
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = origHtml; }
        }
    }

    // ── Confirm Modal ─────────────────────────────────────────────

    function showConfirmModal(title, message, onConfirm) {
        document.getElementById('confirm-modal-title').textContent = title;
        document.getElementById('confirm-modal-message').textContent = message;
        confirmCallback = onConfirm;
        document.getElementById('confirm-modal').classList.add('active');
    }

    function confirmModalAction() {
        closeModal('confirm-modal');
        if (confirmCallback) {
            confirmCallback();
            confirmCallback = null;
        }
    }

    // ── Modals ────────────────────────────────────────────────────

    function openNewPageModal() {
        if (!currentStoreId) { showToast('Select a store first', 'error'); return; }
        document.getElementById('new-page-title').value = '';
        document.getElementById('new-page-handle').value = '';
        document.getElementById('new-page-modal').classList.add('active');
    }

    function openGenerateModal() {
        if (!currentStoreId) { showToast('Select a store first', 'error'); return; }
        document.getElementById('gen-topic').value = '';
        document.getElementById('gen-keywords').value = '';
        document.getElementById('generate-modal').classList.add('active');
    }

    function closeModal(id) {
        document.getElementById(id).classList.remove('active');
    }

    async function createNewPage() {
        const title = document.getElementById('new-page-title').value.trim();
        if (!title) { showToast('Title required', 'error'); return; }
        const btn = document.querySelector('#new-page-modal .btn-primary');
        const origHtml = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="material-symbols-rounded spin">progress_activity</span> Creating...'; }

        try {
            const page = await apiRequest('/api/landing-pages', {
                method: 'POST',
                body: JSON.stringify({
                    store_id: currentStoreId,
                    title,
                    handle: document.getElementById('new-page-handle').value.trim() || undefined,
                }),
            });
            closeModal('new-page-modal');
            loadPageBuilder(page.id);
            showToast('Page created');
        } catch (e) {
            showToast('Failed to create page: ' + e.message, 'error');
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = origHtml; }
        }
    }

    async function generatePage() {
        const topic = document.getElementById('gen-topic').value.trim();
        if (!topic) { showToast('Topic required', 'error'); return; }
        const keywordsStr = document.getElementById('gen-keywords').value.trim();
        const keywords = keywordsStr ? keywordsStr.split(',').map(k => k.trim()).filter(Boolean) : [];

        const btn = document.querySelector('#generate-modal .btn-primary');
        const origHtml = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="material-symbols-rounded spin">progress_activity</span> Generating...'; }

        try {
            const { task_id } = await apiRequest('/api/landing-pages/ai-generate', {
                method: 'POST',
                body: JSON.stringify({ store_id: currentStoreId, topic, keywords }),
            });
            closeModal('generate-modal');
            showToast('Generating page with AI...');
            await pollTask(task_id, async (result) => {
                if (result.sections) {
                    const page = await apiRequest('/api/landing-pages', {
                        method: 'POST',
                        body: JSON.stringify({
                            store_id: currentStoreId,
                            title: result.title || topic,
                            handle: result.handle || '',
                            meta_title: result.meta_title || '',
                            meta_description: result.meta_description || '',
                        }),
                    });
                    const sections = result.sections.map((s, i) => ({
                        ...s,
                        id: s.id || crypto.randomUUID(),
                        order: s.order !== undefined ? s.order : i,
                    }));
                    await apiRequest(`/api/landing-pages/${page.id}`, {
                        method: 'PUT',
                        body: JSON.stringify({ sections }),
                    });
                    loadPageBuilder(page.id);
                    showToast('AI page generated!');
                } else {
                    showToast('AI generation returned unexpected format', 'error');
                }
            });
        } catch (e) {
            showToast('Generation failed: ' + e.message, 'error');
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = origHtml; }
        }
    }

    function toggleAddMenu() {
        const menu = document.getElementById('add-section-menu');
        menu.style.display = menu.style.display === 'none' ? 'block' : 'none';
    }

    // ── Helpers ────────────────────────────────────────────────────

    async function pollTask(taskId, onComplete) {
        const task = await pollTaskUntilDone(`/api/tasks/${taskId}`, { intervalMs: 2000, timeoutMs: 2 * 60 * 1000 });
        if (!task.result) throw new Error('Task completed without a result');
        const result = typeof task.result === 'string' ? JSON.parse(task.result) : task.result;
        await onComplete(result);
    }

    // ── Version History ─────────────────────────────────────────

    function toggleVersions() {
        const panel = document.getElementById('versions-panel');
        if (panel.style.display === 'none') {
            panel.style.display = 'block';
            loadVersions();
        } else {
            panel.style.display = 'none';
        }
    }

    async function loadVersions() {
        if (!currentBuilderPageId) return;
        const container = document.getElementById('versions-list');
        container.innerHTML = '<p style="color:var(--on-surface-secondary)">Loading...</p>';
        try {
            const versions = await apiRequest(`/api/landing-pages/${currentBuilderPageId}/versions`);
            if (!versions.length) {
                container.innerHTML = '<div class="empty-state" style="padding: 16px 0;">No versions saved yet. Versions are created automatically before publish, AI optimize, and re-import.</div>';
                return;
            }
            let html = '';
            versions.forEach(v => {
                const date = new Date(v.created_at).toLocaleString();
                html += `
                    <div style="display: flex; align-items: center; gap: 8px; padding: 10px 0; border-bottom: 1px solid var(--outline);">
                        <span class="material-symbols-rounded" style="font-size: 18px; color: var(--primary);">restore</span>
                        <div style="flex: 1; min-width: 0;">
                            <div style="font-weight: 500; font-size: 14px;">${escapeHtml(v.label)}</div>
                            <div style="font-size: 12px; color: var(--on-surface-secondary);">${date}</div>
                        </div>
                        <button class="btn btn-secondary btn-sm" onclick="LandingPages.restoreVersion(${v.id})" title="Restore this version">
                            <span class="material-symbols-rounded" style="font-size: 16px;">undo</span> Restore
                        </button>
                        <button class="icon-btn" onclick="LandingPages.deleteVersion(${v.id})" title="Delete version">
                            <span class="material-symbols-rounded" style="font-size: 16px;">delete</span>
                        </button>
                    </div>`;
            });
            container.innerHTML = html;
        } catch (e) {
            container.innerHTML = '<p style="color: var(--error);">Failed to load versions.</p>';
        }
    }

    async function saveManualVersion() {
        if (!currentBuilderPageId) return;
        const label = prompt('Version label:', 'Manual save');
        if (!label) return;
        try {
            const saved = await saveBuilderPage();
            if (!saved) return;
            await apiRequest(`/api/landing-pages/${currentBuilderPageId}/versions`, {
                method: 'POST', body: JSON.stringify({ label }),
            });
            showToast('Version saved');
            loadVersions();
        } catch (e) {
            showToast('Failed to save version: ' + e.message, 'error');
        }
    }

    async function restoreVersion(versionId) {
        if (!currentBuilderPageId) return;
        if (!confirm('Restore this version? Your current state will be backed up automatically.')) return;
        try {
            await apiRequest(`/api/landing-pages/${currentBuilderPageId}/versions/${versionId}/restore`, {
                method: 'POST',
            });
            showToast('Version restored');
            loadPageBuilder(currentBuilderPageId);
            loadVersions();
        } catch (e) {
            showToast('Restore failed: ' + e.message, 'error');
        }
    }

    async function deleteVersion(versionId) {
        if (!currentBuilderPageId) return;
        if (!confirm('Delete this version?')) return;
        try {
            await apiRequest(`/api/landing-pages/${currentBuilderPageId}/versions/${versionId}`, {
                method: 'DELETE',
            });
            showToast('Version deleted');
            loadVersions();
        } catch (e) {
            showToast('Delete failed: ' + e.message, 'error');
        }
    }

    // ── Public API ────────────────────────────────────────────────

    window.LandingPages = {
        syncPages,
        loadSyncedPages,
        openSeoAnalysis,
        aiOptimizeSynced,
        runAiOptimize,
        applyRecommendations,
        importToBuilder,
        loadPageBuilder,
        addSection,
        moveSection,
        editSection,
        deleteSection,
        saveSectionEdit,
        saveBuilderPage,
        updatePreview,
        publishPage,
        openNewPageModal,
        openGenerateModal,
        closeModal,
        createNewPage,
        generatePage,
        toggleAddMenu,
        toggleSchema,
        addRepeatableItem,
        generateHeroFromEditor,
        addHeroSlide,
        addHeroCategory,
        confirmModalAction,
        toggleVersions,
        saveManualVersion,
        restoreVersion,
        deleteVersion,
    };

    document.addEventListener('DOMContentLoaded', init);

    // Close dropdown on outside click
    document.addEventListener('click', (e) => {
        const menu = document.getElementById('add-section-menu');
        if (menu && menu.style.display !== 'none' && !e.target.closest('.dropdown')) {
            menu.style.display = 'none';
        }
    });
})();
