# Publishing

This repository is a distributable marketplace/source package. GitHub distribution does not mean Anthropic/OpenAI accepted or certified it.

## Prepare a release

1. Review behavior, privacy boundary, MIT license, publisher, target repository and visibility.
2. Run the complete test suite and record exact results in `VALIDATION.md`.
3. Run `claude plugin validate --strict ./plugins/job-pipeline` and marketplace validation.
4. Inspect both manifests and shared skills. Register/load from a local test marketplace only when the account/environment permits it; do not confuse CLI registration with host loading.
5. Review and refresh `template-manifest.json` for runtime source changes and `release-files.json` for intentional public source changes. Never add an initialized workspace or blindly include untracked files.
6. Stage only reviewed inventory paths. Run `python tools/release.py --check-git`; it checks tracked/staged path membership and actual blob bytes against reviewed source, including forced additions. Runtime/cache paths are forbidden even if explicitly inventoried. The Git attributes preserve source bytes so template checksums survive checkout. Resolve every finding, then build into a new directory outside the source tree with `--output`.
7. Inspect generated repository ZIP and standalone `.plugin`, verify `MANIFEST.sha256`, extract to a fresh test directory and bootstrap a new empty workspace. Check review-only and cap 0.
8. Recheck the Git index after any edits, commit, then publish only the reviewed repository and data-free artifacts. Keep the private source project outside the repository.

The repository ZIP includes both catalogs. The `.plugin` archive includes only the standalone plugin at archive root. Bump both manifest versions on every release so hosts can recognize updates.

## Vendor directories

Claude supports distribution through a Git-hosted marketplace and has a separate directory submission process. Follow [Anthropic's publishing guide](https://code.claude.com/docs/en/plugins/publish); account eligibility, scans and review are separate from local validation.

OpenAI's portable format is documented in [Package your plugin](https://developers.openai.com/plugins/build/plugins). Official discovery uses a separate [plugin submission/review process](https://developers.openai.com/plugins/deploy/submission); desktop requirements may require its desktop-only route. Publisher verification and platform approval are not performed by a GitHub push.

Do not upload user CVs, private state, test receipts or credentials to a portal to demonstrate functionality. Use synthetic examples and describe which external capabilities users must connect themselves.

Before any official submission, verify current vendor requirements and complete missing metadata/host tests. v0.1 validation is reported explicitly; no directory listing is claimed.
