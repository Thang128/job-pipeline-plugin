# Configuration

Public schemas live in `runtime-template/config/` and start blank. Initialization copies them under the user's workspace `private/`. Never populate the shared examples with a real candidate.

## Private inputs

| File | Contents |
|---|---|
| `private/candidate.yml` | Identity/contact, residence, employment status/current employer, explicit country authorization, languages, notice period, known truthful answers, career evidence. |
| `private/job-preferences.yml` | Target titles, role families, industries, seniority, ordered countries, work modes, salary/currency, keywords/exclusions, preferred company types and VIE flags. |
| `private/master-cv.pdf` or `.docx` | Registered source CV, copied without changing the user's original. PDF is the most direct visual-import route. |
| `private/master-profile.md` | Reviewed narrative/evidence; unresolved inconsistencies block completion. |
| `private/runtime.json` | Submission mode, configured browser profile, master path/hash, onboarding flags and auto-apply approval timestamp. |
| `private/accounts.json` | Identity-provider and employer-tenant status, login route and observations; never credentials. |
| `private/cv-creator/` | Source geometry, mapping, private template/assets, comparison pages and approval bindings. |
| `private/jobs/` | Per-job overlays. |
| `private/imports/` | Bounded job-import JSON. |

Country authorization is explicit: `countries` lists two-letter country codes and `status` is `authorized` or `sponsorship_required`. Coverage must be unambiguous. Residence/nationality is not a substitute. Add coverage before applying to a newly discovered VIE destination.

Employment status is `employed` or `not_employed`. An employed candidate must identify the current employer. Put known group aliases into employer exclusions; matching is normalized exact equality across company, employer and end employer, not automatic corporate-family discovery.

## Canonical SQLite settings

Public settings schema is **1**. It is independent from other personal pipeline schemas; do not import another database or copy settings blindly. The SQLite settings revision number is separate from the schema number.

| Field | Fresh default | Rule |
|---|---|---|
| `role_families` | Empty | Rows: `id`, `label`, nonempty `keywords`, `enabled`, `priority`. IDs use kebab-case. |
| `countries` | Empty | Rows: two-letter `code`, `label`, `enabled`, `priority`. |
| `minimum_salary` | Amount 0, currency EUR | Nonnegative annual amount; three uppercase currency letters. Setup requires deliberate user inputs. |
| `score_threshold` | 75 | Number from 0 through 100. |
| `daily_submission_cap` | 0 | Integer from 0 through 50; 0 disables submission. |
| `minimum_seniority` | `entry` | Inclusive lower bound. |
| `maximum_seniority` | `mid` | Inclusive upper bound, not below minimum. |
| `exclude_unknown_seniority` | `true` | Unknown seniority is excluded by default. |
| `include_keywords` | Empty | When nonempty, at least one must appear in title/description. |
| `exclude_keywords` | Empty | Any match excludes. |
| `excluded_employers` | Empty | Exact normalized identities, including known aliases. Checked against all employer fields. |
| `prioritize_large_groups` | `false` | Tie-break after country order. |
| `large_group_employers` | Empty | Explicit identities; no guessed company-size classification. |
| `vie_enabled` | `false` | Also requires both private VIE flags true. |

Priority values in each role/country list must be the unique contiguous integers 1 through N. Role priorities sort descending; country priorities sort ascending. Internal seniority order is `entry`, `mid`, `senior`, `lead_manager`, `director_executive`. Review broad mappings during onboarding.

`industries`, `work_modes`, and `preferred_company_types` are collected preferences for agent judgment. They are not independent deterministic ranking fields. In particular, setting a preferred company type does not automatically enable the explicit large-group tie-break.

Onboarding seeds SQLite from private preferences. Thereafter SQLite is authoritative. Editing YAML alone does not silently change live controls. The dashboard or versioned API changes settings; every save records history and rejects a stale expected revision.

Dashboard preview reports **settings matches**, not complete candidate eligibility. Its historical `eligible_count`/`eligible_job_ids` response fields describe settings-only filtering. The operator queue adds candidate authorization/current-employer/VIE checks and is the application-selection view.

```python
from pathlib import Path
from Pipeline.pipeline_store import PipelineStore

store = PipelineStore(Path.cwd() / "Pipeline/pipeline.db")
current = store.get_settings()
updated = current["settings"]
updated["daily_submission_cap"] = 0
store.save_settings(updated, expected_version=current["version"], reason="User disabled submissions")
store.export_state()
```

Only make user-requested changes. Do not raise the cap to make a blocked job proceed.

## Job records

Required: `id`, `title`, `company`, two-letter `country`, HTTP(S) `source_url`, `status`, 0–100 `score`, and `detected_seniority` (known enum or null). `job_description` must contain reliable description evidence before tailoring/applying, even though the storage schema permits missing descriptions during discovery.

Useful optional fields include source/requisition IDs, `employer`, `end_employer`, location/work mode, employment type, dates, `application_url`, salary amount/currency, `sponsorship_status`, `is_vie`, CV/letter paths, classification reason and blocker. See the exact allowlist in `Pipeline/pipeline_store.py`; unknown keys are rejected.

`salary_amount` is a reviewed annual guaranteed-base comparison amount in `salary_currency`; do not put hourly/monthly/raw allowance values there for ordinary roles. Store raw/conversion evidence privately when normalization was necessary. The engine has no FX service. `sponsorship_status` is `unknown`, `offered`, `not_offered`, or `not_required`; for a sponsorship-requiring candidate, use the last two only for an explicit restriction, not mere absence of a sponsorship sentence.

Record `tailoring_overlay` as a confined YAML file directly under `private/jobs/`. Document-review metadata binds CV, letter, master, creator, overlay, candidate facts and narrative profile hashes. Corrections to those inputs require fresh review, even when a correction is unrelated to the rendered CV. `portal_error_count` is 0–3 and `last_portal_error_run` prevents counting repeated failures within one run. A blocker has exactly `category` and concise `detail`.

Secrets are forbidden everywhere. Keep imported job/form content private and bounded. [Operator workflows](../references/workflows.md) show supported commands.
