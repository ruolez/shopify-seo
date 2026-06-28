// ── News page ────────────────────────────────────────────────────

let storeId = null;
let currentSessionId = null;
let current = null;       // working edited_result
let lastSession = null;   // last fetched session row (hero/inline state)
let newsCategories = [];

document.addEventListener("DOMContentLoaded", async () => {
    initTabs();
    addSourceUrl();
    storeId = await resolveStoreId();
    await loadCategories();
    await checkTemplateStatus();
});

// ── Source URLs (multiple) ───────────────────────────────────────

function addSourceUrl(value = "") {
    const wrap = document.getElementById("source-urls");
    if (!wrap) return;
    const row = document.createElement("div");
    row.className = "source-url-row";
    row.style.cssText = "display:flex; gap:6px; margin-bottom:6px;";
    row.innerHTML = `
        <input type="url" class="form-control source-url-input" placeholder="https://…" value="${escapeAttr(value)}" style="flex:1;">
        <button type="button" class="icon-btn" title="Remove" onclick="this.parentElement.remove()">
            <span class="material-symbols-rounded">close</span>
        </button>`;
    wrap.appendChild(row);
}

function collectSourceUrls() {
    return [...document.querySelectorAll(".source-url-input")]
        .map((el) => el.value.trim())
        .filter(Boolean);
}

function resetSourceUrls() {
    const wrap = document.getElementById("source-urls");
    if (wrap) wrap.innerHTML = "";
    addSourceUrl();
}

// ── Markdown import ──────────────────────────────────────────────

// Splits a news .md file into the article body (everything before the
// "## Sources" heading) and the list of source URLs found in that section.
function parseNewsMarkdown(text) {
    const normalized = (text || "").replace(/\r\n/g, "\n");
    const lines = normalized.split("\n");
    const sourcesIdx = lines.findIndex((l) => /^##\s+sources\s*$/i.test(l.trim()));

    const articleLines = sourcesIdx === -1 ? lines : lines.slice(0, sourcesIdx);
    const sourcesText = sourcesIdx === -1 ? "" : lines.slice(sourcesIdx + 1).join("\n");

    // Drop a trailing "---" separator and surrounding blank lines left before the heading.
    let article = articleLines.join("\n").replace(/\n+-{3,}\s*$/g, "").trimEnd();

    const urls = [];
    const seen = new Set();
    const push = (u) => {
        const url = (u || "").trim();
        if (url && !seen.has(url)) {
            seen.add(url);
            urls.push(url);
        }
    };

    const linkRe = /\[[^\]]*\]\(([^)\s]+)\)/g;
    let m;
    while ((m = linkRe.exec(sourcesText)) !== null) push(m[1]);

    if (urls.length === 0) {
        const bareRe = /(https?:\/\/[^\s)]+)/g;
        while ((m = bareRe.exec(sourcesText)) !== null) push(m[1]);
    }

    return { article, urls };
}

function importNewsMarkdown(event) {
    const input = event.target;
    const file = input.files && input.files[0];
    if (!file) return;

    const status = document.getElementById("news-md-status");
    const reader = new FileReader();
    reader.onload = () => {
        const { article, urls } = parseNewsMarkdown(String(reader.result || ""));
        if (!article) {
            showToast("Could not read any article text from that file.", "error");
            input.value = "";
            return;
        }

        document.getElementById("news-source").value = article;

        resetSourceUrls();
        if (urls.length) {
            document.getElementById("source-urls").innerHTML = "";
            urls.forEach((u) => addSourceUrl(u));
        }

        if (status) {
            status.textContent = `Loaded “${file.name}” — article + ${urls.length} source${urls.length === 1 ? "" : "s"}.`;
        }
        showToast(`Imported article and ${urls.length} source${urls.length === 1 ? "" : "s"}.`, "success");
        input.value = "";
    };
    reader.onerror = () => {
        showToast("Failed to read the file.", "error");
        input.value = "";
    };
    reader.readAsText(file);
}

async function resolveStoreId() {
    try {
        const stores = await apiRequest("/api/stores");
        return stores && stores.length ? stores[0].id : null;
    } catch {
        return null;
    }
}

