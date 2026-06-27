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

function escapeHtml(str) {
    if (!str) return "";
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

function escapeAttr(str) {
    if (!str) return "";
    return String(str).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/'/g, "&#39;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
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

function formatDate(isoStr) {
    if (!isoStr) return "—";
    const d = new Date(isoStr);
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
    const d = new Date(isoStr);
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
});
