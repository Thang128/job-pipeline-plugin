import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "job-pipeline" / "runtime-template"))
sys.path.insert(0, str(ROOT / "plugins" / "job-pipeline" / "scripts"))
