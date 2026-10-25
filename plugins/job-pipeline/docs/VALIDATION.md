# Validation

Version 0.1.0, verified on Windows on 25 October 2026. **666 distinct test cases have passing coverage.** Tests used a fresh Python 3.11 environment with the pinned public dependencies and isolated synthetic workspaces.

## Test evidence

| Group | Result |
|---|---|
| New distribution/bootstrap/operator regressions plus inherited settings/store | 154 passed |
| CV importer and document builder | 194 passed |
| Onboarding, policy, exact run locking and dashboard synchronization | Initial shard: 151 passed, 1 skipped |
| Privacy scanner and dashboard/client/server | 166 passed |
| Entire onboarding module after correcting the skipped fixture | 64 passed, no skips |

The initial skip was a fixture trying to create a link where its copied Documents directory already existed. It was changed to expose that failure, repaired to move only its own confined fixture directory, and rerun. The final 64-case onboarding run replaces its earlier 63 passing/1 skipped results. Thus all 666 collected cases have passed; duplicate reruns are not added to that total.

The tests cover settings/version validation, queue order, sponsorship silence versus refusal, VIE exceptions, employer/end-employer exclusions, current eligibility rechecks, caps and confirmation, immutable applied rows, creator/source drift, exact document/overlay/candidate/profile hashes, bounded owner handshake, cross-process exclusion/release, transient failures per run, private/cache inventory rejection and actual staged Git blob checks.

## Additional verification

- Strict installed Claude CLI plugin and marketplace validation passed.
- A fresh virtual environment installed every pinned package from PyPI. Dependency imports and real WeasyPrint HTML-to-PDF rendering passed with system libraries available.
- Generated repository ZIP and standalone `.plugin` were inspected through their content manifests. Fresh extraction, template checksum verification, separate workspace bootstrap, empty initialization, store validation, expected incomplete onboarding, review-only mode, cap 0 and installed blank privacy scan passed.
- Rebuilt archive's bounded holder, fresh owner handshake, rejection of a second owner and clean release passed across processes.
- Public release uses an explicit 95-file inventory. Private/runtime/cache paths cannot be allowlisted. Git staging is checked for both unexpected paths and differences between staged blobs and reviewed source. Source attributes preserve checksum bytes across checkout.
- Fresh independent source review found two Important issues (input-review drift and release/index boundaries) plus dashboard labeling clarity; all were addressed. Integration exposed the Windows locked-file reader problem; a holder handshake fixed it without weakening the exclusive lock. New regressions were observed failing before the fixes and passing afterward.

Some initial executions encountered sandbox temporary-directory/hardlink restrictions. Synthetic Windows tests were run with normal file/process access instead of weakening atomic writes or worker isolation. An initial broad run was interrupted to apply review findings; only completed final checks count above.

No real job application, portal account creation, email send or recurring task was performed. No private originating candidate data or operational database was imported. Updating the test fixture and documentation did not change application behavior after the final code checks.

## Unverified or intentionally limited

- Actual plugin installation/loading in signed-in Claude/Codex clients was not performed; package format/CLI validation and runtime execution are distinct evidence.
- Arbitrary live job-site behavior, account verification and real submissions are not proven by synthetic tests.
- macOS/Linux execution is unverified; the inherited full run-lock backend requires Windows.
- GitHub distribution does not constitute official vendor-directory acceptance. Publisher metadata and the vendors' current review processes remain separate.
- Applied rows remain protected from ordinary rewrites, including later-outcome changes. Agent judgments, deduplication and live-source observations remain external to deterministic validation. UTC defines the daily cap.

Artifact content hashes are generated in `MANIFEST.sha256` inside each archive; `SHA256SUMS` covers the final archive files. A passing scan does not make any initialized user workspace safe to publish.
