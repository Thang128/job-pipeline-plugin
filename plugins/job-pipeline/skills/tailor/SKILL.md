---
name: tailor
description: This skill tailors a CV and cover letter for a tracked pipeline job when the user asks to prepare application documents or adapt their resume to a role.
---

Read [shared workflows](../../references/workflows.md), sections Workspace, Documents, and Run ownership. Resolve a tracked job, reload current eligibility and reviewed private sources, and acquire the run lock. Do not tailor an excluded employer or an ineligible posting.

Require an approved current private CV creator. Write a per-job overlay under `private/jobs/` and reword only approved editable zones with supported facts. Preserve identity, contact, employers, dates, education, qualifications and protected blocks. Keep each numeric claim bound to its matching evidence context and unit.

Use the workspace document builder. Inspect every preview page after automated checks; failed checks publish no final document. Record visual review against the exact files and hashes using the documented operation before `docs_ready`. Do not submit or upload documents merely because tailoring was requested.
