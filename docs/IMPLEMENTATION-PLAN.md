# The Foreman — Implementation Plan

**Version:** 1.0 · 2026-11-19
**Audience:** Claude, as the builder of The Foreman
**Source of truth:** `DESIGN.md`. If this plan and the design disagree, the design wins. Fix the plan.

---

## 1. Build rules

1. **Python 3.11+ standard library only.** No `pip install`, no virtualenv. Allowed modules include `argparse`, `tomllib`, `json`, `subprocess`, `pathlib`, `ctypes`, `fnmatch`, `datetime`, `tempfile`, `shutil`, `unittest`.
2. **Build only inside the tool home** (`C:\Tools	he-foreman\`). Project repositories are not touched until Phase 5, and only after the user's go-ahead. The tool stays project-neutral; project specifics live in rooms.
3. **Tests never touch real repos.** Every git test creates its own repository in a `tempfile.TemporaryDirectory()`.
4. **Most of the tool is tested without quota.** The fake agent (§4) emulates the agy CLI. Real agents run only in Phase 0 and Phase 5.
5. **Windows first.** Tests run on this Windows machine. Do not break POSIX needlessly, but do not build POSIX-only paths.
6. **Every subprocess has a timeout.** Use argv lists, except for verification commands, which run with `shell=True` because they are trusted, Claude-authored strings.
7. **No hidden behaviour.** Every state change writes an event to `events.jsonl`.
8. **Phase reports.** After each phase, send the user a short report: what was built, test results, any deviation from `DESIGN.md`, and open questions.
   - **Gate G0** (after Phase 0) and **Gate G5** (before Phase 5) wait for the user's explicit go-ahead.
   - Phases 1–4 run back to back as long as their tests pass.

---

## 2. Module layout

```
connect-room\
  room.py                    from connect_room.cli import main; raise SystemExit(main())
  connect_room\
    __init__.py              __version__ = "0.1.0"
    cli.py                   argparse, command routing, exit codes, summary printing
    config.py                room.toml load + strict validation; task.md front matter (+++ TOML +++)
    store.py                 paths, atomic write (temp + os.replace + retry), JSON I/O, events.jsonl
    proc.py                  Windows liveness (ctypes), kill tree (taskkill /T /F), run-with-timeout
    lock.py                  acquire / release / stale detection / recovery hook
    state.py                 room state.json, task-state.json, lifecycle transitions + invariant
    gitops.py                preflight, branch, change detection, snapshot, squash accept, stash
    checks.py                scope, protected paths, sensitive files
    agent.py                 agy adapter: argv, env scrub, settings merge, launch, extract_result, validate
    verify.py                verification commands with timeout and logs
    render.py                report.md, CLI summary, `show` packet
  prompts\gemini-preamble.md
  templates\room.toml  task.md  feedback.md  result.schema.json  agy-settings.base.json
  tests\
    fake_agent.py            agy emulator (§4)
    helpers.py               temp repo factory, room factory
    test_store.py  test_proc.py  test_lock.py  test_config.py  test_state.py
    test_gitops.py  test_checks.py  test_agent.py  test_dispatch.py  test_lifecycle.py
  acceptance\                sandbox-repo\ (gitignored) · RESULTS.md
  .gitignore                 rooms/  acceptance/sandbox-repo/  __pycache__/
```

**Expected size:** roughly 1,000–1,400 lines of code plus a similar amount of tests. Keep each module under about 300 lines.

---

## 3. Phases

### Phase 0 — Confirm CLI facts *(real agents; sandbox only)* — DONE 2026-11-19

**Outcome:**
- All A1–A4 questions are answered, and A2 was extended with A2f–A2k.
- `templates/agy-settings.base.json` is written in the confirmed syntax and smoke-tested (A2k).
- `DESIGN.md` is now v1.1, with facts F1–F16.
- Results: `acceptance/RESULTS.md`.
- **Gate G0** is waiting for the user's review.

**Goal:** remove the unknowns that the design depends on before any code relies on them.

**Do:**
- Run acceptance tests A1–A4 from `ACCEPTANCE-TESTS.md`, in the sandbox repo.
- A5–A6 are optional and inform v2 only.

**Produce:**
- `acceptance/RESULTS.md`, filled in.
- The real permission syntax in `templates/agy-settings.base.json`.
- A short "agy output shape" note: where the structured result, the conversation id and any error fields sit, and how a permission denial appears.
- Updates to every *[Phase 0]* marker in `DESIGN.md` (§1.3, §5, §6, §7, §8, §11, §12), stating what was confirmed and what changed.

**Exit:**
- Every A1–A4 question has an observed answer.
- Design changes are listed.
- **Gate G0:** the user reviews `RESULTS.md` and the design deltas.

### Phase 1 — Foundations

**Build:** `store.py`, `proc.py`, `lock.py`, `config.py`, `state.py`, and the commands `init`, `status`, `new`, `pause`, `unpause`, `doctor`.

**Tests:**
- Atomic write survives a simulated `PermissionError` on the first two attempts.
- Lock contention: a second process gets exit 3 while the first holds the lock.
- Stale lock with a dead PID is detected and recovered.
- Liveness: the current PID is alive; a finished child's PID is dead; a mismatched image name counts as not alive.
- **Regression guard:** `os.kill` is never called. A grep test over the package source enforces this.
- Config: unknown key → exit 2; missing required key → exit 2; Windows literal paths parse.
- Front matter: valid, missing `+++`, bad TOML, and an Arabic title round-trip intact.
- `new` allocates sequential ids (`T-0001`, `T-0002`) under contention.

**Exit:**
- `python -m unittest discover -s tests` passes.
- `room.py --room my-app doctor` runs against the real config. It only reads the repo.

### Phase 2 — Git operations and checks

**Build:** `gitops.py`, `checks.py`.

**Tests (temp repos):**
- Clean-tree check catches modified, staged and untracked files, and a merge in progress.
- Branch creation from the base branch; a round-2 HEAD that is not on `room/T` is refused.
- Change detection covers modify, add, delete, rename and untracked.
- `head_after` ≠ `head_before` → `AGENT_COMMITTED`.
- Snapshot commit uses the Foreman identity without touching git config.
- A failing pre-commit hook in the temp repo → `FAILED_HOOK`, with the hook output captured and the tree left staged.
- A sensitive file present → no commit, `SKIPPED_SENSITIVE`; `.env.example` is exempt.
- Scope checks: inside, outside and no-scope cases. Protected-path detection.
- `accept`: squash merge produces exactly one commit on base with the given message, and the branch is kept.
- `accept` when the base moved → refused, exit 3, nothing changed.
- `reject` on a dirty tree → refused; with `--stash-leftovers` → a stash exists and the tree is clean.

### Phase 3 — Dispatch with the fake agent

**Build:** `agent.py`, `verify.py`, `render.py`, and the `dispatch`, `show` and `verify` commands.

**Fake agent scenarios.** Each asserts the expected `run.json` fields:

| Scenario | Fake agent behaviour | Expected |
|----------|---------------------|----------|
| `done_ok` | edits an allowed file, valid result `DONE` | `COMPLETED` / `VALID` / `DONE` / `COMMITTED` / verify `PASSED` / `READY_FOR_REVIEW` |
| `done_tests_fail` | `DONE`, but the verify command fails | verify `FAILED`, `NEEDS_ATTENTION` |
| `blocked` | no edits, `BLOCKED` with a reason | `NO_CHANGES`, verify `NOT_RUN` |
| `invalid_json` | prints prose | result `INVALID`, raw text in `report.md` |
| `no_output` | exits 0 with empty stdout | result `MISSING` |
| `crash` | exit 1, no result | `CRASHED` |
| `aborted_on_ask` | edits a file, then a `run_command` step ERROR ("user denied permission"); `result` has no `structured_output`, `denied_actions: [command]`, exit 0 | `ABORTED_PERMISSION`, `aborted_on` = the command, partial snapshot |
| `deny_rule_continue` | one step ERROR "Matches user-configured deny rule", then a valid `DONE` result | warning `DENIED_BY_RULE`, otherwise normal |
| `done_but_cmd_failed` | a `run_command` step in state DONE whose output says `Command exited with code 1`; result claims `passed` | activity shows exit 1; agent evidence `FAILED`; warning |
| `bad_settings` | exit 1, stderr `--settings must be a JSON object…` | `CRASHED`, config error surfaced |
| `hang` | sleeps past the timeout | `TIMED_OUT`, process gone, partial snapshot |
| `commits_itself` | runs `git commit` | warning `AGENT_COMMITTED` |
| `out_of_scope` | edits outside `allowed_paths` | warning `SCOPE_VIOLATION` |
| `writes_env` | creates `.env` | `SKIPPED_SENSITIVE`, tree left dirty |
| `arabic` | writes Arabic text to files and the result | bytes identical end to end |
| `long_prompt` | — | dispatch refused, exit 3, length printed |

**Also test:**
- Env scrubbing removes matching names and keeps passthrough names.
- `prompt.md` contains the preamble, metadata, body and (round 2) feedback, in that order.
- The summary stays within 30 lines.

### Phase 4 — Stop, recovery, rework, lifecycle

**Build:** the `stop`, `rework`, `accept` and `reject` commands wired end to end, plus recovery in `lock.py`.

**Tests:**
- Full lifecycle: `new` → `dispatch` → `rework` → `dispatch` → `accept`.
- `reject` path.
- Rework cap enforced; `--override-cap` works and is logged.
- Invariant: dispatching a second task while one is open → exit 3.
- `pause` blocks dispatch but not a running round; `unpause` restores.
- Acceptance tests **A7** (git isolation) and **A8** (emergency stop), run with the fake agent.

**Exit:**
- All unit tests pass, plus A7 and A8.
- The CLI help text matches `DESIGN.md` §9.

### Phases 1–4 — DONE 2026-11-19

**Outcome:**
- **Code:** 14 modules in `connect_room/`, about 2,100 lines.
- **Tests:** 68 tests in four files pass in about 90 s, with no resource warnings: `test_foundation`, `test_git`, `test_dispatch`, `test_lifecycle`. They include:
  - acceptance tests **A7** and **A8**;
  - every fake-agent scenario in §3;
  - golden-stream tests on the real Phase 0 recordings.
- **Room:** `rooms/my-app` is initialised, and `doctor` runs against the real repo (read-only).

**Deviations, all recorded in `DESIGN.md` v1.2:**
- an `args_prefix` config key;
- decimal timeouts;
- warnings vs notices;
- recovery snapshots partial work;
- `accept` undoes a failed squash with `reset --merge`;
- tests consolidated into four files.

**Made project-neutral (2026-11-19, at the user's request):**
- The tool moved from a project-named folder to its own tool home.
- Added the `list` command, and a `PROJECT-NOTES.md` written into each room by `init`.
- The pnpm deny rule moved from the template into the pilot room.
- The operator guide is generic.
- A test fails if a project name or toolchain appears in the code, templates or preamble.
- The pilot project remains the first room.

### Phase 5 — Real agent, then pilot — DONE 2026-11-19

**Outcome:**
- **Prerequisites:**
  - The old copy is retired: renamed to `<repo>-RETIRED` after confirming it held nothing unique.
  - The local-docs folder is excluded via `.git/info/exclude`.
  - A2k and A4 were re-run on agy 1.5.12 and pass.
  - `doctor` is clean.
- **5a:** three end-to-end tasks with real agy behaved as designed: success and accept, `BLOCKED` on missing information, and `BLOCKED` on an unlisted command instead of aborting.
- **5c:** the pilot was accepted as `a0d9aa6` on `main` and not pushed.
- **New finding F17** (the sandbox can be bypassed for allowed commands) is recorded in `DESIGN.md` §1.3 and §11 and in `acceptance/RESULTS.md`.

### After v1 — sandbox model (2026-11-19, the user's decision)

1. **`permission_model = "sandbox"` is the default.** No command allow rules, plus `toolPermission: proceed-in-sandbox`, so a bypass ends the round. The allowlist model stays available per room.
2. **`verify.room_run = "after_review"` is the default.** room.py no longer runs agent code outside the sandbox before Claude's review.
3. **`accept` verify gate:** accept requires a passing verification of the exact branch head on a clean tree. `--skip-verify-gate` is logged.
4. **`settle_stat_dirty` fix:** files a formatter hook rewrote with identical content no longer block dispatch and accept. This was found while committing to the pilot repo.
5. **Tests:** 77 pass.

### Phase 5 — Real agent, then pilot (plan)

**Gate G5** before starting: the user gives the go-ahead.

**5a — Sandbox end-to-end with real agy:**
- One `DONE` task.
- One task that must come back `BLOCKED`.
- One task whose verification needs a command that is not allowlisted.

Confirm that `run.json` tells the truth in all three.

**5b — Pilot prerequisites.** Each needs the user's approval:
1. Exclude the repo's untracked local-docs folder via `.git/info/exclude`. This changes no tracked file.
2. Retire a second working copy of the repo (rename it to `...-RETIRED`) so no agent works there.
3. Make sure the Antigravity IDE agent is idle on this repo during the pilot.
4. agy updated itself to **1.5.12** on 2026-11-19, and `doctor` warns about it. Rerun A2k (base settings smoke) and A4 (production setup) on the new version, then set `tested_version = "1.5.12"`.
5. Run `room.py doctor` clean.

**5c — Pilot:**
- One low-risk task, chosen by Claude, run with the user watching.
- Review with `show` plus the actual diff, then `accept`.

**Exit:**
- The pilot task is accepted on `main` as one squash commit.
- `OPERATOR-GUIDE.md` is updated with anything learned.

---

## 4. Fake agent (`tests/fake_agent.py`)

- Accepts the same flags as agy (`-p`, `--output-format`, `--mode`, `--json-schema`, `--settings`, `--print-timeout`, `--conversation`, `--model`, `--effort`, `--sandbox`) and ignores the ones it does not need.
- The scenario is chosen by the environment variable `FAKE_AGENT_SCENARIO`. It is never inferred from the prompt.
- It writes to the current directory, which is the temp repo, exactly as the scenario specifies.
- It prints an NDJSON event stream in the shape recorded in Phase 0 (`DESIGN.md` F11):
  - `init`;
  - one `step_update` per tool call, with `CommandLine`/`Path` and either an output containing `Command exited with code N.` or `error.message`;
  - a final `result` envelope.

  Real streams in `acceptance/runs/*/stdout.bin` serve as golden samples for the parser tests.
- Its `--version` prints `1.5.9`.
- For A8, the `spawn_tree` scenario starts two grandchild processes that append heartbeats to files, so the test can prove that the whole tree died.

---

## 5. Definition of done (v1)

- [x] Phases 0–5 complete; G0 and G5 approved by the user.
- [x] `python -m unittest discover -s tests` passes on this machine (70 tests).
- [x] Acceptance tests A1–A4, A7 and A8 pass, and the results are recorded.
- [x] Every *[Phase 0]* marker in `DESIGN.md` is resolved (v1.1).
- [x] One pilot task is accepted on the pilot repo through `room.py accept` (`a0d9aa6`).
- [x] `OPERATOR-GUIDE.md` matches the real CLI.
- [ ] No code path uses `os.kill`, `--dangerously-skip-permissions`, `--no-verify`, `git push`, `git reset --hard` or `git clean`. A grep test enforces this.

---

## 6. Build risks

| Risk | Mitigation |
|------|-----------|
| agy JSON shape differs from expectations | Phase 0 first; `extract_result()` is the only parser; the fake agent mirrors the recorded shape |
| Permission rule syntax unclear | Phase 0 A2 writes `agy-settings.base.json` from observed behaviour, not from guesses |
| Husky / lint-staged slow or failing on snapshot | Hooks run normally; `FAILED_HOOK` is a recorded outcome; the snapshot gets its own timeout (5 min) |
| Windows file locks (antivirus, editors) | Atomic write retries; logs opened in append mode with short-lived handles |
| Orphaned agy after room.py dies | `state.running.agent_pid` recorded before waiting; `doctor`, `status` and `stop` detect and kill orphans |
| agy updates change behaviour | `tested_version` warning; rerun A1–A2 after any agy update |
