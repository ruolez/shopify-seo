document.addEventListener("DOMContentLoaded", () => {
    loadAIConfig();
    initSettingsTabs();
});

// ─── Tabs ───
function initSettingsTabs() {
    const tabs = document.querySelectorAll(".tabs .tab");
    tabs.forEach((tab) => {
        tab.addEventListener("click", () => {
            tabs.forEach((t) => t.classList.remove("active"));
            document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
            tab.classList.add("active");
            const panel = document.getElementById("tab-" + tab.dataset.tab);
            if (panel) panel.classList.add("active");

            const tabId = tab.dataset.tab;
            if (tabId === "ai-prompts" && !panel.dataset.loaded) {
                panel.dataset.loaded = "1";
                loadPrompts();
                loadBlogSearchKeywords();
            } else if (tabId === "seo-rules" && !panel.dataset.loaded) {
                panel.dataset.loaded = "1";
                loadRules();
            } else if (tabId === "word-lists" && !panel.dataset.loaded) {
                panel.dataset.loaded = "1";
                loadLists();
            }
        });
    });
}

// ─── AI Config (Tab 1) ───
document.getElementById("ai-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await saveAIConfig();
});

document.getElementById("test-ai-btn").addEventListener("click", testAIConnection);

async function loadOpenRouterModels(savedModel, savedTopicModel) {
    const select = document.getElementById("ai-model");
    const topicSelect = document.getElementById("ai-topic-model");
    try {
        const data = await apiRequest("/api/settings/ai/models");
        if (data.models && data.models.length > 0) {
            select.innerHTML = "";
            topicSelect.innerHTML = '<option value="">Same as Text AI Model</option>';
            data.models.forEach((model) => {
                const option = document.createElement("option");
                option.value = model.id;
                const promptPrice = model.pricing.prompt.toFixed(2);
                const completionPrice = model.pricing.completion.toFixed(2);
                option.textContent = `${model.name} — $${promptPrice} / $${completionPrice} per M tokens`;
                select.appendChild(option);
                topicSelect.appendChild(option.cloneNode(true));
            });
            if (savedModel) {
                select.value = savedModel;
                if (!select.value) select.value = data.models[0].id;
            }
            topicSelect.value = savedTopicModel || "";
        } else {
            fallbackToTextInput(select, savedModel);
        }
    } catch (err) {
        console.error("Failed to load OpenRouter models:", err);
        fallbackToTextInput(select, savedModel);
    }
}

function fallbackToTextInput(select, savedModel) {
    const parent = select.parentNode;
    const input = document.createElement("input");
    input.type = "text";
    input.id = "ai-model";
    input.className = "form-control";
    input.placeholder = "e.g. anthropic/claude-sonnet-4";
    input.value = savedModel || "anthropic/claude-sonnet-4";
    parent.replaceChild(input, select);
}

async function loadImageModels(savedModel) {
    const select = document.getElementById("ai-image-model");
    try {
        const data = await apiRequest("/api/settings/ai/image-models");
        if (data.models && data.models.length > 0) {
            select.innerHTML = "";
            data.models.forEach((model) => {
                const option = document.createElement("option");
                option.value = model.id;
                const promptPrice = model.pricing.prompt.toFixed(2);
                const imagePrice = model.pricing.image.toFixed(2);
                option.textContent = `${model.name} — $${promptPrice} in / $${imagePrice} img per M`;
                select.appendChild(option);
            });
            if (savedModel) {
                select.value = savedModel;
                if (!select.value) select.value = data.models[0].id;
            }
        }
    } catch (err) {
        console.error("Failed to load image models:", err);
    }
}

async function loadAIConfig() {
    try {
        const config = await apiRequest("/api/settings/ai");
        await Promise.all([
            loadOpenRouterModels(config.model, config.topic_research_model),
            loadImageModels(config.image_model),
        ]);
        document.getElementById("ai-budget").value = config.daily_budget;
        document.getElementById("ai-topic-count").value = config.topic_suggestion_count || 3;
        const status = document.getElementById("key-status");
        if (config.has_key) {
            status.textContent = "API key is configured. Leave blank to keep existing key.";
        } else {
            status.textContent = "No API key configured yet.";
        }
    } catch (err) {
        showToast("Failed to load AI config", "error");
    }
}

