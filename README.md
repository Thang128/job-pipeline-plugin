# Job Pipeline

A reusable local job-search and application workflow for Claude and Codex. One engine, one private workspace, and the same rules on both hosts.

**Version:** 0.1.0. **Execution:** Windows-first. **Default:** `review_only`, daily submission cap **0**. Installing the plugin creates no schedule and submits nothing.

See the [plugin guide](plugins/job-pipeline/README.md), [how it works](plugins/job-pipeline/docs/HOW_IT_WORKS.md), and [review checklist](plugins/job-pipeline/docs/REVIEW_CHECKLIST.md).

## Install the plugin

From a trusted checkout, Claude Code can load it for a session:

```powershell
claude --plugin-dir ./plugins/job-pipeline
```

Or add this repository as a marketplace:

```powershell
claude plugin marketplace add .
claude plugin install job-pipeline@job-pipeline-tools
```

For Codex, add the local marketplace source and install the plugin from the desktop plugin directory:

```powershell
codex plugin marketplace add .
```

These commands register instructions and code. Browser, job-search, email, and filesystem capabilities still come from the host and the user's own connected accounts. See [installation and compatibility](plugins/job-pipeline/docs/INSTALLATION.md). A parsed manifest does not prove a host loaded the plugin; check the [validation report](plugins/job-pipeline/docs/VALIDATION.md).

## Create a separate workspace

Use Python 3.11 or newer. Choose a directory outside the plugin or marketplace checkout:

```powershell
python plugins/job-pipeline/scripts/create_workspace.py --destination ../my-job-search
Set-Location ../my-job-search
.\Install.ps1
```

Then ask the installed plugin: **“Set up my job pipeline. Ask me for my own master CV and preferences.”**

The setup command refuses existing destinations and checks runtime-template hashes before copying. It does not install dependencies, initialize private data, or contact external services; `Install.ps1` is a separate reviewed step.

Your workspace contains your CV, private profile, job database, accounts registry and generated documents. Keep it outside GitHub. The public repository contains blank configuration and synthetic tests only. See [privacy](plugins/job-pipeline/docs/PRIVACY.md).

## What it does

- Sets up your own factual profile, country/work-authorization preferences, and approved CV creator.
- Searches supported sources, checks duplicates, explains fit and orders an eligible queue.
- Tailors a CV and cover letter from supported facts; requires document checks and visual review.
- Prepares portal applications and records exact blockers instead of guessing.
- Optionally submits after your separate authorization and a nonzero cap, subject to host controls.
- Records `applied` only with visible confirmation metadata and transactional checks.
- Shares one SQLite database and run lock across Claude and Codex.

It is an agent workflow with local validation. Python does not browse or submit independently, score career fit semantically, prove browser observations, or bypass site security. Keyword role classification and other limits are documented in [how it works](plugins/job-pipeline/docs/HOW_IT_WORKS.md).

## Development and release

```powershell
python -B -m pytest -q -p no:cacheprovider
claude plugin validate --strict ./plugins/job-pipeline
claude plugin validate --strict .
python tools/release.py
python tools/release.py --output ../job-pipeline-release
```

Tests require the dependencies in `plugins/job-pipeline/runtime-template/requirements.txt`. Some Windows security/process tests need normal filesystem and process privileges. Never weaken atomic writes or isolation to make a sandbox test pass.

Release packaging uses an explicit inventory, private-data checks and checksums. [Publishing](plugins/job-pipeline/docs/PUBLISHING.md) distinguishes GitHub distribution from vendor-directory approval. [MIT license](LICENSE).
