/* Content Calendar — merged month view of scheduled blog + news publishing.
 * Read/click-through v1: click an entry to jump to its editor. */

let calCursor = new Date();
calCursor.setDate(1);

const CAL_STATUS_COLOR = (status) => {
    if (status === "published") return "var(--success)";
    if (status === "error" || status === "failed") return "var(--error)";
    if (status === "scheduled" || status === "auto_queued") return "var(--primary)";
    return "var(--warning)"; // researching / generating / auto_publishing …
};

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

    const byDay = {};
    for (const e of entries) {
        if (!e.scheduled_for) continue;
        const day = new Date(e.scheduled_for);
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
                const href = e.kind === "blog" ? "/blog?tab=scheduled" : "/news";
                const time = e.scheduled_for
                    ? new Date(e.scheduled_for).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })
                    : "";
                return `<a class="cal-entry" href="${href}" title="${escapeAttr(`${e.kind}: ${e.title || ""} · ${e.status} · ${time}`)}">
                    <span class="cal-dot" style="background:${CAL_STATUS_COLOR(e.status)};"></span>
                    <span class="cal-entry-kind">${e.kind === "blog" ? "B" : "N"}</span>
                    <span class="cal-entry-title">${escapeHtml(e.title || "Untitled")}</span>
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
