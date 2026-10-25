(function (root) {
  "use strict";

  const STATUS_LABELS = Object.freeze({
    new: "New",
    shortlisted: "Shortlisted",
    docs_ready: "Documents ready",
    applied: "Applied",
    needs_user: "Needs user",
    skipped: "Skipped",
    interview: "Interview",
    offer: "Offer",
    rejected: "Rejected",
  });

  const SETTINGS_DEFAULTS = Object.freeze({
    schema_version: 1,
    role_families: [],
    countries: [],
    minimum_salary: { amount: 0, currency: "EUR" },
    score_threshold: 75,
    daily_submission_cap: 0,
    minimum_seniority: "entry",
    maximum_seniority: "mid",
    exclude_unknown_seniority: true,
    include_keywords: [],
    exclude_keywords: [],
    excluded_employers: [],
    prioritize_large_groups: false,
    large_group_employers: [],
    vie_enabled: false,
  });

  function words(value) {
    const normalized = String(value || "")
      .replace(/[_-]+/g, " ")
      .trim()
      .toLocaleLowerCase();
    return normalized ? normalized[0].toLocaleUpperCase() + normalized.slice(1) : "";
  }

  function statusLabel(status) {
    const key = String(status || "").trim();
    return STATUS_LABELS[key] || words(key) || "Unknown";
  }

  function safeExternalUrl(value) {
    try {
      const parsed = new URL(String(value || ""));
      return (parsed.protocol === "http:" || parsed.protocol === "https:") && !parsed.username && !parsed.password ? parsed.href : null;
    } catch (_error) {
      return null;
    }
  }

  function parseStrictNumber(value, options) {
    const settings = options && typeof options === "object" ? options : {};
    const label = typeof settings.label === "string" && settings.label ? settings.label : "number";
    const text = typeof value === "number" ? String(value) : typeof value === "string" ? value.trim() : "";
    if (!/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(text)) throw new Error(`${label} must be a number`);
    const number = Number(text);
    if (
      !Number.isFinite(number)
      || (settings.integer === true && !Number.isInteger(number))
      || (typeof settings.minimum === "number" && number < settings.minimum)
      || (typeof settings.maximum === "number" && number > settings.maximum)
    ) throw new Error(`${label} is outside allowed bounds`);
    return number;
  }

  function finiteNumber(value, fallback, minimum, maximum) {
    const number = typeof value === "number" && Number.isFinite(value) ? value : fallback;
    return Math.min(maximum, Math.max(minimum, number));
  }

  function stringList(value) {
    return Array.isArray(value) ? value.filter((item) => typeof item === "string").map((item) => item.trim()).filter(Boolean) : [];
  }

  function normalizeSettings(value) {
    const source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    const salary = source.minimum_salary && typeof source.minimum_salary === "object" ? source.minimum_salary : {};
    return {
      schema_version: 1,
      role_families: Array.isArray(source.role_families) ? source.role_families.map((row) => Object.assign({}, row)) : [],
      countries: Array.isArray(source.countries) ? source.countries.map((row) => Object.assign({}, row)) : [],
      minimum_salary: {
        amount: finiteNumber(salary.amount, SETTINGS_DEFAULTS.minimum_salary.amount, 0, 1e12),
        currency: typeof salary.currency === "string" && /^[A-Z]{3}$/.test(salary.currency) ? salary.currency : "EUR",
      },
      score_threshold: finiteNumber(source.score_threshold, 75, 0, 100),
      daily_submission_cap: finiteNumber(source.daily_submission_cap, 0, 0, 50),
      minimum_seniority: typeof source.minimum_seniority === "string" ? source.minimum_seniority : "entry",
      maximum_seniority: typeof source.maximum_seniority === "string" ? source.maximum_seniority : "mid",
      exclude_unknown_seniority: source.exclude_unknown_seniority !== false,
      include_keywords: stringList(source.include_keywords),
      exclude_keywords: stringList(source.exclude_keywords),
      excluded_employers: stringList(source.excluded_employers),
      prioritize_large_groups: source.prioritize_large_groups === true,
      large_group_employers: stringList(source.large_group_employers),
      vie_enabled: source.vie_enabled === true,
    };
  }

  function countMap(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter(([, count]) => Number.isInteger(count) && count >= 0));
  }

  function normalizeSummaries(value) {
    const source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    return {
      total_jobs: Number.isInteger(source.total_jobs) && source.total_jobs >= 0 ? source.total_jobs : 0,
      by_status: countMap(source.by_status),
      by_country: countMap(source.by_country),
      applied_today: Number.isInteger(source.applied_today) && source.applied_today >= 0 ? source.applied_today : 0,
    };
  }

  function splitList(value) {
    return String(value || "").split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
  }

  const api = { normalizeSettings, normalizeSummaries, parseStrictNumber, safeExternalUrl, splitList, statusLabel };
  root.JobDashboardHelpers = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
