"""Windows startup launcher for RemotePC."""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from remotepc.__main__ import main

raise SystemExit(main(["--gui", "--autostart", *sys.argv[1:]]))
