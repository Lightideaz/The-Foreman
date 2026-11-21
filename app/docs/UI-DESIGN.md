# The Foreman — Local UI design

**Status:** approved and built · 2026-11-21 (fonts: option B)
**Goal:** make first-time setup and day-to-day monitoring easy for a new user, without moving any decision
away from the manager agent.

---

## 1. Scope

| The UI does | The UI never does |
|---|---|
| Check this machine's prerequisites and explain fixes | Download or run an installer |
| Add a project: pick a folder, detect the stack, prefill settings, `init`, `doctor` | Create, dispatch, verify, rework, accept or reject tasks |
| Show every room: open task, round, running time, last evidence | Edit task files, room state or git |
| Pause / Unpause a room, Stop a running round, open a report or task folder | Handle passwords, tokens or the agy sign-in |
| Copy the manager-agent instructions | Talk to the network (no CDN, no fonts, no telemetry) |

Dispatch, review and accept stay with the manager agent. The UI is a window onto `room.py`, not a second
manager.

## 2. Screens

Since 1.2 the look is a calm desktop-app style: a sidebar (views, the rooms with a status dot, a machine
status card), a greeting header, three stat cards with dot-matrix numbers, hairline panels, uppercase mono
status chips. Light mode is a warm off-white; dark mode is near-black with one lime accent. The page follows
the Windows app theme through `prefers-color-scheme`; the button beside the name in the sidebar cycles
System → Light → Dark. The choice is saved in `app/ui-prefs.json` (git-ignored), not in browser storage,
because every launch gets a new port and so a new browser origin; the server writes it onto `<html>` before
the page loads, so there is no flash. The home page also shows a
**Recent activity** feed (the newest events of every room, from `list --json`), and a floating pill with a
live timer while a round runs. Keys `1`–`4` switch views and `N` opens the new-room wizard.
(1.0–1.1 used the light-coffee identity from the design canvas; it was replaced on 2026-11-21.)

### 2.1 Setup (machine checks)

One card per prerequisite, each with a status chip and, when not OK, one plain-language fix line and a link.

| Check | How | Fix hint when missing |
|---|---|---|
| Python ≥ 3.11 | `sys.version_info` | Link to python.org/downloads |
| Git ≥ 2.40 | `git --version` | Link to git-scm.com/downloads |
| agy CLI | file exists at the configured path, `agy --version` | Link to the official Antigravity download page; "set the path" field |
| agy version vs tested | compare with `tested_version` | "Works, but untested with this version" (amber) |
| agy sign-in | **Not checked automatically** (every known check costs a model call) | "Open a terminal and run `agy` once to sign in" |
| Antigravity IDE running | existing process check | Info only: keep its agent idle during dispatches |

Links open in the default browser only when the user clicks them. Nothing is fetched by the UI.

### 2.2 Rooms dashboard (home)

Matches the canvas layout: left column *This machine* summary + espresso *How it works* card; centre a card
per room; right column *Last round evidence* and *Your manager agent* (Copy instructions).

Room card: name, repo path and base branch (mono), status chip (Idle · Running · Awaiting review · Paused ·
Problem), open task id and title, round *n of max*, running time, and these actions:

| Action | Shown when | Runs | Guard |
|---|---|---|---|
| Pause room / Unpause | always | `room.py --room X pause "<reason>"` / `unpause` | reason field |
| Stop round | a round is running | `room.py --room X stop --reason "<reason>"` | confirm dialog: "Partial work is snapshotted; nothing is deleted" |
| Open report | a round has `report.md` | `room.py --room X show T-NNNN` → rendered in a side panel as escaped text | read-only |
| Open task folder | a task exists | Windows Explorer on that folder | path must resolve inside the room folder |
| Run doctor | always | `room.py --room X doctor --json` | results shown with fix hints |

The page refreshes every 5 s while a round runs, every 30 s otherwise.

### 2.3 Add-a-project wizard (4 steps)

1. **Folder.** "Browse…" opens the native Windows folder dialog (Python's built-in `tkinter.filedialog`, run
   in a short child process). A text field accepts a pasted path as a fallback. The folder must be a git
   repository; the base branch defaults to the current branch and is editable. Room name defaults to the
   folder name (lower-case, `[a-z0-9-]`).
2. **Stack.** The UI reads marker files (never runs anything in the repo) and shows what it found, each
   suggestion with a checkbox, unticked items dropped:

   | Marker | Suggested `verify.default_commands` | Extra executor deny rules |
   |---|---|---|
   | `package.json` + lockfile (npm and others) | `<pm> run typecheck`, `<pm> test`, `<pm> run lint` — only scripts that exist | the detected package manager, if not already denied |
   | `pyproject.toml` / `requirements*.txt` / `setup.py` | `python -m pytest` if pytest is configured, else `python -m unittest discover` | `poetry`, `pipenv` when present |
   | `gradlew.bat` | `.\gradlew.bat test` | none (the sandbox has no network) |
   | `mvnw.cmd` / `pom.xml` | `.\mvnw.cmd -q test` / `mvn -q test` | none (the sandbox has no network) |
   | `Cargo.toml` | `cargo test` | `cargo (add\|install\|update\|publish)` |
   | `go.mod` | `go test ./...` | `go (get\|install\|mod)` |
   | `*.sln` / `*.csproj` | `dotnet test` | `dotnet (add\|restore\|tool\|new)` |
   | `composer.json` | `composer test` if scripted | `composer` |

   Suggested `policy.protected_paths` (checkboxes, off by default): CI config (`.github/`), lockfiles,
   migrations folders when found. Commands stay editable text; the user confirms every line.
3. **Review.** A plain summary: "The manager will verify with: …", "The executor may not run: …",
   "These paths need your approval to change: …". Free-text verify commands are the user's own
   configuration; the screen says they will run outside the sandbox when the manager calls `verify`.
