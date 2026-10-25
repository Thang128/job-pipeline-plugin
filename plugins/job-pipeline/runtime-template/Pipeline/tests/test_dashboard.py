import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest


PIPELINE_DIR = Path(__file__).resolve().parents[1]


def node_executable() -> str:
    executable = shutil.which("node.exe")
    if executable is None:
        pytest.skip("Node.js is unavailable")
    assert Path(executable).name.casefold() == "node.exe"
    return executable


class DashboardParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.inline_script_text = []
        self._inside_inline_script = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if tag == "script" and not attributes.get("src"):
            self._inside_inline_script = True

    def handle_endtag(self, tag):
        if tag == "script":
            self._inside_inline_script = False

    def handle_data(self, data):
        if self._inside_inline_script and data.strip():
            self.inline_script_text.append(data)


def parsed_dashboard():
    parser = DashboardParser()
    parser.feed((PIPELINE_DIR / "job-pipeline.html").read_text(encoding="utf-8"))
    return parser


def test_dashboard_shell_uses_external_assets_and_accessible_four_view_structure():
    parser = parsed_dashboard()
    by_id = {attrs["id"]: (tag, attrs) for tag, attrs in parser.elements if "id" in attrs}
    assert {"overview-view", "strategy-view", "jobs-view", "history-view"}.issubset(by_id)
    assert by_id["onboarding-banner"][1].get("role") == "status"
    assert by_id["settings-form"][0] == "form"
    scripts = [attrs.get("src") for tag, attrs in parser.elements if tag == "script"]
    styles = [attrs.get("href") for tag, attrs in parser.elements if tag == "link" and attrs.get("rel") == "stylesheet"]
    assert scripts == ["dashboard_helpers.js", "dashboard.js"]
    assert styles == ["dashboard.css"]
    assert parser.inline_script_text == []
    assert all(not key.casefold().startswith("on") for _, attrs in parser.elements for key in attrs)
    assert all("style" not in attrs for _, attrs in parser.elements)
    controls = [attrs for tag, attrs in parser.elements if tag in {"button", "input", "select", "textarea"}]
    label_targets = {attrs.get("for") for tag, attrs in parser.elements if tag == "label"}
    assert controls
    assert all(attrs.get("aria-label") or attrs.get("id") in label_targets for attrs in controls)
    nav_controls = {attrs.get("aria-controls") for tag, attrs in parser.elements if tag == "button" and attrs.get("aria-controls")}
    assert nav_controls == {"overview-view", "strategy-view", "jobs-view", "history-view"}


def test_dashboard_exposes_complete_settings_editor_contract():
    parser = parsed_dashboard()
    identifiers = {attrs["id"] for _, attrs in parser.elements if "id" in attrs}
    assert {
        "role-rows", "add-role", "country-rows", "add-country", "minimum-salary-amount",
        "minimum-salary-currency", "minimum-seniority", "maximum-seniority",
        "exclude-unknown-seniority",
        "include-keywords", "exclude-keywords", "excluded-employers",
        "prioritize-large-groups", "large-group-employers", "vie-enabled",
        "score-threshold", "daily-submission-cap", "save-settings", "action-status", "retry-export",
        "jobs-previous", "jobs-page", "jobs-next", "history-previous", "history-page", "history-next",
    }.issubset(identifiers)
    cap = next(attrs for tag, attrs in parser.elements if tag == "input" and attrs.get("id") == "daily-submission-cap")
    assert (cap.get("type"), cap.get("min"), cap.get("max")) == ("number", "0", "50")