async function saveAIConfig() {
    const data = {
        api_key: document.getElementById("ai-key").value.trim(),
        model: document.getElementById("ai-model").value,
        daily_budget: parseFloat(document.getElementById("ai-budget").value),
        image_model: document.getElementById("ai-image-model").value,
        topic_research_model: document.getElementById("ai-topic-model").value,
        topic_suggestion_count: parseInt(document.getElementById("ai-topic-count").value, 10) || 3,
    };
    try {
        await apiRequest("/api/settings/ai", { method: "POST", body: data });
        showToast("AI configuration saved", "success");
        document.getElementById("ai-key").value = "";
        loadAIConfig();
    } catch (err) {
        showToast(err.message, "error");
    }
}

async function testAIConnection() {
    const btn = document.getElementById("test-ai-btn");
    btn.disabled = true;
    btn.textContent = "Testing...";
    try {
        const result = await apiRequest("/api/settings/ai/test", { method: "POST" });
        showToast(`AI connected! Response: ${result.response}`, "success");
    } catch (err) {
        showToast(`AI test failed: ${err.message}`, "error");
    } finally {
        btn.disabled = false;
        btn.textContent = "Test Connection";
    }
}

// Brave Search API
document.getElementById("brave-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const key = document.getElementById("brave-key").value.trim();
    if (!key) {
        showToast("Please enter a Brave API key", "error");
        return;
    }
    try {
        await apiRequest("/api/settings/brave", { method: "POST", body: { api_key: key } });
        showToast("Brave API key saved", "success");
        document.getElementById("brave-key").value = "";
        loadBraveStatus();
    } catch (err) {
        showToast(err.message, "error");
    }
});

document.getElementById("test-brave-btn").addEventListener("click", async () => {
    const btn = document.getElementById("test-brave-btn");
    btn.disabled = true;
    btn.textContent = "Testing...";
    try {
        await apiRequest("/api/settings/brave/test", { method: "POST" });
        showToast("Brave Search connected!", "success");
    } catch (err) {
        showToast(`Brave test failed: ${err.message}`, "error");
    } finally {
        btn.disabled = false;
        btn.textContent = "Test Connection";
    }
});

function loadBraveStatus() {
    apiRequest("/api/settings/ai").then((config) => {
        const status = document.getElementById("brave-key-status");
        if (config.has_brave_key) {
            status.textContent = "Brave API key is configured. Leave blank to keep existing key.";
        } else {
            status.textContent = "No Brave API key configured. SERP research will be skipped during optimization.";
        }
    }).catch(() => {});
}
loadBraveStatus();

// ─── Google Search Console ───
document.getElementById("gsc-redirect-hint").textContent = window.location.origin + "/api/gsc/auth/callback";

document.getElementById("gsc-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const clientId = document.getElementById("gsc-client-id").value.trim();
    const clientSecret = document.getElementById("gsc-client-secret").value.trim();
    if (!clientId || !clientSecret) {
        showToast("Both Client ID and Client Secret are required", "error");
        return;
    }
    try {
        await apiRequest("/api/settings/gsc", { method: "POST", body: { client_id: clientId, client_secret: clientSecret } });
        showToast("GSC credentials saved", "success");
        document.getElementById("gsc-client-id").value = "";
        document.getElementById("gsc-client-secret").value = "";
        loadGSCStatus();
    } catch (err) {
        showToast(err.message, "error");
    }
});

function loadGSCStatus() {
    apiRequest("/api/settings/ai").then((config) => {
        const idStatus = document.getElementById("gsc-id-status");
        const secretStatus = document.getElementById("gsc-secret-status");
        if (config.has_gsc_client_id) {
            idStatus.textContent = "Client ID is configured. Leave blank to keep existing.";
        } else {
            idStatus.textContent = "Not configured yet.";
        }
        if (config.has_gsc_client_secret) {
            secretStatus.textContent = "Client Secret is configured. Leave blank to keep existing.";
        } else {
            secretStatus.textContent = "Not configured yet.";
        }
    }).catch(() => {});
}
loadGSCStatus();


