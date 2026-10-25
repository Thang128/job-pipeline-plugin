# Job Pipeline workspace adapter

Read `AGENTS.md`, `Pipeline/PIPELINE.md` and `WORKFLOWS.md` before job work. These adapters share one engine and state with Codex. Use this exact local workspace, its `.venv` interpreter, current SQLite settings, private facts and account registry.

Run onboarding status first. If not ready, use setup only. Acquire the shared bounded run lock before mutable/external work and retain it across browser calls. Never run Claude and Codex concurrently against this workspace.

Load the installed Job Pipeline skill for the requested action; `WORKFLOWS.md` contains the same commands if skill discovery is unavailable. Respect host/browser controls and missing capabilities. Initial `review_only` and cap 0 never submit. Onboarding/CV-creator approval is distinct from user-owned application authorization.

Use truthful private evidence, current eligibility, approved creator and exact document-review hashes. Never invent answers, read passwords, solve CAPTCHA, save OTPs or mark applied without observed confirmation and the guarded recording API. No automatic schedule or external action follows from opening this folder.