def test_javascript_helpers_and_dom_rendering_are_safe_and_normalized():
    script = r'''
const assert = require('node:assert/strict');
const helpers = require('./dashboard_helpers.js');
const dashboard = require('./dashboard.js');

assert.equal(helpers.statusLabel('needs_user'), 'Needs user');
assert.equal(helpers.statusLabel('made_up_status'), 'Made up status');
assert.equal(helpers.safeExternalUrl('https://jobs.invalid/a'), 'https://jobs.invalid/a');
assert.equal(helpers.safeExternalUrl('http://jobs.invalid/a'), 'http://jobs.invalid/a');
assert.equal(helpers.safeExternalUrl('javascript:alert(1)'), null);
assert.equal(helpers.safeExternalUrl('file:///private'), null);
assert.equal(helpers.safeExternalUrl('https://user:password@jobs.invalid/a'), null);
assert.deepEqual(helpers.normalizeSummaries({total_jobs: 'bad', by_status: {needs_user: 2}}), {
  total_jobs: 0, by_status: {needs_user: 2}, by_country: {}, applied_today: 0
});
const normalized = helpers.normalizeSettings({daily_submission_cap: 99, role_families: 'bad'});
assert.equal(normalized.daily_submission_cap, 50);
assert.deepEqual(normalized.role_families, []);
assert.deepEqual(normalized.countries, []);

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.textContent = '';
    this.hidden = false; this.disabled = false; this.attributes = {}; this.value = '';
    this.checked = false; this.dataset = {}; this.listeners = {};
  }
  get firstChild() { return this.children[0] || null; }
  append(...items) { this.children.push(...items); }
  appendChild(item) { this.children.push(item); return item; }
  removeChild(item) { this.children.splice(this.children.indexOf(item), 1); return item; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelectorAll(selector) {
    const tag = selector.toUpperCase();
    return this.children.flatMap(child => [child, ...child.querySelectorAll(selector)]).filter(child => child.tagName === tag);
  }
}
const elements = new Map();
const doc = {
  createElement(tag) { return new Element(tag); },
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
  querySelectorAll(selector) { return selector === '[data-onboarding-required]' ? [this.getElementById('save-settings'), this.getElementById('apply-action')] : []; }
};
dashboard.renderOnboarding(doc, {ready: false, missing: ['master_cv', '<img src=x onerror=alert(1)>']});
assert.equal(doc.getElementById('onboarding-banner').hidden, false);
assert.equal(doc.getElementById('onboarding-banner').textContent, 'Onboarding required: master_cv, <img src=x onerror=alert(1)>');
assert.equal(doc.getElementById('save-settings').disabled, true);
assert.equal(doc.getElementById('apply-action').disabled, true);
const card = dashboard.createJobCard(doc, {title: '<script>alert(1)</script>', company: 'Safe Org', status: 'needs_user', source_url: 'javascript:alert(1)'});
assert.equal(card.children[0].textContent, '<script>alert(1)</script>');
assert.equal(card.children.some(child => child.tagName === 'A'), false);
dashboard.renderOnboarding(doc, {ready: true, missing: []});
assert.equal(doc.getElementById('onboarding-banner').hidden, true);
assert.equal(doc.getElementById('save-settings').disabled, false);
dashboard.renderSettings(doc, {
  role_families: [
    {id: 'one', label: 'One', keywords: ['one'], enabled: true, priority: 1},
    {id: 'two', label: 'Two', keywords: ['two'], enabled: true, priority: 2}
  ],
  countries: [
    {code: 'ZZ', label: 'Synthetic', enabled: true, priority: 1},
    {code: 'YY', label: 'Second', enabled: true, priority: 2}
  ]
});
const roleRows = doc.getElementById('role-rows');
const countryRows = doc.getElementById('country-rows');
assert.equal(roleRows.children.length, 2);
assert.equal(countryRows.children.length, 2);
const removeRole = roleRows.children[0].children.at(-1);
const removeCountry = countryRows.children[0].children.at(-1);
assert.equal(removeRole.tagName, 'BUTTON');
assert.equal(removeRole.attributes['aria-label'], 'Remove role family One');
assert.equal(removeCountry.tagName, 'BUTTON');
removeRole.listeners.click();
removeCountry.listeners.click();
assert.equal(roleRows.children.length, 1);
assert.equal(countryRows.children.length, 1);
assert.equal(roleRows.children[0].querySelectorAll('input')[3].value, '1');
assert.equal(countryRows.children[0].querySelectorAll('input')[2].value, '1');
const fullSettings = {
  schema_version: 1,
  role_families: [{id: 'risk', label: 'Risk', keywords: ['risk', 'pricing'], enabled: true, priority: 1}],
  countries: [{code: 'ZZ', label: 'Synthetic', enabled: true, priority: 1}],
  minimum_salary: {amount: 54321.5, currency: 'USD'},
  score_threshold: 82.5,
  daily_submission_cap: 7,
  minimum_seniority: 'mid', maximum_seniority: 'senior', exclude_unknown_seniority: false,
  include_keywords: ['pricing'], exclude_keywords: ['sales'], excluded_employers: ['Excluded Org'],
  prioritize_large_groups: true, large_group_employers: ['Large Org'], vie_enabled: true
};
dashboard.renderSettings(doc, fullSettings);
assert.deepEqual(dashboard.collectSettings(doc), fullSettings);
for (const [value, options, label] of [
  ['', {label: 'salary', minimum: 0, maximum: 1e12}, 'salary'],
  ['NaN', {label: 'score', minimum: 0, maximum: 100}, 'score'],
  ['1.5', {label: 'cap', minimum: 0, maximum: 50, integer: true}, 'cap'],
  ['', {label: 'priority', minimum: 1, maximum: 100, integer: true}, 'priority']
]) assert.throws(() => helpers.parseStrictNumber(value, options), new RegExp(label));
dashboard.renderPagination(doc, 'jobs', {page: 2, pages: 3, total: 120});
assert.equal(doc.getElementById('jobs-page').textContent, 'Page 2 of 3 · 120 total');
assert.equal(doc.getElementById('jobs-previous').disabled, false);
assert.equal(doc.getElementById('jobs-next').disabled, false);
dashboard.renderPagination(doc, 'jobs', {page: 3, pages: 3, total: 120});
assert.equal(doc.getElementById('jobs-next').disabled, true);
''';
    result = subprocess.run(
        [node_executable(), "-e", script],
        cwd=PIPELINE_DIR,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_javascript_central_action_state_handles_commit_pending_export_and_conflict_refresh():
    script = r'''
const assert = require('node:assert/strict');
require('./dashboard_helpers.js');
const dashboard = require('./dashboard.js');
class Element {
  constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.textContent = ''; this.value = ''; this.checked = false; this.hidden = false; this.disabled = false; this.dataset = {}; this.attributes = {}; }
  get firstChild() { return this.children[0] || null; }
  append(...items) { this.children.push(...items); }
  removeChild(item) { this.children.splice(this.children.indexOf(item), 1); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener() {}
  querySelectorAll(selector) { const tag = selector.toUpperCase(); return this.children.flatMap(child => [child, ...child.querySelectorAll(selector)]).filter(child => child.tagName === tag); }
}
const elements = new Map();
const doc = { createElement: tag => new Element(tag), getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); } };
const settings = {
  schema_version: 1, role_families: [], countries: [], minimum_salary: {amount: 0, currency: 'EUR'},
  score_threshold: 75, daily_submission_cap: 0, minimum_seniority: 'entry', maximum_seniority: 'mid',
  exclude_unknown_seniority: true, include_keywords: [], exclude_keywords: [], excluded_employers: [],
  prioritize_large_groups: false, large_group_employers: [], vie_enabled: false
};
(async () => {
  let historyRefreshes = 0;
  let fullRefreshes = 0;
  const pending = await dashboard.handleSettingsAction(
    doc,
    async () => ({version: 3, settings, export_status: 'pending'}),
    async () => { historyRefreshes += 1; },
    async () => { fullRefreshes += 1; }
  );
  assert.equal(pending.version, 3);
  assert.equal(doc.getElementById('settings-version').textContent, 'Version 3');
  assert.match(doc.getElementById('action-status').textContent, /saved.*export.*pending/i);
  assert.equal(doc.getElementById('retry-export').hidden, false);
  assert.equal(historyRefreshes, 1);

  await dashboard.handleSettingsAction(doc, async () => ({version: 3, settings, export_status: 'current'}), async () => { historyRefreshes += 1; });
  assert.equal(doc.getElementById('retry-export').hidden, true);
  assert.equal(historyRefreshes, 2);

  const conflict = new Error('conflict'); conflict.status = 409;
  const result = await dashboard.handleSettingsAction(
    doc,
    async () => { throw conflict; },
    async () => { historyRefreshes += 1; },
    async () => { fullRefreshes += 1; dashboard.applyCommittedSettings(doc, {version: 4, settings, export_status: 'current'}); }
  );
  assert.equal(result, null);
  assert.equal(fullRefreshes, 1);
  assert.equal(doc.getElementById('settings-version').textContent, 'Version 4');
  assert.match(doc.getElementById('action-status').textContent, /changed.*refreshed/i);
})().catch(error => { console.error(error); process.exit(1); });
''';
    result = subprocess.run(
        [node_executable(), "-e", script],
        cwd=PIPELINE_DIR,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_dashboard_assets_contain_no_embedded_candidate_state_or_unsafe_html_sink():
    assets = [PIPELINE_DIR / name for name in ("job-pipeline.html", "dashboard_helpers.js", "dashboard.js", "dashboard.css")]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in assets)
    forbidden = ("candidate_owner", "current employer", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")
    assert all(item.casefold() not in combined.casefold() for item in forbidden)
    assert "JOB PIPELINE STARTER" in combined
    assert "needs_user" in combined
    json.dumps(combined)
