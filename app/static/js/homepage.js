(function () {
    'use strict';

    let currentStoreId = null;
    let slides = [];
    let isPublished = false;
    let imageModalIndex = null;

    // ── Init ──────────────────────────────────────────────────────

    async function init() {
        await loadStores();
        setupCharCounters();
        setupSlideInputBinding();
    }

    async function loadStores() {
        try {
            const stores = await apiRequest('/api/stores');
            const sel = document.getElementById('hp-store-select');
            if (!sel) return;
            sel.innerHTML = '<option value="">Select store...</option>';
            stores.forEach(s => {
                sel.innerHTML += `<option value="${s.id}">${escapeHtml(s.name)} (${escapeHtml(s.domain)})</option>`;
            });
            sel.addEventListener('change', () => {
                currentStoreId = sel.value ? parseInt(sel.value) : null;
                if (currentStoreId) loadHomePage();
                else showWorkspace(false);
            });
            if (stores.length === 1) {
                sel.value = stores[0].id;
                currentStoreId = stores[0].id;
                loadHomePage();
            }
        } catch (e) {
            showToast('Failed to load stores: ' + e.message, 'error');
        }
    }

    function showWorkspace(show) {
        document.getElementById('hp-workspace').style.display = show ? 'block' : 'none';
        document.getElementById('hp-empty').style.display = show ? 'none' : 'block';
    }

    async function loadHomePage() {
        try {
            const data = await apiRequest(`/api/home-page?store_id=${currentStoreId}`);
            const home = data.home_page || {};
            slides = home.slides || [];
            document.getElementById('hp-meta-title').value = home.meta_title || '';
            document.getElementById('hp-meta-desc').value = home.meta_description || '';
            isPublished = !!home.is_published;
            updatePublishedBadge();
            updateCounters();
            renderSlides();
            showWorkspace(true);
            refreshPreview();
        } catch (e) {
            showToast('Failed to load home page: ' + e.message, 'error');
        }
    }

    function updatePublishedBadge() {
        const badge = document.getElementById('hp-published-badge');
        badge.style.display = isPublished ? 'inline-flex' : 'none';
    }

    // ── Meta counters ─────────────────────────────────────────────

    function setupCharCounters() {
        document.getElementById('hp-meta-title').addEventListener('input', updateCounters);
        document.getElementById('hp-meta-desc').addEventListener('input', updateCounters);
    }

    function counterText(len, min, max) {
        const ok = len >= min && len <= max;
        return `${len} chars ${ok ? '✓' : `(aim ${min}–${max})`}`;
    }

    function updateCounters() {
        const t = document.getElementById('hp-meta-title').value.length;
        const d = document.getElementById('hp-meta-desc').value.length;
        document.getElementById('hp-meta-title-counter').textContent = counterText(t, 50, 60);
        document.getElementById('hp-meta-desc-counter').textContent = counterText(d, 140, 160);
    }

    // ── Slides ────────────────────────────────────────────────────

    function renderSlides() {
        const container = document.getElementById('hp-slides');
        if (!slides.length) {
            container.innerHTML = '<div class="empty-state" style="padding: 16px 0;">No slides yet. Add a slide or generate with AI.</div>';
            return;
        }
        container.innerHTML = slides.map((s, i) => slideCard(s, i)).join('');
    }

    function slideCard(s, i) {
        const thumb = s.image_url
            ? `<img src="${escapeAttr(s.image_url)}" alt="" style="width: 100%; height: 90px; object-fit: cover; border-radius: 6px;">`
            : `<div style="width: 100%; height: 90px; display: flex; align-items: center; justify-content: center; background: var(--surface-variant, #eee); border-radius: 6px; color: var(--on-surface-secondary); font-size: 12px;">No image</div>`;
        const tag = i === 0 ? '<span class="badge badge-success" title="This headline is the page H1">H1</span>' : '<span class="badge">H2</span>';
        return `
        <div class="hp-slide" data-idx="${i}" style="border: 1px solid var(--outline); border-radius: 8px; padding: 12px; margin-bottom: 10px;">
            <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
                <strong>Slide ${i + 1}</strong> ${tag}
                <div style="flex: 1;"></div>
                <button class="icon-btn" title="Move up" onclick="HomePage.moveSlide(${i}, -1)"><span class="material-symbols-rounded" style="font-size: 18px;">arrow_upward</span></button>
                <button class="icon-btn" title="Move down" onclick="HomePage.moveSlide(${i}, 1)"><span class="material-symbols-rounded" style="font-size: 18px;">arrow_downward</span></button>
                <button class="icon-btn" title="Remove" onclick="HomePage.removeSlide(${i})"><span class="material-symbols-rounded" style="font-size: 18px;">delete</span></button>
            </div>
            <div style="display: grid; grid-template-columns: 160px 1fr; gap: 12px;">
                <div>
                    ${thumb}
                    <div style="display: flex; flex-direction: column; gap: 4px; margin-top: 6px;">
                        <button class="btn btn-secondary btn-sm" onclick="HomePage.generateImage(${i})"><span class="material-symbols-rounded" style="font-size: 16px;">auto_awesome</span> AI Image</button>
                        <div style="display: flex; gap: 4px;">
                            <button class="btn btn-secondary btn-sm" style="flex:1;" onclick="HomePage.openImageModal(${i}, 'product')">Product</button>
                            <button class="btn btn-secondary btn-sm" style="flex:1;" onclick="HomePage.openImageModal(${i}, 'collection')">Collection</button>
                        </div>
                    </div>
                </div>
                <div style="display: flex; flex-direction: column; gap: 6px;">
                    <input class="form-control" data-field="headline" placeholder="Headline (${i === 0 ? 'H1 — include primary keyword' : 'H2'})" value="${escapeAttr(s.headline)}">
                    <input class="form-control" data-field="subheadline" placeholder="Subheadline" value="${escapeAttr(s.subheadline)}">
                    <div style="display: flex; gap: 6px;">
                        <input class="form-control" data-field="cta_text" placeholder="CTA text" value="${escapeAttr(s.cta_text)}" style="flex: 1;">
                        <input class="form-control" data-field="cta_url" placeholder="/collections/handle" value="${escapeAttr(s.cta_url)}" style="flex: 2;">
                    </div>
                    <input class="form-control" data-field="image_alt" placeholder="Image alt text" value="${escapeAttr(s.image_alt)}">
                    <input class="form-control" data-field="image_prompt" placeholder="AI image prompt (what the background should show)" value="${escapeAttr(s.image_prompt)}">
                </div>
            </div>
        </div>`;
    }

    // Update state on text input without re-rendering (preserves focus).
    function setupSlideInputBinding() {
        document.getElementById('hp-slides').addEventListener('input', (e) => {
            const field = e.target.dataset.field;
            if (!field) return;
            const card = e.target.closest('.hp-slide');
            if (!card) return;
            const idx = parseInt(card.dataset.idx);
            if (slides[idx]) slides[idx][field] = e.target.value;
        });
    }

    function addSlide() {
        slides.push({ headline: '', subheadline: '', cta_text: 'Shop Now', cta_url: '/collections/all', image_url: '', image_alt: '', image_prompt: '', source: 'ai' });
        renderSlides();
    }

    function removeSlide(i) {
        slides.splice(i, 1);
        renderSlides();
        refreshPreview();
    }

    function moveSlide(i, dir) {
        const j = i + dir;
        if (j < 0 || j >= slides.length) return;
        [slides[i], slides[j]] = [slides[j], slides[i]];
        renderSlides();
        refreshPreview();
    }

    // ── AI generation ─────────────────────────────────────────────

    async function generate() {
        if (!currentStoreId) return;
        const topic = document.getElementById('hp-topic').value.trim();
        const keywords = document.getElementById('hp-keywords').value.split(',').map(k => k.trim()).filter(Boolean);
        if (!topic) { showToast('Enter a store theme / topic first', 'error'); return; }
        showToast('Generating carousel… this can take up to a minute', 'info');
        try {
            const { task_id } = await apiRequest('/api/home-page/generate', {
                method: 'POST', body: { store_id: currentStoreId, topic, keywords },
            });
            await pollTask(task_id, (result) => {
                if (result.raw_response) { showToast('AI response could not be parsed', 'error'); return; }
                document.getElementById('hp-meta-title').value = result.meta_title || '';
                document.getElementById('hp-meta-desc').value = result.meta_description || '';
                slides = result.slides || [];
                updateCounters();
                renderSlides();
                showToast('Carousel generated. Now generate images for each slide.');
                refreshPreview();
            });
        } catch (e) {
            showToast('Generation failed: ' + e.message, 'error');
        }
    }

    async function generateImage(i) {
        if (!currentStoreId || !slides[i]) return;
        showToast(`Generating AI image for slide ${i + 1}…`, 'info');
        try {
            const { task_id } = await apiRequest('/api/home-page/generate-image', {
                method: 'POST',
                body: {
                    store_id: currentStoreId,
                    slide_index: i,
                    prompt: slides[i].image_prompt || '',
                    headline: slides[i].headline || '',
                },
            });
            await pollTask(task_id, (result) => {
                if (!result.image_url) { showToast('No image returned', 'error'); return; }
                slides[i].image_url = result.image_url;
                slides[i].source = 'ai';
                if (!slides[i].image_alt) slides[i].image_alt = result.alt_text || '';
                renderSlides();
                refreshPreview();
                showToast(`Image ready for slide ${i + 1}`);
            });
        } catch (e) {
            showToast('Image generation failed: ' + e.message, 'error');
        }
    }

    // ── Real image picker ─────────────────────────────────────────

    async function openImageModal(i, source) {
        imageModalIndex = i;
        const modal = document.getElementById('hp-image-modal');
        const grid = document.getElementById('hp-image-grid');
        document.getElementById('hp-image-modal-title').textContent =
            source === 'collection' ? 'Pick a collection image' : 'Pick a product image';
        grid.innerHTML = '<p style="grid-column: 1/-1; color: var(--on-surface-secondary);">Loading…</p>';
        modal.classList.add('active');
        try {
            const data = await apiRequest(`/api/home-page/images?store_id=${currentStoreId}&source=${source}`);
            const imgs = data.images || [];
            if (!imgs.length) {
                grid.innerHTML = '<p style="grid-column: 1/-1;">No images found. Sync your store products/collections first.</p>';
                return;
            }
            grid.innerHTML = imgs.map((img, k) => `
                <div style="cursor: pointer; border: 1px solid var(--outline); border-radius: 6px; overflow: hidden;" onclick="HomePage.pickImage(${k})">
                    <img src="${escapeAttr(img.url)}" alt="" style="width: 100%; height: 100px; object-fit: cover;">
                    <div style="padding: 4px 6px; font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${escapeHtml(img.alt)}</div>
                </div>`).join('');
            window._hpImageCandidates = imgs;
        } catch (e) {
            grid.innerHTML = '<p style="grid-column: 1/-1; color: var(--error);">Failed to load images.</p>';
        }
    }

    function pickImage(k) {
        const img = (window._hpImageCandidates || [])[k];
        const i = imageModalIndex;
        if (!img || i == null || !slides[i]) return;
        slides[i].image_url = img.url;
        slides[i].source = 'product';
        if (!slides[i].image_alt) slides[i].image_alt = img.alt || '';
        if (!slides[i].cta_url || slides[i].cta_url === '/collections/all') slides[i].cta_url = img.cta_url || slides[i].cta_url;
        if (!slides[i].cta_text) slides[i].cta_text = img.cta_text || 'Shop Now';
        closeImageModal();
        renderSlides();
        refreshPreview();
    }

    function closeImageModal() {
        document.getElementById('hp-image-modal').classList.remove('active');
        imageModalIndex = null;
    }

    // ── Save / Preview / Publish ──────────────────────────────────

    function collectState() {
        return {
            store_id: currentStoreId,
            meta_title: document.getElementById('hp-meta-title').value,
            meta_description: document.getElementById('hp-meta-desc').value,
            slides,
        };
    }

    async function save(silent) {
        if (!currentStoreId) return;
        await apiRequest('/api/home-page', { method: 'PUT', body: collectState() });
        if (!silent) showToast('Saved');
    }

    async function refreshPreview() {
        if (!currentStoreId) return;
        try {
            await save(true);
            const data = await apiRequest(`/api/home-page/preview?store_id=${currentStoreId}`);
            const frame = document.getElementById('hp-preview');
            frame.srcdoc = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>body{margin:0;font-family:system-ui,-apple-system,sans-serif}</style></head><body>${data.html}</body></html>`;
        } catch (e) {
            showToast('Preview failed: ' + e.message, 'error');
        }
    }

    async function publish() {
        if (!currentStoreId) return;
        if (!slides.length) { showToast('Add at least one slide first', 'error'); return; }
        if (!confirm('Publish this carousel to your live home page?\n\nThis modifies your active theme (templates/index.json). A backup of the original is saved automatically, and you can Unpublish to remove it.')) return;
        try {
            await save(true);
            await apiRequest('/api/home-page/publish', { method: 'POST', body: { store_id: currentStoreId } });
            isPublished = true;
            updatePublishedBadge();
            showToast('Published to your home page 🎉');
        } catch (e) {
            showToast('Publish failed: ' + e.message, 'error');
        }
    }

    async function unpublish() {
        if (!currentStoreId) return;
        if (!confirm('Remove the AI carousel section from your live home page?')) return;
        try {
            await apiRequest('/api/home-page/unpublish', { method: 'POST', body: { store_id: currentStoreId } });
            isPublished = false;
            updatePublishedBadge();
            showToast('Removed from home page');
        } catch (e) {
            showToast('Unpublish failed: ' + e.message, 'error');
        }
    }

    // ── Task polling (mirrors landing_pages.js) ───────────────────

    async function pollTask(taskId, onComplete) {
        for (let i = 0; i < 60; i++) {
            await new Promise(r => setTimeout(r, 2000));
            try {
                const task = await apiRequest(`/api/tasks/${taskId}`);
                if (task.status === 'completed' && task.result) {
                    const result = typeof task.result === 'string' ? JSON.parse(task.result) : task.result;
                    onComplete(result);
                    return;
                }
                if (task.status === 'failed') {
                    throw new Error(task.error || 'Task failed');
                }
            } catch (e) {
                if (e.message.includes('Task failed') || e.message.includes('failed')) throw e;
            }
        }
        throw new Error('Task timed out');
    }

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str || '';
        return div.innerHTML;
    }

    function escapeAttr(str) {
        return (str || '').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // ── Public API ────────────────────────────────────────────────

    window.HomePage = {
        generate,
        addSlide,
        removeSlide,
        moveSlide,
        generateImage,
        openImageModal,
        pickImage,
        closeImageModal,
        save: () => save(false),
        refreshPreview,
        publish,
        unpublish,
    };

    document.addEventListener('DOMContentLoaded', init);
})();
