/* Act on a keyword — the decision surface for closing a coverage gap.
 *
 * Replaces the old two-button surface (a context-free product/collection
 * picker, and a fire-and-forget research call). One modal routes a keyword to
 * the page type its intent actually requires, then either assigns it to an
 * existing page or hands off to the Blog wizard.
 *
 * Owns no coverage logic: which content types satisfy an intent comes from the
 * server's `required_types` (rules.keyword_coverage), never a map duplicated
 * here, or the two drift.
 */

const KA_TYPE_LABEL = { product: "Product", collection: "Collection", article: "Article", page: "Page" };
const KA_TYPE_ICON = {
    product: "inventory_2", collection: "category", article: "article", page: "description",
};

/* Assignment writes go to different endpoints by type: products and
 * collections must also rebuild their keyword resolution, articles and pages
 * have none. One write path per type keeps the ownership ledger consistent. */
function kaIsResolverType(t) {
    return t === "product" || t === "collection";
}

function kaEscape(s) {
    return escapeHtml(s == null ? "" : String(s));
}

function kaVolume(n) {
    return (n || 0).toLocaleString();
}

/* ── Entry point ─────────────────────────────────────────────────────────── */

/** Open the act-on-keyword modal.
 *  opts: {intent, clusterId, requiredTypes[], onDone} */
async function ksOpenActionModal(keywordId, opts = {}) {
    if (!ksStoreId) return;
    let detail;
    try {
        detail = await apiRequest(`/api/keyword-strategy/keywords/${keywordId}/detail?store_id=${ksStoreId}`);
    } catch (e) {
        showToast("Could not load keyword: " + e.message, "error");
        return;
    }

    const kw = detail.keyword || {};
    const intent = opts.intent || kw.intent || "commercial";
    const required = opts.requiredTypes && opts.requiredTypes.length
        ? opts.requiredTypes
        : kaRequiredTypesFor(detail, intent);

    const state = {
        keywordId,
        keyword: kw.keyword,
        volume: kw.search_volume || 0,
        intent,
        clusterId: opts.clusterId != null ? opts.clusterId : (kw.cluster_id || 0),
        family: (detail.family && detail.family.cluster && detail.family.cluster.name) || null,
        owner: detail.owner || null,
        required,
        onDone: opts.onDone || null,
    };

    const modal = UIModal.open({
        title: `Act on “${kaEscape(state.keyword)}”`,
        bodyHtml: '<div id="ka-body"></div>',
        size: "lg",
    });
    state.modal = modal;
    kaRenderRoutes(state);
}

/* required_types comes from the family payload the coverage service already
 * computed for this intent; the flat keyword list has no such context, so fall
 * back to the keyword's own owner-type expectations only as a last resort. */
function kaRequiredTypesFor(detail, intent) {
    const gaps = (detail.family && detail.family.gaps) || [];
    const hit = gaps.find(g => g.intent === intent && (g.required_types || []).length);
    if (hit) return hit.required_types;
    const cell = detail.family && detail.family.coverage && detail.family.coverage[intent];
    if (cell && (cell.required_types || []).length) return cell.required_types;
    return ["collection", "article"];
}

/* Ownership has three strengths and they mean different things. A resolved
 * primary or an accepted assignment is a real claim; content_keyword_map's
 * is_primary ("mapped_primary") is just the last content audit's guess, is
 * rebuilt on every run, and routinely names several pages for one keyword —
 * calling that "owned by" would misrepresent it. */
const KA_ROLE_LABEL = {
    resolved_primary: "owned by",
    assigned: "assigned to",
    mapped_primary: "mentioned on",
};

function kaHeader(state) {
    const ownerLine = state.owner
        ? `<span class="badge badge-${typeBadge(state.owner.item_type)}" style="font-size:10px;">${state.owner.item_type}</span>
           <span style="color:var(--on-surface-secondary);">${KA_ROLE_LABEL[state.owner.role] || "owned by"}</span>
           <strong>${kaEscape(state.owner.title || "#" + state.owner.item_id)}</strong>`
        : '<span style="color:var(--warning);">No page owns this keyword</span>';
    return `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:13px;
                        padding-bottom:12px;margin-bottom:14px;border-bottom:1px solid var(--outline);">
        <strong style="font-size:15px;">${kaEscape(state.keyword)}</strong>
        <span style="color:var(--on-surface-secondary);">${kaVolume(state.volume)} vol/mo</span>
        <span class="badge badge-${intentBadge(state.intent)}" style="font-size:10px;">${kaEscape(state.intent)}</span>
        ${state.family ? `<span style="color:var(--on-surface-secondary);">${kaEscape(state.family)}</span>` : ""}
        <span style="flex:1;"></span>
        ${ownerLine}
    </div>`;
}

/* ── Step 1: route ───────────────────────────────────────────────────────── */

function kaRenderRoutes(state) {
    const req = state.required;
    const canAssign = req.filter(t => KA_TYPE_LABEL[t]);
    const canArticle = req.includes("article");

    const cards = [];
    canAssign.forEach(t => {
        cards.push(kaRouteCard({
            icon: KA_TYPE_ICON[t],
            title: `Assign to an existing ${KA_TYPE_LABEL[t].toLowerCase()}`,
            desc: kaIsResolverType(t)
                ? "Pins the keyword as that page's primary and re-analyzes it."
                : "Records ownership so the gap closes; optimize the page separately.",
            action: `ksActionPickTarget(${state.keywordId}, '${t}')`,
            recommended: t === req[0],
        }));
    });
    if (req.includes("collection")) {
        cards.push(kaRouteCard({
            icon: "add_circle",
            title: "Create a collection for this demand",
            desc: "Builds a rule-based category page that keeps populating itself. Previewed before anything is created.",
            action: `ksActionCreateCollection(${state.keywordId})`,
        }));
    }
    if (canArticle) {
        cards.push(kaRouteCard({
            icon: "edit_note",
            title: "Write a new article",
            desc: "Opens the Blog wizard seeded with this keyword and its family siblings.",
            action: `ksActionWriteArticle(${state.keywordId})`,
        }));
    }

    const body = state.modal.el.querySelector("#ka-body");
    body.innerHTML = kaHeader(state) + `
        <div style="font-size:12.5px;color:var(--on-surface-secondary);margin-bottom:10px;">
            ${kaEscape(state.intent)} intent is satisfied by
            <strong>${req.map(t => kaEscape(t)).join(" or ")}</strong> pages.
        </div>
        <div style="display:grid;gap:10px;">${cards.join("")}</div>`;
    window._kaState = state;
}