// ─── Blog Search Keywords ───
async function loadBlogSearchKeywords() {
    try {
        const data = await apiRequest("/api/settings/custom/blog.search_keywords");
        document.getElementById("blog-search-keywords").value = data.value || "";
    } catch {
        document.getElementById("blog-search-keywords").value = "";
    }
}

document.getElementById("save-blog-keywords-btn").addEventListener("click", async () => {
    const value = document.getElementById("blog-search-keywords").value.trim();
    if (!value) {
        showToast("Please enter keywords", "error");
        return;
    }
    try {
        await apiRequest("/api/settings/custom/blog.search_keywords", {
            method: "POST",
            body: { value },
        });
        showToast("Blog search keywords saved", "success");
    } catch (err) {
        showToast(err.message, "error");
    }
});

document.getElementById("reset-blog-keywords-btn").addEventListener("click", async () => {
    try {
        const result = await apiRequest("/api/settings/custom/blog.search_keywords", { method: "DELETE" });
        document.getElementById("blog-search-keywords").value = result.default_value || "";
        showToast("Blog search keywords reset to default", "success");
    } catch (err) {
        showToast(err.message, "error");
    }
});


// ─── AI Prompts (Tab 2) ───
const PROMPT_GROUPS = {
    "Product Prompts": ["prompts.product_seo", "prompts.product_description", "prompts.product_title", "prompts.product_discovery"],
    "Collection Prompts": ["prompts.collection_seo", "prompts.collection_description", "prompts.collection_title"],
    "Brand Prompts": ["prompts.brand_seo", "prompts.brand_description", "prompts.brand_logo"],
    "Article Prompts": ["prompts.article_seo", "prompts.article_body", "prompts.topic_discovery", "prompts.article_research", "prompts.article_discovery", "prompts.article_outline", "prompts.article_generate", "prompts.article_summary", "prompts.article_refresh_links", "prompts.blog_seo_suggestions"],
    "Landing Page Prompts": ["prompts.landing_page_seo", "prompts.landing_page_generate", "prompts.landing_page_rewrite"],
    "Cannibalization Prompts": ["prompts.cannibalization_analysis", "prompts.cannibalization_fix"],
    "Content Gap Prompts": ["prompts.content_gap_analysis", "prompts.content_brief_generation", "prompts.intent_classification"],
    "Competitor Prompts": ["prompts.competitor_analysis"],
    "News Prompts": ["prompts.news_analyze", "prompts.news_links"],
    "Other Prompts": ["prompts.interlinking", "prompts.inject_interlink", "prompts.technical_seo_fix", "prompts.keyword_phase1", "prompts.keyword_phase2", "prompts.store_audit"],
    "Image Prompts": ["prompts.blog_image", "prompts.blog_hero_image", "prompts.collection_image", "prompts.landing_hero_image", "prompts.news_inline_image"],
};

async function loadPrompts() {
    const container = document.getElementById("prompts-container");
    container.innerHTML = '<p style="color:var(--on-surface-secondary)">Loading prompts...</p>';

    try {
        const data = await apiRequest("/api/settings/custom?prefix=prompts.");
        container.innerHTML = "";

        for (const [groupName, keys] of Object.entries(PROMPT_GROUPS)) {
            const details = document.createElement("details");
            details.className = "settings-accordion";
            const summary = document.createElement("summary");
            const customCount = keys.filter((k) => data[k] && data[k].is_custom).length;
            summary.innerHTML = groupName + (customCount ? ` <span class="customized-badge">${customCount} customized</span>` : "");
            details.appendChild(summary);

            const body = document.createElement("div");
            body.className = "accordion-body";

            keys.forEach((key) => {
                const entry = data[key];
                if (!entry) return;
                const shortKey = key.replace("prompts.", "");
                const item = document.createElement("div");
                item.style.marginBottom = "20px";
                item.innerHTML = `
                    <div class="prompt-key-label">${key} ${entry.is_custom ? '<span class="customized-badge">Customized</span>' : ""}</div>
                    <textarea class="prompt-textarea" id="prompt-${shortKey}">${escapeHtml(typeof entry.value === "string" ? entry.value : JSON.stringify(entry.value, null, 2))}</textarea>
                    <div class="prompt-actions">
                        <button class="btn btn-primary btn-sm" onclick="savePrompt('${key}')">Save</button>
                        <button class="btn btn-secondary btn-sm" onclick="resetPrompt('${key}')">Reset to Default</button>
                    </div>
                `;
                body.appendChild(item);
            });

            details.appendChild(body);
            container.appendChild(details);
        }
    } catch (err) {
        container.innerHTML = `<p style="color:var(--error)">Failed to load prompts: ${err.message}</p>`;
    }
}

