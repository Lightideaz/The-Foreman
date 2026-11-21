# The Foreman — Design

**Version:** 1.2 · 2026-11-19
**Status:**
- Architecture approved.
- Phase 0 complete: every former *[Phase 0]* item is confirmed and applied here. Evidence: `acceptance/RESULTS.md`.
- Phases 1–4 built and tested. v1.2 records the implementation details that refine this design (§4, §8, §9, §10, §12).
**Companion docs:** `IMPLEMENTATION-PLAN.md`, `ACCEPTANCE-TESTS.md`, `OPERATOR-GUIDE.md`

---

## 0. Purpose and principles

The Foreman removes manual copy-paste between **Claude Code** (manager and technical lead) and the **Antigravity CLI `agy`** (Gemini, the implementing agent). It is a small command-line utility that Claude calls when Claude decides to delegate. It is not a manager, not a daemon, not a file watcher and not a third agent.

| # | Principle | Meaning |
|---|-----------|---------|
| P1 | **Claude decides** | Nothing is dispatched unless Claude runs `room.py dispatch`. Files never trigger anything. |
| P2 | **Evidence over claims** | Git state and verification that The Foreman runs itself are the record. Gemini's report is a claim. |
| P3 | **One writer at a time** | One open task per repository. No concurrent agent edits. |
| P4 | **Nothing is destroyed automatically** | No auto-merge, no push, no reset, no clean. Rejected work stays on its branch; leftovers are stashed, never deleted. |
| P5 | **No bypassed safeguards** | No `--dangerously-skip-permissions`, no skipped git hooks. Agent permissions are an allowlist. |
| P6 | **Small and inspectable** | Python 3.11+ standard library only. Plain files: TOML, JSON, JSONL, Markdown. |
| P7 | **Honest boundaries** | Operational safeguards are labelled as such and never presented as security boundaries (§11). |

---

## 1. Verified context

### 1.1 Machine facts (verified 2026-11-19)

| Item | Value |
|------|-------|
| Claude Code | 2.1.326, bundled with the Claude desktop app, not on PATH. **v1 never launches Claude**, so its path is irrelevant. |
| Antigravity CLI | `agy` 1.5.9 at `%USERPROFILE%\.gemini\bin\agy.exe`, not on PATH |
| Gemini CLI (`gemini`) | Not installed. Not used. |
| Python | 3.12.4 (`tomllib` available) |
| Git | 2.55.0.windows.2 |
| Tool home | `C:\Tools\the-foreman` (project-neutral; §3) |
| Projects | One room per project under `rooms/` (§3.3). Each room's specifics live in `rooms/<room>/PROJECT-NOTES.md`. |

Each project must have exactly **one** working copy that agents use. Its room names it.

### 1.2 `agy` flags used

`-p`, `--output-format stream-json`, `--mode accept-edits`, `--sandbox`, `--settings <file>`, `--json-schema <file>`, `--print-timeout <duration>`, `--conversation <id>`, `--model`, `--effort`.

### 1.3 `agy` behaviour confirmed in Phase 0 (agy 1.5.9, this host)

| # | Fact | Tests |
|---|------|-------|
| F1 | Exit code 0 and `status: "SUCCESS"` even when a permission check aborted the run and it produced no answer. **Exit code and status are never evidence.** | A1, A2c, A2d |
| F2 | An action that matches **no rule** needs approval. Headless mode auto-denies it and **ends the run immediately with no answer**: empty `response`, no `structured_output`, `denied_actions: [{action, display_name}]` (no command text), and the stderr notice `jetski: no output produced …`. | A1, A2c–A2i |
| F3 | An action that matches an explicit **deny** rule is refused, the agent **continues**, and the final answer arrives. These refusals are **not** listed in `denied_actions`; they appear only as a step error ("Matches user-configured deny rule"). | A2h, A2k |
| F4 | Rule syntax is `{"permissions": {"allow": […], "deny": […]}}`. Plain `command(X)` matches only the **exact full command**: `command(git)`, `command(node)` and `command(git add)` did not match longer commands, contrary to the docs' "prefix" wording. `command(regex:…)` is a **full match** (`regex:status` did not match `git status`), and `^…$` is accepted. Precedence is Deny > Ask > Allow. | A2f–A2i |
| F5 | `--settings` accepts inline JSON or a file path, and its values are never saved. Malformed settings → exit 1 immediately with an error on stderr. Shell-quoted inline JSON is fragile, so settings are always passed as a file. | A2b, A2c, A4 |
| F6 | `accept-edits` auto-approves workspace file edits only. | A1 |
| F7 | `--mode auto` is **not available for this account**. agy falls back to "default" mode, which ran a command with **no allow rule**. The Foreman never uses `auto` or `default`. | A2e |
| F8 | Reading a file outside the workspace needs approval, so the run ends. agy's own permission system keeps Gemini's file tools away from The Foreman's records. | A2d |
| F9 | `--sandbox` works on this host (Windows 11 build 26300): sandboxed commands run, and reading `%APPDATA%` → `EPERM`. With the sandbox on, **file rules must use absolute paths**: a relative `.env` rule broke every sandboxed command ("deny .env: non-absolute file path"). | A3, A2j, A2k |
| F10 | A tool step with state `DONE` can still be a failed command. The real result is in the tool output (`Command exited with code N`, or error text). | A2j |
| F11 | `stream-json` is NDJSON with three event types: `init` (cwd, `permission_mode`, tools); `step_update` (tool name, parameters such as `CommandLine`/`Path`, output, error, state); and `result` (the same envelope as `json`: `conversation_id`, `status`, `response`, `structured_output`, `denied_actions`, `usage`, `duration_seconds`, `num_turns`). | A2c-stream, A4 |
| F12 | Works with the Antigravity IDE closed. Two `Antigravity.exe` sidecar processes (`sidecar_sdk`, started by `cmd.exe`) live independently of runs. | A4 |
| F13 | agy writes nothing into the repo besides the requested edits. Its state lives in `~/.gemini/antigravity-cli/`. | all |
| F14 | Arabic is byte-correct in edited files and in agy's UTF-8 output. This host's ANSI code page is 1256 (`ar-SA`), so Python prints to pipes in cp1256 unless forced to UTF-8. | A1, A2b |
| F15 | Global `core.autocrlf=true`: git warns `LF will be replaced by CRLF`. | setup |
| F16 | A typical trivial run takes 20–100 s and uses about 22–33k tokens. Through room.py, with the prompt and preamble, a small task took about 135 s and 49k tokens, and the pilot about 297 s and 170k tokens. | all, Phase 5 |
| F17 | (agy 1.5.12) For a command an allow rule permits, the agent can re-run it **outside the sandbox** (a bypass option of its command tool) without any approval. The stream parameters do not show the bypass. In the pilot, `pnpm` failed in the sandbox (`EPERM` on `AppData\Roaming`), and Gemini bypassed it. | Phase 5 pilot |

