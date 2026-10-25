from __future__ import annotations

import argparse
from pathlib import Path

from Pipeline.io_utils import confined_mkdir, confined_path, publish_bytes_no_overwrite
from Pipeline.pipeline_store import PipelineStore


PRIVATE_EXAMPLES = {
    "candidate.example.yml": "candidate.yml",
    "job-preferences.example.yml": "job-preferences.yml",
    "accounts.example.json": "accounts.json",
    "runtime.example.json": "runtime.json",
}


def initialize_project(project_root: Path) -> None:
    """Create only blank private state; preserve an existing friend setup."""
    project_root = confined_path(project_root, project_root)
    private = confined_mkdir(project_root, project_root / "private")
    for name in ("cv-creator", "imports", "jobs"):
        confined_mkdir(project_root, private / name)
    confined_mkdir(project_root, project_root / "Documents" / "generated")
    for source_name, destination_name in PRIVATE_EXAMPLES.items():
        source = confined_path(project_root, project_root / "config" / source_name)
        destination = confined_path(project_root, private / destination_name)
        if not destination.exists():
            try:
                publish_bytes_no_overwrite(project_root, destination, source.read_bytes())
            except FileExistsError:
                pass
    database = confined_path(project_root, project_root / "Pipeline" / "pipeline.db")
    store = PipelineStore(database)
    store.initialize()
    store.export_state()


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize private job-pipeline state.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    arguments = parser.parse_args()
    initialize_project(arguments.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
