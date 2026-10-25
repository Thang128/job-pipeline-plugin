# Privacy

The release contains code, instructions, blank schemas and synthetic tests. It contains no real candidate CV/profile, account history, job history, browser session, application evidence, or standing application authorization.

Your separate workspace contains private material: `private/`, `Pipeline/pipeline.db` and its journals, `Pipeline/pipeline_state.json`, `Documents/generated/`, CV-creator comparisons, screenshots, logs, backups and imported forms/jobs. Keep the entire working folder private. Share the clean release archive, not an initialized workspace.

Passwords, API/OAuth credentials, cookies, OTPs, recovery codes and private keys must never enter any project file or chat transcript. The account registry stores status and routes only. The local run-ownership token is a coordination nonce, not an account credential; keep it within the active workspace/session.

The plugin has no hosted backend or telemetry. SQLite and documents stay on the selected filesystem until a host/tool reads or transmits them. **Local storage does not mean local model processing.** Content read by Claude/OpenAI is processed according to the user's product, account and plan. Browser/email applications send reviewed data to employers. Connected services have their own data handling and permission controls.

Reports should use counts and exact blocker categories without dumping full profiles, forms, receipts or unrelated messages. Support requests should contain sanitized error categories and reproducible synthetic cases. Do not upload private databases or screenshots for routine troubleshooting.

Release tooling uses `release-files.json`, refuses unexpected/private files, checks known sensitive patterns and blank defaults, and generates content checksums. Bootstrap verifies a separate runtime-template manifest. These checks catch classes of accidental disclosure; they cannot prove that arbitrary prose contains no private fact. Manual review remains required before publishing.

Installed workspace check:

```powershell
.\.venv\Scripts\python.exe -B -E -s -m Pipeline.privacy_scan --installed .
```

A passing scan never makes an initialized workspace safe to publish. Do not repackage `private/`, database, generated documents, `.venv/` or browser data. The source release and workspace are intentionally separate.
