from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Pipeline.io_utils import confined_path
from Pipeline.pipeline_store import PipelineStore, StoreError
from Pipeline.policy_guard import (
    PolicyError,
    _load_candidate,
    application_policy_lock,
    enforce_current_employer_exclusion,
)


def synchronize(project_root: Path) -> None:
    try:
        root = confined_path(project_root, project_root)
        with application_policy_lock(root):
            store = PipelineStore(confined_path(root, root / "Pipeline" / "pipeline.db"))
            store.validate_integrity()
            state = store.load_state()
            candidate = _load_candidate(root)
            proposed = copy.deepcopy(state)
            changed = enforce_current_employer_exclusion(proposed, candidate)
            if changed:
                by_id = {job["id"]: job for job in state["jobs"]}
                expected = [
                    {
                        "id": identifier,
                        "status": by_id[identifier]["status"],
                        "fingerprint": store.job_fingerprint(by_id[identifier]),
                    }
                    for identifier in changed
                ]
                store.skip_jobs_if_unchanged(expected)
            store.load_state()
            store.validate_integrity()
            store.export_state()
    except PolicyError:
        raise
    except (StoreError, OSError, ValueError) as error:
        raise PolicyError("canonical state is invalid") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and export canonical pipeline state.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    arguments = parser.parse_args()
    try:
        synchronize(arguments.root)
        print(json.dumps({"ok": True}, sort_keys=True, separators=(",", ":")))
        return 0
    except (PolicyError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error), "ok": False}, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