function kaRouteCard({ icon, title, desc, action, recommended }) {
    return `<div class="ka-route-card${recommended ? " recommended" : ""}" onclick="${action}">
        <span class="material-symbols-rounded" style="font-size:22px;">${icon}</span>
        <div style="flex:1;min-width:0;">
            <div style="font-weight:500;font-size:13.5px;">${title}
                ${recommended ? '<span class="badge badge-success" style="font-size:9.5px;margin-left:6px;">recommended</span>' : ""}</div>
            <div style="font-size:12px;color:var(--on-surface-secondary);margin-top:2px;">${desc}</div>
        </div>
        <span class="material-symbols-rounded" style="font-size:18px;color:var(--on-surface-secondary);">chevron_right</span>
    </div>`;
}

/* ── Step 2a: pick a target ──────────────────────────────────────────────── */

async function ksActionPickTarget(keywordId, itemType) {
    const state = window._kaState;
    if (!state) return;
    state.pickType = itemType;
    const body = state.modal.el.querySelector("#ka-body");
    body.innerHTML = kaHeader(state) + `
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;">
            <button class="btn btn-sm btn-outline" onclick="kaRenderRoutes(window._kaState)">
                <span class="material-symbols-rounded" style="font-size:15px;vertical-align:middle;">arrow_back</span> Back</button>
            <select id="ka-type" class="form-control" style="width:auto;font-size:13px;">
                ${["product", "collection", "article", "page"].map(t =>
                    `<option value="${t}" ${t === itemType ? "selected" : ""}>${KA_TYPE_LABEL[t]}</option>`).join("")}
            </select>
            <input type="text" id="ka-search" class="form-control" placeholder="Search pages…" style="flex:1;font-size:13px;">
        </div>
        <div id="ka-results" style="max-height:340px;overflow-y:auto;"></div>`;

    const search = body.querySelector("#ka-search");
    body.querySelector("#ka-type").addEventListener("change", e => {
        state.pickType = e.target.value;
        kaLoadTargets(state);
    });
    let t;
    search.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => kaLoadTargets(state), 300); });
    search.addEventListener("keydown", e => { if (e.key === "Enter") { clearTimeout(t); kaLoadTargets(state); } });
    search.focus();
    kaLoadTargets(state);
}

async function kaLoadTargets(state) {
    const results = state.modal.el.querySelector("#ka-results");
    const q = (state.modal.el.querySelector("#ka-search") || {}).value || "";
    results.innerHTML = '<div class="spinner" style="margin:18px auto"></div>';
    let data;
    try {
        data = await apiRequest(`/api/keyword-strategy/keywords/${state.keywordId}/targets`
            + `?store_id=${ksStoreId}&item_type=${state.pickType}&q=${encodeURIComponent(q)}`);
    } catch (e) {
        results.innerHTML = `<div style="font-size:13px;color:var(--error);padding:12px;">${kaEscape(e.message)}</div>`;
        return;
    }
    const targets = data.targets || [];
    if (!targets.length) {
        results.innerHTML = `<div style="font-size:13px;color:var(--on-surface-secondary);padding:14px;">
            No matching ${state.pickType}s. Try a broader search term${state.required.includes("collection")
                ? ", or create a collection for this demand instead." : "."}</div>`;
        return;
    }
    results.innerHTML = `<div class="autocomplete-dropdown" style="position:static;max-height:none;box-shadow:none;">
        ${targets.map((t, i) => kaTargetRow(t, i)).join("")}</div>`;
    results.querySelectorAll("[data-ka-pick]").forEach(row => {
        row.addEventListener("click", () => kaConfirmAssign(state, targets[parseInt(row.dataset.kaPick, 10)]));
    });
}

function kaTargetRow(t, i) {
    const bits = [];
    if (t.seo_score != null) {
        bits.push(`<span style="color:${scoreColor(t.seo_score)};font-weight:500;">SEO ${t.seo_score}</span>`);
    }
    if (t.gsc_position != null) {
        bits.push(`<span style="color:${t.gsc_position <= 20 ? "var(--success)" : "var(--warning)"};">
            already ranks #${t.gsc_position} for this keyword</span>`);
    }
    if (t.products_count != null) bits.push(`${t.products_count} products`);
    if (t.current_primary && t.current_primary.keyword) {
        bits.push(`primary: ${kaEscape(t.current_primary.keyword)}${t.current_primary.manual ? " 📌" : ""}`);
    } else if (!kaIsResolverType(t.item_type)) {
        bits.push("no keyword assigned");
    }

    // The single most useful thing the old picker never showed: what this
    // assignment costs. Losing a higher-volume primary is usually a mistake.
    const warn = t.would_demote && t.would_demote.loses_volume > 0
        ? `<div style="font-size:11.5px;color:var(--warning);margin-top:3px;">
             ⚠ Replaces “${kaEscape(t.would_demote.keyword)}” (${kaVolume(t.would_demote.volume)} vol)${
                t.would_demote.manual ? " — manually pinned" : ""}</div>`
        : "";

    return `<div class="autocomplete-item${t.already_owns ? " disabled" : ""}" ${t.already_owns ? "" : `data-ka-pick="${i}"`}>
        <div style="display:flex;align-items:center;gap:8px;">
            <span class="badge badge-${typeBadge(t.item_type)}" style="font-size:9.5px;">${t.item_type}</span>
            <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${kaEscape(t.title)}</span>
            ${t.already_owns ? '<span class="badge badge-success" style="font-size:9.5px;">already owns it</span>' : ""}
        </div>
        <div class="autocomplete-item-meta">${bits.join(" · ")}</div>
        ${warn}
    </div>`;
}