4. **Create and check.** Runs `init` with the confirmed values, then `doctor --json`, and shows each result
   with a fix hint. A green result ends with "Copy agent instructions" for this room.

## 3. Technical design

### 3.1 Shape

```text
Foreman.cmd                   (tool root) double-click launcher: runs app\foreman_ui.py with pythonw
app/foreman_ui.py             entry: start server, open the default browser, print the URL
app/foreman_ui/
  server.py                   http.server on 127.0.0.1, token checks, JSON API, static files
  commands.py                 builds argv lists for room.py from UI actions (pure, tested)
  stacks.py + stacks.toml     stack detection from marker files (pure, tested)
  pick_folder.py              tkinter folder dialog, prints the chosen path, exits
  static/index.html app.css app.js   vanilla HTML/CSS/JS, tokens as CSS variables
```

Python standard library only. No build step, no npm, no framework.

`room.py` stays the single source of truth: every read and write goes through it as a child process
(`subprocess.run([sys.executable, room.py, …])`, argument list, never a shell). The UI does not import
`connect_room` internals and never writes room files itself.

The stack catalogue names package managers, so it lives in `foreman_ui/stacks.toml`, outside the folders
the project-neutrality test scans. The neutrality test is extended to the rest of `foreman_ui/`.

### 3.2 Small `room.py` additions (so the UI never parses text or edits TOML)

| Addition | Why |
|---|---|
| `list --json [--rooms-dir DIR]` | dashboard rows: each room's status and its latest round's evidence |
| `doctor --json`, each result with a `hint` | structured results; hints help the manager agent too |
| `check [--json]` (no `--room`) | machine checks for the Setup screen before any room exists |
| `show T-NNNN --json` | evidence card: commands, exit codes, warnings |
| `init --verify CMD` (repeatable), `--protect PATH` (repeatable), `--deny RULE` (repeatable) | the wizard's confirmed values written by `init` itself |

All additions are backwards compatible; the existing 77 tests stay unchanged. The UI passes full room
paths to `--room`, so `foreman_ui.py --rooms-dir DIR` can point it at another rooms folder.

### 3.3 Security model

The UI is a local control panel for one user on one machine. It is **not** reachable from the network and
offers no remote access.

| Threat | Control |
|---|---|
| Access from another machine | Server binds `127.0.0.1` only, never `0.0.0.0`. No option to change it. |
| Another local program or web page calling the API | Random port (OS-assigned) and a 32-byte per-launch token (`secrets.token_urlsafe`). The browser is opened at `/?t=<token>`. Every request, page and API, must carry the token (query on the page and static files, `X-Foreman-Token` header on API calls), compared with `hmac.compare_digest`. A missing or wrong token → 403 with no body. The token stays in the address bar so a reload works; it is worthless once the UI exits, and `Referrer-Policy: no-referrer` keeps it out of outgoing links. No cookies: they would be shared with every other port on 127.0.0.1. |
| A malicious web page in the same browser (CSRF, DNS rebinding) | No CORS headers at all. API calls need the custom header, which forces a preflight that is refused. `Host` must equal `127.0.0.1:<port>`; `Origin`, when present, must equal the UI's own origin. |
| Injected markup from reports or task files | Agent output is shown as text (`textContent`), never as HTML. Strict CSP: `default-src 'none'`, scripts, styles and fetches from `'self'` only, `frame-ancestors 'none'`. |
| Command injection | Fixed allowlist of `room.py` sub-commands (`check list status show doctor init pause unpause stop`). Argument lists, no shell. Room names match `^[a-z0-9][a-z0-9-]{0,39}$`. "Open folder" accepts only paths that resolve inside that room's folder. |
| Leaking the token | Never logged, never written to disk, gone when the process exits. The console prints the URL once. |
| Forgotten server | Exits on Ctrl+C, on the Quit button, or 10 minutes after the last page heartbeat. |
| Secrets | The UI never asks for, stores or displays passwords, tokens or API keys. agy sign-in happens in agy. |

### 3.4 Fonts

A local UI must not call a font service, so the fonts ship in `foreman_ui/static/fonts/`, all under the SIL
Open Font License 1.1 (license files beside them), about 115 KB in total:

| Font | Use | Source |
|---|---|---|
| Inter (variable, Latin) | all UI text | `@fontsource-variable/inter` 5.1.0 |
| IBM Plex Mono 400/500 (Latin-1) | paths, commands, chips | `@ibm/plex-mono` 2.5.0 |
| Doto 700 (Latin) | the big stat numbers and the round timer | `@fontsource/doto` 5.1.0 |

Characters outside these subsets fall back to Segoe UI / Consolas.

## 4. Tests (non-UI logic)

- `stacks.py`: each marker set → expected commands and deny rules; mixed repos; missing scripts not suggested;
  nothing executed in the repo.
- `commands.py`: each UI action → exact argv; room-name and path validation; refuses sub-commands outside the
  allowlist.
- `server.py` request checks: token required on every route, constant-time compare, wrong `Host` /
  foreign `Origin` refused, no CORS headers in any response, open-folder path containment.
- New `room.py` flags: `--json` outputs parse; `init --verify/--protect/--deny` write valid `room.toml` and
  settings that `doctor` accepts.

## 5. Out of scope for v1

Task authoring, any write to tasks, multi-user access, remote access, auto-update, an installer,
non-Windows support.

## 6. Decisions (2026-11-21)

1. Design approved.
2. Fonts: option B (bundled IBM Plex subsets).
3. Folder dialog: `tkinter` in a child process; a pasted path is the fallback.
4. 1.2: new look (sidebar layout, light and dark following Windows), replacing the light-coffee identity.
