---
name: vie
description: This skill handles tracked VIE applications and Business France routes when the user asks to prepare, apply, resume, or retry a VIE mission in the job pipeline.
---

Read [shared workflows](../../references/workflows.md), including Application and VIE. Use the same user workspace, run lock, account registry, reviewed documents and daily allowance as ordinary applications.

Require explicit private `vie.enabled` and `vie.eligible` and current settings enabling VIE. Never infer age, nationality, eligibility, language, account or authorization. Enabled VIE may target any known country except Belgium and statutory allowances are exempt from the ordinary salary floor. All remaining guards still apply.

Authenticate through the user's existing normal profile and available approved verification capabilities; do not expose passwords or persist codes. Require reliable field/upload readback. External redirects change the route and require current destination/payload review; they are not submissions.

In review mode prepare only. Authorized auto-apply remains bounded by the shared cap and platform controls. Record success only after visibly confirmed Business France delivery or actual external-ATS receipt, using the same guarded recording API. Preserve exact blockers and clean up only target tabs.
