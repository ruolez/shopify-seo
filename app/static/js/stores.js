document.addEventListener("DOMContentLoaded", () => {
    loadStores();
    checkActiveSyncs();
});

document.getElementById("store-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await saveStore();
});

let activeSyncStoreId = null;
let storesCache = [];

async function loadStores() {
    const container = document.getElementById("stores-container");
    try {
        const stores = await apiRequest("/api/stores");
        storesCache = stores;
        if (stores.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <span class="material-symbols-rounded">store</span>
                    <p>No stores connected yet</p>
                    <button class="btn btn-primary" onclick="openAddModal()">
                        <span class="material-symbols-rounded">add</span>
                        Add Your First Store
                    </button>
                </div>`;
            return;
        }

        let html = '<div class="table-container"><table><thead><tr>';
        html +=
            "<th>Name</th><th>Domain</th><th>API Version</th><th>Last Synced</th><th>Actions</th>";
        html += "</tr></thead><tbody>";

        for (const s of stores) {
            html += `<tr>
                <td><strong>${escapeHtml(s.name)}</strong></td>
                <td>${escapeHtml(s.domain)}</td>
                <td>${escapeHtml(s.api_version)}</td>
                <td>${formatDate(s.last_synced_at)}</td>
                <td style="display: flex; gap: 4px; flex-wrap: wrap;">
                    <button class="btn btn-sm btn-secondary" onclick="testStore(${s.id})">Test</button>
                    <button class="btn btn-sm btn-primary" onclick="syncStore(${s.id})">Sync</button>
                    <button class="btn btn-sm btn-secondary" onclick="openEditModal(${s.id})">Edit</button>
                    <button class="btn btn-sm btn-danger" onclick="deleteStore(${s.id})">Delete</button>
                </td>
            </tr>`;
        }
        html += "</tbody></table></div>";
        container.innerHTML = html;
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>Failed to load stores: ${escapeHtml(err.message)}</p></div>`;
    }
}

function openAddModal() {
    document.getElementById("modal-title").textContent = "Add Store";
    document.getElementById("store-id").value = "";
    document.getElementById("store-name").value = "";
    document.getElementById("store-domain").value = "";
    document.getElementById("store-token").value = "";
    document.getElementById("store-api-version").value = "2024-10";
    document.getElementById("store-modal").classList.add("active");
}

function openEditModal(id) {
    const store = storesCache.find((s) => s.id === id);
    if (!store) return;
    document.getElementById("modal-title").textContent = "Edit Store";
    document.getElementById("store-id").value = id;
    document.getElementById("store-name").value = store.name;
    document.getElementById("store-domain").value = store.domain;
    document.getElementById("store-token").value = "";
    document.getElementById("store-api-version").value = store.api_version;
    document.getElementById("store-modal").classList.add("active");
}

function closeModal() {
    document.getElementById("store-modal").classList.remove("active");
}

async function saveStore() {
    const id = document.getElementById("store-id").value;
    const data = {
        name: document.getElementById("store-name").value.trim(),
        domain: document.getElementById("store-domain").value.trim(),
        access_token: document.getElementById("store-token").value.trim(),
        api_version: document.getElementById("store-api-version").value.trim(),
    };

    try {
        if (id) {
            await apiRequest(`/api/stores/${id}`, { method: "PUT", body: data });
            showToast("Store updated", "success");
        } else {
            if (!data.access_token) {
                showToast("Access token is required for new stores", "error");
                return;
            }
            const resp = await apiRequest("/api/stores", { method: "POST", body: data });
            if (resp && resp.warning) {
                showToast(resp.warning, "error");
            } else {
                showToast("Store added", "success");
            }
        }
        closeModal();
        loadStores();
    } catch (err) {
        showToast(err.message, "error");
    }
}

async function testStore(id) {
    try {
        const result = await apiRequest(`/api/stores/${id}/test`, {
            method: "POST",
        });
        showToast(`Connected to ${result.shop.name}`, "success");
    } catch (err) {
        showToast(`Connection failed: ${err.message}`, "error");
    }
}

function showSyncOverlay() {
    const overlay = document.getElementById("sync-progress-overlay");
    const bar = document.getElementById("sync-progress-bar");
    const subtitle = document.getElementById("sync-progress-subtitle");
    const stats = document.getElementById("sync-progress-stats");
    const current = document.getElementById("sync-progress-current");
    bar.style.width = "0%";
    bar.classList.remove("indeterminate");
    subtitle.textContent = "Connecting to Shopify...";
    stats.textContent = "";
    current.textContent = "";
    overlay.classList.add("active");
}

function hideSyncOverlay() {
    document.getElementById("sync-progress-overlay").classList.remove("active");
}

async function syncStore(id) {
    try {
        await apiRequest(`/api/stores/${id}/sync`, { method: "POST" });
        activeSyncStoreId = id;
        showSyncOverlay();
        pollSyncStatus(id);
    } catch (err) {
        showToast(`Sync failed: ${err.message}`, "error");
    }
}

function pollSyncStatus(storeId) {
    let consecutiveErrors = 0;
    const interval = setInterval(async () => {
        try {
            const status = await apiRequest(
                `/api/stores/${storeId}/sync-status`
            );
            consecutiveErrors = 0;

            const bar = document.getElementById("sync-progress-bar");
            const subtitle = document.getElementById("sync-progress-subtitle");
            const stats = document.getElementById("sync-progress-stats");
            const current = document.getElementById("sync-progress-current");

            const phase = status.current_phase;

            if (status.status === "completed") {
                clearInterval(interval);
                activeSyncStoreId = null;
                bar.classList.remove("indeterminate");
                bar.style.width = "100%";
                subtitle.textContent = "Sync complete!";
                stats.textContent = `${status.products_synced} products, ${status.collections_synced} collections, ${status.blogs_synced || 0} blogs, ${status.articles_synced || 0} articles`;
                current.textContent = "";
                setTimeout(() => {
                    hideSyncOverlay();
                    const counts = `${status.products_synced} products, ${status.collections_synced} collections, ${status.blogs_synced || 0} blogs, ${status.articles_synced || 0} articles`;
                    if (status.error) {
                        showToast(`Sync finished with problems (${counts}): ${status.error}`, "warning");
                    } else {
                        showToast(`Sync complete: ${counts}`, "success");
                    }
                    loadStores();
                }, 1500);
                return;
            }

            if (status.status === "failed") {
                clearInterval(interval);
                activeSyncStoreId = null;
                hideSyncOverlay();
                showToast(`Sync failed: ${status.error}`, "error");
                return;
            }

            if (phase === "fetching_products") {
                bar.classList.add("indeterminate");
                bar.style.width = "";
                subtitle.textContent = "Fetching products from Shopify...";
                stats.textContent = "";
                current.textContent = "This may take a moment for large stores";
            } else if (phase === "saving_products") {
                bar.classList.remove("indeterminate");
                const total = status.total_products || 1;
                const done = status.products_synced || 0;
                const pct = Math.round((done / total) * 100);
                bar.style.width = pct + "%";
                subtitle.textContent = "Saving products...";
                stats.textContent = `${done} / ${total} products`;
                current.textContent = "";
            } else if (phase === "fetching_collections") {
                bar.classList.add("indeterminate");
                bar.style.width = "";
                subtitle.textContent = "Fetching collections from Shopify...";
                stats.textContent = "";
                current.textContent = "";
            } else if (phase === "saving_collections") {
                bar.classList.remove("indeterminate");
                const total = status.total_collections || 1;
                const done = status.collections_synced || 0;
                const pct = Math.round((done / total) * 100);
                bar.style.width = pct + "%";
                subtitle.textContent = "Saving collections...";
                stats.textContent = `${done} / ${total} collections`;
                current.textContent = "";
            } else if (phase === "mapping_products") {
                bar.classList.remove("indeterminate");
                const total = status.total_collections || 1;
                const done = status.collections_mapped || 0;
                const pct = Math.round((done / total) * 100);
                bar.style.width = pct + "%";
                subtitle.textContent = "Mapping collection products...";
                stats.textContent = `${done} / ${total} collections mapped`;
                current.textContent = "";
            } else if (phase === "fetching_blogs") {
                bar.classList.add("indeterminate");
                bar.style.width = "";
                subtitle.textContent = "Fetching blogs & articles from Shopify...";
                stats.textContent = "";
                current.textContent = "";
            } else if (phase === "saving_blogs") {
                bar.classList.add("indeterminate");
                bar.style.width = "";
                subtitle.textContent = "Saving blogs & articles...";
                stats.textContent = `${status.blogs_synced || 0} blogs, ${status.articles_synced || 0} articles`;
                current.textContent = "";
            }
        } catch (err) {
            consecutiveErrors++;
            if (consecutiveErrors >= 5) {
                clearInterval(interval);
                activeSyncStoreId = null;
                hideSyncOverlay();
                showToast(`Lost contact with the sync (${err.message}). It may still be running — reload to check.`, "error");
            }
        }
    }, 1500);
}

async function checkActiveSyncs() {
    try {
        const stores = await apiRequest("/api/stores");
        for (const s of stores) {
            const status = await apiRequest(`/api/stores/${s.id}/sync-status`);
            if (status.status === "running") {
                activeSyncStoreId = s.id;
                showSyncOverlay();
                pollSyncStatus(s.id);
                break;
            }
        }
    } catch {
        // ignore
    }
}

async function deleteStore(id) {
    const store = storesCache.find((s) => s.id === id);
    const name = store ? store.name : "";
    if (!confirm(`Delete store "${name}"? This will remove all synced data.`))
        return;
    try {
        await apiRequest(`/api/stores/${id}`, { method: "DELETE" });
        showToast("Store deleted", "success");
        loadStores();
    } catch (err) {
        showToast(err.message, "error");
    }
}
