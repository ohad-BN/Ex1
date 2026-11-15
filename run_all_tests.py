from __future__ import annotations

import subprocess
import sys


def main() -> int:
    """Execute all ClassPulse automated test suites (Suites 1-8 + Multi-Course Portals)."""
    cmd = [sys.executable, "-m", "pytest", "-v"]
    proc = subprocess.run(cmd)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
