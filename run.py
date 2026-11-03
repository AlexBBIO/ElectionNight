"""Entry point that works from inside this folder, whatever the folder or repo is called.

  python3 run.py            # live dashboard on http://127.0.0.1:8765
  python3 run.py --demo     # synthetic night
  python3 run.py --once     # one poll, print table
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if "election_night" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "election_night", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["election_night"] = pkg
    spec.loader.exec_module(pkg)

from election_night import server  # noqa: E402

if __name__ == "__main__":
    server.main()
