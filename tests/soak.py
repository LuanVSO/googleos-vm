#!/usr/bin/env python3
"""Boot the VM N times and require sys.boot_completed=1 and no EGL abort each time."""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERIAL = ROOT / "work" / "build" / "serial.log"


def main() -> int:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 240
    failures = 0
    for i in range(1, runs + 1):
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "run" / "launch.py")],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline, ok = time.time() + timeout, False
        while time.time() < deadline and proc.poll() is None:
            time.sleep(5)
            text = SERIAL.read_text(errors="replace") if SERIAL.exists() else ""
            if "boot_completed=1" in text and "no suitable EGLConfig" not in text:
                ok = True
                break
        try:
            proc.stdin.write(b"quit\n")
            proc.stdin.close()
            proc.wait(30)
        except Exception:
            proc.kill()
        print(f"run {i}: {'ok' if ok else 'FAIL'}")
        failures += not ok
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