/* ── Confirm + write ─────────────────────────────────────────────────────── */

function kaConfirmAssign(state, target) {
    const cost = target.would_demote
        ? `<br><br>“${kaEscape(target.would_demote.keyword)}” (${kaVolume(target.would_demote.volume)} vol/mo)
           will stop being its primary keyword.`
        : "";
    const effect = kaIsResolverType(target.item_type)
        ? "The page is re-analyzed against the new keyword, and any other page holding it is re-resolved."
        : "Ownership is recorded so the coverage gap closes. The page content is not changed.";

    document.getElementById("confirm-dialog-title").textContent = "Assign keyword";
    document.getElementById("confirm-dialog-message").innerHTML =
        `Make <strong>“${kaEscape(state.keyword)}”</strong> the primary keyword of
         <strong>${kaEscape(target.title)}</strong>?${cost}
         <br><br><span style="color:var(--on-surface-secondary);font-size:12.5px;">${effect}</span>`;

    const okBtn = document.getElementById("confirm-dialog-ok");
    const fresh = okBtn.cloneNode(true);
    okBtn.parentNode.replaceChild(fresh, okBtn);
    fresh.textContent = "Assign";
    fresh.addEventListener("click", async () => {
        document.getElementById("confirm-dialog-overlay").classList.remove("active");
        await kaApplyAssign(state, target);
    });
    document.getElementById("confirm-dialog-overlay").classList.add("active");
}

async function kaApplyAssign(state, target) {
    const body = state.modal.el.querySelector("#ka-body");
    // The product/collection path re-resolves every page that held this
    // keyword — that can run for tens of seconds, so the modal shows real
    // progress rather than a frozen list.
    body.innerHTML = kaHeader(state) + `
        <div style="text-align:center;padding:34px 12px;">
            <div class="spinner" style="margin:0 auto 14px;"></div>
            <div style="font-size:13.5px;">Assigning “${kaEscape(state.keyword)}” to ${kaEscape(target.title)}…</div>
            <div style="font-size:12px;color:var(--on-surface-secondary);margin-top:6px;">
                ${kaIsResolverType(target.item_type)
                    ? "Re-analyzing the page and re-resolving any page that held this keyword."
                    : "Recording ownership."}</div>
        </div>`;

    try {
        let result;
        if (kaIsResolverType(target.item_type)) {
            result = await apiRequest(
                `/api/seo/items/${target.item_type}/${target.item_id}/primary-keyword?store_id=${ksStoreId}`,
                { method: "POST", headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ keyword: state.keyword, keyword_id: state.keywordId }) });
        } else {
            result = await apiRequest(
                `/api/keyword-strategy/keywords/${state.keywordId}/assign?store_id=${ksStoreId}`,
                { method: "POST", headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ item_type: target.item_type, item_id: target.item_id }) });
        }
        kaRenderAssignResult(state, target, result);
    } catch (e) {
        body.innerHTML = kaHeader(state) + `
            <div style="font-size:13px;color:var(--error);padding:14px;">Assign failed: ${kaEscape(e.message)}</div>
            <button class="btn btn-outline" onclick="ksActionPickTarget(${state.keywordId}, '${state.pickType}')">Back to results</button>`;
    }
}

function kaRenderAssignResult(state, target, result) {
    // Report what actually changed. A zero-volume keyword never flips a
    // coverage cell green, so promising one would be a lie.
    const superseded = (result && result.superseded) || [];
    const flips = state.volume > 0;
    // The header renders the keyword's owner; leaving the pre-assign value
    // would have it read "No page owns this keyword" directly above the line
    // saying which page now does.
    state.owner = { item_type: target.item_type, item_id: target.item_id, title: target.title,
                    role: kaIsResolverType(target.item_type) ? "resolved_primary" : "assigned" };
    const body = state.modal.el.querySelector("#ka-body");
    body.innerHTML = kaHeader(state) + `
        <div style="padding:8px 0 16px;">
            <div style="display:flex;align-items:center;gap:8px;font-size:14px;margin-bottom:12px;">
                <span class="material-symbols-rounded" style="color:var(--success);">check_circle</span>
                <span><strong>${kaEscape(target.title)}</strong> now owns “${kaEscape(state.keyword)}”.</span>
            </div>
            <ul style="font-size:12.5px;color:var(--on-surface-secondary);margin:0 0 14px 18px;">
                ${superseded.length
                    ? `<li>It stopped owning ${superseded.map(x => `“${kaEscape(x.keyword)}”`).join(", ")}.</li>`
                    : ""}
                <li>${flips
                    ? "The family's coverage cell for this intent now counts this page."
                    : "This keyword has no recorded search volume, so the coverage cell won't change — coverage is weighted by volume."}</li>
                ${kaIsResolverType(target.item_type)
                    ? `<li>SEO score after re-analysis: <strong>${result && result.score != null ? result.score : "—"}</strong>.</li>`
                    : "<li>The article's content is unchanged — optimize it to actually target the keyword.</li>"}
            </ul>
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
                ${kaIsResolverType(target.item_type)
                    ? `<button class="btn btn-primary btn-sm" onclick="kaCloseAndOptimize('${target.item_type}', ${target.item_id})">Optimize now</button>`
                    : ""}
                <button class="btn btn-outline btn-sm" onclick="kaUndoAssign(${state.keywordId}, '${target.item_type}', ${target.item_id}, this)">Undo</button>
                <button class="btn btn-outline btn-sm" onclick="kaFinish()">Done</button>
            </div>
        </div>`;
    ksFamilyCache = {};
}

