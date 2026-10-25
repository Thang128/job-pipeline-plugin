# Capabilities and fallbacks

No custom MCP server or service credentials are bundled. The host supplies tools, which the assistant discovers at runtime. Installation does not grant external access.

| Capability | Needed for | Fallback or failure |
|---|---|---|
| Local files + Python/process execution | Workspace, SQLite and documents | Required for execution. An instruction-only/cloud surface may explain but must not pretend to run local commands. |
| Persistent or explicitly bounded process control | Hold run ownership across external tool calls | Required for browser/application work. Stop if the lock holder cannot stay alive or be checked/released. |
| Indeed search/details | Optional preferred search source | Supported web search plus reliable employer/job pages. |
| Web/page access | Reliable descriptions and source evidence | Without reliable evidence, do not tailor or apply. |
| Controllable normal browser profile | Portal applications and account reuse | Keep prepared jobs retryable as `transient_no_browser`; do not substitute a clean profile. |
| Native computer control | Some browser password-manager UI or upload pickers | Stop affected step if supported DOM/browser controls cannot complete it safely. |
| Authorized email search/read | Optional registration verification and sent-state proof | Record precise account/verification blocker. Do not assume a specific host lacks or has Gmail. |
| Authorized email send | Optional official recruiter-email application | Prepare only unless authorized; verify sent state before recording application. |

Tool names differ across Claude and Codex. Prefer a purpose-built connected capability; use browser/computer control only when needed and supported. A browser login does not authorize a connector API.

Known portal patterns are starting hypotheses, not permanent universal behavior: Workday may need real coordinate clicks; uploads may report success through page text after clearing an input; redirects may leave the current tab. Verify actual state. Never query password values during DOM readback. Probe an existing browser connection before starting any launcher, then confirm the user's configured profile.

Some hosts redact credential-field DOM readback. A redacted value is unavailable evidence, not proof of a mismatch. Use supported visual confirmation for non-secret fields or hand off; never try to unmask credentials or bypass an approval rejection.

No identity provider, account, nationality, salary, country market or email inbox is a public default. The user's private configuration supplies these.
