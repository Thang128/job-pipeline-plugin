---
name: explain
description: This skill explains or audits job-pipeline behavior when the user asks how it works, why a job was ranked or blocked, what data is shared, or how to review its controls.
---

Read [shared workflows](../../references/workflows.md) and the package's `docs/HOW_IT_WORKS.md`, `docs/CONFIGURATION.md`, `docs/SAFETY.md`, and `docs/PRIVACY.md` as relevant. Explain the actual implemented path and its limits. For a specific job, inspect current canonical facts/settings and show gate and ranking reasons without exposing unrelated private content.

Distinguish agent judgments and browser observations from deterministic Python checks. Explain private workspace boundaries, optional connectors, review-only mode, explicit authorization, shared cap, evidence recording, run locking and transient retries. Never describe manifests or unit tests as proof that a browser action or vendor directory listing works. This skill does not authorize any external action or configuration change.
