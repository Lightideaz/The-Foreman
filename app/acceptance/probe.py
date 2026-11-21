"""Acceptance probe: run a CLI with an exact argv list and record raw output plus facts.

Usage:
  python probe.py <case> <repo-dir> [--task-file F] [--preamble F|none] [--timeout SEC] -- <argv...>
The literal argument {PROMPT} in argv is replaced by preamble + task file text.
Output goes to runs/<case>/ next to the repo: stdout.bin, stderr.bin, stdout.json, stderr.txt, meta.json.
"""
import argparse
import csv
import io
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
WATCH = ("Antigravity.exe", "agy.exe", "language_server.exe")


def processes() -> list:
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    rows = [r for r in csv.reader(io.StringIO(out)) if r and r[0] in WATCH]
    return sorted({f"{r[0]}:{r[1]}" for r in rows})


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout.rstrip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("case")
    ap.add_argument("repo")
    ap.add_argument("--task-file")
    ap.add_argument("--preamble", default=str(HERE.parent / "prompts" / "gemini-preamble.md"))
    ap.add_argument("--timeout", type=int, default=720)
    if "--" not in sys.argv:
        ap.error("missing '--' before the command to run")
    split = sys.argv.index("--")
    a = ap.parse_args(sys.argv[1:split])
    argv = sys.argv[split + 1:]

    out_dir = HERE / "runs" / a.case
    out_dir.mkdir(parents=True, exist_ok=True)
    if a.task_file:
        task = Path(a.task_file).read_text(encoding="utf-8")
        pre = "" if a.preamble == "none" else Path(a.preamble).read_text(encoding="utf-8").rstrip() + "\n\n"
        prompt = pre + task
        (out_dir / "prompt.md").write_text(prompt, encoding="utf-8", newline="\n")
        argv = [prompt if x == "{PROMPT}" else x for x in argv]

    repo = Path(a.repo).resolve()
    meta = {
        "case": a.case,
        "repo": str(repo),
        "argv": ["{PROMPT}" if a.task_file and x == prompt else x for x in argv],
        "head_before": git(repo, "rev-parse", "HEAD"),
        "processes_before": processes(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    samples: list = []
    start = time.monotonic()
    with open(out_dir / "stdout.bin", "wb") as so, open(out_dir / "stderr.bin", "wb") as se:
        proc = subprocess.Popen(argv, cwd=repo, stdout=so, stderr=se, stdin=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)

        def sampler() -> None:
            for t in (5, 20):
                wait = t - (time.monotonic() - start)
                if wait > 0:
                    time.sleep(wait)
                if proc.poll() is None:
                    samples.append({"t": t, "processes": processes()})

        threading.Thread(target=sampler, daemon=True).start()
        try:
            code, timed_out = proc.wait(timeout=a.timeout), False
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            code, timed_out = proc.wait(), True

    meta.update({
        "exit_code": code,
        "timed_out": timed_out,
        "duration_s": round(time.monotonic() - start, 1),
        "process_samples": samples,
        "processes_after": processes(),
        "head_after": git(repo, "rev-parse", "HEAD"),
        "status_after": git(repo, "status", "--porcelain=v1", "--untracked-files=all").splitlines(),
        "diff_stat": git(repo, "diff", "--stat"),
    })
    raw = (out_dir / "stdout.bin").read_bytes()
    err = (out_dir / "stderr.bin").read_bytes().decode("utf-8", errors="replace")
    (out_dir / "stderr.txt").write_text(err, encoding="utf-8", newline="\n")
    try:
        doc = json.loads(raw.decode("utf-8"))
        (out_dir / "stdout.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                                             encoding="utf-8", newline="\n")
        meta["stdout_json_keys"] = sorted(doc) if isinstance(doc, dict) else type(doc).__name__
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        meta["stdout_json_error"] = str(exc)
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                       encoding="utf-8", newline="\n")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