async function kaUndoAssign(keywordId, itemType, itemId, btn) {
    await guardButton(btn, async () => {
        try {
            await apiRequest(`/api/keyword-strategy/keywords/${keywordId}/assign?store_id=${ksStoreId}`,
                { method: "DELETE", headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ item_type: itemType, item_id: itemId }) });
            showToast("Assignment undone", "success");
            ksFamilyCache = {};
            if (window._kaState) window._kaState.owner = null;
            kaFinish();
        } catch (e) {
            showToast("Undo failed: " + e.message, "error");
        }
    });
}

function kaCloseAndOptimize(itemType, itemId) {
    const state = window._kaState;
    const kw = state && state.keyword;
    const vol = state && state.volume;
    kaFinish();
    if (typeof ksOptimizePage === "function") ksOptimizePage(itemType, itemId, kw, vol);
}

function kaFinish() {
    const state = window._kaState;
    if (!state) return;
    state.modal.close();
    if (state.onDone) state.onDone();
    else if (typeof loadFamilies === "function") loadFamilies();
    window._kaState = null;
}

/* ── Step 2b: hand off to the Blog wizard ────────────────────────────────── */

async function ksActionWriteArticle(keywordId) {
    const state = window._kaState;
    if (!state) return;
    const body = state.modal.el.querySelector("#ka-body");
    body.innerHTML = kaHeader(state) + '<div class="spinner" style="margin:24px auto"></div>';

    // Seed the post with the family's uncovered informational/commercial
    // siblings so one article covers the cluster, not one keyword.
    // cluster_id 0 is the unclustered bucket, not a topic family — its members
    // share nothing, and seeding them would produce an incoherent article.
    let siblings = [];
    try {
        if (state.clusterId) {
            const fam = await apiRequest(`/api/keyword-strategy/families/${state.clusterId}?store_id=${ksStoreId}`);
            siblings = (fam.unowned || [])
                .filter(u => u.id !== keywordId && (u.intent === "informational" || u.intent === "commercial"))
                .slice(0, 9);
        }
    } catch (e) { /* seeding is a bonus, not a requirement */ }

    const keywords = [state.keyword, ...siblings.map(s => s.keyword)];
    // Seeds carry volume: the research prompt only prints "(vol N)" when it's
    // there, and without it the model can't rank the seeds by demand — so it
    // falls back on its own keyword research and the seed drifts.
    state.seedKeywords = [
        { keyword: state.keyword, volume: state.volume || 0 },
        ...siblings.map(s => ({ keyword: s.keyword, volume: s.search_volume || 0 })),
    ];
    const list = siblings.length
        ? `<ul style="font-size:12.5px;margin:6px 0 0 18px;color:var(--on-surface-secondary);">
             ${siblings.map(s => `<li>${kaEscape(s.keyword)} (${kaVolume(s.search_volume)})</li>`).join("")}</ul>`
        : `<div style="font-size:12.5px;color:var(--on-surface-secondary);">${
            state.clusterId
                ? "No other uncovered keywords in this family."
                : "This keyword isn't in a topic family yet — run AI Classify to group it, and the article can cover its whole cluster."}</div>`;

    body.innerHTML = kaHeader(state) + `
        <div style="font-size:13px;margin-bottom:6px;">
            The article will target <strong>“${kaEscape(state.keyword)}”</strong> as its primary keyword${
                siblings.length ? ", with these family siblings as secondary terms:" : "."}</div>
        ${list}
        <p style="font-size:12px;color:var(--on-surface-secondary);margin:14px 0;">
            Blog posts should link to the money pages in this family rather than compete with them —
            the wizard's research step handles that.</p>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
            <button class="btn btn-primary btn-sm" onclick='kaOpenWizard(${JSON.stringify(keywords)})'>
                <span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">edit_note</span>
                Open in Blog wizard</button>
            <button class="btn btn-outline btn-sm" id="ka-research-btn" onclick="kaQueueResearch()">
                Queue AI topic research</button>
            <button class="btn btn-outline btn-sm" onclick="kaRenderRoutes(window._kaState)">Back</button>
        </div>
        <div id="ka-research-status" style="margin-top:12px;"></div>`;
    state.seedKeywords = keywords;
}

function kaOpenWizard(keywords) {
    const state = window._kaState;
    const topic = state ? state.keyword : keywords[0];
    window.location.href = `/blog?tab=create&topic=${encodeURIComponent(topic)}`
        + `&keywords=${encodeURIComponent(keywords.join("\n"))}`;
}

/* The research endpoint already returns a run_id — the old flow threw it away
 * and left the user with a toast and no way to know what happened. */