window.savePrompt = async function (key) {
    const shortKey = key.replace("prompts.", "");
    const textarea = document.getElementById("prompt-" + shortKey);
    if (!textarea) return;

    try {
        await apiRequest(`/api/settings/custom/${key}`, {
            method: "POST",
            body: { value: textarea.value },
        });
        showToast(`Saved ${key}`, "success");
        loadPrompts();
    } catch (err) {
        showToast(`Failed to save: ${err.message}`, "error");
    }
};

window.resetPrompt = async function (key) {
    try {
        const result = await apiRequest(`/api/settings/custom/${key}`, { method: "DELETE" });
        showToast(`Reset ${key} to default`, "success");
        loadPrompts();
    } catch (err) {
        showToast(`Failed to reset: ${err.message}`, "error");
    }
};


// ─── SEO Rules (Tab 3) ───
const RULE_KEYS = [
    { key: "rules.product_seo", label: "Product SEO Scoring" },
    { key: "rules.collection_seo", label: "Collection SEO Scoring" },
    { key: "rules.article_seo", label: "Article SEO Scoring" },
    { key: "rules.technical_product", label: "Technical SEO — Products" },
    { key: "rules.technical_collection", label: "Technical SEO — Collections" },
    { key: "rules.interlinking", label: "Interlinking Scoring" },
];

async function loadRules() {
    const container = document.getElementById("rules-container");
    container.innerHTML = '<p style="color:var(--on-surface-secondary)">Loading rules...</p>';

    try {
        const data = await apiRequest("/api/settings/custom?prefix=rules.");
        container.innerHTML = "";

        for (const { key, label } of RULE_KEYS) {
            const entry = data[key];
            if (!entry) continue;
            const value = entry.value;

            const details = document.createElement("details");
            details.className = "settings-accordion";
            const summary = document.createElement("summary");
            summary.innerHTML = label + (entry.is_custom ? ' <span class="customized-badge">Customized</span>' : "");
            details.appendChild(summary);

            const body = document.createElement("div");
            body.className = "accordion-body";

            if (value.weights) {
                const wTitle = document.createElement("div");
                wTitle.className = "settings-section-title";
                wTitle.textContent = "Weights";
                body.appendChild(wTitle);

                const wGroup = document.createElement("div");
                wGroup.className = "weight-group";
                wGroup.id = `weights-${key}`;

                for (const [wKey, wVal] of Object.entries(value.weights)) {
                    const item = document.createElement("div");
                    item.className = "weight-item";
                    item.innerHTML = `
                        <label>${wKey}</label>
                        <input type="number" step="0.01" min="0" max="1" value="${wVal}" data-weight-key="${wKey}" onchange="updateWeightTotal('${key}')">
                    `;
                    wGroup.appendChild(item);
                }

                const totalEl = document.createElement("div");
                totalEl.className = "weight-total valid";
                totalEl.id = `weight-total-${key}`;
                totalEl.textContent = "Total: 1.00";
                wGroup.appendChild(totalEl);
                body.appendChild(wGroup);
            }

            if (value.thresholds || value.score_thresholds || value.crosslink_thresholds || value.distribution) {
                const tTitle = document.createElement("div");
                tTitle.className = "settings-section-title";
                tTitle.textContent = "Thresholds";
                body.appendChild(tTitle);

                const pre = document.createElement("textarea");
                pre.className = "prompt-textarea";
                pre.id = `thresholds-${key}`;
                const thresholdData = {};
                if (value.thresholds) thresholdData.thresholds = value.thresholds;
                if (value.score_thresholds) thresholdData.score_thresholds = value.score_thresholds;
                if (value.crosslink_thresholds) thresholdData.crosslink_thresholds = value.crosslink_thresholds;
                if (value.distribution) thresholdData.distribution = value.distribution;
                pre.value = JSON.stringify(thresholdData, null, 2);
                pre.style.minHeight = "120px";
                body.appendChild(pre);
            }

            const actions = document.createElement("div");
            actions.className = "prompt-actions";
            actions.innerHTML = `
                <button class="btn btn-primary btn-sm" onclick="saveRules('${key}')">Save</button>
                <button class="btn btn-secondary btn-sm" onclick="resetRules('${key}')">Reset to Default</button>
            `;
            body.appendChild(actions);

            details.appendChild(body);
            container.appendChild(details);
        }
    } catch (err) {
        container.innerHTML = `<p style="color:var(--error)">Failed to load rules: ${err.message}</p>`;
    }
}