function initTabs() {
    document.querySelectorAll(".tab").forEach((tab) => {
        tab.addEventListener("click", () => switchTab(tab.dataset.tab));
    });
}

function switchTab(name) {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    document.getElementById("tab-" + name).classList.add("active");
    if (name === "published") loadNewsArticles();
}

async function loadCategories() {
    try {
        const data = await apiRequest("/api/settings/custom?prefix=lists.");
        const entry = data["lists.news_categories"];
        if (entry && Array.isArray(entry.value)) newsCategories = entry.value;
    } catch { /* ignore */ }
    const dl = document.getElementById("category-list");
    if (dl) dl.innerHTML = newsCategories.map((c) => `<option value="${escapeAttr(c)}">`).join("");
}

// ── Template controls ────────────────────────────────────────────

async function checkTemplateStatus() {
    const chip = document.getElementById("template-status-chip");
    const installBtn = document.getElementById("install-template-btn");
    const revertBtn = document.getElementById("revert-template-btn");
    if (!storeId) { chip.textContent = "Template: no store"; return; }
    try {
        const s = await apiRequest(`/api/news/template-status?store_id=${storeId}`);
        if (s.installed && !s.outdated) {
            chip.className = "news-badge news-badge--ok";
            chip.textContent = "Template: installed";
            installBtn.style.display = "none";
            revertBtn.style.display = "";
        } else if (s.installed && s.outdated) {
            chip.className = "news-badge news-badge--pending";
            chip.textContent = "Template: update available";
            installBtn.style.display = "";
            revertBtn.style.display = "";
        } else {
            chip.className = "news-badge news-badge--pending";
            chip.textContent = "Template: not installed";
            installBtn.style.display = "";
            revertBtn.style.display = "none";
        }
    } catch {
        chip.textContent = "Template: unknown";
    }
}

async function installTemplate() {
    if (!storeId) return showToast("No store found", "error");
    const btn = document.getElementById("install-template-btn");
    btn.disabled = true;
    try {
        await apiRequest("/api/news/push-template", { method: "POST", body: { store_id: storeId } });
        showToast("News template installed to your theme", "success");
        await checkTemplateStatus();
    } catch (e) {
        showToast("Install failed: " + e.message, "error");
    } finally {
        btn.disabled = false;
    }
}

async function revertTemplate() {
    if (!storeId) return;
    if (!confirm("Remove the News template from your theme? News articles will fall back to the default article layout.")) return;
    try {
        const r = await apiRequest("/api/news/revert-template", { method: "POST", body: { store_id: storeId } });
        showToast(`Reverted (${r.articles_cleared || 0} articles reset)`, "success");
        await checkTemplateStatus();
    } catch (e) {
        showToast("Revert failed: " + e.message, "error");
    }
}

async function regenerateHub() {
    if (!storeId) return showToast("No store found", "error");
    const btn = document.querySelector('button[onclick="regenerateHub()"]');
    const original = btn ? btn.innerHTML : null;
    if (btn) { btn.disabled = true; btn.innerHTML = `<div class="spinner" style="width:16px;height:16px;border-width:2px;"></div> Regenerating…`; }
    try {
        const res = await apiRequest("/api/news/regenerate-hub", { method: "POST", body: { store_id: storeId } });
        pollTask(res.task_id, (task) => {
            if (btn) { btn.disabled = false; btn.innerHTML = original; }
            if (task.status === "completed") {
                const r = task.result || {};
                showToast(`News hub updated — ${r.articles_count ?? 0} article(s) at ${r.url || "/pages/news"}`, "success");
            } else {
                showToast("Hub regeneration failed: " + (task.error || "unknown error"), "error");
            }
        });
    } catch (e) {
        if (btn) { btn.disabled = false; btn.innerHTML = original; }
        showToast("Failed: " + e.message, "error");
    }
}

// ── Analyze ──────────────────────────────────────────────────────

