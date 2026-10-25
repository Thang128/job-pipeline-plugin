(function (root) {
  "use strict";

  const helpers = root.JobDashboardHelpers || (typeof require === "function" ? require("./dashboard_helpers.js") : null);
  const SENIORITY = ["entry", "mid", "senior", "lead_manager", "director_executive"];
  let csrfToken = "";
  let settingsVersion = 0;
  let onboardingReady = false;
  let jobsPage = 1;
  let historyPage = 1;

  function element(documentObject, tag, text, className) {
    const node = documentObject.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (className) node.setAttribute("class", className);
    return node;
  }

  function renderOnboarding(documentObject, status) {
    const ready = status && status.ready === true;
    const missing = status && Array.isArray(status.missing) ? status.missing : [];
    const banner = documentObject.getElementById("onboarding-banner");
    banner.hidden = ready;
    banner.textContent = ready ? "" : `Onboarding required: ${missing.join(", ")}`;
    documentObject.querySelectorAll("[data-onboarding-required]").forEach((control) => {
      control.disabled = !ready;
    });
    onboardingReady = ready;
  }

  function createJobCard(documentObject, job) {
    const card = element(documentObject, "article", undefined, "job-card");
    card.append(element(documentObject, "h3", job && job.title ? job.title : "Untitled role"));
    card.append(element(documentObject, "p", job && job.company ? job.company : "Unknown organization"));
    card.append(element(documentObject, "span", helpers.statusLabel(job && job.status), "status-chip"));
    const safeUrl = helpers.safeExternalUrl(job && (job.application_url || job.source_url));
    if (safeUrl) {
      const link = element(documentObject, "a", "Open source");
      link.setAttribute("href", safeUrl);
      link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener noreferrer");
      card.append(link);
    }
    return card;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function labelledInput(documentObject, labelText, value, type) {
    const label = element(documentObject, "label", labelText);
    const input = element(documentObject, "input");
    input.type = type || "text";
    input.value = value == null ? "" : String(value);
    label.append(input);
    return { label, input };
  }

  function renderRoleRows(documentObject, rows) {
    const container = documentObject.getElementById("role-rows");
    clear(container);
    rows.forEach((row, index) => {
      const fieldset = element(documentObject, "fieldset", undefined, "dynamic-row");
      const id = labelledInput(documentObject, "Role ID", row.id, "text");
      const label = labelledInput(documentObject, "Label", row.label, "text");
      const keywords = labelledInput(documentObject, "Keywords", (row.keywords || []).join(", "), "text");
      const priority = labelledInput(documentObject, "Priority", row.priority, "number");
      const enabled = labelledInput(documentObject, "Enabled", "", "checkbox");
      enabled.input.checked = row.enabled !== false;
      const remove = element(documentObject, "button", "Remove");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove role family ${row.label || index + 1}`);
      remove.addEventListener("click", () => {
        const current = collectRows(container, "role");
        current.splice(index, 1);
        current.forEach((item, priorityIndex) => { item.priority = priorityIndex + 1; });
        renderRoleRows(documentObject, current);
      });
      fieldset.append(id.label, label.label, keywords.label, priority.label, enabled.label, remove);
      fieldset.dataset.index = String(index);
      container.append(fieldset);
    });
  }

  function renderCountryRows(documentObject, rows) {
    const container = documentObject.getElementById("country-rows");
    clear(container);
    rows.forEach((row, index) => {
      const fieldset = element(documentObject, "fieldset", undefined, "dynamic-row");
      const code = labelledInput(documentObject, "Country code", row.code, "text");
      const label = labelledInput(documentObject, "Label", row.label, "text");
      const priority = labelledInput(documentObject, "Priority", row.priority, "number");
      const enabled = labelledInput(documentObject, "Enabled", "", "checkbox");
      enabled.input.checked = row.enabled !== false;
      const remove = element(documentObject, "button", "Remove");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove country ${row.label || row.code || index + 1}`);
      remove.addEventListener("click", () => {
        const current = collectRows(container, "country");
        current.splice(index, 1);
        current.forEach((item, priorityIndex) => { item.priority = priorityIndex + 1; });
        renderCountryRows(documentObject, current);
      });
      fieldset.append(code.label, label.label, priority.label, enabled.label, remove);
      fieldset.dataset.index = String(index);
      container.append(fieldset);
    });
  }

  function setValue(documentObject, id, value) {
    documentObject.getElementById(id).value = value == null ? "" : String(value);
  }

  function setChecked(documentObject, id, value) {
    documentObject.getElementById(id).checked = value === true;
  }

  function renderSettings(documentObject, raw) {
    const settings = helpers.normalizeSettings(raw);
    renderRoleRows(documentObject, settings.role_families);
    renderCountryRows(documentObject, settings.countries);
    setValue(documentObject, "minimum-salary-amount", settings.minimum_salary.amount);
    setValue(documentObject, "minimum-salary-currency", settings.minimum_salary.currency);
    setValue(documentObject, "minimum-seniority", settings.minimum_seniority);
    setValue(documentObject, "maximum-seniority", settings.maximum_seniority);
    setChecked(documentObject, "exclude-unknown-seniority", settings.exclude_unknown_seniority);
    setValue(documentObject, "include-keywords", settings.include_keywords.join("\n"));
    setValue(documentObject, "exclude-keywords", settings.exclude_keywords.join("\n"));
    setValue(documentObject, "excluded-employers", settings.excluded_employers.join("\n"));
    setChecked(documentObject, "prioritize-large-groups", settings.prioritize_large_groups);
    setValue(documentObject, "large-group-employers", settings.large_group_employers.join("\n"));
    setChecked(documentObject, "vie-enabled", settings.vie_enabled);
    setValue(documentObject, "score-threshold", settings.score_threshold);
    setValue(documentObject, "daily-submission-cap", settings.daily_submission_cap);
  }

  function collectRows(container, kind) {
    return Array.from(container.children).map((fieldset, index) => {
      const inputs = fieldset.querySelectorAll("input");
      if (kind === "role") {
        return {
          id: inputs[0].value.trim(),
          label: inputs[1].value.trim(),
          keywords: helpers.splitList(inputs[2].value),
          priority: helpers.parseStrictNumber(inputs[3].value, { label: "role priority", minimum: 1, maximum: 100, integer: true }),
          enabled: inputs[4].checked,
        };
      }
      return {
        code: inputs[0].value.trim().toUpperCase(),
        label: inputs[1].value.trim(),
        priority: helpers.parseStrictNumber(inputs[2].value, { label: "country priority", minimum: 1, maximum: 100, integer: true }),
        enabled: inputs[3].checked,
      };
    });
  }

  function collectSettings(documentObject) {
    return {
      schema_version: 1,
      role_families: collectRows(documentObject.getElementById("role-rows"), "role"),
      countries: collectRows(documentObject.getElementById("country-rows"), "country"),
      minimum_salary: {
        amount: helpers.parseStrictNumber(documentObject.getElementById("minimum-salary-amount").value, { label: "salary", minimum: 0, maximum: 1e12 }),
        currency: documentObject.getElementById("minimum-salary-currency").value.trim().toUpperCase(),
      },
      score_threshold: helpers.parseStrictNumber(documentObject.getElementById("score-threshold").value, { label: "score", minimum: 0, maximum: 100 }),
      daily_submission_cap: helpers.parseStrictNumber(documentObject.getElementById("daily-submission-cap").value, { label: "cap", minimum: 0, maximum: 50, integer: true }),
      minimum_seniority: documentObject.getElementById("minimum-seniority").value,
      maximum_seniority: documentObject.getElementById("maximum-seniority").value,
      exclude_unknown_seniority: documentObject.getElementById("exclude-unknown-seniority").checked,
      include_keywords: helpers.splitList(documentObject.getElementById("include-keywords").value),
      exclude_keywords: helpers.splitList(documentObject.getElementById("exclude-keywords").value),
      excluded_employers: helpers.splitList(documentObject.getElementById("excluded-employers").value),
      prioritize_large_groups: documentObject.getElementById("prioritize-large-groups").checked,
      large_group_employers: helpers.splitList(documentObject.getElementById("large-group-employers").value),
      vie_enabled: documentObject.getElementById("vie-enabled").checked,
    };
  }

  async function api(path, options) {
    const request = Object.assign({ headers: { Accept: "application/json" } }, options || {});
    if (request.body) {
      request.headers["Content-Type"] = "application/json";
      request.headers["X-CSRF-Token"] = csrfToken;
    }
    const response = await root.fetch(path, request);
    const payload = await response.json();
    if (!response.ok) {
      const error = new Error(payload.error || "Dashboard request failed");
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function renderSummary(documentObject, raw) {
    const summary = helpers.normalizeSummaries(raw);
    const container = documentObject.getElementById("summary-cards");
    clear(container);
    [["Jobs", summary.total_jobs], ["Applied today", summary.applied_today], ["Needs user", summary.by_status.needs_user || 0]].forEach(([label, value]) => {
      const card = element(documentObject, "article", undefined, "summary-card");
      card.append(element(documentObject, "strong", value), element(documentObject, "span", label));
      container.append(card);
    });
  }

  function renderPagination(documentObject, prefix, payload) {
    const page = Number.isInteger(payload.page) ? payload.page : 1;
    const pages = Number.isInteger(payload.pages) ? payload.pages : 0;
    const total = Number.isInteger(payload.total) ? payload.total : 0;
    documentObject.getElementById(`${prefix}-page`).textContent = `Page ${page} of ${pages} · ${total} total`;
    documentObject.getElementById(`${prefix}-previous`).disabled = page <= 1;
    documentObject.getElementById(`${prefix}-next`).disabled = pages === 0 || page >= pages;
  }

  async function loadJobs(documentObject, page = jobsPage) {
    const payload = await api(`/api/jobs?page=${page}&page_size=50`);
    jobsPage = payload.page;
    const container = documentObject.getElementById("job-list");
    clear(container);
    payload.items.forEach((job) => container.append(createJobCard(documentObject, job)));
    renderPagination(documentObject, "jobs", payload);
  }

  async function loadHistory(documentObject, page = historyPage) {
    const payload = await api(`/api/settings/history?page=${page}&page_size=50`);
    historyPage = payload.page;
    const container = documentObject.getElementById("history-list");
    clear(container);
    payload.items.forEach((revision) => {
      const row = element(documentObject, "article", undefined, "history-row");
      row.append(element(documentObject, "strong", `Version ${revision.version}`), element(documentObject, "span", revision.reason));
      const button = element(documentObject, "button", "Rollback");
      button.type = "button";
      button.disabled = !onboardingReady;
      button.addEventListener("click", async () => {
        await handleSettingsAction(
          documentObject,
          () => api("/api/settings/rollback", { method: "POST", body: JSON.stringify({ target_version: revision.version, expected_version: settingsVersion, reason: "dashboard rollback" }) })
        );
      });
      row.append(button);
      container.append(row);
    });
    renderPagination(documentObject, "history", payload);
  }

  function bindNavigation(documentObject) {
    documentObject.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => {
      documentObject.querySelectorAll(".view").forEach((view) => { view.hidden = view.id !== button.dataset.view; });
      documentObject.querySelectorAll("[data-view]").forEach((item) => item.setAttribute("aria-current", item === button ? "page" : "false"));
    }));
  }

  function applyCommittedSettings(documentObject, result) {
    settingsVersion = result.version;
    renderSettings(documentObject, result.settings);
    documentObject.getElementById("settings-version").textContent = `Version ${settingsVersion}`;
    const pending = result.export_status === "pending";
    documentObject.getElementById("retry-export").hidden = !pending;
    documentObject.getElementById("action-status").textContent = pending
      ? "Settings saved; dashboard export is pending. Retry export."
      : "Settings saved and dashboard export is current.";
  }

  async function handleSettingsAction(
    documentObject,
    action,
    refreshHistory = (target) => loadHistory(target, 1),
    refreshAll = refreshDashboardState
  ) {
    try {
      const result = await action();
      applyCommittedSettings(documentObject, result);
      await refreshHistory(documentObject);
      return result;
    } catch (error) {
      if (error && error.status === 409) {
        await refreshAll(documentObject);
        documentObject.getElementById("action-status").textContent = "Settings changed elsewhere; refreshed current version.";
        return null;
      }
      documentObject.getElementById("action-status").textContent = "Settings action failed.";
      throw error;
    }
  }

  async function refreshDashboardState(documentObject) {
    const bootstrap = await api("/api/bootstrap");
    csrfToken = bootstrap.csrf_token;
    settingsVersion = bootstrap.settings_version;
    renderOnboarding(documentObject, bootstrap.onboarding);
    renderSummary(documentObject, bootstrap.summaries);
    renderSettings(documentObject, bootstrap.settings);
    documentObject.getElementById("settings-version").textContent = `Version ${settingsVersion}`;
    await Promise.all([loadJobs(documentObject), loadHistory(documentObject)]);
    return bootstrap;
  }

  async function startDashboard(documentObject) {
    bindNavigation(documentObject);
    await refreshDashboardState(documentObject);
    documentObject.getElementById("add-role").addEventListener("click", () => {
      const settings = collectSettings(documentObject);
      settings.role_families.push({ id: "", label: "", keywords: [], enabled: true, priority: settings.role_families.length + 1 });
      renderRoleRows(documentObject, settings.role_families);
    });
    documentObject.getElementById("add-country").addEventListener("click", () => {
      const settings = collectSettings(documentObject);
      settings.countries.push({ code: "", label: "", enabled: true, priority: settings.countries.length + 1 });
      renderCountryRows(documentObject, settings.countries);
    });
    documentObject.getElementById("preview-settings").addEventListener("click", async () => {
      const result = await api("/api/settings/preview", { method: "POST", body: JSON.stringify({ settings: collectSettings(documentObject) }) });
      documentObject.getElementById("preview-result").textContent = `${result.eligible_count} settings matches. Candidate eligibility is checked in the operator queue.`;
    });
    documentObject.getElementById("settings-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!onboardingReady) return;
      await handleSettingsAction(
        documentObject,
        () => api("/api/settings", { method: "PUT", body: JSON.stringify({ settings: collectSettings(documentObject), expected_version: settingsVersion, reason: "dashboard edit" }) })
      );
    });
    documentObject.getElementById("retry-export").addEventListener("click", async () => {
      await handleSettingsAction(
        documentObject,
        () => api("/api/export/retry", { method: "POST", body: "{}" })
      );
    });
    documentObject.getElementById("jobs-previous").addEventListener("click", () => loadJobs(documentObject, jobsPage - 1));
    documentObject.getElementById("jobs-next").addEventListener("click", () => loadJobs(documentObject, jobsPage + 1));
    documentObject.getElementById("history-previous").addEventListener("click", () => loadHistory(documentObject, historyPage - 1));
    documentObject.getElementById("history-next").addEventListener("click", () => loadHistory(documentObject, historyPage + 1));
  }

  const publicApi = {
    applyCommittedSettings,
    collectSettings,
    createJobCard,
    handleSettingsAction,
    refreshDashboardState,
    renderPagination,
    renderOnboarding,
    renderSettings,
    startDashboard,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = publicApi;
  if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", () => startDashboard(document).catch((error) => {
    document.getElementById("connection-status").textContent = error.message;
  }));
})(typeof window !== "undefined" ? window : globalThis);
