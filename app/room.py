"""The Foreman command-line entry point. See docs/OPERATOR-GUIDE.md."""
import sys

from connect_room.cli import main

raise SystemExit(main(sys.argv[1:]))
