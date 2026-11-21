# Changelog

The highlights of each release. The About page in the local UI shows this file.

## 1.1.0 — 2026-11-21

- Local control panel: double-click `Foreman.cmd` to set up and watch your rooms in the browser.
- Setup checks for Python, Git and the agy CLI, with links to the official downloads.
- Add-a-project wizard that detects the stack and suggests verify commands and executor rules.
- Machine-readable `list`, `doctor` and `show` output, and a room-less `check` command.
- One-folder layout: `app`, `rooms` and `Foreman.cmd`.

## 1.0.0 — 2026-11-21

- Supervised relay: one task at a time from a manager agent to the agy CLI.
- Evidence for every round: the diff, the commands that really ran, scope and secret checks.
- Sandbox by default; verification runs only after the manager has read the diff.
- Verify-gated accept: one squash commit per task, never pushed.
- Pause, stop and crash recovery built in.