**Key consequences** (applied in §4–§12):
- The Foreman parses the event stream itself (§8).
- Runs aborted by a permission check get their own status.
- Every round gets a narrow allowlist, and the prompt tells Gemini exactly which commands it may run (Appendix A).

---

## 2. Architecture

```
 ┌───────────────────────────────────────────┐
 │ Claude Code — manager session              │  decides · writes tasks · reviews
 │ interactive, opened by the user            │  accepts / reworks / rejects · escalates
 └──────────────────────┬────────────────────┘
                        │ runs room.py (as a background command)
                        ▼
 ┌───────────────────────────────────────────┐
 │ room.py — The Foreman                     │  lock · preflight · task branch · launch agy
 │ lives OUTSIDE the project repository       │  snapshot commit · checks · verify · records · stop
 └──────────┬─────────────────────┬──────────┘
            │ launches            │ git (all writes)
            ▼                     ▼
 ┌────────────────────┐   ┌───────────────────────────────────────┐
 │ agy -p  (Gemini)   │──▶│ Project repo — one working copy        │
 │ cwd = repo root    │   │ the repo named in the room's room.toml │
 └────────────────────┘   └───────────────────────────────────────┘
```

| Component | Owns | Never does |
|-----------|------|------------|
| **Claude** | Delegation decisions, task text, review, accept/rework/reject, escalation | Edits the repo while a task is open |
| **room.py** | Lock, state, task branches, snapshot commits, checks, verification runs, records, stop | Decides, merges without a command, pushes, deletes work |
| **agy (Gemini)** | Implementing one task in the working tree | Git writes, network, installs, touching The Foreman data |
| **User** | Goals, approvals, pause/stop, every push | — |

**Why no watcher:**
- A watcher makes "a file appeared" the decision, which violates P1.
- It creates problems this design avoids entirely: half-written files, duplicate filesystem events on Windows, reprocessing after restarts, and agents triggering each other.

**Why the manager is an interactive session:**
- An idle Claude session sends no requests, so it costs no tokens.
- Resuming a session after the prompt cache expires costs the same tokens as staying open.
- An interactive session can ask the user. A headless run denies anything that needs approval.

---

## 3. Locations

### 3.1 Tool home (outside every project repository)

```
C:\Tools\the-foreman\
  Foreman.cmd                   opens the local UI (docs/UI-DESIGN.md)
  rooms\<room-name>\            runtime data per project (never committed)
  app\
    room.py                     entry point
    connect_room\               package (standard library only)
    foreman_ui.py, foreman_ui\  the local UI
    prompts\gemini-preamble.md  injected before every task (Appendix A)
    templates\                  room.toml, task.md, feedback.md, PROJECT-NOTES.md, result.schema.json,
                                agy-settings.base.json
    docs\                       DESIGN, IMPLEMENTATION-PLAN, ACCEPTANCE-TESTS, OPERATOR-GUIDE, UI-DESIGN
    tests\                      unittest suite + fake agent
    acceptance\                 Phase 0 sandbox + RESULTS.md
```

- The tool home may be moved. Nothing inside a project references it, and the code computes its own location.
- One tool home serves any number of projects, one room each.
- **The tool is project-neutral.** The code, templates and preamble contain no project names or toolchains, and a test enforces this. Everything project-specific lives in the project's room.

### 3.2 Room layout (runtime data)

```
rooms\<room-name>\          e.g. rooms\my-app\
  room.toml                 project configuration (§4)
  agy-settings.base.json    agent permission rules for this project (copied from templates, then tuned)
  PROJECT-NOTES.md          project specifics Claude reads at session start: commands, conventions,
                            specs, extra permissions, prerequisites
  state.json                room state — written only by room.py
  events.jsonl              append-only event log
  room.lock                 exists only while a mutating command runs
  STOP_REQUESTED            transient marker written by `stop`
  PAUSE                     exists = no new dispatch (the user may create it by hand)
  HANDOFF.md                Claude's milestone handoff notes (optional)
  tasks\T-0007\
    task.md                 written by Claude (§5)
    task-state.json         lifecycle — written only by room.py
    rounds\1\
      prompt.md             exact prompt sent to agy
      agy-settings.json     effective permission settings for this round
      agy-stdout.jsonl      raw event stream (stream-json)
      agy-stderr.log        raw stderr
      result.json           Gemini's structured result (extracted)
      report.md             human-readable rendering
      verify-1.log …        output of each verification command
      run.json              facts recorded by room.py (Appendix C)
    rounds\2\
      feedback.md           written by Claude before a rework round
      …
```

