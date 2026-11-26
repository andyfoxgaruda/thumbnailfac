"""Run the existing server directly in the process owned by Task Scheduler."""

import os
import runpy
import site
import socket
import sys
from pathlib import Path


def main():
    service, photocraft_directory = sys.argv[1:]
    if service not in {"server.py", "portal.py"}:
        raise SystemExit(2)
    # A manually started copy may already own the existing loopback port.
    with socket.socket() as listener:
        listener.settimeout(1)
        if listener.connect_ex(("127.0.0.1", 8000 if service == "server.py" else 8001)) == 0:
            return
    root = Path(__file__).resolve().parent
    virtualenv = root / ".venv"

    # Using the base pythonw.exe avoids the venv redirector process. Activate
    # the project's packages in-process so Scheduler can stop the real server.
    sys.prefix = sys.exec_prefix = str(virtualenv)
    site.addsitedir(str(virtualenv / "Lib" / "site-packages"))
    os.chdir(root)
    os.environ["PATH"] = os.pathsep.join((photocraft_directory, os.environ.get("PATH", "")))
    sys.argv = [str(root / service)]

    with open(os.devnull, "w", encoding="utf-8") as output:
        sys.stdout = sys.stderr = output
        runpy.run_path(sys.argv[0], run_name="__main__")


if __name__ == "__main__":
    main()
