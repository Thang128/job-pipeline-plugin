---
name: apply
description: This skill prepares or applies to tracked pipeline jobs when the user asks to apply, continue an application, retry a prepared job, or process the next eligible application.
---

Read [shared workflows](../../references/workflows.md), especially Application, Accounts, Authorization and Run ownership. Use the user's explicitly selected workspace, current settings, private facts and account registry. Acquire its run lock. Resolve named tracked targets exactly; without a target choose only the highest-ranked eligible job. Do not start another search unless requested.

Prepare and visually verify tailored documents. Reuse the user's configured signed-in profile, account registry and safe identity providers/password-manager flow. Never expose passwords or solve CAPTCHA. Fill only known truthful answers and read required values, declarations, consent, files and destination back from the actual page. Stop the affected job on unknown answers, unsupported verification, ambiguous consent, or unreliable state.

In `review_only`, stop at `docs_ready`. Submission requires this user's explicit auto-apply approval, positive remaining cap, current policy preflight, unchanged reviewed payload, and any host/browser confirmations. Respect existing authorization without inventing repeated permission gates. Recheck immediately before the final action.

Only record `applied` after visible post-action receipt/history or verified sent-message evidence. A click, upload, redirect or tool success is not evidence. Use policy-guarded `record_applied`, synchronize, release lock and report precise result. Retry missing browser/temporary portal errors as documented. Close only tabs opened for the target, except one recorded stateful handoff tab.