**Why outside the repo:**
- `git status` stays clean without ignore rules.
- The records sit outside the agent's workspace: agy runs with cwd = repo root and no `--add-dir`, so accidental edits to `run.json` or `state.json` are unlikely.
- This location is **not** a security boundary (§11).

### 3.3 Projects

**Adding a project:**
1. `room.py --room <name> init --repo <path> [--base main]` creates `rooms/<name>/` from the neutral templates.
2. In `room.toml`, set `verify.default_commands` and `policy.protected_paths`.
3. Fill in `PROJECT-NOTES.md`.
4. Tune `agy-settings.base.json`:
   - Add deny rules for the project's own package manager. For example, the pilot room denies `pnpm add/install/exec/dlx…`.
   - If the build needs the network, either add `read_url(<domain>)` allow rules or set `sandbox = false` in `room.toml` with a reason in the notes. A Gradle dependency download is one such case.
5. Run `room.py --room <name> doctor`.

**Other operations:**
- `room.py list` shows every project with its repo, open task, and running/paused state.
- Each room has its own lock, state, task numbers and history. Rooms never share anything.

---

## 4. Configuration — `room.toml`

The values below are an **example**: the `my-app` room, a pnpm monorepo. `init` writes a neutral template, and the project-specific values were then set by hand.

```toml
[project]
name          = "my-app"
repo          = 'D:\Code\my-app'
base_branch   = "main"
branch_prefix = "room/"

[agent]
command          = '%USERPROFILE%\.gemini\bin\agy.exe'
args_prefix      = []        # extra arguments right after command (the tests use it to run the fake agent)
tested_version   = "1.5.12"
permission_model = "sandbox" # sandbox (default) | allowlist — see §11
mode             = "accept-edits"   # the only accepted value (F7)
model            = ""        # empty = agy default
effort           = ""        # empty = agy default
sandbox          = true      # works on this host (F9)
settings_file    = "agy-settings.base.json"   # {REPO} → absolute repo path, forward slashes (F9)
timeout_minutes  = 45
max_prompt_chars = 24000
env_scrub        = ["*TOKEN*", "*SECRET*", "*PASSWORD*", "*API_KEY*", "*ACCESS_KEY*",
                    "GITHUB_*", "GH_*", "ANTHROPIC_*", "OPENAI_*", "AWS_*", "AZURE_*", "NPM_TOKEN"]
env_passthrough  = []        # names exempt from env_scrub, e.g. "GEMINI_API_KEY" if agy auth needs it

[verify]
default_commands = ["pnpm run typecheck", "pnpm run test"]
timeout_minutes  = 15
room_run         = "after_review"   # when room.py runs these itself: after_review | after_round | never (§8)

[policy]
max_rounds           = 3
protected_paths      = ["specs/", ".github/", ".husky/", ".npmrc", "pnpm-workspace.yaml", "turbo.json"]
sensitive_patterns   = [".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.ppk",
                        "*.keystore", "*.jks", "id_rsa*", "id_ed25519*", "credentials.json"]
sensitive_exceptions = [".env.example"]
```

**Parsing:**
- Parsed with `tomllib`.
- Single-quoted literal strings keep Windows backslashes intact.
- **Unknown keys are a config error (exit 2)**, so a typo can never silently disable a policy.

**Matching rules:**
- `sensitive_patterns` match file basenames, case-insensitively, using `fnmatch`.
- `protected_paths` and `allowed_paths` are repo-relative prefixes written with forward slashes.

---

## 5. Task format — `task.md`

`room.py new` creates the file from a template. Claude fills it in. It has TOML front matter between `+++` lines, followed by a Markdown body. The example below is a task in the pilot room.

```markdown
+++
title           = "Add UUIDv7 helper to shared-types"
allowed_paths   = ["packages/shared-types/"]
verify          = ["pnpm run typecheck", "pnpm --filter @my-app/shared-types test"]
timeout_minutes = 30
extra_allow     = []
max_rounds      = 3
+++

## Goal
## Context        (repo files and specs to read, by path)
## Requirements
## Do not
## Acceptance criteria
```

| Field | Required | Meaning |
|-------|----------|---------|
| `title` | yes | One line, used in branch logs and commit messages |
| `allowed_paths` | no | Prefixes Gemini may change. Missing or empty → no scope check, warning `NO_SCOPE`. |
| `verify` | no | Commands that prove the task. Each is added to Gemini's allowlist as an exact rule and listed in the prompt. room.py also runs them after a `DONE` round (§8). Missing → room defaults. `[]` → verification skipped (warning `VERIFY_SKIPPED`). |
| `timeout_minutes`, `max_rounds` | no | Override room defaults |
| `extra_allow` | no | Extra allow rules for this task only, e.g. `command(regex:^npm --workspace web test$)`. Plain `command(X)` is an exact match; `command(regex:…)` is a full match (F4). Base deny rules cannot be overridden (Deny > Allow). Claude's choice; recorded in `run.json`. |

The task id comes from the folder name (`T-0007`) and is not repeated in the front matter.

---

## 6. Prompt assembly and agent launch

**`prompt.md`** is assembled in this order:
1. The preamble (`prompts/gemini-preamble.md`, Appendix A).
2. A metadata block: task id, round, branch, allowed paths, verification commands, and **"Commands you may run"**, the exact allowed commands in plain form, one per line.
3. The task body.
4. For round 2 and later: `feedback.md` plus a summary of the previous round's result.

**Prompt length:**
- The prompt is passed as the `-p` argument.
- If it is longer than `max_prompt_chars`, dispatch is refused (exit 3) and the measured length is printed.
- Windows command lines are limited to 32,767 characters. Claude shortens the task or moves detail into repo files that the task references.

