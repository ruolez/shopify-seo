// ── Intro Links: collection link in each post's opening paragraph ──
// Audit table → draft intro rewrites (background task) → review before/after
// per post, retarget/redraft → apply the checked drafts to Shopify.

let introLinksAudit = null;
let introLinksPlan = null;
let introLinksFilter = "fixable";
let introLinksStoreId = null;
const introLinksSelected = new Set();
const introDraftsSelected = new Set();

const INTRO_STATUS_LABELS = {
    ok: ["Linked", "badge-success"],
    needs_fix: ["Link too deep", "badge-warning"],
    weak_anchor: ["Weak anchor", "badge-warning"],
    off_target: ["Off-topic link", "badge-warning"],
    no_collection: ["No collection link", "badge-error"],
    no_intro: ["No intro paragraph", "badge-default"],
};

async function initIntroLinksTab() {
    introLinksStoreId = getSelectedStoreId() || (await getFirstStoreId());
    if (!introLinksStoreId) return;
    UISkeleton.show(document.getElementById("intro-links-table"), { type: "table", rows: 8 });
    try {
        const [audit, plan] = await Promise.all([
            apiRequest(`/api/blog/intro-links?store_id=${introLinksStoreId}`),
            apiRequest(`/api/blog/intro-links/plan/latest?store_id=${introLinksStoreId}`),
        ]);
        introLinksAudit = audit;
        introLinksPlan = plan.task;
        renderIntroLinksSummary();
        renderIntroLinksTable();
        renderIntroDrafts();
        if (introLinksPlan && !TERMINAL_TASK_STATUSES.includes(introLinksPlan.status)) watchIntroPlan(introLinksPlan.id);
    } catch (e) {
        document.getElementById("intro-links-table").innerHTML =
            `<p style="color: var(--error);">${escapeHtml(e.message)}</p>`;
    }
}

async function reloadIntroLinksAudit() {
    introLinksAudit = await apiRequest(`/api/blog/intro-links?store_id=${introLinksStoreId}`);
    renderIntroLinksSummary();
    renderIntroLinksTable();
}

function renderIntroLinksSummary() {
    const s = introLinksAudit.summary;
    const pct = (n) => (s.posts ? Math.round((n / s.posts) * 100) : 0);
    const share = s.clicks_share_without_p1 == null ? "—" : `${Math.round(s.clicks_share_without_p1 * 100)}%`;
    const card = (value, label, sub) => `
        <div class="stat-card">
            <div class="stat-value">${value}</div>
            <div class="stat-label">${label}</div>
            ${sub ? `<div class="stat-sub">${sub}</div>` : ""}
        </div>`;
    document.getElementById("intro-links-summary").innerHTML = [
        card(`${s.in_p1}<span class="stat-of">/${s.posts}</span>`, "Collection link in paragraph 1", `${pct(s.in_p1)}% of ${s.guides} guides + ${s.product_news} product news`),
        card(`${s.in_first_two}<span class="stat-of">/${s.posts}</span>`, "Within the first two paragraphs", `${pct(s.in_first_two)}%`),
        card(s.median_words_before_first ?? "—", "Median words before the first link", "Lower is better"),
        card(share, "Clicks landing on posts without a P1 link", `${s.fixable} posts can be fixed`),
    ].join("");
}

function setIntroLinksFilter(value) {
    introLinksFilter = value;
    renderIntroLinksTable();
}

function visibleIntroRows() {
    const rows = introLinksAudit ? introLinksAudit.articles : [];
    return introLinksFilter === "fixable" ? rows.filter((r) => r.fixable) : rows;
}

function introTargetCell(r) {
    if (!r.target) return '<span class="muted">—</span>';
    const mode = {
        moved: `<span class="badge badge-info">moves</span>`,
        added: `<span class="badge badge-warning" title="The post doesn't link this collection yet — one link is added">adds link</span>`,
        reanchor: `<span class="badge badge-default">re-anchors</span>`,
    }[r.target.mode] || "";
    return `<div class="intro-target"><code>/collections/${escapeHtml(r.target.handle)}</code> ${mode}</div>
        <div class="muted intro-target-kw">anchor: ${escapeHtml(r.target.keyword || r.target.title)}</div>`;
}