async function analyzeNews() {
    const source = document.getElementById("news-source").value.trim();
    const sourceUrls = collectSourceUrls();
    const status = document.getElementById("news-analyze-status");
    if (!storeId) return showToast("No store found. Add a store first.", "error");
    if (!source) return showToast("Paste an article first", "error");

    status.innerHTML = `<div class="spinner" style="width:20px;height:20px;border-width:2px;"></div> Analyzing… preserving your facts and wording.`;
    try {
        const res = await apiRequest("/api/news/analyze", {
            method: "POST",
            body: { store_id: storeId, source_text: source, source_urls: sourceUrls },
        });
        currentSessionId = res.session_id;
        pollTask(res.task_id, async (task) => {
            if (task.status === "completed") {
                const session = await apiRequest(`/api/news/session/${currentSessionId}`);
                renderReview(session);
                status.innerHTML = "";
            } else {
                status.innerHTML = `<p style="color: var(--error);">Analysis failed: ${escapeHtml(task.error || "Unknown error")}</p>`;
            }
        });
    } catch (e) {
        status.innerHTML = `<p style="color: var(--error);">${escapeHtml(e.message)}</p>`;
    }
}

function pollTask(taskId, onDone) {
    const poll = async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            if (task.status === "completed" || task.status === "failed") {
                onDone(task);
            } else {
                setTimeout(poll, 2000);
            }
        } catch (e) {
            onDone({ status: "failed", error: e.message });
        }
    };
    poll();
}

// ── Review rendering ─────────────────────────────────────────────

function renderReview(session) {
    lastSession = session;
    current = session.edited_result || session.analyze_result || {};
    show("news-step-paste", false);
    show("news-step-review", true);
    show("news-step-done", false);

    val("f-headline", current.headline);
    val("f-kicker", current.kicker);
    val("f-category", current.category);
    val("f-metatitle", current.meta_title);
    val("f-metadesc", current.meta_description);
    val("f-summary", current.summary);
    val("f-tags", (current.tags || []).join(", "));
    val("f-body", current.body_html);

    countChar("f-metatitle", "cc-metatitle", 60);
    countChar("f-metadesc", "cc-metadesc", 160);
    countChar("f-summary", "cc-summary", 160);

    renderHeroPreview();
    renderInlineList();
    renderPreview();
}

function renderHeroPreview() {
    const wrap = document.getElementById("hero-preview-wrap");
    if (lastSession && lastSession.hero_image_url) {
        wrap.innerHTML = `<img src="${escapeAttr(lastSession.hero_image_url)}" class="news-hero-preview" alt="hero" onerror="this.style.display='none'">
            <span class="news-badge news-badge--ok">Hero ready</span>`;
    } else {
        wrap.innerHTML = `<p style="font-size:13px;color:var(--on-surface-secondary);">No hero yet.</p>`;
    }
}

function generatedInlineByToken(token) {
    const list = (lastSession && lastSession.inline_images) || [];
    return list.find((x) => x.token === token);
}

function renderInlineList() {
    const list = document.getElementById("inline-list");
    const specs = (current && current.inline_images) || [];
    if (!specs.length) {
        list.innerHTML = `<p style="font-size:13px;color:var(--on-surface-secondary);">No inline images planned.</p>`;
        return;
    }
    list.innerHTML = specs.map((s) => {
        const gen = generatedInlineByToken(s.token);
        let badge = `<span class="news-badge news-badge--pending">pending</span>`;
        let thumb = "";
        if (gen && gen.status === "ready" && gen.cdn_url) {
            badge = `<span class="news-badge news-badge--ok">ready</span>`;
            thumb = `<img src="${escapeAttr(gen.cdn_url)}" alt="">`;
        } else if (gen && gen.status === "failed") {
            badge = `<span class="news-badge news-badge--fail">failed</span>`;
        }
        return `<div class="news-inline-item">${thumb}<div style="flex:1;">
            <div><strong>${escapeHtml(s.token)}</strong> ${badge}</div>
            <div style="color:var(--on-surface-secondary);">${escapeHtml(s.alt || s.prompt || "")}</div>
        </div></div>`;
    }).join("");
}