window.updateWeightTotal = function (ruleKey) {
    const group = document.getElementById(`weights-${ruleKey}`);
    if (!group) return;
    const inputs = group.querySelectorAll("input[type=number]");
    let total = 0;
    inputs.forEach((inp) => { total += parseFloat(inp.value) || 0; });
    const el = document.getElementById(`weight-total-${ruleKey}`);
    if (el) {
        el.textContent = `Total: ${total.toFixed(2)}`;
        el.className = "weight-total " + (Math.abs(total - 1.0) <= 0.01 ? "valid" : "invalid");
    }
};

window.saveRules = async function (key) {
    try {
        const currentData = await apiRequest(`/api/settings/custom/${key}`);
        const value = JSON.parse(JSON.stringify(currentData.value));

        const weightGroup = document.getElementById(`weights-${key}`);
        if (weightGroup) {
            const inputs = weightGroup.querySelectorAll("input[data-weight-key]");
            const weights = {};
            inputs.forEach((inp) => { weights[inp.dataset.weightKey] = parseFloat(inp.value) || 0; });
            const total = Object.values(weights).reduce((a, b) => a + b, 0);
            if (Math.abs(total - 1.0) > 0.01) {
                showToast(`Weights must sum to 1.0 (currently ${total.toFixed(2)})`, "error");
                return;
            }
            value.weights = weights;
        }

        const thresholdEl = document.getElementById(`thresholds-${key}`);
        if (thresholdEl) {
            try {
                const parsed = JSON.parse(thresholdEl.value);
                Object.assign(value, parsed);
            } catch (e) {
                showToast("Invalid JSON in thresholds", "error");
                return;
            }
        }

        await apiRequest(`/api/settings/custom/${key}`, {
            method: "POST",
            body: { value },
        });
        showToast(`Saved ${key}`, "success");
        showToast("Run Re-analyze All (SEO → Issues tab) to re-score existing items with the new rules", "info");
        loadRules();
    } catch (err) {
        showToast(`Failed to save: ${err.message}`, "error");
    }
};

window.resetRules = async function (key) {
    try {
        await apiRequest(`/api/settings/custom/${key}`, { method: "DELETE" });
        showToast(`Reset ${key} to default`, "success");
        loadRules();
    } catch (err) {
        showToast(`Failed to reset: ${err.message}`, "error");
    }
};


// ─── Word Lists (Tab 4) ───
const LIST_KEYS = [
    { key: "lists.stop_words", label: "Stop Words", type: "comma" },
    { key: "lists.ngram_multipliers", label: "N-gram Multipliers", type: "json" },
    { key: "lists.article_title_styles", label: "Article Title Styles", type: "json" },
    { key: "lists.collection_image_styles", label: "Collection Image Styles", type: "json" },
    { key: "lists.news_categories", label: "News Categories", type: "comma" },
];

