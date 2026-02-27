document.addEventListener("DOMContentLoaded", loadStats);

async function loadStats() {
    try {
        const stats = await apiRequest("/api/dashboard/stats");
        document.getElementById("stat-stores").textContent = stats.stores;
        document.getElementById("stat-products").textContent =
            stats.products.toLocaleString();
        document.getElementById("stat-seo").textContent = stats.avg_seo_score;
        document.getElementById("stat-tasks").textContent = stats.ai_tasks;
    } catch (err) {
        showToast("Failed to load dashboard stats", "error");
    }
}