**Command line:**

```
agy -p <prompt> --output-format stream-json --mode accept-edits
    --json-schema <tool home>\templates\result.schema.json
    --settings <round dir>\agy-settings.json
    --print-timeout <timeout_minutes>m
    [--model M] [--effort E] [--sandbox] [--conversation <id>]
```

**Launch rules:**
- **cwd:** repo root.
- **Never passed:** `--add-dir`, `--dangerously-skip-permissions`.
- **`--sandbox`:** only if `[agent] sandbox = true`.
- **`--conversation`:** only on rework rounds, with the previous round's `conversation_id` (F11). Phase 5 checks that a resumed conversation behaves well in print mode. If it does not, every round is a fresh conversation, with full context in the prompt.
- **Effective settings**, written to the round folder and passed as a file (F5, F9). They always start from `agy-settings.base.json`, with `{REPO}` replaced by the absolute repo path in forward-slash form. Then:
  - **sandbox model (default):**
    - `"toolPermission": "proceed-in-sandbox"` is added;
    - every `command(...)` allow rule is removed, and none are added;
    - a task `extra_allow` that contains a command rule is refused (exit 2);
    - deny rules are kept.
  - **allowlist model:** one anchored, regex-escaped exact allow rule per verify command, plus the task's `extra_allow`.

  The result is validated as JSON before launch.
- **Environment:** the parent environment, minus variables whose names match `env_scrub` (case-insensitive), plus any listed in `env_passthrough`.
- **Process launch:** `subprocess.Popen` with an argv list, `creationflags=CREATE_NEW_PROCESS_GROUP`, and stdout/stderr redirected to files.

---

## 7. Result contract (Gemini → The Foreman)

Gemini's final answer must be one JSON object that matches `templates/result.schema.json` (Appendix B), enforced through `--json-schema`.

| Field | Content |
|-------|---------|
| `status` | `DONE` · `BLOCKED` · `FAILED` |
| `summary` | What was done, plainly |
| `files_changed[]` | `{path, change}` |
| `commands_run[]` | `{command, outcome: passed\|failed\|denied\|not_run, note}` |
| `blocked_reason` | Required when `BLOCKED` |
| `questions[]` | Questions for Claude |
| `open_issues[]` | Known gaps or risks |

**Extraction and validation:**
- The result is the `structured_output` object of the final `result` event (F11). It is absent when a permission check aborted the run (F2). Extraction is isolated in one function, `agent.extract_result()`.
- Validation uses the standard library only: required keys, types and enum values.
- The outcome is `VALID`, `INVALID` (raw text kept in `report.md`) or `MISSING`.

---

## 8. Dispatch algorithm

```
dispatch T
 1  acquire room.lock (O_CREAT|O_EXCL); if held → recover-or-refuse (§10)
 2  refuse (exit 3) if: PAUSE exists · another task is open · T is not DRAFT/REWORK_READY ·
    rounds ≥ max_rounds
 3  load and validate room.toml and task.md
 4  preflight: repo exists and is a git repo · agent command exists ·
    `agy --version` recorded (warning AGENT_VERSION_CHANGED if ≠ tested_version) ·
    warning ANTIGRAVITY_IDE_RUNNING if Antigravity.exe is running
 5  git preflight: `git status --porcelain=v1 --untracked-files=all` must be empty;
    no merge/rebase/cherry-pick in progress
 6  branch: round 1 → create room/T from base_branch HEAD and check it out;
    round ≥ 2 → HEAD must already be room/T
 7  record base_sha (round 1) and head_before
 8  write prompt.md and agy-settings.json · state.running = {...} · event dispatch_started
 9  launch agy · record agent_pid in state.json
10  wait with timeout:
      timeout          → kill tree → run_status TIMED_OUT
      STOP_REQUESTED   → run_status INTERRUPTED
      exit ≠ 0         → run_status CRASHED (stderr saved; "--settings must be…" → config error)
      exit 0           → run_status COMPLETED, refined in step 11
11  parse the event stream (agy-stdout.jsonl):
      activity  every tool step → commands (CommandLine + real exit code parsed from the
                tool output, or the error text), files read and written, deny-rule refusals
      result    structured_output → validate → result.json, report.md
      no structured_output AND denied_actions non-empty → run_status ABORTED_PERMISSION,
                with the attempted command or path from the last ERROR step (F2)
      deny-rule refusals → warning DENIED_BY_RULE (F3)
      a DONE command step with no exit code or a non-zero one → recorded as failed (F10)
12  git facts: head_after (moved → warning AGENT_COMMITTED); changed files =
    `git diff --name-status head_before` + `git ls-files --others --exclude-standard`
13  checks: scope (allowed_paths) · protected_paths · sensitive_patterns
14  snapshot:
      sensitive file present → SKIPPED_SENSITIVE (tree left as is)
      changes present        → `git add -A` + commit as "The Foreman" (hooks run normally,
                               5-minute timeout) → COMMITTED, or FAILED_HOOK (hook output saved)
      no changes             → NO_CHANGES
15  verify:
      agent evidence  for each verify command, its last sandboxed run after the last file edit,
                      with the real exit code from the stream (source "agent-sandbox")
      room run        only if run COMPLETED, agent DONE, snapshot COMMITTED or NO_CHANGES:
                        room_run = after_round  → run each command now (shell, cwd = repo,
                                                  timeout, tree kill) → PASSED / FAILED
                        room_run = after_review → AWAITING_ROOM_RUN: Claude reviews the diff,
                                                  then runs `verify T` (§9)
                        room_run = never        → the agent evidence decides
      → otherwise NOT_RUN; verify.head records the branch head the status refers to
16  write run.json (atomic) · task → AWAITING_REVIEW · state.running = null ·
    event round_recorded
17  print the summary (≤ 30 lines) · release the lock · exit 0
```