async function loadLists() {
    const container = document.getElementById("lists-container");
    container.innerHTML = '<p style="color:var(--on-surface-secondary)">Loading lists...</p>';

    try {
        const data = await apiRequest("/api/settings/custom?prefix=lists.");
        container.innerHTML = "";

        for (const { key, label, type } of LIST_KEYS) {
            const entry = data[key];
            if (!entry) continue;

            const details = document.createElement("details");
            details.className = "settings-accordion";
            const summary = document.createElement("summary");
            summary.innerHTML = label + (entry.is_custom ? ' <span class="customized-badge">Customized</span>' : "");
            details.appendChild(summary);

            const body = document.createElement("div");
            body.className = "accordion-body";

            let displayValue;
            if (type === "comma" && Array.isArray(entry.value)) {
                displayValue = entry.value.join(", ");
            } else {
                displayValue = JSON.stringify(entry.value, null, 2);
            }

            body.innerHTML = `
                <div class="prompt-key-label">${key}</div>
                <textarea class="prompt-textarea" id="list-${key}" style="min-height:${type === "comma" ? "120" : "200"}px">${escapeHtml(displayValue)}</textarea>
                <div class="prompt-actions">
                    <button class="btn btn-primary btn-sm" onclick="saveList('${key}', '${type}')">Save</button>
                    <button class="btn btn-secondary btn-sm" onclick="resetList('${key}')">Reset to Default</button>
                </div>
            `;

            details.appendChild(body);
            container.appendChild(details);
        }
    } catch (err) {
        container.innerHTML = `<p style="color:var(--error)">Failed to load lists: ${err.message}</p>`;
    }
}

window.saveList = async function (key, type) {
    const textarea = document.getElementById("list-" + key);
    if (!textarea) return;

    let value;
    if (type === "comma") {
        value = textarea.value.split(",").map((s) => s.trim()).filter(Boolean);
    } else {
        try {
            value = JSON.parse(textarea.value);
        } catch (e) {
            showToast("Invalid JSON", "error");
            return;
        }
    }

    try {
        await apiRequest(`/api/settings/custom/${key}`, {
            method: "POST",
            body: { value },
        });
        showToast(`Saved ${key}`, "success");
        loadLists();
    } catch (err) {
        showToast(`Failed to save: ${err.message}`, "error");
    }
};

window.resetList = async function (key) {
    try {
        await apiRequest(`/api/settings/custom/${key}`, { method: "DELETE" });
        showToast(`Reset ${key} to default`, "success");
        loadLists();
    } catch (err) {
        showToast(`Failed to reset: ${err.message}`, "error");
    }
};

// --- Dashboard automation settings ---
async function loadDashboardAutomation() {
    try {
        const vis = await apiRequest("/api/settings/custom/dashboard.weekly_visibility_enabled");
        document.getElementById("dash-weekly-visibility").checked = vis.value === true;
        const anomaly = await apiRequest("/api/settings/custom/dashboard.anomaly");
        const a = anomaly.value || {};
        document.getElementById("dash-anomaly-drop").value = a.clicks_drop_pct ?? 30;
        document.getElementById("dash-anomaly-clicks").value = a.min_weekly_clicks ?? 10;
        document.getElementById("dash-anomaly-volume").value = a.min_keyword_volume ?? 50;
    } catch {
        // Defaults shown if load fails
    }
}

document.getElementById("dash-automation-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
        await apiRequest("/api/settings/custom/dashboard.weekly_visibility_enabled", {
            method: "POST",
            body: JSON.stringify({ value: document.getElementById("dash-weekly-visibility").checked }),
        });
        await apiRequest("/api/settings/custom/dashboard.anomaly", {
            method: "POST",
            body: JSON.stringify({
                value: {
                    clicks_drop_pct: parseInt(document.getElementById("dash-anomaly-drop").value, 10) || 30,
                    min_weekly_clicks: parseInt(document.getElementById("dash-anomaly-clicks").value, 10) || 10,
                    min_keyword_volume: parseInt(document.getElementById("dash-anomaly-volume").value, 10) || 0,
                },
            }),
        });
        showToast("Dashboard settings saved", "success");
    } catch (err) {
        showToast(err.message || "Failed to save dashboard settings", "error");
    }
});

loadDashboardAutomation();
