/* Content Calendar — merged month view of scheduled blog + news publishing.
 * Click an entry to jump to its editor; approve waiting entries in place. */

let calCursor = new Date();
calCursor.setDate(1);

// Dot COLOR encodes the content type (unmistakably distinct); dot SHAPE
// encodes status: filled = published, outlined = approved and waiting for
// its slot, dashed = not approved yet (the scheduler will skip it).
// Failed entries override to red so problems always stand out.
const CAL_KIND_COLOR = { blog: "#8b5cf6", news: "#f59e0b" }; // violet / amber

function calDot(entry) {
    if (entry.status === "error" || entry.status === "failed") {
        return `<span class="cal-dot" style="background:var(--error);"></span>`;
    }
    const color = CAL_KIND_COLOR[entry.kind] || "var(--on-surface-secondary)";
    if (entry.status === "published") return `<span class="cal-dot" style="background:${color};"></span>`;
    if (entry.needs_approval) return `<span class="cal-dot" style="background:transparent;border:2px dashed ${color};"></span>`;
    return `<span class="cal-dot" style="background:transparent;border:2px solid ${color};"></span>`;
}

function calStatusLabel(entry) {
    if (entry.needs_approval) return "needs approval — will not publish until approved";
    if (entry.status === "scheduled") return "approved, publishes at its slot";
    return entry.status;
}

function calMonthParam() {
    return `${calCursor.getFullYear()}-${String(calCursor.getMonth() + 1).padStart(2, "0")}`;
}

function calShiftMonth(delta) {
    calCursor.setMonth(calCursor.getMonth() + delta);
    loadCalendar();
}

function calToday() {
    calCursor = new Date();
    calCursor.setDate(1);
    loadCalendar();
}

async function calApprove(kind, id, btn) {
    if (btn) btn.disabled = true;
    try {
        const res = await apiRequest(`/api/${kind}/scheduled/${id}/approve`, { method: "POST", body: { approved: true } });
        const when = res.scheduled_for ? parseServerDate(res.scheduled_for).toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" }) : "its slot";
        showToast(`Approved — publishes ${when}`, "success");
        loadCalendar();
    } catch (e) {
        if (btn) btn.disabled = false;
        showToast(`Approve failed: ${e.message}`, "error");
    }
}

function calRenderBanner(entries) {
    const banner = document.getElementById("cal-approval-banner");
    if (!banner) return;
    const waiting = entries.filter((e) => e.needs_approval);
    if (!waiting.length) {
        banner.style.display = "none";
        banner.innerHTML = "";
        return;
    }
    const news = waiting.filter((e) => e.kind === "news").length;
    const blog = waiting.length - news;
    const parts = [];
    if (news) parts.push(`<a href="/news?tab=scheduled">${news} news</a>`);
    if (blog) parts.push(`<a href="/blog?tab=scheduled">${blog} blog</a>`);
    banner.style.display = "";
    banner.innerHTML = `<span class="material-symbols-rounded" style="color:var(--warning);">pending_actions</span>
        <span><strong>${waiting.length} post${waiting.length > 1 ? "s" : ""} waiting for approval</strong> (${parts.join(", ")}) —
        the scheduler skips a post until you approve it. Use the <em>Approve</em> button on the entry or in its Scheduled tab.</span>`;
}

async function loadCalendar() {
    document.getElementById("cal-month-label").textContent = calCursor.toLocaleDateString("en-US", { month: "long", year: "numeric" });

    const weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    document.getElementById("cal-weekdays").innerHTML = weekdays
        .map((d) => `<div style="font-size:11px;font-weight:600;color:var(--on-surface-secondary);text-align:center;padding:4px 0;">${d}</div>`)
        .join("");

    const grid = document.getElementById("cal-days");
    UISkeleton.show(grid, { type: "cards", rows: 7 });

    let entries = [];
    const storeId = StoreSelector.getStoreId();
    if (storeId) {
        try {
            const d = await apiRequest(`/api/calendar?store_id=${storeId}&month=${calMonthParam()}`);
            entries = d.entries || [];
        } catch (err) {
            showToast(`Failed to load calendar: ${err.message}`, "error");
        }
    }
    calRenderBanner(entries);

    const byDay = {};
    for (const e of entries) {
        if (!e.scheduled_for) continue;
        const day = parseServerDate(e.scheduled_for);
        const key = `${day.getFullYear()}-${day.getMonth()}-${day.getDate()}`;
        (byDay[key] = byDay[key] || []).push(e);
    }

    const year = calCursor.getFullYear();
    const month = calCursor.getMonth();
    const firstWeekday = (new Date(year, month, 1).getDay() + 6) % 7; // Monday-first
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const today = new Date();
    const isToday = (d) => d === today.getDate() && month === today.getMonth() && year === today.getFullYear();

    let cells = "";
    for (let i = 0; i < firstWeekday; i++) cells += `<div class="cal-cell cal-cell-empty"></div>`;
    for (let d = 1; d <= daysInMonth; d++) {
        const dayEntries = byDay[`${year}-${month}-${d}`] || [];
        const chips = dayEntries
            .map((e) => {
                const href = e.kind === "blog" ? "/blog?tab=scheduled" : "/news?tab=scheduled";
                const time = e.scheduled_for
                    ? parseServerDate(e.scheduled_for).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })
                    : "";
                const approve = e.needs_approval
                    ? `<button class="cal-approve" title="Approve — lets this post publish at its slot" onclick="event.preventDefault(); event.stopPropagation(); calApprove('${e.kind}', ${e.id}, this)">Approve</button>`
                    : "";
                return `<a class="cal-entry ${e.needs_approval ? "cal-entry-waiting" : ""}" href="${href}" title="${escapeAttr(`${e.kind}: ${e.title || ""} · ${calStatusLabel(e)} · ${time}`)}">
                    ${calDot(e)}
                    <span class="cal-entry-kind" style="color:${CAL_KIND_COLOR[e.kind] || "var(--on-surface-secondary)"};">${e.kind === "blog" ? "B" : "N"}</span>
                    <span class="cal-entry-title">${escapeHtml(e.title || "Untitled")}</span>
                    ${approve}
                </a>`;
            })
            .join("");
        cells += `<div class="cal-cell ${isToday(d) ? "cal-cell-today" : ""}">
            <div class="cal-cell-day">${d}</div>${chips}
        </div>`;
    }
    grid.innerHTML = cells;
}

document.addEventListener("DOMContentLoaded", () => {
    // StoreSelector.init() (ui.js) resolves the store asynchronously — wait a
    // beat, then render; re-render on store switches.
    setTimeout(loadCalendar, 350);
    document.addEventListener("store:changed", loadCalendar);
});
