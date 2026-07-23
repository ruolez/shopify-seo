async function apiRequest(url, options = {}) {
    const defaults = {
        headers: { "Content-Type": "application/json" },
    };
    const config = { ...defaults, ...options };
    if (options.body && typeof options.body === "object") {
        config.body = JSON.stringify(options.body);
    }

    const response = await fetch(url, config);

    if (response.status === 401) {
        window.location.href = "/login";
        return null;
    }

    const data = await response.json();
    if (!response.ok) {
        throw new Error(data.error || `Request failed (${response.status})`);
    }
    return data;
}

const TERMINAL_TASK_STATUSES = ["completed", "failed", "completed_with_errors"];

/**
 * Poll a task endpoint until it reaches a terminal status.
 * Tolerates transient fetch errors (maxErrors consecutive) instead of dying on
 * the first blip, treats status "failed" as terminal, and enforces a max duration.
 *
 * Returns the final task payload. Throws on task failure, timeout, or too many
 * consecutive poll errors — always with a human-readable message.
 */
function pollTaskUntilDone(url, { intervalMs = 2000, maxErrors = 5, timeoutMs = 15 * 60 * 1000, onProgress = null } = {}) {
    return new Promise((resolve, reject) => {
        const startedAt = Date.now();
        let consecutiveErrors = 0;

        const tick = async () => {
            if (Date.now() - startedAt > timeoutMs) {
                reject(new Error("Task timed out — it may still be running in the background."));
                return;
            }
            let task;
            try {
                task = await apiRequest(url);
                consecutiveErrors = 0;
            } catch (e) {
                consecutiveErrors += 1;
                if (consecutiveErrors >= maxErrors) {
                    reject(new Error(`Lost contact with the task (${e.message}). It may still be running — reload to check.`));
                    return;
                }
                setTimeout(tick, intervalMs);
                return;
            }
            if (onProgress) {
                try { onProgress(task); } catch (_) { /* progress rendering must not kill the poll */ }
            }
            const status = task && task.status;
            if (status === "failed") {
                reject(new Error(task.error || "Task failed"));
            } else if (TERMINAL_TASK_STATUSES.includes(status)) {
                resolve(task);
            } else {
                setTimeout(tick, intervalMs);
            }
        };
        tick();
    });
}

/**
 * Disable a button (with .btn-busy class) while an async action runs.
 * Prevents double-submits of paid AI / Shopify-mutating actions.
 */
async function guardButton(btn, fn) {
    if (!btn) return fn();
    if (btn.disabled) return;
    btn.disabled = true;
    btn.classList.add("btn-busy");
    try {
        return await fn();
    } finally {
        btn.disabled = false;
        btn.classList.remove("btn-busy");
    }
}