function renderIntroLinksTable() {
    const rows = visibleIntroRows();
    const container = document.getElementById("intro-links-table");
    if (!rows.length) {
        container.innerHTML = `<div class="empty-state"><span class="material-symbols-rounded">task_alt</span>
            <p>${introLinksFilter === "fixable" ? "Every eligible post already links a collection in its opening paragraph." : "No eligible posts."}</p></div>`;
        updateIntroDraftButton();
        return;
    }
    const body = rows.map((r) => {
        const [label, cls] = INTRO_STATUS_LABELS[r.status] || [r.status, "badge-default"];
        const checked = introLinksSelected.has(r.article_id) ? "checked" : "";
        const depth = r.words_before_first == null ? "—" : `${r.words_before_first} words`;
        return `<tr>
            <td><input type="checkbox" ${r.fixable ? "" : "disabled"} ${checked} onchange="toggleIntroLinkRow(${r.article_id}, this.checked)"></td>
            <td class="intro-title-cell">
                <div class="intro-title">${escapeHtml(r.title || "")}</div>
                <div class="muted">${r.kind === "product news" ? "Product News" : "Guide"} · query: ${escapeHtml(r.primary_query || "")}</div>
            </td>
            <td><span class="badge ${cls}">${label}</span></td>
            <td class="num">${depth}</td>
            <td>${introTargetCell(r)}</td>
            <td class="num">${r.clicks ?? "—"}</td>
        </tr>`;
    }).join("");
    container.innerHTML = `<div class="table-container"><table class="data-table intro-links-table">
        <thead><tr>
            <th><input type="checkbox" title="Select all fixable" onchange="toggleAllIntroLinks(this.checked)"></th>
            <th>Post</th><th>Status</th><th class="num">First collection link</th><th>Intro link target</th><th class="num">Clicks (30d)</th>
        </tr></thead>
        <tbody>${body}</tbody></table></div>`;
    updateIntroDraftButton();
}

function toggleIntroLinkRow(id, on) {
    if (on) introLinksSelected.add(id);
    else introLinksSelected.delete(id);
    updateIntroDraftButton();
}

function toggleAllIntroLinks(on) {
    visibleIntroRows().filter((r) => r.fixable).forEach((r) => toggleIntroLinkRow(r.article_id, on));
    renderIntroLinksTable();
}

function selectTopIntroLinks(n) {
    introLinksSelected.clear();
    introLinksAudit.articles.filter((r) => r.fixable).slice(0, n).forEach((r) => introLinksSelected.add(r.article_id));
    renderIntroLinksTable();
}

function updateIntroDraftButton() {
    const btn = document.getElementById("intro-links-draft-btn");
    const n = introLinksSelected.size;
    document.getElementById("intro-links-draft-label").textContent = n ? `Draft ${n} intro${n === 1 ? "" : "s"}` : "Draft intros";
    btn.disabled = n === 0 || n > 50 || introPlanRunning();
    btn.title = n > 50 ? "Draft at most 50 posts per run" : "";
}

function introPlanRunning() {
    return !!(introLinksPlan && !TERMINAL_TASK_STATUSES.includes(introLinksPlan.status));
}

async function draftIntroLinks(btn) {
    const ids = [...introLinksSelected];
    if (!ids.length) return;
    await guardButton(btn, async () => {
        try {
            const res = await apiRequest("/api/blog/intro-links/plan", {
                method: "POST",
                body: { store_id: introLinksStoreId, article_ids: ids },
            });
            introLinksPlan = { id: res.task_id, status: "pending", result: null };
            introLinksSelected.clear();
            renderIntroLinksTable();
            watchIntroPlan(res.task_id);
        } catch (e) {
            showToast(e.message, "error");
        }
    });
}

async function watchIntroPlan(taskId) {
    renderIntroDrafts();
    try {
        await pollTaskUntilDone(`/api/tasks/${taskId}`, {
            onProgress: (task) => {
                introLinksPlan = { ...introLinksPlan, status: task.status, result: task.result };
                renderIntroDraftProgress(task.result && task.result.progress);
            },
        });
        const latest = await apiRequest(`/api/blog/intro-links/plan/latest?store_id=${introLinksStoreId}`);
        introLinksPlan = latest.task;
        showToast("Intro drafts are ready to review", "success");
    } catch (e) {
        showToast(e.message, "error");
    }
    renderIntroDrafts();
    updateIntroDraftButton();
}

function renderIntroDraftProgress(progress) {
    const el = document.getElementById("intro-drafts-progress");
    if (!el || !progress) return;
    const pct = progress.total ? Math.round((progress.completed / progress.total) * 100) : 0;
    el.innerHTML = `<div class="progress-bar-container"><div class="progress-bar" style="width:${pct}%"></div></div>
        <div class="progress-stats">${progress.completed} of ${progress.total} drafted${progress.failed ? ` · ${progress.failed} failed` : ""}</div>`;
}

