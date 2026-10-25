# Private Job Pipeline workspace

This folder becomes your private working pipeline after initialization. It is separate from the public plugin. Use Claude Code or Codex with this exact local folder. Do not publish it or copy it into a second active worktree.

## Install

Use Python 3.11+, PowerShell 5.1+ and WeasyPrint's required Pango libraries. Review `Install.ps1`, then run it. It creates `.venv`, installs pinned requirements, smoke-tests rendering and initializes empty private state. It makes no application or schedule.

Equivalent manual steps, after installing the required system libraries:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip --isolated install -r requirements.txt
.\.venv\Scripts\python.exe -B -E -s -m Pipeline.initialize
.\.venv\Scripts\python.exe -B -E -s -m Pipeline.pipeline_store validate
.\.venv\Scripts\python.exe -B -E -s -m Pipeline.pipeline_store export
.\.venv\Scripts\python.exe -B -E -s -m Pipeline.onboarding status
.\.venv\Scripts\python.exe -B -E -s -m Pipeline.privacy_scan --installed .
```

Ask the installed plugin to set up your own CV, factual profile, preferences and approved private CV creator. Follow [WORKFLOWS.md](WORKFLOWS.md). Start mode is `review_only`, cap 0. Onboarding approval does not authorize applications. Read [SAFETY.md](SAFETY.md) and [PRIVACY.md](PRIVACY.md).

## State and dashboard

`Pipeline/pipeline.db` is authoritative. Private facts are under `private/`; generated documents are under `Documents/generated/`. Never edit `pipeline_state.json`. Use validated APIs and synchronize after writes.

```powershell
.\.venv\Scripts\python.exe -B -E -s -m Pipeline.operations status
.\.venv\Scripts\python.exe -B -E -s -m Pipeline.operations queue
.\Start-Dashboard.ps1
```

Only one run across Claude/Codex may operate this workspace at once. The bounded run holder in `WORKFLOWS.md` keeps ownership across browser calls. Stop when ownership ends. Other execution surfaces must demonstrate required local capabilities; the inherited full run-lock implementation requires Windows.

`WORKSPACE_MANIFEST.sha256` describes the original clean code copied during bootstrap. Private runtime files are intentionally outside that manifest. Do not overwrite this workspace during plugin updates; v0.1 provides no automatic state migration.
