let cgStoreId = null;
let cgData = [];
let cgSort = { col: "opportunity_score", dir: "desc" };
let cgPage = 1;
const CG_PAGE_SIZE = 50;

document.addEventListener("DOMContentLoaded", () => {
    loadCgStores();
    initCgEvents();
});

async function loadCgStores() {
    const stores = await apiRequest("/api/stores");
    const select = document.getElementById("cg-store-select");
    stores.forEach(s => { const o = document.createElement("option"); o.value = s.id; o.textContent = s.name; select.appendChild(o); });
    if (stores.length === 1) { select.value = stores[0].id; onCgStore(stores[0].id); }
}

document.getElementById("cg-store-select").addEventListener("change", e => { if (e.target.value) onCgStore(parseInt(e.target.value)); });

function onCgStore(id) { cgStoreId = id; loadCgOverview(); loadCgGaps(); }

function initCgEvents() {
    document.getElementById("cg-discover-btn").addEventListener("click", async () => {
        if (!cgStoreId) return;
        const btn = document.getElementById("cg-discover-btn");
        btn.disabled = true; btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Discovering...';
        try {
            const r = await apiRequest(`/api/content-gaps/discover?store_id=${cgStoreId}`, { method: "POST" });
            showToast(`Found ${r.gaps_found} content gaps`, "success");
            loadCgOverview(); loadCgGaps();
        } catch (e) { showToast("Discovery failed: " + e.message, "error"); }
        finally { btn.disabled = false; btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">search</span> Run Gap Discovery'; }
    });

    let searchT;
    document.getElementById("cg-search").addEventListener("input", () => { clearTimeout(searchT); searchT = setTimeout(loadCgGaps, 300); });
    ["cg-type-filter", "cg-intent-filter", "cg-status-filter"].forEach(id => document.getElementById(id).addEventListener("change", loadCgGaps));
}

async function loadCgOverview() {
    if (!cgStoreId) return;
    try {
        const d = await apiRequest(`/api/content-gaps/overview?store_id=${cgStoreId}`);
        if (!d.total) { document.getElementById("cg-overview").style.display = "none"; return; }
        document.getElementById("cg-overview").style.display = "";
        document.getElementById("cg-total").textContent = d.total.toLocaleString();
        document.getElementById("cg-volume").textContent = d.total_volume.toLocaleString();
        document.getElementById("cg-score").textContent = d.avg_score;
        document.getElementById("cg-missing").textContent = `${d.by_type.missing} missing`;
        document.getElementById("cg-weak").textContent = `${d.by_type.weak} weak`;
        document.getElementById("cg-transactional").textContent = `${d.by_intent.transactional} txn`;
        document.getElementById("cg-commercial").textContent = `${d.by_intent.commercial} com`;
        document.getElementById("cg-informational").textContent = `${d.by_intent.informational} info`;
    } catch (e) {}
}

async function loadCgGaps() {
    if (!cgStoreId) return;
    document.getElementById("cg-content").style.display = "";
    const tbody = document.getElementById("cg-tbody");
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center"><div class="spinner" style="margin:20px auto"></div></td></tr>';

    const search = document.getElementById("cg-search").value.trim();
    const gapType = document.getElementById("cg-type-filter").value;
    const intent = document.getElementById("cg-intent-filter").value;
    const status = document.getElementById("cg-status-filter").value;
    let url = `/api/content-gaps?store_id=${cgStoreId}&limit=10000`;
    if (search) url += `&search=${encodeURIComponent(search)}`;
    if (gapType) url += `&gap_type=${gapType}`;
    if (intent) url += `&intent=${intent}`;
    if (status) url += `&status=${status}`;

    try {
        const d = await apiRequest(url);
        cgData = d.gaps || [];
        cgPage = 1;
        if (!cgData.length) { tbody.innerHTML = ""; document.getElementById("cg-empty").style.display = ""; document.getElementById("cg-count").textContent = "0 gaps"; return; }
        document.getElementById("cg-empty").style.display = "none";
        renderCgTable();
    } catch (e) { tbody.innerHTML = `<tr><td colspan="9" style="color:var(--error)">Failed: ${escapeHtml(e.message)}</td></tr>`; }
}

function sortCg(col) {
    if (cgSort.col === col) cgSort.dir = cgSort.dir === "desc" ? "asc" : "desc";
    else { cgSort.col = col; cgSort.dir = col === "keyword" ? "asc" : "desc"; }
    cgPage = 1; renderCgTable();
}
function goCgPage(p) { cgPage = p; renderCgTable(); }

function cgSortH(col, label) {
    const a = cgSort.col === col ? (cgSort.dir === "asc" ? " \u2191" : " \u2193") : "";
    const s = cgSort.col === col ? "color:var(--primary);" : "";
    return `<th class="sortable-header" style="cursor:pointer;user-select:none;${s}" onclick="sortCg('${col}')">${label}${a}</th>`;
}

function renderCgTable() {
    document.getElementById("cg-thead").innerHTML = cgSortH("keyword", "Keyword") + cgSortH("search_volume", "Volume")
        + cgSortH("our_position", "Our Pos.") + '<th>Intent</th>' + '<th>Type</th>'
        + cgSortH("opportunity_score", "Score") + '<th>Content</th>' + '<th>Status</th>' + '<th>Actions</th>';

    const sorted = [...cgData].sort((a, b) => {
        let va = a[cgSort.col], vb = b[cgSort.col];
        if (typeof va === "string") return cgSort.dir === "asc" ? (va||"").localeCompare(vb||"") : (vb||"").localeCompare(va||"");
        va = va ?? -99999; vb = vb ?? -99999;
        return cgSort.dir === "asc" ? va - vb : vb - va;
    });

    const start = (cgPage - 1) * CG_PAGE_SIZE;
    const page = sorted.slice(start, start + CG_PAGE_SIZE);
    document.getElementById("cg-count").textContent = `${cgData.length.toLocaleString()} gaps`;

    const intentColors = { transactional: "badge-success", commercial: "badge-warning", informational: "badge-info", navigational: "" };
    const typeColors = { missing: "badge-error", weak: "badge-warning", declining: "badge-info" };
    const statusIcons = { new: "", briefed: "description", in_progress: "edit", published: "check_circle", dismissed: "cancel" };

    document.getElementById("cg-tbody").innerHTML = page.map(r => {
        const ic = intentColors[r.intent] || "";
        const tc = typeColors[r.gap_type] || "";
        const scoreColor = r.opportunity_score > 100 ? "var(--success)" : r.opportunity_score > 30 ? "var(--warning)" : "var(--on-surface-secondary)";
        return `<tr>
            <td><strong>${escapeHtml(r.keyword)}</strong></td>
            <td>${r.search_volume ? r.search_volume.toLocaleString() : '—'}</td>
            <td>${r.our_position ? `<span class="badge ${r.our_position <= 10 ? 'badge-warning' : 'badge-error'}">${r.our_position}</span>` : '<span class="badge badge-error">N/A</span>'}</td>
            <td><span class="badge ${ic}">${r.intent || '—'}</span></td>
            <td><span class="badge ${tc}">${r.gap_type || '—'}</span></td>
            <td style="color:${scoreColor};font-weight:600;">${r.opportunity_score.toFixed(0)}</td>
            <td style="font-size:12px;">${r.suggested_content_type || '—'}</td>
            <td><span class="badge">${r.status}</span></td>
            <td style="white-space:nowrap;">
                <button class="action-icon" title="Generate Brief" onclick="generateCgBrief(${r.id})"><span class="material-symbols-rounded">description</span></button>
                ${r.has_brief ? `<button class="action-icon" title="View Brief" onclick="viewCgBrief(${r.id})"><span class="material-symbols-rounded">visibility</span></button>` : ''}
                <button class="action-icon" title="Dismiss" onclick="updateCgStatus(${r.id},'dismissed')"><span class="material-symbols-rounded" style="font-size:16px;">close</span></button>
            </td>
        </tr>`;
    }).join("") + `<tr><td colspan="9">${renderPagination("cg", cgPage, sorted.length, "goCgPage")}</td></tr>`;
}

function renderPagination(id, currentPage, totalItems, fn) {
    const totalPages = Math.ceil(totalItems / CG_PAGE_SIZE);
    if (totalPages <= 1) return "";
    let h = '<div style="display:flex;align-items:center;justify-content:center;gap:6px;padding:12px 0;">';
    h += `<button class="btn btn-secondary btn-sm" ${currentPage<=1?"disabled":""} onclick="${fn}(${currentPage-1})">&laquo;</button>`;
    const s = Math.max(1, currentPage-2), e = Math.min(totalPages, currentPage+2);
    if (s > 1) h += `<button class="btn btn-secondary btn-sm" onclick="${fn}(1)">1</button>`;
    if (s > 2) h += '<span>...</span>';
    for (let i = s; i <= e; i++) h += `<button class="btn btn-sm ${i===currentPage?'btn-primary':'btn-secondary'}" onclick="${fn}(${i})">${i}</button>`;
    if (e < totalPages-1) h += '<span>...</span>';
    if (e < totalPages) h += `<button class="btn btn-secondary btn-sm" onclick="${fn}(${totalPages})">${totalPages}</button>`;
    h += `<button class="btn btn-secondary btn-sm" ${currentPage>=totalPages?"disabled":""} onclick="${fn}(${currentPage+1})">&raquo;</button>`;
    h += `<span style="font-size:12px;color:var(--on-surface-secondary);margin-left:8px;">Page ${currentPage}/${totalPages} (${totalItems.toLocaleString()})</span></div>`;
    return h;
}

async function generateCgBrief(gapId) {
    showToast("Generating content brief...", "info");
    try {
        const brief = await apiRequest(`/api/content-gaps/${gapId}/brief?store_id=${cgStoreId}`, { method: "POST" });
        showBrief(gapId, brief);
        loadCgGaps();
    } catch (e) { showToast("Brief failed: " + e.message, "error"); }
}

async function viewCgBrief(gapId) {
    try {
        const cached = cgData.find(g => g.id === gapId);
        if (cached && cached.brief) {
            showBrief(gapId, cached.brief);
            return;
        }
        const gap = await apiRequest(`/api/content-gaps/${gapId}?store_id=${cgStoreId}`);
        if (gap && gap.brief) showBrief(gapId, gap.brief);
        else showToast("No brief available", "error");
    } catch (e) { showToast("Failed to load brief: " + e.message, "error"); }
}

let currentWizardGapId = null;
let currentWizardBrief = null;
let currentGeneratedContent = null;

function showBrief(gapId, brief) {
    currentWizardGapId = gapId;
    currentWizardBrief = brief;
    currentGeneratedContent = null;
    const modal = document.getElementById("cg-brief-modal");
    document.getElementById("cg-brief-title").textContent = `Content Wizard: ${brief.primary_keyword || ""}`;

    const gap = cgData.find(g => g.id === gapId);
    const suggestedType = gap?.suggested_content_type || "collection";

    let h = '<div style="display:flex;flex-direction:column;gap:16px;">';

    // Step indicator
    h += `<div style="display:flex;gap:8px;align-items:center;padding-bottom:12px;border-bottom:1px solid var(--outline);">
        <span class="badge badge-success" id="wiz-step-badge">Step 1: Review Brief</span>
        <span style="font-size:12px;color:var(--on-surface-secondary);">Select options below, then generate content</span>
    </div>`;

    const risk = brief.cannibalization_risk || {};
    if (risk.risk_level === "high" || risk.risk_level === "medium") {
        const riskColor = risk.risk_level === "high" ? "var(--error)" : "var(--warning)";
        const riskBg = risk.risk_level === "high" ? "var(--error-light)" : "var(--warning-light)";
        h += `<div style="padding:12px 16px;background:${riskBg};border:1px solid ${riskColor};border-radius:var(--radius-sm);margin-bottom:8px;">
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px;">
                <span class="material-symbols-rounded" style="color:${riskColor};font-size:20px;">warning</span>
                <strong style="color:${riskColor};font-size:14px;">Cannibalization Risk: ${risk.risk_level.toUpperCase()}</strong>
            </div>
            <p style="font-size:13px;margin:0 0 8px;">${escapeHtml(risk.recommendation)}</p>`;
        if (risk.existing_pages?.length) {
            h += '<div style="font-size:12px;margin-bottom:4px;"><strong>Pages already ranking for this keyword:</strong></div>';
            risk.existing_pages.forEach(p => {
                h += `<div style="font-size:12px;margin-left:8px;color:var(--on-surface-secondary);">- ${escapeHtml(p.url)} (pos: ${p.position}, ${p.source})</div>`;
            });
        }
        if (risk.on_site_matches?.length) {
            h += '<div style="font-size:12px;margin-top:4px;"><strong>On-site pages targeting this keyword:</strong></div>';
            risk.on_site_matches.forEach(m => {
                h += `<div style="font-size:12px;margin-left:8px;color:var(--on-surface-secondary);">- ${escapeHtml(m.title)} (/${m.type}s/${m.handle})</div>`;
            });
        }
        h += `<div style="margin-top:10px;display:flex;gap:8px;">
            <button class="btn btn-primary btn-sm" onclick="optimizeExistingPage(${gapId})">
                <span class="material-symbols-rounded" style="font-size:14px;vertical-align:middle;">tune</span> Optimize Existing Page Instead
            </button>
        </div></div>`;
    }

    if (brief.summary) h += `<p style="color:var(--on-surface-secondary);font-size:14px;">${escapeHtml(brief.summary)}</p>`;

    // Title selection
    if (brief.title_options?.length) {
        h += '<div><h4 style="font-size:13px;margin-bottom:8px;">Select Title</h4>';
        brief.title_options.forEach((t, i) => {
            h += `<label style="display:flex;align-items:center;gap:8px;padding:8px 10px;background:var(--surface-dim);border-radius:6px;margin-bottom:4px;cursor:pointer;font-size:13px;">
                <input type="radio" name="wiz-title" value="${escapeHtml(t)}" ${i === 0 ? "checked" : ""}> ${escapeHtml(t)}
            </label>`;
        });
        h += '</div>';
    }

    // Meta description (editable)
    h += `<div><h4 style="font-size:13px;margin-bottom:4px;">Meta Description</h4>
        <textarea id="wiz-meta-desc" class="form-control" style="font-size:13px;min-height:60px;">${escapeHtml(brief.meta_description || "")}</textarea></div>`;

    // Content type selection
    h += `<div><h4 style="font-size:13px;margin-bottom:8px;">Content Type</h4>
        <div style="display:flex;gap:8px;">
            <label style="display:flex;align-items:center;gap:6px;padding:8px 16px;background:var(--surface-dim);border-radius:6px;cursor:pointer;font-size:13px;border:2px solid ${suggestedType === 'collection' ? 'var(--primary)' : 'transparent'};">
                <input type="radio" name="wiz-type" value="collection" ${suggestedType === 'collection' ? 'checked' : ''}> Collection Page
            </label>
            <label style="display:flex;align-items:center;gap:6px;padding:8px 16px;background:var(--surface-dim);border-radius:6px;cursor:pointer;font-size:13px;border:2px solid ${suggestedType === 'blog' ? 'var(--primary)' : 'transparent'};">
                <input type="radio" name="wiz-type" value="blog" ${suggestedType === 'blog' ? 'checked' : ''}> Blog Article
            </label>
            <label style="display:flex;align-items:center;gap:6px;padding:8px 16px;background:var(--surface-dim);border-radius:6px;cursor:pointer;font-size:13px;border:2px solid ${suggestedType === 'page' ? 'var(--primary)' : 'transparent'};">
                <input type="radio" name="wiz-type" value="page" ${suggestedType === 'page' || suggestedType === 'guide' ? 'checked' : ''}> Page
            </label>
        </div></div>`;

    // Image generation checkbox
    h += `<label style="display:flex;align-items:center;gap:8px;font-size:13px;">
        <input type="checkbox" id="wiz-gen-image" checked> Generate AI image for this content
    </label>`;

    // Brief details (collapsible)
    h += `<details style="margin-top:8px;"><summary style="font-size:13px;cursor:pointer;color:var(--primary);">View Full Brief Details</summary><div style="margin-top:8px;">`;

    h += `<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px;">
        <div style="padding:10px;background:var(--surface-dim);border-radius:8px;text-align:center;"><div style="font-size:11px;color:var(--on-surface-secondary);">Word Count</div><div style="font-size:20px;font-weight:700;">${brief.target_word_count || '—'}</div></div>
        <div style="padding:10px;background:var(--surface-dim);border-radius:8px;text-align:center;"><div style="font-size:11px;color:var(--on-surface-secondary);">Format</div><div style="font-size:14px;font-weight:600;">${escapeHtml(brief.content_format || '—')}</div></div>
        <div style="padding:10px;background:var(--surface-dim);border-radius:8px;text-align:center;"><div style="font-size:11px;color:var(--on-surface-secondary);">Intent</div><div style="font-size:14px;font-weight:600;">${escapeHtml(brief.search_intent || '—')}</div></div>
    </div>`;

    if (brief.heading_structure?.length) {
        h += '<div style="margin-bottom:8px;"><strong style="font-size:12px;">Heading Structure:</strong>';
        brief.heading_structure.forEach(hs => {
            const indent = hs.tag === "h3" ? "padding-left:16px;" : "";
            h += `<div style="font-size:12px;${indent}">${hs.tag.toUpperCase()}: ${escapeHtml(hs.text)}</div>`;
        });
        h += '</div>';
    }

    if (brief.secondary_keywords?.length) {
        h += '<div style="margin-bottom:8px;"><strong style="font-size:12px;">Secondary Keywords:</strong> <span style="font-size:12px;">' + brief.secondary_keywords.map(k => escapeHtml(k)).join(", ") + '</span></div>';
    }

    if (brief.internal_links?.length) {
        h += '<div style="margin-bottom:8px;"><strong style="font-size:12px;">Internal Links to Include:</strong>';
        brief.internal_links.forEach(l => {
            const link = typeof l === 'string' ? l : (l.url || l.text || JSON.stringify(l));
            h += `<div style="font-size:12px;color:var(--primary);margin-left:8px;">- ${escapeHtml(link)}</div>`;
        });
        h += '</div>';
    }

    if (brief.questions_to_answer?.length) {
        h += '<div style="margin-bottom:8px;"><strong style="font-size:12px;">Questions to Answer:</strong>';
        brief.questions_to_answer.forEach(q => { h += `<div style="font-size:12px;margin-left:8px;">- ${escapeHtml(q)}</div>`; });
        h += '</div>';
    }

    h += '</div></details>';

    // Action buttons
    h += `<div style="display:flex;gap:8px;padding-top:12px;border-top:1px solid var(--outline);">
        <button class="btn btn-primary" id="wiz-generate-btn" onclick="wizardGenerateContent()">
            <span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">edit_note</span> Generate Content
        </button>
    </div>`;

    // Content editor area (hidden initially)
    h += '<div id="wiz-content-area" style="display:none;"></div>';

    h += '</div>';
    document.getElementById("cg-brief-content").innerHTML = h;
    modal.classList.add("active");
}

async function wizardGenerateContent() {
    const selectedTitle = document.querySelector('input[name="wiz-title"]:checked')?.value || "";
    const contentType = document.querySelector('input[name="wiz-type"]:checked')?.value || "collection";
    const btn = document.getElementById("wiz-generate-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Generating content...';

    const area = document.getElementById("wiz-content-area");
    area.style.display = "";
    area.innerHTML = '<div style="padding:20px;text-align:center;"><div class="spinner" style="margin:0 auto;"></div><p style="margin-top:8px;font-size:13px;">AI is writing your content...</p></div>';

    document.getElementById("wiz-step-badge").textContent = "Step 2: Generating...";

    try {
        const resp = await fetch(`/api/content-gaps/${currentWizardGapId}/generate-content?store_id=${cgStoreId}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title: selectedTitle, content_type: contentType }),
        });
        const result = await resp.json();

        if (resp.status === 409 && result.cannibalization_warning) {
            const pages = (result.existing_pages || []).map(p => `${p.url} (pos: ${p.position})`).join(", ");
            area.innerHTML = `<div style="padding:16px;border:1px solid var(--error);border-radius:var(--radius-sm);background:var(--error-container);">
                <strong style="color:var(--error);">Cannibalization Risk Detected</strong>
                <p style="margin:8px 0;font-size:13px;">${escapeHtml(result.recommendation)}</p>
                ${pages ? `<p style="font-size:12px;color:var(--on-surface-variant);">Competing pages: ${escapeHtml(pages)}</p>` : ""}
                <button class="btn btn-secondary btn-sm" onclick="forceGenerateContent()" style="margin-top:8px;">Generate Anyway</button>
            </div>`;
            return;
        }
        if (!resp.ok) throw new Error(result.error || `Request failed (${resp.status})`);

        currentGeneratedContent = result;
        showContentEditor(result, contentType);
    } catch (e) {
        area.innerHTML = `<p style="color:var(--error);padding:16px;">Generation failed: ${escapeHtml(e.message)}</p>`;
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">edit_note</span> Regenerate Content';
    }
}

async function forceGenerateContent() {
    const selectedTitle = document.querySelector('input[name="wiz-title"]:checked')?.value || "";
    const contentType = document.querySelector('input[name="wiz-type"]:checked')?.value || "collection";
    const area = document.getElementById("wiz-content-area");
    area.innerHTML = '<div style="padding:20px;text-align:center;"><div class="spinner" style="margin:0 auto;"></div><p style="margin-top:8px;font-size:13px;">AI is writing your content...</p></div>';
    try {
        const result = await apiRequest(`/api/content-gaps/${currentWizardGapId}/generate-content?store_id=${cgStoreId}`, {
            method: "POST",
            body: { title: selectedTitle, content_type: contentType, skip_cannibalization_check: true },
        });
        currentGeneratedContent = result;
        showContentEditor(result, contentType);
    } catch (e) {
        area.innerHTML = `<p style="color:var(--error);padding:16px;">Generation failed: ${escapeHtml(e.message)}</p>`;
    }
}

function showContentEditor(content, contentType) {
    document.getElementById("wiz-step-badge").textContent = "Step 3: Edit & Publish";
    const area = document.getElementById("wiz-content-area");
    const genImage = document.getElementById("wiz-gen-image")?.checked;

    let h = '<div style="display:flex;flex-direction:column;gap:12px;border-top:1px solid var(--outline);padding-top:16px;">';

    h += `<div class="form-group"><label style="font-size:13px;">Title</label>
        <input type="text" id="wiz-edit-title" class="form-control" value="${escapeHtml(content.title || "")}"></div>`;

    h += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
        <div class="form-group"><label style="font-size:13px;">Meta Title</label>
            <input type="text" id="wiz-edit-meta-title" class="form-control" value="${escapeHtml(content.meta_title || "")}"></div>
        <div class="form-group"><label style="font-size:13px;">URL Slug</label>
            <input type="text" id="wiz-edit-slug" class="form-control" value="${escapeHtml(content.slug || "")}"></div>
    </div>`;

    h += `<div class="form-group"><label style="font-size:13px;">Meta Description</label>
        <textarea id="wiz-edit-meta-desc" class="form-control" style="min-height:50px;">${escapeHtml(content.meta_description || document.getElementById("wiz-meta-desc")?.value || "")}</textarea></div>`;

    h += `<div class="form-group"><label style="font-size:13px;">Tags (comma-separated)</label>
        <input type="text" id="wiz-edit-tags" class="form-control" value="${escapeHtml((content.tags || []).join(", "))}"></div>`;

    h += `<div class="form-group"><label style="font-size:13px;">Body Content (HTML) — ${content.word_count || 0} words</label>
        <textarea id="wiz-edit-body" class="form-control" style="min-height:300px;font-family:var(--font-mono);font-size:12px;">${escapeHtml(content.body_html || "")}</textarea></div>`;

    const briefLinks = currentWizardBrief?.internal_links || [];
    if (briefLinks.length) {
        h += '<div style="padding:10px 12px;background:var(--surface-dim);border-radius:var(--radius-sm);border:1px solid var(--outline);"><strong style="font-size:12px;">Internal Links (should be in body):</strong>';
        briefLinks.forEach(l => {
            const link = typeof l === 'string' ? l : (l.url || l.text || JSON.stringify(l));
            h += `<div style="font-size:11px;color:var(--primary);margin-top:2px;">${escapeHtml(link)}</div>`;
        });
        h += '</div>';
    }

    h += `<div style="display:flex;gap:8px;align-items:center;">
        <button class="btn btn-primary" onclick="wizardPublish('${contentType}')">
            <span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">cloud_upload</span> Publish to Shopify
        </button>
        <span style="font-size:12px;color:var(--on-surface-secondary);">Content type: <strong>${contentType}</strong>${genImage ? ' + AI image' : ''}</span>
    </div>`;

    h += '</div>';
    area.innerHTML = h;
}

async function wizardPublish(contentType) {
    const title = document.getElementById("wiz-edit-title").value.trim();
    const metaTitle = document.getElementById("wiz-edit-meta-title").value.trim();
    const metaDesc = document.getElementById("wiz-edit-meta-desc").value.trim();
    const slug = document.getElementById("wiz-edit-slug").value.trim();
    const tags = document.getElementById("wiz-edit-tags").value.trim();
    const bodyHtml = document.getElementById("wiz-edit-body").value;
    const genImage = document.getElementById("wiz-gen-image")?.checked || false;

    if (!title || !bodyHtml) { showToast("Title and body content are required", "error"); return; }

    document.getElementById("wiz-step-badge").textContent = "Step 4: Publishing...";
    showToast("Publishing to Shopify...", "info");

    try {
        const result = await apiRequest(`/api/content-gaps/${currentWizardGapId}/publish?store_id=${cgStoreId}`, {
            method: "POST",
            body: {
                content_type: contentType,
                title, body_html: bodyHtml, meta_title: metaTitle, meta_description: metaDesc,
                slug, tags: tags.split(",").map(t => t.trim()).filter(Boolean),
                generate_image: genImage,
            },
        });

        document.getElementById("wiz-step-badge").textContent = "Published!";
        document.getElementById("wiz-content-area").innerHTML = `
            <div style="padding:24px;text-align:center;border-top:1px solid var(--outline);margin-top:16px;">
                <span class="material-symbols-rounded" style="font-size:48px;color:var(--success);">check_circle</span>
                <h3 style="margin:12px 0 8px;">Published Successfully!</h3>
                <p style="color:var(--on-surface-secondary);font-size:14px;">Your ${contentType} has been created on Shopify.</p>
                ${result.url ? `<a href="${escapeHtml(result.url)}" target="_blank" class="btn btn-primary btn-sm" style="margin-top:12px;">View on Shopify</a>` : ''}
            </div>`;

        showToast(`${contentType} published to Shopify!`, "success");
        loadCgGaps();
        loadCgOverview();
    } catch (e) {
        showToast("Publish failed: " + e.message, "error");
        document.getElementById("wiz-step-badge").textContent = "Step 3: Edit & Publish";
    }
}

document.getElementById("cg-brief-modal").addEventListener("click", e => { if (e.target === e.currentTarget) e.currentTarget.classList.remove("active"); });

async function optimizeExistingPage(gapId) {
    const modal = document.getElementById("cg-brief-modal");
    document.getElementById("cg-brief-title").textContent = "Optimize Existing Page";
    document.getElementById("cg-brief-content").innerHTML = '<div style="padding:40px;text-align:center;"><div class="spinner" style="margin:0 auto;"></div><p style="margin-top:12px;font-size:14px;">Analyzing page and generating optimization strategy...</p><p style="font-size:12px;color:var(--on-surface-secondary);">Fetching current rankings, generating AI strategy</p></div>';
    modal.classList.add("active");

    try {
        const data = await apiRequest(`/api/content-gaps/${gapId}/optimize-existing?store_id=${cgStoreId}`, { method: "POST" });
        renderOptimizationView(data);
    } catch (e) {
        document.getElementById("cg-brief-content").innerHTML = `<p style="padding:20px;color:var(--error);">Failed: ${escapeHtml(e.message)}</p>`;
    }
}

function renderOptimizationView(data) {
    const { gap, page, current_rankings, optimization } = data;
    let h = '<div style="display:flex;flex-direction:column;gap:16px;">';

    // Header
    h += `<div style="display:flex;align-items:center;gap:8px;padding-bottom:12px;border-bottom:1px solid var(--outline);">
        <span class="badge badge-success">Optimize Existing</span>
        <span style="font-size:14px;font-weight:600;">${escapeHtml(page.title)}</span>
        <span class="badge">${page.type}</span>
        <span style="font-size:12px;color:var(--on-surface-secondary);">SEO: ${page.seo_score}/100</span>
    </div>`;

    // Target keyword
    h += `<div style="padding:12px;background:var(--primary-light);border:1px solid var(--primary);border-radius:var(--radius-sm);">
        <strong style="font-size:13px;">Target Keyword:</strong> <span style="font-size:14px;font-weight:700;">"${escapeHtml(gap.keyword)}"</span>
        <span style="font-size:12px;color:var(--on-surface-secondary);margin-left:8px;">${(gap.volume || 0).toLocaleString()} mo. vol. | ${gap.intent}</span>
    </div>`;

    // Current rankings table
    if (current_rankings?.length) {
        const sortedRankings = [...current_rankings].sort((a, b) => (b.volume || 0) - (a.volume || 0));
        const bestAlt = sortedRankings.find(r => r.keyword.toLowerCase() !== gap.keyword.toLowerCase() && (r.volume || 0) > (gap.volume || 0));

        h += '<div><h4 style="font-size:13px;margin-bottom:8px;">Current Rankings (this page)</h4>';
        window._optRankings = sortedRankings;
        window._optGapId = gap.id;

        if (bestAlt) {
            const bestIdx = sortedRankings.indexOf(bestAlt);
            h += `<div style="padding:8px 12px;background:var(--warning-light);border:1px solid var(--warning);border-radius:var(--radius-sm);margin-bottom:8px;font-size:12px;">
                <strong>Higher volume opportunity:</strong> "${escapeHtml(bestAlt.keyword)}" has ${(bestAlt.volume || 0).toLocaleString()} mo. vol. vs your target's ${(gap.volume || 0).toLocaleString()}.
                <button class="btn btn-sm btn-secondary" style="margin-left:8px;font-size:11px;padding:2px 8px;" onclick="switchOptByIndex(${bestIdx})">Switch to this keyword</button>
            </div>`;
        }
        h += '<div style="max-height:250px;overflow:auto;border:1px solid var(--outline);border-radius:var(--radius-sm);">';
        h += '<table class="data-table" style="margin:0;font-size:12px;"><thead><tr><th>Keyword</th><th>Mo. Volume</th><th>Position</th><th>Impressions</th><th>Clicks</th><th>Source</th><th></th></tr></thead><tbody>';
        sortedRankings.slice(0, 25).forEach(r => {
            const posCls = r.position <= 3 ? "badge-success" : r.position <= 10 ? "badge-warning" : "badge-error";
            const isTarget = r.keyword.toLowerCase() === gap.keyword.toLowerCase();
            const rowStyle = isTarget ? "background:var(--primary-light);" : "";
            const volStr = r.volume != null ? r.volume.toLocaleString() : '—';
            h += `<tr style="${rowStyle}">
                <td><strong>${escapeHtml(r.keyword)}</strong>${isTarget ? ' <span class="badge badge-success" style="font-size:9px;">TARGET</span>' : ''}</td>
                <td style="font-weight:600;">${volStr}</td>
                <td><span class="badge ${posCls}">${r.position}</span></td>
                <td>${Math.round(r.impressions).toLocaleString()}</td>
                <td>${Math.round(r.clicks).toLocaleString()}</td>
                <td style="font-size:11px;">${r.source}</td>
                <td>${!isTarget ? `<button class="action-icon" title="Optimize for this keyword instead" onclick="switchOptByIndex(${sortedRankings.indexOf(r)})"><span class="material-symbols-rounded" style="font-size:14px;">swap_horiz</span></button>` : ''}</td>
            </tr>`;
        });
        h += '</tbody></table></div></div>';
    }

    // AI Strategy
    if (optimization.strategy) {
        h += `<div style="padding:12px;background:var(--surface-dim);border-radius:var(--radius-sm);border:1px solid var(--outline);">
            <h4 style="font-size:13px;margin-bottom:6px;"><span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">smart_toy</span> AI Strategy</h4>
            <p style="font-size:13px;margin:0;">${escapeHtml(optimization.strategy)}</p>
        </div>`;
    }

    // Side-by-side editor
    h += '<div><h4 style="font-size:13px;margin-bottom:8px;">Proposed Changes</h4>';
    h += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px;">';

    h += `<div><label style="font-size:11px;color:var(--on-surface-secondary);">Current Meta Title</label>
        <div style="padding:6px 8px;background:var(--surface-dim);border-radius:6px;word-break:break-word;">${escapeHtml(page.meta_title || "Not set")}</div></div>`;
    h += `<div><label style="font-size:11px;color:var(--on-surface-secondary);">New Meta Title</label>
        <input type="text" class="form-control" id="opt-meta-title" style="font-size:12px;padding:6px 8px;" value="${escapeHtml(optimization.new_meta_title || page.meta_title || "")}"></div>`;

    h += `<div><label style="font-size:11px;color:var(--on-surface-secondary);">Current Meta Description</label>
        <div style="padding:6px 8px;background:var(--surface-dim);border-radius:6px;word-break:break-word;">${escapeHtml(page.meta_description || "Not set")}</div></div>`;
    h += `<div><label style="font-size:11px;color:var(--on-surface-secondary);">New Meta Description</label>
        <textarea class="form-control" id="opt-meta-desc" style="font-size:12px;padding:6px 8px;min-height:60px;">${escapeHtml(optimization.new_meta_description || page.meta_description || "")}</textarea></div>`;

    h += '</div>';

    if (optimization.new_title) {
        h += `<div class="form-group" style="margin-top:8px;"><label style="font-size:12px;">New Title</label>
            <input type="text" class="form-control" id="opt-title" style="font-size:12px;" value="${escapeHtml(optimization.new_title)}"></div>`;
    }

    if (optimization.new_description_html) {
        h += `<div class="form-group" style="margin-top:8px;"><label style="font-size:12px;">New Description (HTML)</label>
            <textarea class="form-control" id="opt-desc" style="font-size:12px;font-family:var(--font-mono);min-height:200px;">${escapeHtml(optimization.new_description_html)}</textarea></div>`;
    }

    h += '</div>';

    // Apply button
    h += `<div style="display:flex;gap:8px;padding-top:12px;border-top:1px solid var(--outline);">
        <button class="btn btn-primary" onclick="applyGapOptimization(${gap.id}, '${page.type}', ${page.id})">
            <span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">cloud_upload</span> Apply to Shopify
        </button>
        <span style="font-size:12px;color:var(--on-surface-secondary);align-self:center;">Updates ${page.type} on Shopify and marks gap as addressed</span>
    </div>`;

    h += '</div>';
    document.getElementById("cg-brief-content").innerHTML = h;
}

function switchOptByIndex(idx) {
    const r = window._optRankings?.[idx];
    if (!r || !window._optGapId) return;
    switchOptTarget(window._optGapId, r.keyword, r.volume || 0);
}

async function switchOptTarget(gapId, newKeyword, newVolume) {
    const modal = document.getElementById("cg-brief-modal");
    document.getElementById("cg-brief-title").textContent = "Optimize Existing Page";
    document.getElementById("cg-brief-content").innerHTML = `<div style="padding:40px;text-align:center;"><div class="spinner" style="margin:0 auto;"></div><p style="margin-top:12px;font-size:14px;">Regenerating strategy for "${escapeHtml(newKeyword)}"...</p></div>`;

    try {
        const data = await apiRequest(`/api/content-gaps/${gapId}/optimize-existing?store_id=${cgStoreId}`, {
            method: "POST",
            body: { override_keyword: newKeyword, override_volume: newVolume },
        });
        renderOptimizationView(data);
    } catch (e) {
        document.getElementById("cg-brief-content").innerHTML = `<p style="padding:20px;color:var(--error);">Failed: ${escapeHtml(e.message)}</p>`;
    }
}

async function applyGapOptimization(gapId, itemType, itemId) {
    const changes = {
        item_type: itemType,
        item_id: itemId,
        new_meta_title: document.getElementById("opt-meta-title")?.value?.trim(),
        new_meta_description: document.getElementById("opt-meta-desc")?.value?.trim(),
        new_title: document.getElementById("opt-title")?.value?.trim() || null,
        new_description_html: document.getElementById("opt-desc")?.value || null,
    };

    showToast("Applying optimization to Shopify...", "info");
    try {
        await apiRequest(`/api/content-gaps/${gapId}/apply-optimization?store_id=${cgStoreId}`, {
            method: "POST", body: changes,
        });
        showToast("Optimization applied! Page updated on Shopify.", "success");
        document.getElementById("cg-brief-modal").classList.remove("active");
        loadCgGaps();
        loadCgOverview();
    } catch (e) {
        showToast("Apply failed: " + e.message, "error");
    }
}

async function updateCgStatus(gapId, status) {
    try {
        await apiRequest(`/api/content-gaps/${gapId}/status?store_id=${cgStoreId}`, { method: "PUT", body: { status } });
        loadCgGaps();
    } catch (e) { showToast(e.message, "error"); }
}
