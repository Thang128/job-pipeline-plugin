# Job Pipeline plugin

Seven shared skills: **setup**, **search**, **tailor**, **apply**, **vie**, **status**, and **explain**. Claude and Codex use the same skill files and Python engine.

Start with [installation](docs/INSTALLATION.md), then read [how it works](docs/HOW_IT_WORKS.md). Each user supplies their own CV, facts and preferences. No previous candidate's identity, authorization, salary expectation, account, or application history is included.

The plugin stays read-only. `scripts/create_workspace.py --destination "<new-workspace>"` copies a hash-checked runtime template to a new separate folder. It refuses existing directories, links, host caches and marketplace destinations. Initialization happens separately in that workspace.

New workspaces start in `review_only` with cap 0 and VIE disabled. CV-creator approval and application authorization are separate. Supported local execution requires Python, file/process access, and whichever external capabilities a requested workflow needs.

Documentation:

- [How it works](docs/HOW_IT_WORKS.md): decisions, sequence, states, ranking and limits.
- [Configuration](docs/CONFIGURATION.md): private inputs, settings and defaults.
- [Safety](docs/SAFETY.md) and [privacy](docs/PRIVACY.md): authority and data boundaries.
- [Connectors](docs/CONNECTORS.md): capabilities and fallbacks.
- [Review checklist](docs/REVIEW_CHECKLIST.md) and [validation](docs/VALIDATION.md): evidence and remaining checks.
- [Publishing](docs/PUBLISHING.md): GitHub and separate vendor directories.
- [Operator workflows](references/workflows.md): exact commands and APIs used by skills.
- [Changelog](docs/CHANGELOG.md).

No bundled MCP server, automatic hook, telemetry, hosted database, or recurring schedule. Read all code and instructions before installation. Account, model-provider, browser, and job-site rules still apply.
