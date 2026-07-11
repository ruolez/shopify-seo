// ── News page ────────────────────────────────────────────────────

let storeId = null;
let storeBaseUrl = "";
let currentSessionId = null;
let current = null;       // working edited_result
let lastSession = null;   // last fetched session row (hero/inline state)
let newsCategories = [];

document.addEventListener("DOMContentLoaded", async () => {
    initTabs();
    addSource();
    storeId = await resolveStoreId();
    await loadCategories();
    await checkTemplateStatus();
});

// ── Sources (multiple) ───────────────────────────────────────────

function addSource(url = "", title = "") {
    const wrap = document.getElementById("source-urls");
    if (!wrap) return;
    const row = document.createElement("div");
    row.className = "source-url-row";
    row.style.cssText = "display:flex; gap:6px; margin-bottom:6px;";
    row.innerHTML = `
        <input type="text" class="form-control source-title-input" placeholder="Source title (optional)" value="${escapeAttr(title)}" style="flex:1;">
        <input type="url" class="form-control source-url-input" placeholder="https://…" value="${escapeAttr(url)}" style="flex:1;">
        <button type="button" class="icon-btn" title="Remove" onclick="this.parentElement.remove()">
            <span class="material-symbols-rounded">close</span>
        </button>`;
    wrap.appendChild(row);
}

// Returns [{url, title}] for rows that have a URL. The title is the descriptive
// anchor text rendered in the published article's Sources section.
function collectSources() {
    return [...document.querySelectorAll(".source-url-row")]
        .map((row) => ({
            url: (row.querySelector(".source-url-input")?.value || "").trim(),
            title: (row.querySelector(".source-title-input")?.value || "").trim(),
        }))
        .filter((s) => s.url);
}

function resetSources() {
    const wrap = document.getElementById("source-urls");
    if (wrap) wrap.innerHTML = "";
    addSource();
}

// ── Markdown import ──────────────────────────────────────────────

// Splits a news .md file into the article body (everything before the
// "## Sources" heading) and the list of sources ({url, title}) found in that
// section. Markdown links "[Label](url)" yield title=Label for SEO anchor text.
function parseNewsMarkdown(text) {
    const normalized = (text || "").replace(/\r\n/g, "\n");
    const lines = normalized.split("\n");
    const sourcesIdx = lines.findIndex((l) => /^##\s+sources\s*$/i.test(l.trim()));

    const articleLines = sourcesIdx === -1 ? lines : lines.slice(0, sourcesIdx);
    const sourcesText = sourcesIdx === -1 ? "" : lines.slice(sourcesIdx + 1).join("\n");

    // Drop a trailing "---" separator and surrounding blank lines left before the heading.
    let article = articleLines.join("\n").replace(/\n+-{3,}\s*$/g, "").trimEnd();

    const sources = [];
    const seen = new Set();
    const push = (url, title) => {
        url = (url || "").trim();
        if (url && !seen.has(url)) {
            seen.add(url);
            sources.push({ url, title: (title || "").trim() });
        }
    };

    const linkRe = /\[([^\]]*)\]\(([^)\s]+)\)/g;
    let m;
    while ((m = linkRe.exec(sourcesText)) !== null) push(m[2], m[1]);

    if (sources.length === 0) {
        const bareRe = /(https?:\/\/[^\s)]+)/g;
        while ((m = bareRe.exec(sourcesText)) !== null) push(m[1], "");
    }

    return { article, sources };
}

function readFileText(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ""));
        reader.onerror = () => reject(new Error(`Failed to read ${file.name}`));
        reader.readAsText(file);
    });
}

// One file → fill the single-article form (manual or single auto flow).
async function loadSingleMarkdown(file) {
    const status = document.getElementById("news-md-status");
    let text;
    try {
        text = await readFileText(file);
    } catch (e) {
        showToast(e.message, "error");
        return;
    }
    const { article, sources } = parseNewsMarkdown(text);
    if (!article) {
        showToast("Could not read any article text from that file.", "error");
        return;
    }
    document.getElementById("news-source").value = article;
    resetSources();
    if (sources.length) {
        document.getElementById("source-urls").innerHTML = "";
        sources.forEach((s) => addSource(s.url, s.title));
    }
    if (status) {
        status.textContent = `Loaded “${file.name}” — article + ${sources.length} source${sources.length === 1 ? "" : "s"}.`;
    }
    showToast(`Imported article and ${sources.length} source${sources.length === 1 ? "" : "s"}.`, "success");
}

