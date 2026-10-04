/* Automatic links card (Internal links page). Each plan places links
 * deterministically — wrapping words the page already uses — and applies them
 * through the apply pipeline, so every one shows in the page's History with
 * an Undo. Depends on app.js (apiRequest, pollTaskUntilDone) and ui.js. */
(() => {
    const PLANS = [
        {
            kind: "article_collection",
            title: "Link articles to collections",
            what: (p) => `${p.pages} articles name a collection without linking to it. This links the first mention in each — up to three new links per article — ${p.links} links in all.`,
            sample: (row) => `${row.title} → ${row.collections.map((c) => c.title).join(", ")}`,
            confirm: (p) => `${p.links} links will be added across ${p.pages} live articles. Only existing words become links; no text is rewritten. Each one can be undone from the article's History.`,
        },
        {
            kind: "collection_back",
            title: "Link collections back to their guides",
            what: (p) => `${p.pages} collections are linked from a guide but do not link back. This adds a "Related" line to each collection description pointing to its most-read guide.`,
            sample: (row) => `${row.title} → ${row.article_title}`,
            confirm: (p) => `A "Related" line with one link will be added to ${p.pages} live collection descriptions. Each one can be undone from the collection's History.`,
        },
    ];

    function storeId() {
        return typeof StoreSelector !== "undefined" ? StoreSelector.getStoreId() : null;
    }

    let waits = 0;

    async function load() {
        const box = document.getElementById("il-auto-links");
        if (!box) return;
        const sid = storeId();
        if (!sid) {
            // The global store selector resolves a moment after page load.
            if (waits++ < 8) setTimeout(load, 700);
            return;
        }
        box.innerHTML = '<div class="card"><div class="loading-spinner"><div class="spinner"></div></div></div>';
        try {
            const preview = await apiRequest(`/api/auto-links/preview?store_id=${sid}`);
            box.innerHTML = `<div class="card">
                <h3 style="margin:0 0 4px;">Automatic links</h3>
                <p style="font-size:13px;color:var(--on-surface-secondary);margin:0 0 12px;">Collections and products rank when your own pages link to them. These add the missing links for you.</p>
                ${PLANS.map((plan) => {
                    const p = preview[plan.kind];
                    if (!p.pages) {
                        return `<div style="padding:10px 0;border-top:1px solid var(--outline);"><strong>${plan.title}</strong>
                            <span style="color:var(--success);font-size:13px;"> — nothing missing.</span></div>`;
                    }
                    return `<div style="padding:10px 0;border-top:1px solid var(--outline);">
                        <div style="display:flex;gap:12px;align-items:flex-start;">
                            <div style="flex:1;"><strong>${plan.title}</strong>
                                <div style="font-size:13px;margin-top:2px;">${escapeHtml(plan.what(p))}</div>
                                <details style="margin-top:4px;"><summary style="cursor:pointer;font-size:12px;color:var(--on-surface-secondary);">Examples</summary>
                                    <ul style="font-size:12px;margin:4px 0 0 18px;">${p.sample.map((r) => `<li>${escapeHtml(plan.sample(r))}</li>`).join("")}</ul></details>
                            </div>
                            <button class="btn btn-primary btn-sm" data-auto-link="${plan.kind}" style="white-space:nowrap;">Add ${p.links} link${p.links === 1 ? "" : "s"}</button>
                        </div></div>`;
                }).join("")}
            </div>`;
            box.querySelectorAll("[data-auto-link]").forEach((btn) => btn.addEventListener("click", () => {
                const plan = PLANS.find((x) => x.kind === btn.dataset.autoLink);
                run(plan, preview[plan.kind], btn);
            }));
        } catch (e) {
            box.innerHTML = `<div class="card"><p style="color:var(--error);">${escapeHtml(e.message)}</p></div>`;
        }
    }

    async function run(plan, preview, btn) {
        const ok = await UIConfirm.ask({ title: `${plan.title}?`, message: plan.confirm(preview), confirmLabel: "Add links" });
        if (!ok) return;
        btn.disabled = true;
        try {
            const { task_id } = await apiRequest("/api/auto-links/run", { method: "POST", body: { store_id: storeId(), kind: plan.kind } });
            const task = await pollTaskUntilDone(`/api/tasks/${task_id}`, {
                intervalMs: 3000, timeoutMs: 60 * 60 * 1000,
                onProgress: (t) => {
                    const p = t.result && t.result.progress;
                    if (p) btn.textContent = `Adding… ${p.completed}/${p.total}`;
                },
            });
            const r = task.result || {};
            showToast(`${r.added || 0} links added${r.failed ? `, ${r.failed} failed` : ""}${r.already ? `, ${r.already} already there` : ""}.`, r.failed ? "warning" : "success");
        } catch (e) {
            showToast(`Adding links failed: ${e.message}`, "error");
        }
        load();
    }

    document.addEventListener("DOMContentLoaded", () => {
        setTimeout(load, 900);
        document.addEventListener("store:changed", load);
    });
})();