function introDraftItems() {
    return (introLinksPlan && introLinksPlan.result && introLinksPlan.result.items) || [];
}

function renderIntroDrafts() {
    const container = document.getElementById("intro-links-drafts");
    if (!introLinksPlan) {
        container.innerHTML = "";
        return;
    }
    if (introPlanRunning()) {
        container.innerHTML = `<div class="card"><h3 style="margin:0 0 12px;">Drafting intros…</h3><div id="intro-drafts-progress"></div></div>`;
        renderIntroDraftProgress(introLinksPlan.result && introLinksPlan.result.progress);
        return;
    }
    if (introLinksPlan.status === "failed") {
        container.innerHTML = `<div class="card"><p style="color: var(--error); margin:0;">Drafting failed: ${escapeHtml(introLinksPlan.error || "unknown error")}</p></div>`;
        return;
    }
    const items = introDraftItems();
    const pending = items.filter((i) => i.valid && !i.applied);
    introDraftsSelected.forEach((id) => {
        if (!pending.some((i) => i.article_id === id)) introDraftsSelected.delete(id);
    });
    if (!container.dataset.planId || container.dataset.planId !== String(introLinksPlan.id)) {
        introDraftsSelected.clear();
        pending.filter((i) => !i.apply_error).forEach((i) => introDraftsSelected.add(i.article_id));
        container.dataset.planId = String(introLinksPlan.id);
    }
    const applied = items.filter((i) => i.applied).length;
    const review = items.filter((i) => !i.valid && !i.applied).length;
    container.innerHTML = `<div class="card">
        <div class="intro-drafts-head">
            <div>
                <h3 style="margin:0;">Review drafts</h3>
                <div class="muted">${formatDate(introLinksPlan.created_at)} · ${pending.length} ready · ${review} need attention · ${applied} applied</div>
            </div>
            <div class="intro-links-actions">
                <button class="btn btn-secondary btn-sm" onclick="dismissIntroDrafts()">Hide</button>
                <button class="btn btn-primary btn-sm" id="intro-apply-btn" onclick="applyIntroDrafts(this)">
                    <span class="material-symbols-rounded">publish</span>
                    <span id="intro-apply-label"></span>
                </button>
            </div>
        </div>
        <div id="intro-apply-progress"></div>
        <div class="intro-draft-list">${items.map(renderIntroDraftCard).join("")}</div>
    </div>`;
    updateIntroApplyButton();
}

function renderIntroDraftCard(item) {
    const id = item.article_id;
    if (item.error) {
        return `<div class="intro-draft">
            <div class="intro-draft-title">${escapeHtml(item.title || `Article ${id}`)}</div>
            <p style="color: var(--error); margin: 6px 0 0;">${escapeHtml(item.error)}</p></div>`;
    }
    const t = item.target || {};
    const modeText = {
        moved: `moved from ${item.words_before_first ?? "?"} words in`,
        added: "adds a link (post didn't link this collection)",
        reanchor: "re-anchored",
    }[t.mode] || "";
    const status = item.applied
        ? `<span class="badge badge-success">Applied</span>`
        : item.apply_error
            ? `<span class="badge badge-error" title="${escapeHtml(item.apply_error)}">Not applied</span>`
            : item.valid ? "" : `<span class="badge badge-warning">Needs attention</span>`;
    const options = (item.target_options || []).map((o) =>
        `<option value="${escapeHtml(o.handle)}" ${o.handle === t.handle ? "selected" : ""}>${escapeHtml(o.title)}${o.linked ? " · linked" : ""}</option>`
    ).join("");
    const violations = (item.violations || []).length
        ? `<ul class="intro-violations">${item.violations.map((v) => `<li>${escapeHtml(v.message)}</li>`).join("")}</ul>` : "";
    const dropped = (item.dropped_collection_links || []).length
        ? `<div class="muted">Dropped from the intro: ${item.dropped_collection_links.map(escapeHtml).join(", ")}</div>` : "";
    const canSelect = item.valid && !item.applied;
    return `<div class="intro-draft ${item.applied ? "is-applied" : ""}" id="intro-draft-${id}">
        <div class="intro-draft-top">
            <label class="intro-draft-title">
                <input type="checkbox" ${canSelect ? "" : "disabled"} ${introDraftsSelected.has(id) ? "checked" : ""}
                    onchange="toggleIntroDraft(${id}, this.checked)">
                ${escapeHtml(item.title || "")}
            </label>
            ${status}
        </div>
        <div class="muted">${item.kind === "product news" ? "Product News" : "Guide"} · query “${escapeHtml(item.primary_query || "")}” ·
            <code>/collections/${escapeHtml(t.handle || "")}</code> ${escapeHtml(modeText)} ·
            words ${item.before_words ?? "—"} → ${item.after_words ?? "—"} · collection links ${item.links_before ?? "—"} → ${item.links_after ?? "—"}</div>
        ${item.apply_error ? `<div style="color: var(--error); font-size: 13px;">${escapeHtml(item.apply_error)}</div>` : ""}
        ${violations}${dropped}
        ${item.before_intro ? `<div class="comparison-grid">
            <div class="comparison-pane"><h5>Current intro</h5>${item.before_intro}</div>
            <div class="comparison-pane new-body"><h5>New intro</h5>${item.after_intro || "<p>—</p>"}</div>
        </div>` : ""}
        ${item.applied ? "" : `<div class="intro-redraft">
            <select class="form-control" id="intro-target-${id}">${options}</select>
            <button class="btn btn-secondary btn-sm" onclick="redraftIntro(${id}, this)">
                <span class="material-symbols-rounded">refresh</span> Redraft
            </button>
        </div>`}
    </div>`;
}

