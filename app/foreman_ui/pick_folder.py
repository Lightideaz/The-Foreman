"""Show the native folder dialog and print the chosen folder (empty output = cancelled).

Runs as its own short process so Tk never shares a thread with the web server. Exit code 3 means
tkinter is not available; the UI then falls back to a pasted path.
"""
import sys


def main() -> int:
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        return 3
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        folder = filedialog.askdirectory(parent=root, title="Choose your project folder", mustexist=True)
    finally:
        root.destroy()
    if folder:
        sys.stdout.reconfigure(encoding="utf-8")
        print(folder.replace("/", "\\"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