async function kaQueueResearch() {
    const state = window._kaState;
    const btn = document.getElementById("ka-research-btn");
    const status = document.getElementById("ka-research-status");
    await guardButton(btn, async () => {
        status.innerHTML = '<div style="font-size:12.5px;color:var(--on-surface-secondary);">Starting topic research…</div>';
        let run;
        try {
            run = await apiRequest("/api/blog/topics/research", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    store_id: ksStoreId, count: 2,
                    seed: {
                        cluster_id: state.clusterId || null,
                        family: state.family,
                        intent: state.intent,
                        keywords: state.seedKeywords || [{ keyword: state.keyword, volume: state.volume || 0 }],
                    },
                }),
            });
        } catch (e) {
            status.innerHTML = `<div style="font-size:12.5px;color:var(--error);">Failed to start: ${kaEscape(e.message)}</div>`;
            return;
        }
        if ((run.excluded_seeds || []).length) {
            // Money-page keywords are filtered out before the model sees them;
            // say so up front rather than letting them vanish silently.
            status.innerHTML = `<div style="font-size:12px;color:var(--warning);margin-bottom:6px;">
                ${run.excluded_seeds.length} keyword(s) excluded — already owned by a product or
                collection page, so an article can't target them:
                ${run.excluded_seeds.map(e => `“${kaEscape(e.keyword)}”`).join(", ")}</div>`;
        }
        kaPollResearch(run.run_id, status);
    });
}

function kaPollResearch(runId, status) {
    status.innerHTML += '<div style="display:flex;align-items:center;gap:8px;font-size:12.5px;">'
        + '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Researching topics…</div>';
    const timer = setInterval(async () => {
        let run;
        try {
            run = await apiRequest(`/api/blog/topics/${runId}?store_id=${ksStoreId}`);
        } catch (e) { return; }
        if (run.status === "running" || run.status === "pending") return;
        clearInterval(timer);
        if (run.status === "completed") {
            status.innerHTML = kaResearchOutcome(run);
        } else {
            status.innerHTML = `<div style="font-size:12.5px;color:var(--error);">
                Research failed: ${kaEscape(run.error || "unknown error")}</div>`;
        }
    }, 3000);
}


/* ── Step 2c: create a collection ────────────────────────────────────────── */

async function ksActionCreateCollection(keywordId) {
    const state = window._kaState;
    if (!state) return;
    const body = state.modal.el.querySelector("#ka-body");

    // Cover the family's uncovered transactional/commercial siblings with this
    // one page — the cluster goes into the copy as secondary terms, not into
    // the ownership ledger.
    let cluster = [{ id: keywordId, keyword: state.keyword, search_volume: state.volume }];
    try {
        if (state.clusterId) {
            const fam = await apiRequest(`/api/keyword-strategy/families/${state.clusterId}?store_id=${ksStoreId}`);
            cluster = cluster.concat((fam.unowned || [])
                .filter(u => u.id !== keywordId && (u.required_types || []).includes("collection"))
                .slice(0, 9));
        }
    } catch (e) { /* the primary keyword alone is enough */ }
    state.cluster = cluster;

    body.innerHTML = kaHeader(state) + `
        <div style="font-size:13px;margin-bottom:8px;">One category page will target
            <strong>“${kaEscape(state.keyword)}”</strong>${cluster.length > 1
                ? `, covering ${cluster.length - 1} related keyword${cluster.length > 2 ? "s" : ""} as secondary terms`
                : ""}.</div>
        ${cluster.length > 1 ? `<div id="ka-cluster" style="max-height:150px;overflow-y:auto;margin-bottom:12px;">
            ${cluster.slice(1).map((k, i) => `<label style="display:flex;gap:8px;align-items:center;font-size:12.5px;padding:3px 0;">
                <input type="checkbox" data-ka-kw="${i + 1}" checked>
                <span style="flex:1;">${kaEscape(k.keyword)}</span>
                <span style="color:var(--on-surface-secondary);">${kaVolume(k.search_volume)}</span>
            </label>`).join("")}</div>` : ""}
        <p style="font-size:12px;color:var(--on-surface-secondary);margin-bottom:14px;">
            One page per keyword cluster, never one per keyword — separate pages for the same
            intent compete with each other.</p>
        <div style="display:flex;gap:8px;">
            <button class="btn btn-primary btn-sm" id="ka-propose-btn" onclick="kaProposeCollection()">
                <span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">auto_awesome</span>
                Draft the page</button>
            <button class="btn btn-outline btn-sm" onclick="kaRenderRoutes(window._kaState)">Back</button>
        </div>`;
}

async function kaProposeCollection() {
    const state = window._kaState;
    const btn = document.getElementById("ka-propose-btn");
    const checked = [...state.modal.el.querySelectorAll("[data-ka-kw]:checked")]
        .map(c => state.cluster[parseInt(c.dataset.kaKw, 10)]);
    const ids = [state.cluster[0].id, ...checked.map(k => k.id)];
    const body = state.modal.el.querySelector("#ka-body");

    await guardButton(btn, async () => {
        body.innerHTML = kaHeader(state) + `
            <div style="text-align:center;padding:34px 12px;">
                <div class="spinner" style="margin:0 auto 14px;"></div>
                <div style="font-size:13.5px;">Drafting a category page for “${kaEscape(state.keyword)}”…</div>
                <div style="font-size:12px;color:var(--on-surface-secondary);margin-top:6px;">
                    Choosing membership rules from your catalog, then writing the copy.</div>
            </div>`;
        try {
            const r = await apiRequest("/api/keyword-strategy/collections/propose", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ store_id: ksStoreId, keyword_ids: ids }),
            });
            state.proposal = r.proposal;
            kaRenderCollectionPreview(state, r.preview);
        } catch (e) {
            body.innerHTML = kaHeader(state) + `
                <div style="font-size:13px;color:var(--error);padding:14px;">Could not draft the page: ${kaEscape(e.message)}</div>
                <button class="btn btn-outline btn-sm" onclick="ksActionCreateCollection(${state.keywordId})">Try again</button>`;
        }
    });
}