async function importNewsMarkdownFiles(event) {
    const input = event.target;
    const files = [...(input.files || [])];
    input.value = ""; // allow re-selecting the same file(s)
    if (!files.length) return;

    if (files.length === 1) {
        hideBatch();
        await loadSingleMarkdown(files[0]);
        return;
    }

    // Multiple files → batch auto-publish.
    batchItems = [];
    for (const file of files) {
        let item = { name: file.name, source_text: "", sources: [], ok: false, error: "" };
        try {
            const { article, sources } = parseNewsMarkdown(await readFileText(file));
            if (article) {
                item.source_text = article;
                item.sources = sources;
                item.ok = true;
            } else {
                item.error = "no article text";
            }
        } catch (e) {
            item.error = e.message;
        }
        batchItems.push(item);
    }
    renderBatchList();
    show("news-batch", true);
    const ready = batchItems.filter((i) => i.ok).length;
    document.getElementById("news-md-status").textContent =
        `${files.length} files — ${ready} ready, ${files.length - ready} skipped.`;
}

// ── Batch auto-publish ───────────────────────────────────────────

let batchItems = [];

function hideBatch() {
    show("news-batch", false);
    batchItems = [];
}

function batchChip(item) {
    if (!item.ok) return `<span style="color:var(--on-surface-secondary);">Skipped — ${escapeHtml(item.error || "no article")}</span>`;
    const s = item.status;
    if (!s) return `<span style="color:var(--on-surface-secondary);">Ready</span>`;
    if (s === "published") {
        const url = `${storeBaseUrl}/blogs/news/${item.handle || ""}`;
        return `<span style="color:var(--success);">✓ Published — <a href="${escapeAttr(url)}" target="_blank">view →</a></span>`;
    }
    if (s === "failed") return `<span style="color:var(--error);">Failed: ${escapeHtml(item.error || "error")}</span>`;
    return `<span>${escapeHtml(AUTO_STAGES[s] || "Working…")}</span>`;
}

function renderBatchList() {
    const list = document.getElementById("news-batch-list");
    list.innerHTML = batchItems
        .map(
            (item, i) => `<div style="display:flex; justify-content:space-between; gap:12px; padding:8px 0; border-bottom:1px solid var(--outline);">
                <span style="font-weight:500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(item.name)}</span>
                <span id="batch-chip-${i}" style="font-size:13px; flex:none;">${batchChip(item)}</span>
            </div>`,
        )
        .join("");
    const ready = batchItems.filter((i) => i.ok).length;
    const btn = document.getElementById("news-batch-btn");
    btn.disabled = ready === 0;
    btn.querySelector("#news-batch-label").textContent = `Auto-Publish ${ready} article${ready === 1 ? "" : "s"}`;
}

async function startBatch() {
    if (!storeId) return showToast("No store found. Add a store first.", "error");
    const ready = batchItems.filter((i) => i.ok);
    if (!ready.length) return showToast("No valid articles to publish.", "error");

    const btn = document.getElementById("news-batch-btn");
    btn.disabled = true;
    const status = document.getElementById("news-batch-status");
    status.innerHTML = `<div class="spinner" style="width:20px;height:20px;border-width:2px;"></div> Queuing ${ready.length} articles…`;
    try {
        const res = await apiRequest("/api/news/auto-batch", {
            method: "POST",
            body: {
                store_id: storeId,
                articles: ready.map((i) => ({ source_text: i.source_text, source_urls: i.sources })),
            },
        });
        // Map returned session ids back onto the ready items (same order).
        (res.sessions || []).forEach((sid, idx) => {
            if (ready[idx]) ready[idx].sessionId = sid;
        });
        pollBatch();
    } catch (e) {
        status.innerHTML = `<p style="color: var(--error);">${escapeHtml(e.message)}</p>`;
        btn.disabled = false;
    }
}

