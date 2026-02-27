document.addEventListener("DOMContentLoaded", loadAIConfig);

document.getElementById("ai-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await saveAIConfig();
});

document
    .getElementById("test-ai-btn")
    .addEventListener("click", testAIConnection);

async function loadOpenRouterModels(savedModel) {
    const select = document.getElementById("ai-model");

    try {
        const data = await apiRequest("/api/settings/ai/models");

        if (data.models && data.models.length > 0) {
            select.innerHTML = "";
            data.models.forEach((model) => {
                const option = document.createElement("option");
                option.value = model.id;
                const promptPrice = model.pricing.prompt.toFixed(2);
                const completionPrice = model.pricing.completion.toFixed(2);
                option.textContent = `${model.name} — $${promptPrice} / $${completionPrice} per M tokens`;
                select.appendChild(option);
            });

            if (savedModel) {
                select.value = savedModel;
                if (!select.value) {
                    select.value = data.models[0].id;
                }
            }
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

async function loadAIConfig() {
    try {
        const config = await apiRequest("/api/settings/ai");

        await loadOpenRouterModels(config.model);

        document.getElementById("ai-budget").value = config.daily_budget;

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
        const result = await apiRequest("/api/settings/ai/test", {
            method: "POST",
        });
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