function kaRenderCollectionPreview(state, preview) {
    state.preview = preview;
    const body = state.modal.el.querySelector("#ka-body");
    const thin = preview.match_count < preview.thin_threshold;

    const warnings = (preview.warnings || []).length
        ? `<div style="border:1px solid var(--warning);background:rgba(249,171,0,0.08);border-radius:6px;
                       padding:10px 12px;margin-bottom:14px;font-size:12.5px;">
             ${preview.warnings.map(w => `<div style="margin:2px 0;">⚠ ${kaEscape(w)}</div>`).join("")}
           </div>`
        : "";

    const sample = (preview.sample || []).map(p => `
        <div style="display:flex;align-items:center;gap:8px;font-size:12px;padding:3px 0;">
            ${p.image ? `<img src="${kaEscape(p.image)}" alt="" style="width:26px;height:26px;object-fit:cover;border-radius:4px;">`
                      : '<span style="width:26px;height:26px;border-radius:4px;background:var(--surface-variant);display:inline-block;"></span>'}
            <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${kaEscape(p.title)}</span>
        </div>`).join("");

    body.innerHTML = kaHeader(state) + `
        ${warnings}
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
          <div>
            <div class="ka-field-label">Title</div>
            <input id="ka-c-title" class="form-control" style="font-size:13px;" value="${escapeAttr(preview.title || "")}">
            <div class="ka-field-label">Handle <span id="ka-c-url" style="font-weight:400;color:var(--on-surface-secondary);"></span></div>
            <input id="ka-c-handle" class="form-control" style="font-size:13px;" value="${escapeAttr(preview.handle || "")}">
            <div class="ka-field-label">Meta title <span id="ka-c-mt-len" class="ka-counter"></span></div>
            <input id="ka-c-mt" class="form-control" style="font-size:13px;" value="${escapeAttr(preview.meta_title || "")}">
            <div class="ka-field-label">Meta description <span id="ka-c-md-len" class="ka-counter"></span></div>
            <textarea id="ka-c-md" class="form-control" rows="3" style="font-size:13px;">${kaEscape(preview.meta_description || "")}</textarea>
          </div>
          <div>
            <div class="ka-field-label">Membership rules
                <span style="font-weight:400;color:var(--on-surface-secondary);">
                    (${preview.applied_disjunctively ? "match ANY" : "match ALL"})</span></div>
            <div id="ka-rules">${(preview.rules || []).map((r, i) => kaRuleRow(r, i)).join("")}</div>
            <button class="btn btn-sm btn-outline" style="font-size:11px;margin-top:6px;" onclick="kaAddRule()">+ Add rule</button>
            <div class="ka-field-label" style="margin-top:14px;">
                Products matched
                <span style="font-weight:400;color:${thin ? "var(--warning)" : "var(--success)"};">
                    ${preview.match_count} active${preview.count_is_approximate ? " (approx.)" : ""}</span>
            </div>
            <div style="max-height:170px;overflow-y:auto;">${sample || '<div style="font-size:12px;color:var(--on-surface-secondary);">Nothing matches yet.</div>'}</div>
            <div style="font-size:11px;color:var(--on-surface-secondary);margin-top:6px;">
                Shopify also counts draft products, so the live total can be higher.</div>
          </div>
        </div>

        <div class="ka-field-label" style="margin-top:14px;">Description</div>
        <div id="ka-c-desc" contenteditable="true" class="form-control"
             style="font-size:13px;max-height:180px;overflow-y:auto;line-height:1.5;">${preview.description_html || ""}</div>

        <div style="display:flex;gap:8px;margin-top:16px;align-items:center;flex-wrap:wrap;">
            <button class="btn btn-primary btn-sm" id="ka-create-btn" onclick="kaCreateCollection()"
                ${preview.blocking ? "disabled title='Fix the warnings above first'" : ""}>
                <span class="material-symbols-rounded" style="font-size:16px;vertical-align:middle;">publish</span>
                Create &amp; publish</button>
            <button class="btn btn-outline btn-sm" id="ka-recheck-btn" onclick="kaRecheckCollection()">Re-check</button>
            <label style="display:flex;gap:6px;align-items:center;font-size:12.5px;">
                <input type="checkbox" id="ka-c-image"> Generate a collection image</label>
            <span style="flex:1;"></span>
            <button class="btn btn-outline btn-sm" onclick="ksActionCreateCollection(${state.keywordId})">Back</button>
        </div>`;

    kaBindCollectionInputs(state);
}

function kaRuleRow(rule, i) {
    const cols = ["TAG", "VENDOR", "TYPE", "TITLE"];
    // TAG only supports equality — Shopify accepts the others at schema level
    // and then rejects them at runtime, so the UI can't offer them.
    const rels = rule.column === "TAG"
        ? ["EQUALS", "NOT_EQUALS"]
        : ["EQUALS", "NOT_EQUALS", "CONTAINS", "NOT_CONTAINS", "STARTS_WITH", "ENDS_WITH"];
    return `<div class="ka-rule-row" data-ka-rule="${i}">
        <select class="form-control ka-rule-col">${cols.map(c =>
            `<option value="${c}" ${c === rule.column ? "selected" : ""}>${c}</option>`).join("")}</select>
        <select class="form-control ka-rule-rel">${rels.map(r =>
            `<option value="${r}" ${r === rule.relation ? "selected" : ""}>${r.replace(/_/g, " ").toLowerCase()}</option>`).join("")}</select>
        <input class="form-control ka-rule-cond" value="${escapeAttr(rule.condition || "")}">
        <button class="icon-btn" onclick="this.parentElement.remove();kaRecheckCollection()" title="Remove">
            <span class="material-symbols-rounded" style="font-size:16px;">close</span></button>
    </div>`;
}