**Snapshot commits** use `git -c user.name="The Foreman" -c user.email="foreman@localhost" commit -m "room(T-0007): round 1 snapshot — <title>"`. Git config is never modified.

**Why room.py commits, not Gemini:**
- Gemini needs no git write permission at all. Pushing is denied by the rules (§11) instead of being left to Gemini's judgment.
- Every round produces an exact, comparable snapshot.
- Rejecting a task destroys nothing.

**Why two sources of verification evidence:**
- Gemini's `commands_run` is a claim. The event stream records the commands agy actually executed inside the sandbox, with their real exit codes (F10, F11).
- room.py's own run is a deterministic second check. It is **not** sandboxed (§11).
  - With `room_run = "after_review"` (the default), it runs only when Claude calls `verify T` after reading the diff.
  - So no code Gemini wrote is executed outside the sandbox before someone has looked at it.

**Why the clean-tree check counts untracked files:** `git add -A` in step 14 would otherwise sweep unrelated untracked files into the snapshot. For example, an untracked local-docs folder blocks dispatch until it is excluded in `.git/info/exclude`.

**If agy writes its own side files into the repo** (acceptance test A1, question 7), they are excluded through the repo's local `.git/info/exclude`, with the user's approval. They are never committed, and no tracked `.gitignore` is changed for them.

**`verdict_hint`** in `run.json`:
- `READY_FOR_REVIEW` only when every one of these holds:
  - run `COMPLETED`, result `VALID`, agent `DONE`;
  - snapshot `COMMITTED` or `NO_CHANGES`;
  - verify `PASSED`;
  - no warnings.
- `NEEDS_ATTENTION` otherwise.
- It is a hint for Claude, never a decision.

**Warnings and notices** (as implemented):
- **Warnings affect the verdict:** `AGENT_ERROR`, `AGENT_SETTINGS_REJECTED`, `DENIED_BY_RULE`, `CLAIM_MISMATCH`, `AGENT_COMMITTED`, `NO_SCOPE`, `SCOPE_VIOLATION`, `PROTECTED_PATH`, `SENSITIVE_FILES`, `SNAPSHOT_FAILED`, `VERIFY_SKIPPED`, `RECOVERED`, `ROOM_ERROR`.
- **`CLAIM_MISMATCH`** means Gemini says a command passed, but the activity log shows it was not run, was refused, or exited non-zero.
- **Notices are information only** and do not affect the verdict:
  - `ANTIGRAVITY_IDE_RUNNING`: the IDE's `language_server.exe` is running; agy's sidecars do not count.
  - `AGENT_VERSION_CHANGED`.
  - `STREAM_BAD_LINES`.

**Timeouts:**
- `timeout_minutes` accepts decimals.
- room.py kills the agent tree at the timeout plus a grace period of `min(90 s, 25 % of the timeout)`, so agy's own `--print-timeout` normally fires first.

**Internal errors:** if room.py itself fails mid-round, it kills the agent tree, records `run_status: ROOM_ERROR` with the traceback, moves the task to `AWAITING_REVIEW`, and exits 1.

---

## 9. Task lifecycle and commands

```
  new ──▶ DRAFT ──dispatch──▶ RUNNING ──round recorded──▶ AWAITING_REVIEW
                                ▲                              │
                                └──dispatch── REWORK_READY ◀──rework
                                                               │
                                        accept ──▶ ACCEPTED ◀──┤
                                        reject ──▶ REJECTED ◀──┘
```

**Room invariant:** at most one task is *open* (`RUNNING`, `AWAITING_REVIEW` or `REWORK_READY`). Any number of `DRAFT` tasks may exist.

**`accept T -m "<message>"`**
1. Requires `AWAITING_REVIEW`, a clean tree, and HEAD on `room/T`.
2. Refuses (exit 3) if `base_branch` moved since `base_sha`. The invariant should prevent this; if it happens, Claude resolves it manually.
3. **Verify gate.** If the branch has changes and the task has verify commands, `accept` requires a **passing verification of exactly the current branch head**. Either of these counts:
   - a `verify-manual-*.json` with `tree_clean: true`;
   - a `run.json` verify block with `status: PASSED`.

   `--skip-verify-gate "<reason>"` overrides the gate, and the reason is logged as `verify_gate_skipped`.
4. Runs `git switch <base>` → `git merge --squash room/T` → `git commit -m "<message>"`, with hooks running normally.
   - If that commit fails (a hook, for example), the staged squash is dropped with `git reset --merge` and HEAD returns to `room/T`. The work stays untouched on the branch, and the task stays `AWAITING_REVIEW`.
   - If the branch has no changes, nothing is committed.
4. Marks the task `ACCEPTED`. The branch is kept for audit.

This produces one commit per task. Each project's commit-message convention for `-m` is in its `PROJECT-NOTES.md`.

**`reject T [--reason ".."] [--stash-leftovers]`**
1. Requires `AWAITING_REVIEW`.
2. Refuses on a dirty tree unless `--stash-leftovers` is given, which runs `git stash push -u -m "foreman T-0007 leftovers"`. Stashing is reversible.
3. Runs `git checkout <base>` and marks the task `REJECTED`. The branch is kept.

**`rework T [--override-cap "<reason>"]`**
1. Requires `AWAITING_REVIEW`.
2. Requires rounds < `max_rounds`, unless the override is given (its reason is logged).
3. Creates `rounds/<n+1>/feedback.md` from a template and marks the task `REWORK_READY`.
4. Claude writes the feedback, then runs `dispatch T`.