function pollBatch() {
    const status = document.getElementById("news-batch-status");
    const tracked = batchItems.filter((i) => i.sessionId);
    let consecutiveErrors = 0;
    const poll = async () => {
        let rows;
        try {
            const res = await apiRequest("/api/news/session-status", {
                method: "POST",
                body: { ids: tracked.map((i) => i.sessionId) },
            });
            consecutiveErrors = 0;
            rows = res.sessions || [];
        } catch (e) {
            consecutiveErrors++;
            if (consecutiveErrors >= 5) {
                status.innerHTML = `<p style="color: var(--error);">${escapeHtml(e.message)}</p>`;
                return;
            }
            setTimeout(poll, 2000);
            return;
        }
        const byId = Object.fromEntries(rows.map((r) => [r.id, r]));
        tracked.forEach((item) => {
            const r = byId[item.sessionId];
            if (!r) return;
            item.status = r.status;
            item.handle = r.article_handle;
            item.error = r.error;
            const chip = document.getElementById(`batch-chip-${batchItems.indexOf(item)}`);
            if (chip) chip.innerHTML = batchChip(item);
        });
        const done = tracked.filter((i) => i.status === "published" || i.status === "failed").length;
        const published = tracked.filter((i) => i.status === "published").length;
        if (done >= tracked.length) {
            status.innerHTML = `<p style="color: var(--success);">Done — ${published}/${tracked.length} published.</p>`;
            loadNewsArticles();
        } else {
            status.innerHTML = `<div class="spinner" style="width:20px;height:20px;border-width:2px;"></div> ${done}/${tracked.length} done…`;
            setTimeout(poll, 2000);
        }
    };
    poll();
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

// ── Start (dispatch to manual analyze or auto-publish) ───────────

function startNews() {
    const auto = document.getElementById("news-auto");
    return auto && auto.checked ? autoPublish() : analyzeNews();
}

function updateStartButton() {
    const auto = document.getElementById("news-auto");
    const label = document.getElementById("news-start-label");
    if (label) label.textContent = auto && auto.checked ? "Auto-Publish" : "Analyze & Enhance";
}

// ── Auto mode (analyze → images → links → publish) ───────────────

const AUTO_STAGES = {
    auto_queued: "Queued…",
    auto_analyzing: "Analyzing & enhancing…",
    auto_hero: "Generating hero image…",
    auto_inline: "Generating images…",
    auto_links: "Adding internal links…",
    auto_publishing: "Publishing to Shopify…",
};

async function autoPublish() {
    const source = document.getElementById("news-source").value.trim();
    const sourceUrls = collectSources();
    const status = document.getElementById("news-analyze-status");
    if (!storeId) return showToast("No store found. Add a store first.", "error");
    if (!source) return showToast("Paste or upload an article first", "error");

    const btn = typeof event !== "undefined" && event && event.target instanceof Element ? event.target.closest("button") : null;
    return guardButton(btn, async () => {
        status.innerHTML = `<div class="spinner" style="width:20px;height:20px;border-width:2px;"></div> Starting auto-publish…`;
        try {
            const res = await apiRequest("/api/news/auto", {
                method: "POST",
                body: { store_id: storeId, source_text: source, source_urls: sourceUrls },
            });
            currentSessionId = res.session_id;
            pollAutoSession(currentSessionId, status);
        } catch (e) {
            status.innerHTML = `<p style="color: var(--error);">${escapeHtml(e.message)}</p>`;
        }
    });
}

function pollAutoSession(sessionId, status) {
    let consecutiveErrors = 0;
    const poll = async () => {
        let session;
        try {
            session = await apiRequest(`/api/news/session/${sessionId}`);
            consecutiveErrors = 0;
        } catch (e) {
            consecutiveErrors++;
            if (consecutiveErrors >= 5) {
                status.innerHTML = `<p style="color: var(--error);">${escapeHtml(e.message)}</p>`;
                return;
            }
            setTimeout(poll, 2000);
            return;
        }
        if (session.status === "published") {
            status.innerHTML = "";
            const url = `${storeBaseUrl}/blogs/news/${session.article_handle || ""}`;
            show("news-step-paste", false);
            show("news-step-review", false);
            show("news-step-done", true);
            document.getElementById("done-msg").textContent = "Published!";
            const link = document.getElementById("done-link");
            link.textContent = url;
            link.href = url;
            showToast("Article auto-published.", "success");
            loadNewsArticles();
            return;
        }
        if (session.status === "failed") {
            status.innerHTML = `<p style="color: var(--error);">Auto-publish failed: ${escapeHtml(session.error || "Unknown error")}</p>`;
            return;
        }
        const label = AUTO_STAGES[session.status] || "Working…";
        status.innerHTML = `<div class="spinner" style="width:20px;height:20px;border-width:2px;"></div> ${escapeHtml(label)}`;
        setTimeout(poll, 2000);
    };
    poll();
}

// ── Analyze ──────────────────────────────────────────────────────

async function analyzeNews() {
    const source = document.getElementById("news-source").value.trim();
    const sourceUrls = collectSources();
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
    let consecutiveErrors = 0;
    const poll = async () => {
        try {
            const task = await apiRequest(`/api/tasks/${taskId}`);
            consecutiveErrors = 0;
            if (task.status === "completed" || task.status === "failed") {
                onDone(task);
            } else {
                setTimeout(poll, 2000);
            }
        } catch (e) {
            consecutiveErrors++;
            if (consecutiveErrors >= 5) {
                onDone({ status: "failed", error: e.message });
                return;
            }
            setTimeout(poll, 2000);
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
    const btn = typeof event !== "undefined" && event && event.target instanceof Element ? event.target.closest("button") : null;
    return guardButton(btn, async () => {
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
            link.textContent = res.url || "";
            link.href = res.url || "#";
        } catch (e) {
            showToast("Publish failed: " + e.message, "error");
        }
    });
}

function resetCreate() {
    currentSessionId = null;
    current = null;
    lastSession = null;
    document.getElementById("news-source").value = "";
    resetSources();
    document.getElementById("news-analyze-status").innerHTML = "";
    show("news-step-done", false);
    show("news-step-review", false);
    show("news-step-paste", true);
    switchTab("create");
}

// ── Published list ───────────────────────────────────────────────

async function loadNewsArticles() {
    closeManage();
    const container = document.getElementById("news-articles-container");
    if (!storeId) { container.innerHTML = `<p>No store found.</p>`; return; }
    try {
        const data = await apiRequest(`/api/news/articles?store_id=${storeId}`);
        storeBaseUrl = data.store_base_url || storeBaseUrl;
        const articles = data.articles || [];
        if (!articles.length) {
            container.innerHTML = `<div class="card"><p style="color:var(--on-surface-secondary);">No news published yet. Create your first article in the “Create News” tab.</p></div>`;
            return;
        }
        publishedArticles = articles;
        const rows = articles.map((a, i) => {
            const status = a.is_published
                ? `<span class="news-badge news-badge--ok">Published</span>`
                : `<span class="news-badge news-badge--pending">Draft</span>`;
            const url = `${storeBaseUrl}/blogs/news/${a.handle || ""}`;
            return `<tr>
                <td>${escapeHtml(a.title || "Untitled")}</td>
                <td>${escapeHtml(a.category || "—")}</td>
                <td>${status}</td>
                <td>${a.published_at ? formatDate(a.published_at) : "—"}</td>
                <td style="white-space:nowrap;">
                    <button class="btn btn-secondary btn-sm" onclick="openManage(${i})"><span class="material-symbols-rounded">image</span> Images</button>
                    <a class="news-link" href="${escapeAttr(url)}" target="_blank" rel="noopener" style="margin-left:10px;">View ↗</a>
                </td>
            </tr>`;
        }).join("");
        container.innerHTML = `<div class="table-container"><table class="data-table">
            <thead><tr><th>Title</th><th>Category</th><th>Status</th><th>Published</th><th></th></tr></thead>
            <tbody>${rows}</tbody></table></div>`;
    } catch (e) {
        container.innerHTML = `<p style="color:var(--error);">${escapeHtml(e.message)}</p>`;
    }
}

// ── Manage images on a published article ─────────────────────────

let publishedArticles = [];
let manageShopifyId = null;

function closeManage() {
    show("news-manage", false);
    show("news-articles-container", true);
}

async function openManage(index) {
    const a = publishedArticles[index];
    if (!a) return;
    manageShopifyId = a.shopify_id;
    show("news-articles-container", false);
    show("news-manage", true);
    document.getElementById("news-manage-title").textContent = a.title || "Untitled";
    const body = document.getElementById("news-manage-body");
    body.innerHTML = `<div class="spinner" style="width:24px;height:24px;"></div>`;
    try {
        const data = await apiRequest(`/api/news/article-images?shopify_id=${encodeURIComponent(a.shopify_id)}`);
        renderManage(data);
    } catch (e) {
        body.innerHTML = `<p style="color:var(--error);">${escapeHtml(e.message)}</p>`;
    }
}

function _imageCard(kind, idx, img) {
    const thumb = img.image_url || img.cdn_url || "";
    const thumbHtml = thumb
        ? `<img id="manage-thumb-${kind}-${idx}" src="${escapeAttr(thumb)}" alt="" style="width:100%; max-width:320px; border-radius:8px; display:block;">`
        : `<div id="manage-thumb-${kind}-${idx}" style="width:100%; max-width:320px; aspect-ratio:16/9; background:#f1f3f4; border:1px dashed #bbb; border-radius:8px; display:flex; align-items:center; justify-content:center; color:#888; font-size:13px;">No image</div>`;
    const caption = kind === "inline" && img.caption ? `<p style="font-size:12px; color:var(--on-surface-secondary); margin:6px 0 0;">${escapeHtml(img.caption)}</p>` : "";
    const label = kind === "hero" ? "Hero image" : `Inline image ${idx + 1}`;
    const onclick = kind === "hero" ? "regenHero()" : `regenInline(${idx})`;
    return `<div class="card" style="margin-bottom:16px;">
        <h4 style="margin:0 0 10px;">${label}</h4>
        ${thumbHtml}${caption}
        <div class="form-group" style="margin-top:12px;">
            <label style="font-size:13px;">Image prompt</label>
            <textarea id="manage-prompt-${kind}-${idx}" class="form-control" rows="3">${escapeHtml(img.prompt || "")}</textarea>
        </div>
        <button class="btn btn-primary btn-sm" id="manage-btn-${kind}-${idx}" onclick="${onclick}">
            <span class="material-symbols-rounded">refresh</span> Regenerate
        </button>
        <span id="manage-status-${kind}-${idx}" style="margin-left:10px; font-size:13px;"></span>
    </div>`;
}

function renderManage(data) {
    const body = document.getElementById("news-manage-body");
    let html = _imageCard("hero", 0, data.hero || {});
    if (data.inline && data.inline.length) {
        html += `<h4 style="margin:20px 0 8px;">Inline images</h4>`;
        html += data.inline.map((img) => _imageCard("inline", img.index, img)).join("");
    } else if (!data.has_session) {
        html += `<p style="font-size:13px; color:var(--on-surface-secondary);">Inline image prompts aren't available for this article (no source session), so only the hero can be regenerated.</p>`;
    }
    body.innerHTML = html;
}

function _regenStart(kind, idx) {
    const btn = document.getElementById(`manage-btn-${kind}-${idx}`);
    const status = document.getElementById(`manage-status-${kind}-${idx}`);
    if (btn) btn.disabled = true;
    if (status) status.innerHTML = `<span style="color:var(--on-surface-secondary);">Generating…</span>`;
    return { btn, status };
}

function _regenDone(kind, idx, newUrl, ok, errMsg) {
    const btn = document.getElementById(`manage-btn-${kind}-${idx}`);
    const status = document.getElementById(`manage-status-${kind}-${idx}`);
    if (btn) btn.disabled = false;
    if (ok) {
        const thumb = document.getElementById(`manage-thumb-${kind}-${idx}`);
        if (thumb && newUrl) {
            if (thumb.tagName === "IMG") {
                thumb.src = newUrl;
            } else {
                thumb.outerHTML = `<img id="manage-thumb-${kind}-${idx}" src="${escapeAttr(newUrl)}" alt="" style="width:100%; max-width:320px; border-radius:8px; display:block;">`;
            }
        }
        if (status) status.innerHTML = `<span style="color:var(--success);">✓ Updated</span>`;
        showToast("Image regenerated and pushed to Shopify.", "success");
    } else {
        if (status) status.innerHTML = `<span style="color:var(--error);">${escapeHtml(errMsg || "Failed")}</span>`;
    }
}

async function regenHero() {
    if (!manageShopifyId) return;
    const prompt = (document.getElementById("manage-prompt-hero-0")?.value || "").trim();
    _regenStart("hero", 0);
    try {
        const res = await apiRequest("/api/news/regenerate-hero", {
            method: "POST",
            body: { shopify_id: manageShopifyId, prompt },
        });
        pollTask(res.task_id, (task) => {
            _regenDone("hero", 0, task.result?.image_url, task.status === "completed", task.error);
        });
    } catch (e) {
        _regenDone("hero", 0, null, false, e.message);
    }
}

async function regenInline(index) {
    if (!manageShopifyId) return;
    const prompt = (document.getElementById(`manage-prompt-inline-${index}`)?.value || "").trim();
    _regenStart("inline", index);
    try {
        const res = await apiRequest("/api/news/regenerate-inline", {
            method: "POST",
            body: { shopify_id: manageShopifyId, index, prompt },
        });
        pollTask(res.task_id, (task) => {
            _regenDone("inline", index, task.result?.cdn_url, task.status === "completed", task.error);
        });
    } catch (e) {
        _regenDone("inline", index, null, false, e.message);
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
