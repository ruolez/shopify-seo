// Shared Chart.js helpers for the dashboard. Colors come from the CSS
// variables so charts match the active Material theme; charts created
// before a theme toggle keep their palette until the next page load.

function chartColors() {
    const s = getComputedStyle(document.documentElement);
    const v = (name, fallback) => (s.getPropertyValue(name) || "").trim() || fallback;
    return {
        primary: v("--primary", "#1a73e8"),
        success: v("--success", "#1e8e3e"),
        error: v("--error", "#d93025"),
        warning: v("--warning", "#f9ab00"),
        outline: v("--outline", "#dadce0"),
        text: v("--on-surface-secondary", "#5f6368"),
    };
}

function hexToRgba(hex, alpha) {
    const m = hex.replace("#", "");
    const n = parseInt(m.length === 3 ? m.split("").map((c) => c + c).join("") : m, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

function makeSparkline(canvas, values, { color } = {}) {
    if (!canvas || typeof Chart === "undefined" || !values || values.length < 2) return null;
    const c = color || chartColors().primary;
    return new Chart(canvas, {
        type: "line",
        data: {
            labels: values.map((_, i) => i),
            datasets: [{
                data: values,
                borderColor: c,
                backgroundColor: hexToRgba(c, 0.12),
                fill: true,
                borderWidth: 1.5,
                pointRadius: 0,
                tension: 0.3,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            plugins: { legend: { display: false }, tooltip: { enabled: false } },
            scales: { x: { display: false }, y: { display: false } },
        },
    });
}

// Vertical marker lines (e.g. optimization events) drawn on a time-series
// chart. Pass markers as [{index, label}] via options.plugins.eventMarkers.
const eventMarkersPlugin = {
    id: "eventMarkers",
    afterDatasetsDraw(chart, _args, opts) {
        const markers = (opts && opts.markers) || [];
        if (!markers.length) return;
        const { ctx, chartArea, scales } = chart;
        const colors = chartColors();
        ctx.save();
        ctx.strokeStyle = hexToRgba(colors.success, 0.55);
        ctx.setLineDash([4, 3]);
        ctx.lineWidth = 1;
        for (const m of markers) {
            const x = scales.x.getPixelForValue(m.index);
            if (x < chartArea.left || x > chartArea.right) continue;
            ctx.beginPath();
            ctx.moveTo(x, chartArea.top);
            ctx.lineTo(x, chartArea.bottom);
            ctx.stroke();
        }
        ctx.restore();
    },
};
if (typeof Chart !== "undefined") Chart.register(eventMarkersPlugin);

function makeTimeSeriesChart(canvas, { labels, datasets, markers, yReverse }) {
    if (!canvas || typeof Chart === "undefined") return null;
    const colors = chartColors();
    return new Chart(canvas, {
        type: "line",
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { display: datasets.length > 1, labels: { color: colors.text, boxWidth: 12 } },
                eventMarkers: { markers: markers || [] },
            },
            scales: {
                x: {
                    ticks: { color: colors.text, maxTicksLimit: 10, maxRotation: 0 },
                    grid: { display: false },
                },
                y: {
                    reverse: !!yReverse,
                    ticks: { color: colors.text, maxTicksLimit: 6 },
                    grid: { color: hexToRgba(colors.outline, 0.5) },
                },
            },
        },
    });
}