**`stop [--reason ".."]`** (needs no lock, so it works while `dispatch` holds the lock)
- If a round is running: writes `STOP_REQUESTED` and runs `taskkill /PID <agent_pid> /T /F`. The dispatcher then records `INTERRUPTED`, snapshots partial work (unless sensitive files are present), and finishes normally.
- If the dispatcher is also dead: kills the agent tree and runs recovery (§10).
- Nothing is deleted.

| Command | Purpose | Lock | Exit codes |
|---------|---------|------|------------|
| `list` (no `--room`) | List every project room: repo, open task, running/paused | no | 0 |
| `init --repo <path> [--base main]` | Create a room from templates | yes | 0, 2, 3 |
| `status [--json]` | Room state, open task, running round, pause, warnings | no | 0 |
| `new "<title>"` | Allocate `T-NNNN`, create `task.md` from the template, print its path | yes | 0, 3 |
| `dispatch T` | Run one round (blocking; Claude runs it as a background command) | yes (whole run) | 0, 1, 2, 3, 4 |
| `show T [--round N] [--diff]` | Print the review packet: summary, warnings, verification, report, diff stat | no | 0, 4 |
| `verify T` | Re-run verification on the current tree; output saved as `rounds/<n>/verify-manual-*.log` | yes | 0, 3, 4 |
| `rework T` | Open the next round | yes | 0, 3, 4 |
| `accept T -m ".."` | Squash-merge into the base branch | yes | 0, 3, 4 |
| `reject T` | Close without merging | yes | 0, 3, 4 |
| `stop` | Kill the running round | no | 0, 3 |
| `pause ["reason"]` / `unpause` | Create or remove `PAUSE` (does not affect a running round) | no | 0 |
| `doctor` | Check Python, git, repo, agent path and version, config, lock, orphan processes | no | 0, 2 |

**Exit codes:**

| Code | Meaning |
|------|---------|
| 0 | Success. For `dispatch`, the round was recorded, whatever its outcome. |
| 1 | Internal error |
| 2 | Usage or config error |
| 3 | Refused by state or policy |
| 4 | Not found |

---

## 10. Locking, state, recovery, I/O

**Lock**
- `room.lock` holds `{pid, command, started_at, image}` and is created with `os.open(O_CREAT|O_EXCL|O_WRONLY)`.

**Liveness on Windows (ctypes)**
- A process is alive when `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` succeeds, `GetExitCodeProcess` returns `STILL_ACTIVE` (259), and `QueryFullProcessImageNameW` matches the recorded image.
- **Never use `os.kill(pid, 0)` on Windows.** Signal 0 equals `CTRL_C_EVENT`, and any other value terminates the process.

**Recovery from a stale lock** (the holder is dead):
- If `state.running` is set and the agent is still alive → refuse, and tell the operator to run `stop`.
- If `state.running` is set and the agent is dead, recovery does the following:
  - marks the round `INTERRUPTED` with `recovered: true`;
  - records git facts and snapshots the partial work, as `stop` does (skipped if sensitive files are present);
  - parses whatever the agent streamed;
  - logs `recovered_interrupted`.
- Then remove the lock and continue the command.

**Atomic writes**
- Write a temp file in the same directory, then `os.replace`.
- Retry 5 times with backoff on `PermissionError`, which antivirus scanners and open handles can cause on Windows.

**Encoding**
- All files are UTF-8 without BOM, with `\n` newlines.
- Subprocess output is decoded as UTF-8 with `errors="replace"`.
- `sys.stdout` is reconfigured to UTF-8.
- Summaries use ASCII status words (`ok`, `FAIL`), with no symbols that depend on the console encoding.

**Events**
- `events.jsonl` holds one object per line: `{ts, event, task, round, data}`.
- It is append-only and flushed after every line.

---

## 11. Security model

**Real boundaries:**
- The agy permission system (mode plus allow/deny rules), verified in Phase 0 (F2–F8). It also keeps Gemini's file tools away from everything outside the repo, including The Foreman's records (F8).
- The agy terminal sandbox (F9). Commands run in an AppContainer and reads of `AppData\Roaming` are refused. Per the docs, `.env`, `~/.ssh` and the network are also blocked unless allowed.
  - **Limit (F17):** the agent can run any *allowlisted* command outside the sandbox without approval, and the stream does not show it.
  - So the sandbox only contains commands the agent chooses to keep inside it. The effective boundary is the narrow allowlist.
  - Tested 2026-11-19 (`acceptance/RESULTS.md`):
    - A deny `unsandboxed(*)` does **not** block the bypass on this account.
    - Any allow rule that matches a command also approves running it unsandboxed.
    - Only `"toolPermission": "proceed-in-sandbox"` with **no command allow rules** blocks it. Commands then run inside the sandbox without rules, deny rules still apply, and a bypass attempt ends the round.
    - **Decision (user, 2026-11-19): the sandbox model is the default** (`permission_model = "sandbox"`). The allowlist model remains available per room.
- The Windows user account's permissions.
- Git history, for recoverability, together with the rule that nothing is pushed automatically.

**Operational safeguards (not boundaries):**
- Records kept outside the agent's workspace.
- `PAUSE` and `stop`. Stop kills the process tree, but it cannot undo commands that already ran.
- Environment scrubbing. The agent can still read files on disk.
- Scope, protected-path and sensitive-file checks. These detect problems after the fact; they do not prevent them.
- The preamble rules. These are instructions, not enforcement.

**Permission models:**