function showToast(message, type = "info") {
    const existing = document.querySelector(".toast");
    if (existing) existing.remove();

    const toast = document.createElement("div");
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);

    requestAnimationFrame(() => toast.classList.add("show"));
    setTimeout(() => {
        toast.classList.remove("show");
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

// Quote-safe in BOTH text and attribute contexts — always safe to interpolate
// into innerHTML markup, including inside value="..."/title="..." attributes.
// NEVER safe inside inline event-handler JS strings (onclick="fn('...')") —
// the HTML parser entity-decodes before the JS engine parses; use data-*
// attributes or id/index lookups instead.
function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function escapeAttr(str) {
    return escapeHtml(str);
}

// Mirror Shopify handle normalization: lowercase, non-alphanumeric runs -> "-", trim dashes.
function slugify(str) {
    return String(str || "")
        .toLowerCase()
        .trim()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "");
}

function showLoading(container) {
    if (typeof container === "string") {
        container = document.querySelector(container);
    }
    if (!container) return;
    container.innerHTML =
        '<div class="loading-spinner"><div class="spinner"></div><p>Loading...</p></div>';
}

function parseServerDate(isoStr) {
    // Server timestamps are UTC but serialized WITHOUT a timezone marker
    // ("2026-07-23T18:08:05.383109"). new Date() would parse that as LOCAL
    // time and display hours off — mark it UTC unless an offset is present.
    if (typeof isoStr === "string" && isoStr.includes("T") && !/([zZ]|[+-]\d{2}:?\d{2})$/.test(isoStr)) {
        return new Date(isoStr + "Z");
    }
    return new Date(isoStr);
}

function formatDate(isoStr) {
    if (!isoStr) return "—";
    const d = parseServerDate(isoStr);
    return d.toLocaleDateString("en-US", {
        month: "short",
        day: "numeric",
        year: "numeric",
        hour: "numeric",
        minute: "2-digit",
    });
}

function getScoreBadge(score) {
    let cls = "badge-error";
    if (score >= 80) cls = "badge-success";
    else if (score >= 50) cls = "badge-warning";
    return `<span class="badge ${cls}">${score}</span>`;
}

function formatRelativeTime(isoStr) {
    if (!isoStr) return "—";
    const d = parseServerDate(isoStr);
    const now = new Date();
    const diffMs = now - d;
    const diffMin = Math.floor(diffMs / 60000);
    const diffHr = Math.floor(diffMs / 3600000);
    const diffDay = Math.floor(diffMs / 86400000);
    if (diffMin < 1) return "Just now";
    if (diffMin < 60) return `${diffMin}m ago`;
    if (diffHr < 24) return `${diffHr}h ago`;
    if (diffDay < 7) return `${diffDay}d ago`;
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function renderScoreRing(score, size = 44) {
    const cls = score >= 80 ? "high" : score >= 50 ? "medium" : "low";
    const r = (size - 8) / 2;
    const circ = 2 * Math.PI * r;
    const offset = circ - (score / 100) * circ;
    const isLg = size > 60;
    return `<div class="${isLg ? "score-ring-lg" : "score-ring"}" style="${isLg ? "" : "width:" + size + "px;height:" + size + "px;"}">
        <svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
            <circle class="score-ring-bg" cx="${size/2}" cy="${size/2}" r="${r}"/>
            <circle class="score-ring-fill ${cls}" cx="${size/2}" cy="${size/2}" r="${r}"
                stroke-dasharray="${circ}" stroke-dashoffset="${offset}"/>
        </svg>
        <span class="score-ring-value">${score}</span>
    </div>`;
}

function setupModalDismiss(overlayId, closeFn) {
    const overlay = document.getElementById(overlayId);
    if (!overlay) return;
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) closeFn();
    });
}

function initThemeToggle() {
    const saved = localStorage.getItem("theme");
    if (saved === "dark") {
        document.documentElement.setAttribute("data-theme", "dark");
    }
    const btn = document.getElementById("theme-toggle");
    if (btn) {
        btn.addEventListener("click", () => {
            const current =
                document.documentElement.getAttribute("data-theme");
            const next = current === "dark" ? "light" : "dark";
            document.documentElement.setAttribute("data-theme", next);
            localStorage.setItem("theme", next);
            btn.querySelector(".material-symbols-rounded").textContent = next === "dark" ? "light_mode" : "dark_mode";
        });
        const theme =
            document.documentElement.getAttribute("data-theme") || "light";
        btn.querySelector(".material-symbols-rounded").textContent = theme === "dark" ? "light_mode" : "dark_mode";
    }
}

function initSidebar() {
    const toggle = document.getElementById("sidebar-toggle");
    const sidebar = document.querySelector(".sidebar");
    if (toggle && sidebar) {
        toggle.addEventListener("click", () => {
            sidebar.classList.toggle("collapsed");
        });
    }
}

function saveFilter(key, value) {
    try { localStorage.setItem("seoFilter_" + key, JSON.stringify(value)); } catch (e) {}
}

function loadFilter(key, fallback) {
    try {
        const v = localStorage.getItem("seoFilter_" + key);
        return v !== null ? JSON.parse(v) : fallback;
    } catch (e) { return fallback; }
}

document.addEventListener("DOMContentLoaded", () => {
    initThemeToggle();
    initSidebar();
    initTasksPanel();
});