function renderPreview() {
    const body = document.getElementById("f-body").value || "";
    const replaced = body.replace(/(?:<p>\s*)?\[\[INLINE_IMAGE_(\d+)\]\](?:\s*<\/p>)?/gi, (m, n) => {
        const gen = generatedInlineByToken(`[[INLINE_IMAGE_${n}]]`);
        if (gen && gen.cdn_url) {
            return `<figure><img src="${escapeAttr(gen.cdn_url)}" style="max-width:100%;border-radius:8px;" alt="">${gen.caption ? `<figcaption style="font-size:12px;color:#888;">${escapeHtml(gen.caption)}</figcaption>` : ""}</figure>`;
        }
        return `<div style="background:#f1f3f4;border:1px dashed #bbb;border-radius:8px;padding:12px;text-align:center;font-size:12px;color:#888;">${escapeHtml(m.trim())}</div>`;
    });
    document.getElementById("news-preview").innerHTML = replaced;
}

// ── Save / generate / publish ────────────────────────────────────

function collectForm() {
    current = current || {};
    current.headline = document.getElementById("f-headline").value.trim();
    current.kicker = document.getElementById("f-kicker").value.trim();
    current.category = document.getElementById("f-category").value.trim();
    current.meta_title = document.getElementById("f-metatitle").value.trim();
    current.meta_description = document.getElementById("f-metadesc").value.trim();
    current.summary = document.getElementById("f-summary").value.trim();
    current.tags = document.getElementById("f-tags").value.split(",").map((t) => t.trim()).filter(Boolean);
    current.body_html = document.getElementById("f-body").value;
    return current;
}

async function saveSession() {
    collectForm();
    await apiRequest(`/api/news/session/${currentSessionId}`, {
        method: "PATCH",
        body: { edited_result: current },
    });
}

async function generateHero() {
    const btn = document.getElementById("hero-btn");
    btn.disabled = true;
    btn.innerHTML = `<div class="spinner" style="width:16px;height:16px;border-width:2px;"></div> Generating…`;
    try {
        await saveSession();
        const res = await apiRequest(`/api/news/session/${currentSessionId}/generate-hero`, { method: "POST" });
        pollTask(res.task_id, async (task) => {
            if (task.status === "completed") {
                lastSession = await apiRequest(`/api/news/session/${currentSessionId}`);
                renderHeroPreview();
                showToast("Hero image ready", "success");
            } else {
                showToast("Hero generation failed: " + (task.error || ""), "error");
            }
            resetBtn(btn, `<span class="material-symbols-rounded">image</span> Generate Hero`);
        });
    } catch (e) {
        showToast(e.message, "error");
        resetBtn(btn, `<span class="material-symbols-rounded">image</span> Generate Hero`);
    }
}

async function generateInline() {
    const specs = (current && current.inline_images) || [];
    if (!specs.length) return showToast("No inline images planned for this article", "info");
    const btn = document.getElementById("inline-btn");
    btn.disabled = true;
    btn.innerHTML = `<div class="spinner" style="width:16px;height:16px;border-width:2px;"></div> Generating ${specs.length}…`;
    try {
        await saveSession();
        const res = await apiRequest(`/api/news/session/${currentSessionId}/generate-inline`, { method: "POST" });
        pollTask(res.task_id, async (task) => {
            if (task.status === "completed") {
                lastSession = await apiRequest(`/api/news/session/${currentSessionId}`);
                renderInlineList();
                renderPreview();
                showToast("Inline images ready", "success");
            } else {
                showToast("Inline generation failed: " + (task.error || ""), "error");
            }
            resetBtn(btn, `<span class="material-symbols-rounded">add_photo_alternate</span> Generate Inline Images`);
        });
    } catch (e) {
        showToast(e.message, "error");
        resetBtn(btn, `<span class="material-symbols-rounded">add_photo_alternate</span> Generate Inline Images`);
    }
}