| | **sandbox** (default) | **allowlist** |
|-|------------------------|---------------|
| Settings | `toolPermission: proceed-in-sandbox`, **no** command allow rules, deny list | Exact allow rules (read-only git, verify commands, `extra_allow`), deny list |
| Agent runs | Any command, **inside the sandbox only** (no network, no `AppData\Roaming`, `.env`, `~/.ssh`) | Only allowlisted commands |
| Bypassing the sandbox | Needs approval → headless auto-deny → **the round ends** (tested, bypass-pis-noallow) | Allowed silently for allowlisted commands (F17) |
| Unlisted command | Runs in the sandbox | The round ends (`ABORTED_PERMISSION`) |
| Trade-off | Tools that need the user profile (e.g. pnpm) cannot run for the agent, so the room's own verify run carries the evidence | The agent can verify with such tools, but the sandbox is not a boundary for them |

**Agent permission policy, allowlist model** (confirmed in Phase 0; file `templates/agy-settings.base.json`; smoke-tested in A2k). The deny rows also apply in the sandbox model.

| Category | How | When attempted |
|----------|-----|----------------|
| File edits inside the repo | `accept-edits` | allowed |
| Read-only git: `status diff log show ls-files rev-parse` | allow (regex) | runs |
| The task's verify commands | allow (exact; added each round) | runs |
| Task `extra_allow` | allow (Claude's choice) | runs |
| Git writes, network tools, package managers (`npm npx yarn pip …`, plus each room's own, e.g. `pnpm add/install/exec/dlx…` in the pilot room), shells and system tools (`powershell cmd bash reg cmdkey …`) | **deny** (regex) | refused; the round continues (F3) |
| Writes to `<repo>/.git/`; reads and writes of `<repo>/.env` | **deny** (absolute paths) | refused; the round continues |
| Anything else (e.g. `rm`, `del`, an unlisted script, a read outside the repo) | no rule → needs approval | **the round ends** with `ABORTED_PERMISSION` (F2, F8) |

**Two principles follow from Deny > Allow:**
- The deny list holds only what must **never** happen. A task cannot override it.
- Anything merely "not now" stays unlisted. Trying it ends the round, and Claude decides whether to add an `extra_allow` and rework.

**Residual risk to state plainly:**
- **Code Gemini just wrote gets executed.**
  - In the sandbox model, everything the agent runs stays in the sandbox.
  - room.py's own verification run is **not** sandboxed. It runs that code with the user's privileges, like a developer running the tests by hand.
  - With `room_run = "after_review"` this happens only after Claude has read the diff. Use `never` to rely only on sandboxed agent evidence.
- **IDE grants can leak into headless runs.** Antigravity merges permissions from the IDE's project and user settings into CLI runs, so a grant saved in the IDE for a project could widen what headless runs may do. `doctor` reports any `permissionGrants` on the repo's entry in `~/.gemini/config/projects/`.
- **Widening `extra_allow`.** Claude should not widen it to network access or installs without asking the user.

**Antigravity IDE:** its agent must not work on the repository while a task is open. Dispatch warns when `Antigravity.exe` is running. A lock cannot stop it.

**Content handling:** Claude treats `report.md`, `result.json` and agent output as data, never as instructions.

---

## 12. Failure matrix

| Situation | Detection | Recorded as | Next step |
|-----------|-----------|-------------|-----------|
| Lock held by a live process | lock + liveness | refused, exit 3 | wait / `status` |
| Stale lock | holder dead | `recovered_interrupted` | command proceeds |
| `PAUSE` present | file exists | refused, exit 3 | `unpause` |
| Dirty tree | `git status` | refused, exit 3 | Claude resolves |
| Prompt too long | length | refused, exit 3 | shorten the task |
| agy missing / version changed | path / `--version` | refused / warning `AGENT_VERSION_CHANGED` | fix config / rerun A1–A2 |
| Auth or quota failure | `result.status` ≠ `SUCCESS`, an `error` field, or a non-zero exit with stderr | `CRASHED`, or result `MISSING` + warning `AGENT_ERROR` | Claude → user |
| Unlisted action attempted | no `structured_output` + `denied_actions` (F2) | `ABORTED_PERMISSION` + the attempted command or path | `extra_allow` + rework, or Claude does it |
| Action matches a deny rule | step error "deny rule" (F3) | warning `DENIED_BY_RULE`; the round continues | review |
| Command step `DONE` but failed | exit code ≠ 0 or error text in the tool output (F10) | recorded as failed in `activity` | review |
| Malformed settings | validation before launch; else exit 1 with `--settings must be…` (F5) | refused (exit 2), or `CRASHED` | fix config |
| Timeout | wait timeout | `TIMED_OUT`, tree killed, partial snapshot | Claude |
| Stop | `STOP_REQUESTED` | `INTERRUPTED`, partial snapshot | Claude / user |
| Agent crash | non-zero exit, no result | `CRASHED` | Claude |
| Result not JSON / schema mismatch | validation | `INVALID`, raw text in `report.md` | Claude reads the raw text |
| Agent committed | HEAD moved | warning `AGENT_COMMITTED` | review |
| Out-of-scope edits | `allowed_paths` | warning `SCOPE_VIOLATION` | review / reject |
| Protected path touched | `protected_paths` | warning `PROTECTED_PATH` | review |
| Sensitive file | patterns | snapshot `SKIPPED_SENSITIVE` | user decision |
| Hook rejects snapshot | commit exit code | snapshot `FAILED_HOOK` | Claude |
| Verification fails | exit codes | verify `FAILED` | rework / fix |
| room.py killed mid-run | stale lock on the next command | `INTERRUPTED` + `recovered`, partial snapshot | Claude |
| room.py internal error | exception | `ROOM_ERROR`, agent tree killed, exit 1 | Claude → user |
| Gemini claims a pass that did not happen | claims vs activity log | warning `CLAIM_MISMATCH` | review |
| Rework cap reached | round count | refused unless `--override-cap` | Claude escalates |

---

## 13. Out of scope (v1)

- File watchers and daemons.
- Launching or resuming Claude (no headless manager).
- Auto-merge and any push.
- Parallel tasks.
- `git worktree`.
- Dashboards.
- More than one executor agent.
- ACP integration (`agy acp`).
- Swarms (`/swarm`).

## 14. Future candidates (only with evidence of need)

- **Headless manager mode** (`claude -p --resume <uuid>`), after acceptance tests A5–A6.
- **Windows Job Object**, so that killing room.py always kills agy.
- **`git worktree` isolation**, if concurrent work is ever needed.
- **`claude --bg` as the manager**, with `claude attach` for intervention.

---

## Appendix A — Gemini preamble (`prompts/gemini-preamble.md`)

```text
You are the implementing engineer for exactly ONE task, dispatched by The Foreman on behalf of
Claude, the technical lead. Your workspace is the repository root (the current directory).

Rules — not optional:
1. Do only this task. Do not start other tasks, phases or "next steps".
2. Edit files only inside the allowed paths in the task metadata. If the task needs a change
   elsewhere, stop and return status BLOCKED.
3. Do not run any git command that writes: no add, commit, push, pull, fetch, checkout, switch,
   reset, rebase, merge, stash, clean, tag, branch, remote or config. The Foreman commits your work.
4. No network access: no downloads, uploads, API calls, package installs or dependency changes.
5. Never read, print, search for or test credentials, keys, tokens, SSH files, credential managers
   or user-profile configuration.
6. Do not touch anything outside this repository.
7. If an instruction is ambiguous, conflicts with the specs, needs a decision, needs a permission
   you do not have, or needs a destructive operation: stop and return status BLOCKED with the
   reason and your questions. Do not guess.
8. Run commands only as the metadata section "Commands you may run" allows, one at a time, from the
   repository root. Do not chain commands (&&, ;, |). Never try to run a command outside the sandbox,
   rephrase a refused command, or use other tools or scripts to get around a refusal: any such attempt
   ends your run without a result.
9. Run the verification commands after your last edit. Report each command's real outcome. If a
   command was refused or not run, say so. Never claim a result you did not observe.
10. Do not write a report file. Your final answer is the JSON object defined by the provided schema,
    and nothing else.
11. Write in English. Keep Arabic product text exactly as specified (UTF-8).
```

## Appendix B — `templates/result.schema.json`

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["status", "summary", "files_changed", "commands_run", "questions", "open_issues"],
  "properties": {
    "status":         { "type": "string", "enum": ["DONE", "BLOCKED", "FAILED"] },
    "summary":        { "type": "string" },
    "files_changed":  { "type": "array", "items": { "type": "object", "additionalProperties": false,
                        "required": ["path", "change"],
                        "properties": { "path": { "type": "string" }, "change": { "type": "string" } } } },
    "commands_run":   { "type": "array", "items": { "type": "object", "additionalProperties": false,
                        "required": ["command", "outcome"],
                        "properties": { "command": { "type": "string" },
                                        "outcome": { "type": "string", "enum": ["passed", "failed", "denied", "not_run"] },
                                        "note":    { "type": "string" } } } },
    "blocked_reason": { "type": "string" },
    "questions":      { "type": "array", "items": { "type": "string" } },
    "open_issues":    { "type": "array", "items": { "type": "string" } }
  }
}
```

## Appendix C — `run.json` example

```json
{
  "schema": 1,
  "task_id": "T-0007",
  "round": 1,
  "room_version": "0.1.0",
  "started_at": "2026-11-20T09:14:02+02:00",
  "ended_at": "2026-11-20T09:20:14+02:00",
  "duration_s": 372.4,
  "agent": {
    "command": "C:\\Users\\<you>\\.gemini\\bin\\agy.exe",
    "version": "1.5.9",
    "version_matches_tested": true,
    "mode": "accept-edits",
    "sandbox": true,
    "model": null,
    "exit_code": 0,
    "conversation_id": "c5843da9-8a3c-4233-ab20-810b5d2e5378",
    "denied_actions": [],
    "usage": { "input_tokens": 29677, "output_tokens": 3487, "total_tokens": 33164 }
  },
  "run_status": "COMPLETED",
  "result_status": "VALID",
  "agent_status": "DONE",
  "activity": {
    "commands": [
      { "command": "pnpm run typecheck", "state": "DONE", "exit_code": 0 },
      { "command": "pnpm run test", "state": "DONE", "exit_code": 0 }
    ],
    "files_written": ["packages/shared-types/src/ids.ts"],
    "refused_by_rule": [],
    "aborted_on": null
  },
  "git": {
    "branch": "room/T-0007",
    "base_sha": "b405455…",
    "head_before": "b405455…",
    "head_after_agent": "b405455…",
    "snapshot": "COMMITTED",
    "snapshot_sha": "3f2a1c9…",
    "files": [{ "path": "packages/shared-types/src/ids.ts", "status": "A" }],
    "insertions": 120,
    "deletions": 8
  },
  "checks": {
    "scope_violations": [],
    "protected_paths_touched": [],
    "sensitive_files": [],
    "permission_denials": []
  },
  "verify": {
    "status": "PASSED",
    "commands": [
      { "command": "pnpm run typecheck", "agent_exit_code": 0, "room_exit_code": 0, "duration_s": 21.0, "timed_out": false, "log": "verify-1.log" },
      { "command": "pnpm run test",      "agent_exit_code": 0, "room_exit_code": 0, "duration_s": 48.2, "timed_out": false, "log": "verify-2.log" }
    ]
  },
  "warnings": [],
  "verdict_hint": "READY_FOR_REVIEW"
}
```
