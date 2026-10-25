# Installation and compatibility

## Prerequisites

Use Windows, Python 3.11+, PowerShell 5.1+, and a Claude/Codex environment with local file/process access. Python dependencies are pinned in `runtime-template/requirements.txt`. Job search and browser/email tools are separately supplied by the user's host/accounts.

CV rendering uses WeasyPrint's Python library and requires system Pango libraries. On Windows, install/configure those libraries following [WeasyPrint's official installation guide](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#windows). The guide also documents `WEASYPRINT_DLL_DIRECTORIES` when DLL discovery needs configuration. The plugin does not install OS packages or disable antivirus. `Install.ps1` runs a renderer smoke test and refuses to call an incomplete installation successful.

PDF is the direct master-CV route. DOCX visual import needs a supported isolated LibreOffice conversion. Word COM is disabled in this engine; having Word installed alone does not enable that route. If conversion is unavailable, export the original CV to PDF and register that instead. Fonts and substitutions must be reviewed with the user.

## Plugin loading

Claude Code:

```powershell
claude --plugin-dir ./plugins/job-pipeline
```

For ongoing installation, add this repository/checkout as a marketplace and install `job-pipeline@job-pipeline-tools`. Claude's `.claude-plugin/plugin.json` and OpenAI's portable root `plugin.json` point to the same `skills/` directory.

Codex marketplace registration:

```powershell
codex plugin marketplace add .
```

Then install/test the plugin in the desktop plugin directory. CLI marketplace registration and actual host loading are separate checks. Do not edit global configuration or install into another user's account without authorization. See [official OpenAI packaging](https://developers.openai.com/plugins/build/plugins) and [Claude plugin docs](https://code.claude.com/docs/en/plugins).

The `.plugin` archive contains the standalone plugin at archive root. The repository ZIP contains both marketplace catalogs plus the plugin under `plugins/job-pipeline/`. They contain no user state.

## Workspace setup

Run bootstrap from the checkout or installed plugin, using its actual path:

```powershell
python plugins/job-pipeline/scripts/create_workspace.py --destination ../my-job-search
Set-Location ../my-job-search
.\Install.ps1
```

If a PowerShell execution policy prevents running the script, follow your organization's policy or review and run the equivalent Python commands. Do not silently bypass security settings. The basic steps are creating `.venv`, installing pinned requirements there, running `Pipeline.initialize`, validating/exporting the store, and checking onboarding/privacy. See workspace `README.md`.

Open the exact new workspace in Claude Code or Codex local execution and run setup with your own CV/profile/preferences. Do not use a worktree/copy of mutable candidate state, run both hosts concurrently, or create a second authoritative tracker. Another host may use the same workspace later after the first run releases its lock.

macOS/Linux and instruction-only/cloud surfaces are not advertised as verified execution targets for v0.1. In particular, the inherited exact-handle run lock requires Windows. Such surfaces may explain the workflow; execution must stop when required capabilities are absent.

## Updates

Pull/install the reviewed plugin release. Do not overwrite an initialized workspace with a new template. v0.1 has no automatic private-state migration. Compare `WORKSPACE_MANIFEST.sha256` and release notes before a deliberate update; preserve private state and current authorization. Merely updating plugin instructions does not update workspace Python code.