function kaAddRule() {
    const box = document.getElementById("ka-rules");
    box.insertAdjacentHTML("beforeend", kaRuleRow({ column: "TAG", relation: "EQUALS", condition: "" },
                                                  box.children.length));
}

function kaBindCollectionInputs(state) {
    const meta = [["ka-c-mt", "ka-c-mt-len", 60], ["ka-c-md", "ka-c-md-len", 160]];
    meta.forEach(([id, lenId, max]) => {
        const el = document.getElementById(id);
        const out = document.getElementById(lenId);
        const paint = () => {
            const n = el.value.length;
            out.textContent = `${n}/${max}`;
            out.style.color = n > max ? "var(--error)" : (n < max * 0.6 ? "var(--warning)" : "var(--success)");
        };
        el.addEventListener("input", paint);
        paint();
    });
    const handle = document.getElementById("ka-c-handle");
    const url = document.getElementById("ka-c-url");
    const paintUrl = () => { url.textContent = `/collections/${handle.value}`; };
    handle.addEventListener("input", paintUrl);
    paintUrl();
    // Rule edits change what the page would contain — re-check rather than
    // letting the shown product list go stale.
    document.getElementById("ka-rules").addEventListener("change", () => kaRecheckCollection());
}

function kaCollectFromForm(state) {
    const rules = [...state.modal.el.querySelectorAll(".ka-rule-row")].map(row => ({
        column: row.querySelector(".ka-rule-col").value,
        relation: row.querySelector(".ka-rule-rel").value,
        condition: row.querySelector(".ka-rule-cond").value.trim(),
    })).filter(r => r.condition);
    return Object.assign({}, state.proposal, {
        title: document.getElementById("ka-c-title").value.trim(),
        handle: document.getElementById("ka-c-handle").value.trim(),
        meta_title: document.getElementById("ka-c-mt").value.trim(),
        meta_description: document.getElementById("ka-c-md").value.trim(),
        description_html: document.getElementById("ka-c-desc").innerHTML,
        rules,
    });
}

async function kaRecheckCollection() {
    const state = window._kaState;
    if (!state || !state.proposal) return;
    const btn = document.getElementById("ka-recheck-btn");
    const proposal = kaCollectFromForm(state);
    state.proposal = proposal;
    await guardButton(btn, async () => {
        try {
            const preview = await apiRequest("/api/keyword-strategy/collections/preview", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ store_id: ksStoreId, proposal }),
            });
            kaRenderCollectionPreview(state, preview);
        } catch (e) {
            showToast("Re-check failed: " + e.message, "error");
        }
    });
}

function kaCreateCollection() {
    const state = window._kaState;
    const proposal = kaCollectFromForm(state);
    const generateImage = document.getElementById("ka-c-image").checked;
    const count = state.preview ? state.preview.match_count : 0;

    document.getElementById("confirm-dialog-title").textContent = "Create collection";
    document.getElementById("confirm-dialog-message").innerHTML =
        `Create <strong>${kaEscape(proposal.title)}</strong> on your live store at
         <code>/collections/${kaEscape(proposal.handle)}</code>, publish it to the Online Store,
         and make “${kaEscape(state.keyword)}” its primary keyword?
         <br><br><span style="color:var(--on-surface-secondary);font-size:12.5px;">
         About ${count} active product${count === 1 ? "" : "s"} match the rules. Shopify has no
         supported way to remove products from a rule-based collection later — you would edit the
         rules or delete the collection.</span>`;

    const okBtn = document.getElementById("confirm-dialog-ok");
    const fresh = okBtn.cloneNode(true);
    okBtn.parentNode.replaceChild(fresh, okBtn);
    fresh.textContent = "Create & publish";
    fresh.addEventListener("click", async () => {
        document.getElementById("confirm-dialog-overlay").classList.remove("active");
        await kaApplyCreateCollection(state, proposal, generateImage);
    });
    document.getElementById("confirm-dialog-overlay").classList.add("active");
}

async function kaApplyCreateCollection(state, proposal, generateImage) {
    const body = state.modal.el.querySelector("#ka-body");
    body.innerHTML = kaHeader(state) + `
        <div style="text-align:center;padding:34px 12px;">
            <div class="spinner" style="margin:0 auto 14px;"></div>
            <div style="font-size:13.5px;">Creating “${kaEscape(proposal.title)}” on Shopify…</div>
            <div style="font-size:12px;color:var(--on-surface-secondary);margin-top:6px;">
                Publishing, assigning the keyword, then scoring the new page.</div>
        </div>`;
    try {
        const r = await apiRequest("/api/keyword-strategy/collections/create", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ store_id: ksStoreId, proposal, generate_image: generateImage }),
        });
        state.owner = { item_type: "collection", item_id: r.local_id, title: proposal.title,
                        role: "resolved_primary" };
        ksFamilyCache = {};
        body.innerHTML = kaHeader(state) + `
            <div style="padding:8px 0 16px;">
                <div style="display:flex;align-items:center;gap:8px;font-size:14px;margin-bottom:12px;">
                    <span class="material-symbols-rounded" style="color:var(--success);">check_circle</span>
                    <span><strong>${kaEscape(proposal.title)}</strong> is live.</span>
                </div>
                <ul style="font-size:12.5px;color:var(--on-surface-secondary);margin:0 0 14px 18px;">
                    <li><a href="${kaEscape(r.url)}" target="_blank" rel="noopener"
                           style="text-decoration:underline;">${kaEscape(r.url)}</a></li>
                    <li>${r.products_count != null ? r.products_count : "—"} products, kept current by the rules.</li>
                    <li>Primary keyword: ${r.assigned_keyword
                        ? `“${kaEscape(r.assigned_keyword)}”`
                        : "not assigned — set it from the page's SEO tab"}.</li>
                    <li>SEO score: <strong>${r.seo_score != null ? r.seo_score : "—"}</strong>.</li>
                </ul>
                <div style="display:flex;gap:8px;flex-wrap:wrap;">
                    <button class="btn btn-primary btn-sm" onclick="kaCloseAndOptimize('collection', ${r.local_id})">Optimize now</button>
                    <button class="btn btn-outline btn-sm" onclick="kaFinish()">Done</button>
                </div>
            </div>`;
    } catch (e) {
        body.innerHTML = kaHeader(state) + `
            <div style="font-size:13px;color:var(--error);padding:14px;">Creation failed: ${kaEscape(e.message)}</div>
            <p style="font-size:12px;color:var(--on-surface-secondary);padding:0 14px;">
                Check your store — if the collection was created before the failure, it will be there.</p>
            <button class="btn btn-outline btn-sm" onclick="kaRecheckCollection()">Back to the draft</button>`;
    }
}