async function generateLinks() {
    const btn = document.getElementById("links-btn");
    const status = document.getElementById("links-status");
    btn.disabled = true;
    btn.innerHTML = `<div class="spinner" style="width:16px;height:16px;border-width:2px;"></div> Adding links…`;
    try {
        await saveSession();
        const res = await apiRequest(`/api/news/session/${currentSessionId}/generate-links`, { method: "POST" });
        pollTask(res.task_id, async (task) => {
            if (task.status === "completed") {
                const session = await apiRequest(`/api/news/session/${currentSessionId}`);
                current = session.edited_result || current;
                lastSession = session;
                val("f-body", current.body_html);
                renderPreview();
                const n = (task.result && task.result.stats && task.result.stats.internal_links_count) || 0;
                status.innerHTML = `<span class="news-badge news-badge--ok">${n} internal links</span>`;
                showToast(`Added internal links (${n} total)`, "success");
            } else {
                showToast("Linking pass failed: " + (task.error || ""), "error");
            }
            resetBtn(btn, `<span class="material-symbols-rounded">link</span> Generate Internal Links`);
        });
    } catch (e) {
        showToast(e.message, "error");
        resetBtn(btn, `<span class="material-symbols-rounded">link</span> Generate Internal Links`);
    }
}

async function publishNews(isPublished) {
    try {
        await saveSession();
        const res = await apiRequest(`/api/news/session/${currentSessionId}/publish`, {
            method: "POST",
            body: { is_published: isPublished },
        });
        show("news-step-review", false);
        show("news-step-done", true);
        document.getElementById("done-msg").textContent = isPublished ? "Published!" : "Saved as draft";
        const link = document.getElementById("done-link");
        link.textContent = window.location.origin.replace(/\/$/, "") + (res.url || "");
        link.href = res.url || "#";
    } catch (e) {
        showToast("Publish failed: " + e.message, "error");
    }
}

function resetCreate() {
    currentSessionId = null;
    current = null;
    lastSession = null;
    document.getElementById("news-source").value = "";
    resetSourceUrls();
    document.getElementById("news-analyze-status").innerHTML = "";
    show("news-step-done", false);
    show("news-step-review", false);
    show("news-step-paste", true);
    switchTab("create");
}

// ── Published list ───────────────────────────────────────────────

async function loadNewsArticles() {
    const container = document.getElementById("news-articles-container");
    if (!storeId) { container.innerHTML = `<p>No store found.</p>`; return; }
    try {
        const data = await apiRequest(`/api/news/articles?store_id=${storeId}`);
        const articles = data.articles || [];
        if (!articles.length) {
            container.innerHTML = `<div class="card"><p style="color:var(--on-surface-secondary);">No news published yet. Create your first article in the “Create News” tab.</p></div>`;
            return;
        }
        const rows = articles.map((a) => {
            const status = a.is_published
                ? `<span class="news-badge news-badge--ok">Published</span>`
                : `<span class="news-badge news-badge--pending">Draft</span>`;
            const url = `/blogs/news/${a.handle || ""}`;
            return `<tr>
                <td>${escapeHtml(a.title || "Untitled")}</td>
                <td>${escapeHtml(a.category || "—")}</td>
                <td>${status}</td>
                <td>${a.published_at ? formatDate(a.published_at) : "—"}</td>
                <td><a class="news-link" href="${escapeAttr(url)}" target="_blank" rel="noopener">View ↗</a></td>
            </tr>`;
        }).join("");
        container.innerHTML = `<div class="table-container"><table class="data-table">
            <thead><tr><th>Title</th><th>Category</th><th>Status</th><th>Published</th><th></th></tr></thead>
            <tbody>${rows}</tbody></table></div>`;
    } catch (e) {
        container.innerHTML = `<p style="color:var(--error);">${escapeHtml(e.message)}</p>`;
    }
}

// ── Small helpers ────────────────────────────────────────────────

function show(id, on) { const el = document.getElementById(id); if (el) el.style.display = on ? "" : "none"; }
function val(id, v) { const el = document.getElementById(id); if (el) el.value = v || ""; }
function resetBtn(btn, html) { btn.disabled = false; btn.innerHTML = html; }
function countChar(inputId, counterId, ideal) {
    const el = document.getElementById(inputId);
    const c = document.getElementById(counterId);
    if (!el || !c) return;
    const n = (el.value || "").length;
    c.textContent = `${n}/${ideal}`;
    c.style.color = n > ideal ? "var(--error)" : "var(--on-surface-secondary)";
}
