# Project notes - @@NAME@@

Claude reads this at the start of every manager session for this room. The Foreman itself is
project-neutral: everything specific to this project lives here, in this room's `room.toml`, and in
this room's `agy-settings.base.json`.

- Repository: @@REPO@@
- Base branch, verify commands, protected paths: see `room.toml`

## Build and verify commands
Commands that prove a task in this project. Put the usual ones in `verify.default_commands`.

## Conventions
Commit message format for `accept -m`, task size limits, naming rules.

## Specs and references
Documents Claude and the agent should read, by repo path.

## Permissions specific to this project
Extra allow/deny rules added to this room's `agy-settings.base.json`, and why.
If the build needs the network (e.g. a package or Gradle download), the sandbox blocks it: allow the
hosts with `read_url(<domain>)` rules, or set `sandbox = false` for this room and say why here.

## Prerequisites and known issues