/* Research does not simply take the seeded keywords: the topic_discovery
 * prompt makes the model reject candidates that cannibalize a money page,
 * duplicate an existing article, or face an unwinnable SERP — and then
 * substitute its own. That reasoning is recorded on the run but was only ever
 * visible, collapsed, in another section, which made the substitution look
 * arbitrary. Report it where the research was started. */
function kaResearchOutcome(run) {
    const seeded = (run.seed_keywords || []).map(k => String(k).toLowerCase());
    const suggestions = run.suggestions || [];
    const rejected = run.rejected_topics || [];

    const MARK = {
        exact: { icon: "✓", color: "var(--success)", note: () => "" },
        covers: { icon: "✓", color: "var(--success)", note: sd => ` (covers “${kaEscape(sd)}”)` },
        variant: { icon: "≈", color: "var(--warning)", note: sd => ` (close variant of “${kaEscape(sd)}”)` },
        none: { icon: "→", color: "var(--warning)", note: () => " (not from your list)" },
    };
    const rows = suggestions.map(sg => {
        let kws = sg.keywords;
        if (typeof kws === "string") { try { kws = JSON.parse(kws); } catch (e) { kws = {}; } }
        const primary = (kws || {}).primary || "";
        const m = kaSeedMatch(primary, seeded);
        const mark = MARK[m.kind];
        return `<div style="display:flex;gap:8px;align-items:baseline;padding:3px 0;font-size:12.5px;">
            <span style="color:${mark.color};">${mark.icon}</span>
            <span style="flex:1;min-width:0;">
                <strong>${kaEscape(primary)}</strong>
                <span style="color:var(--on-surface-secondary);">${mark.note(m.seed)} — ${kaEscape(sg.topic || "")}</span>
            </span></div>`;
    }).join("");

    const ruledOut = rejected.length
        ? `<details style="margin-top:8px;" open>
             <summary style="cursor:pointer;font-size:12.5px;color:var(--on-surface-secondary);">
               Ruled out (${rejected.length}) — why your keywords weren't used</summary>
             <ul style="font-size:12px;color:var(--on-surface-secondary);margin:6px 0 0 16px;">
               ${rejected.map(r => `<li style="margin:4px 0;"><strong>${kaEscape(r.topic || "")}</strong> — ${kaEscape(r.reason || "")}</li>`).join("")}
             </ul></details>`
        : "";

    return `<div style="border-top:1px solid var(--outline);margin-top:10px;padding-top:10px;">
        <div style="font-size:12.5px;font-weight:500;margin-bottom:4px;">
            ${suggestions.length} suggestion${suggestions.length === 1 ? "" : "s"} ready</div>
        ${rows}
        ${ruledOut}
        <div style="margin-top:10px;">
            <a href="/blog?tab=topics" class="btn btn-sm btn-outline" style="font-size:11px;text-decoration:none;">
                Open Research Topics</a></div>
    </div>`;
}


/* How closely a returned primary tracks a seeded keyword. The prompt permits
 * "a close variant", so exact equality is the wrong test — but plain substring
 * matching is worse: a one-word seed like "dutch" marks every Dutch topic as a
 * hit and hides exactly the drift this is meant to expose. Tokens, plural-
 * folded, with single-word seeds barred from the containment case. */
function kaSeedMatch(primary, seeded) {
    const norm = v => String(v || "").toLowerCase().replace(/[^a-z0-9 ]/g, " ").replace(/\s+/g, " ").trim();
    const fold = t => (t.length > 3 && t.endsWith("s") ? t.slice(0, -1) : t);
    const toks = v => norm(v).split(" ").filter(Boolean).map(fold);

    const p = norm(primary);
    const pt = new Set(toks(primary));
    if (!p) return { kind: "none" };

    for (const sd of seeded) if (norm(sd) === p) return { kind: "exact", seed: sd };
    for (const sd of seeded) {
        const st = toks(sd);
        if (st.length >= 2 && st.every(t => pt.has(t))) return { kind: "covers", seed: sd };
    }
    // A variant must share real substance: two tokens, or the whole of a
    // one-word seed. A single shared token off a multi-word seed is usually a
    // generic like "flavors", which would call an unrelated keyword a variant.
    let best = null, bestShared = 0;
    for (const sd of seeded) {
        const st = toks(sd);
        if (!st.length) continue;
        const shared = st.filter(t => pt.has(t)).length;
        const qualifies = shared >= 2 || (st.length === 1 && shared === 1);
        if (qualifies && shared > bestShared) { bestShared = shared; best = sd; }
    }
    return best ? { kind: "variant", seed: best } : { kind: "none" };
}