// ── Global tasks panel ──────────────────────────────────────────────
// Right-side drawer listing running + recent background tasks across the
// whole app. Reads the unified /api/tasks/active feed; purely additive —
// per-page progress overlays keep working independently.
const TasksPanel = (() => {
    const BADGE_INTERVAL = 15000;
    const LIST_INTERVAL = 3000;
    let panelEl = null;
    let badgeEl = null;
    let badgeTimer = null;
    let listTimer = null;
    let lastCount = 0;
    let open = false;
    let dismissed = new Set();
    let clearedBefore = "";
    let mode = "running"; // running (24h) | history (7d archive)
    let filterGroup = "all";

    // Domain filter chips — predicate on the task's origin page/source.
    const GROUPS = {
        all: { label: "All", test: () => true },
        ai: { label: "AI Search", test: (t) => t.page === "/ai-seo" },
        seo: { label: "SEO", test: (t) => ["/seo", "/technical-seo", "/interlinking", "/images", "/duplicates"].includes(t.page) },
        content: { label: "Content", test: (t) => ["/blog", "/news", "/landing-pages", "/home-page", "/brands"].includes(t.page) },
        insights: { label: "Insights", test: (t) => ["/gsc", "/semrush", "/keyword-strategy", "/content-gaps", "/competitors"].includes(t.page) },
        sync: { label: "Sync", test: (t) => t.source === "sync" },
    };
    const PAGE_TO_GROUP = { "/ai-seo": "ai", "/seo": "seo", "/blog": "content", "/news": "content", "/gsc": "insights", "/stores": "sync" };

    function loadDismissState() {
        try {
            dismissed = new Set(JSON.parse(localStorage.getItem("tasksDismissed") || "[]"));
            clearedBefore = localStorage.getItem("tasksClearedBefore") || "";
        } catch (_) { dismissed = new Set(); clearedBefore = ""; }
    }

    function saveDismissState() {
        try {
            // cap the ledger so it can't grow unbounded
            localStorage.setItem("tasksDismissed", JSON.stringify([...dismissed].slice(-200)));
            localStorage.setItem("tasksClearedBefore", clearedBefore);
        } catch (_) {}
    }

    function taskKey(t) {
        return `${t.source}:${t.id}`;
    }

    function isDismissed(t) {
        if (dismissed.has(taskKey(t))) return true;
        const finishedAt = t.completed_at || t.started_at;
        return Boolean(clearedBefore && finishedAt && finishedAt <= clearedBefore);
    }

    function statusBadge(status) {
        const map = {
            running: ["badge-info", "Running"],
            pending: ["", "Pending"],
            completed: ["badge-success", "Done"],
            failed: ["badge-error", "Failed"],
            completed_with_errors: ["badge-warning", "Done w/ errors"],
        };
        const [cls, text] = map[status] || ["", status];
        return `<span class="badge ${cls}">${escapeHtml(text)}</span>`;
    }

    function taskRow(t) {
        const label = t.page
            ? `<a href="${escapeAttr(t.page)}">${escapeHtml(t.label)}</a>`
            : escapeHtml(t.label);
        const active = t.status === "running" || t.status === "pending";
        let html = `<div class="task-row" data-task-key="${escapeAttr(taskKey(t))}">`;
        const dismissBtn = active ? "" :
            `<button type="button" class="task-row-dismiss" title="Dismiss" data-dismiss="${escapeAttr(taskKey(t))}"><span class="material-symbols-rounded">close</span></button>`;
        html += `<div class="task-row-top"><span>${label}</span>${statusBadge(t.status)}${dismissBtn}</div>`;

        const detail = t.detail || t.current_item;
        if (detail) {
            html += `<div class="task-row-detail" title="${escapeAttr(detail)}">${escapeHtml(detail)}</div>`;
        }

        const isActive = t.status === "running" || t.status === "pending";
        if (isActive && t.progress && t.progress.total > 0) {
            const done = (t.progress.completed || 0) + (t.progress.failed || 0);
            const pct = Math.min(100, Math.round((done / t.progress.total) * 100));
            html += `<div class="progress-bar-container"><div class="progress-bar" style="width:${pct}%"></div></div>`;
            html += `<div class="progress-stats">${done} / ${t.progress.total}` +
                (t.progress.failed ? ` (${t.progress.failed} failed)` : "") + `</div>`;
        } else if (!isActive && t.progress && t.progress.total > 0) {
            html += `<div class="progress-stats">${t.progress.completed || 0} / ${t.progress.total}` +
                (t.progress.failed ? ` (${t.progress.failed} failed)` : "") + `</div>`;
        }

        if (t.status === "failed" && t.error) {
            html += `<div class="task-row-error">${escapeHtml(String(t.error).slice(0, 200))}</div>`;
        }

        // Failed AI SEO batches are retryable right here — parity with the old
        // AI SEO Activity tab's "Retry failed" button.
        const retryable = t.source === "bulk" && ["geo_optimize", "fact_boost"].includes(t.kind) &&
            !active && (t.status === "failed" || (t.progress && t.progress.failed > 0));
        if (retryable) {
            html += `<button type="button" class="btn btn-secondary btn-sm task-row-retry" data-retry="${t.id}" style="margin-top:6px;">
                <span class="material-symbols-rounded">refresh</span> Retry failed</button>`;
        }

        const ts = t.completed_at || t.started_at;
        const abs = formatDate(ts);
        const rel = formatRelativeTime(ts);
        html += `<div class="task-row-time">${escapeHtml(abs)}${rel && rel !== "\u2014" ? ` \u00b7 ${escapeHtml(rel)}` : ""}</div>`;
        html += `</div>`;
        return html;
    }

    function render(tasks) {
        if (!panelEl) return;
        const body = panelEl.querySelector(".tasks-panel-body");
        const test = (GROUPS[filterGroup] || GROUPS.all).test;
        tasks = tasks.filter(test);
        const active = tasks.filter(t => t.status === "running" || t.status === "pending");
        // History is the archive — the local dismiss ledger doesn't hide rows there.
        const recent = tasks.filter(t => !(t.status === "running" || t.status === "pending") &&
            (mode === "history" || !isDismissed(t)));

        let html = "";
        if (active.length === 0 && recent.length === 0) {
            html = `<div class="tasks-empty">${mode === "history" ? "No tasks in the last 7 days" : "No active tasks"}</div>`;
        } else {
            if (active.length) {
                html += `<div class="tasks-group-label">Running</div>`;
                html += active.map(taskRow).join("");
            }
            if (recent.length) {
                html += `<div class="tasks-group-label tasks-group-label-row">${mode === "history" ? "History (7d)" : "Recent (24h)"}` +
                    (mode === "history" ? "" : `<button type="button" class="tasks-clear-btn" id="tasksClearAll">Clear</button>`) + `</div>`;
                html += recent.map(taskRow).join("");
            }
        }
        body.innerHTML = html;

        const clearBtn = body.querySelector("#tasksClearAll");
        if (clearBtn) {
            clearBtn.addEventListener("click", () => {
                clearedBefore = new Date().toISOString();
                dismissed = new Set(); // cutoff covers everything dismissed so far
                saveDismissState();
                pollList();
            });
        }
        body.querySelectorAll(".task-row-dismiss").forEach(btn => {
            btn.addEventListener("click", () => {
                dismissed.add(btn.dataset.dismiss);
                saveDismissState();
                const row = btn.closest(".task-row");
                if (row) row.remove();
                if (!body.querySelector(".task-row")) pollList();
            });
        });
        body.querySelectorAll(".task-row-retry").forEach(btn => {
            btn.addEventListener("click", async () => {
                btn.disabled = true;
                try {
                    await apiRequest(`/api/ai-seo/batches/${btn.dataset.retry}/retry`, { method: "POST" });
                    showToast("Retrying failed items", "success");
                    pollList();
                } catch (err) {
                    showToast(`Retry failed: ${err.message}`, "error");
                    btn.disabled = false;
                }
            });
        });
    }

    function updateBadge(n) {
        if (!badgeEl) return;
        badgeEl.textContent = String(n);
        badgeEl.hidden = n === 0;
        if (n > lastCount) {
            badgeEl.classList.remove("pulse");
            void badgeEl.offsetWidth; // restart animation
            badgeEl.classList.add("pulse");
        }
        lastCount = n;
    }

    async function pollBadge() {
        try {
            const d = await apiRequest("/api/tasks/active?count_only=1");
            if (d) updateBadge(d.active_count || 0);
        } catch (_) { /* polling errors are silent */ }
    }

    async function pollList() {
        try {
            const url = mode === "history" ? "/api/tasks/active?hours=168" : "/api/tasks/active";
            const d = await apiRequest(url);
            if (d) {
                render(d.tasks || []);
                updateBadge(d.active_count || 0);
            }
        } catch (_) { /* polling errors are silent */ }
    }

    function stopTimers() {
        if (badgeTimer) { clearInterval(badgeTimer); badgeTimer = null; }
        if (listTimer) { clearInterval(listTimer); listTimer = null; }
    }

    function startTimers() {
        stopTimers();
        if (document.visibilityState === "hidden") return;
        if (open) {
            pollList();
            listTimer = setInterval(pollList, LIST_INTERVAL);
        } else {
            pollBadge();
            badgeTimer = setInterval(pollBadge, BADGE_INTERVAL);
        }
    }

    function setOpen(next) {
        open = next;
        if (panelEl) panelEl.classList.toggle("open", open);
        try { localStorage.setItem("tasksPanelOpen", open ? "1" : "0"); } catch (_) {}
        startTimers();
    }

    function buildPanel() {
        panelEl = document.createElement("div");
        panelEl.className = "tasks-panel";
        const chips = Object.entries(GROUPS)
            .map(([k, g]) => `<button type="button" class="tasks-filter-chip ${k === filterGroup ? "active" : ""}" data-filter="${k}">${g.label}</button>`)
            .join("");
        panelEl.innerHTML = `
            <div class="tasks-panel-header">
                <span>Activity</span>
                <button type="button" class="tasks-panel-close" title="Close">
                    <span class="material-symbols-rounded">close</span>
                </button>
            </div>
            <div class="tasks-panel-tabs">
                <button type="button" class="tasks-mode-tab active" data-mode="running">Running</button>
                <button type="button" class="tasks-mode-tab" data-mode="history">History</button>
            </div>
            <div class="tasks-panel-filters">${chips}</div>
            <div class="tasks-panel-body"><div class="tasks-empty">Loading…</div></div>`;
        panelEl.querySelector(".tasks-panel-close").addEventListener("click", () => setOpen(false));
        panelEl.querySelectorAll(".tasks-mode-tab").forEach(btn => {
            btn.addEventListener("click", () => {
                mode = btn.dataset.mode;
                panelEl.querySelectorAll(".tasks-mode-tab").forEach(b => b.classList.toggle("active", b === btn));
                pollList();
            });
        });
        panelEl.querySelectorAll(".tasks-filter-chip").forEach(btn => {
            btn.addEventListener("click", () => {
                filterGroup = btn.dataset.filter;
                panelEl.querySelectorAll(".tasks-filter-chip").forEach(b => b.classList.toggle("active", b === btn));
                pollList();
            });
        });
        document.body.appendChild(panelEl);
    }

    function setFilterGroup(key) {
        filterGroup = GROUPS[key] ? key : "all";
        if (panelEl) {
            panelEl.querySelectorAll(".tasks-filter-chip").forEach(b =>
                b.classList.toggle("active", b.dataset.filter === filterGroup));
        }
    }

    function init() {
        const toggle = document.getElementById("tasksPanelToggle");
        if (!toggle) return; // login page: app.js loads without the sidebar
        badgeEl = document.getElementById("tasksBadge");
        loadDismissState();
        buildPanel();
        toggle.addEventListener("click", () => setOpen(!open));
        document.addEventListener("visibilitychange", startTimers);

        let restoreOpen = false;
        try { restoreOpen = localStorage.getItem("tasksPanelOpen") === "1"; } catch (_) {}
        setOpen(restoreOpen);
    }

    /** Programmatic open, optionally pre-filtered: TasksPanel.open({ page: "/ai-seo" }) */
    function openPanel(opts = {}) {
        if (opts.page) setFilterGroup(PAGE_TO_GROUP[opts.page] || "all");
        if (opts.group) setFilterGroup(opts.group);
        setOpen(true);
    }

    return { init, open: openPanel };
})();

function initTasksPanel() {
    TasksPanel.init();
}