function toggleIntroDraft(id, on) {
    if (on) introDraftsSelected.add(id);
    else introDraftsSelected.delete(id);
    updateIntroApplyButton();
}

function updateIntroApplyButton() {
    const btn = document.getElementById("intro-apply-btn");
    if (!btn) return;
    const n = introDraftsSelected.size;
    document.getElementById("intro-apply-label").textContent = `Apply ${n} to Shopify`;
    btn.disabled = n === 0;
}

async function redraftIntro(articleId, btn) {
    const target = document.getElementById(`intro-target-${articleId}`)?.value;
    await guardButton(btn, async () => {
        try {
            const res = await apiRequest(`/api/blog/intro-links/plan/${introLinksPlan.id}/redraft`, {
                method: "POST",
                body: { article_id: articleId, target_handle: target },
            });
            const items = introDraftItems();
            const idx = items.findIndex((i) => i.article_id === articleId);
            if (idx >= 0) items[idx] = res.item;
            if (res.item.valid) introDraftsSelected.add(articleId);
            else introDraftsSelected.delete(articleId);
            renderIntroDrafts();
            showToast(res.item.valid ? "Redrafted" : "Redrafted — still needs attention", res.item.valid ? "success" : "warning");
        } catch (e) {
            showToast(e.message, "error");
        }
    });
}

function applyIntroDrafts(btn) {
    const ids = [...introDraftsSelected];
    if (!ids.length) return;
    showBlogConfirmDialog(
        "Apply intro rewrites?",
        `This updates <strong>${ids.length}</strong> live article${ids.length === 1 ? "" : "s"} on Shopify: the opening paragraph is replaced
         and later links to the same collection become plain text. Posts whose live intro changed since drafting are skipped.`,
        () => runIntroApply(btn, ids),
    );
}

async function runIntroApply(btn, ids) {
    await guardButton(btn, async () => {
        try {
            const res = await apiRequest("/api/blog/intro-links/apply", {
                method: "POST",
                body: { task_id: introLinksPlan.id, article_ids: ids },
            });
            const progressEl = document.getElementById("intro-apply-progress");
            const final = await pollTaskUntilDone(`/api/tasks/${res.task_id}`, {
                onProgress: (task) => {
                    const p = task.result && task.result.progress;
                    if (!p || !progressEl) return;
                    const pct = p.total ? Math.round((p.completed / p.total) * 100) : 0;
                    progressEl.innerHTML = `<div class="progress-bar-container"><div class="progress-bar" style="width:${pct}%"></div></div>
                        <div class="progress-stats">${p.completed} of ${p.total} processed</div>`;
                },
            });
            const results = (final.result && final.result.results) || [];
            const ok = results.filter((r) => r.status === "applied").length;
            const notOk = results.length - ok;
            showToast(`Applied ${ok} intro${ok === 1 ? "" : "s"}${notOk ? ` · ${notOk} skipped or failed` : ""}`, notOk ? "warning" : "success");
        } catch (e) {
            showToast(e.message, "error");
        }
        const latest = await apiRequest(`/api/blog/intro-links/plan/latest?store_id=${introLinksStoreId}`);
        introLinksPlan = latest.task;
        introDraftsSelected.clear();
        document.getElementById("intro-links-drafts").dataset.planId = "";
        renderIntroDrafts();
        await reloadIntroLinksAudit();
    });
}

function dismissIntroDrafts() {
    document.getElementById("intro-links-drafts").innerHTML = "";
}
