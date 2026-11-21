"""The Foreman UI: a local control panel in your browser.

Double-click Foreman.cmd, or run: python foreman_ui.py

It listens on 127.0.0.1 only, on a random port, and every request must carry a token that exists only
for this launch. Close it with Ctrl+C or the Quit button; it also exits after 10 idle minutes.
"""
import argparse
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("The Foreman needs Python 3.11 or newer: https://www.python.org/downloads/")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from foreman_ui.server import UIServer  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Open The Foreman's local control panel in your browser.")
    p.add_argument("--rooms-dir", type=Path, help="rooms folder (default: rooms/ next to room.py)")
    p.add_argument("--no-browser", action="store_true", help="print the address instead of opening it")
    p.add_argument("--idle-minutes", type=float, default=10, help="exit after this long without a visit")
    args = p.parse_args()
    server = UIServer(args.rooms_dir, args.idle_minutes)
    threading.Thread(target=server.watch_idle, daemon=True).start()
    print("The Foreman UI is running on this computer only.")
    print(f"Open: {server.url}")
    print("Keep this window open. Press Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print("The Foreman UI stopped.")
    return 0


def _run_windowless() -> int:
    """Under pythonw (Foreman.cmd) there is no console: record a crash in foreman_ui.log and say where."""
    try:
        return main()
    except Exception:
        log = Path(__file__).resolve().parent / "foreman_ui.log"
        log.write_text(traceback.format_exc(), encoding="utf-8")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f"The Foreman UI could not start.\nDetails: {log}",
                                             "The Foreman", 0x10)
        except (AttributeError, OSError):
            pass
        return 1


if __name__ == "__main__":
    sys.exit(_run_windowless() if sys.stderr is None else main())
