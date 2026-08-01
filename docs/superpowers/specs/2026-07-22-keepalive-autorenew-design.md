# Keep-alive auto-renew for SLURM jobs

## Problem

SLURM jobs on this cluster hit a hard time limit (e.g. 2 days) and are killed.
When the allocation ends, the node's tmux server dies with it. The user holds
GPU allocations with `占坑` (hold-the-spot) scripts — batch scripts whose body
is an infinite loop that keeps the allocation occupied — and wants those to be
resubmitted automatically so the allocation is continuous, **without** manually
watching TIME_LEFT and re-running `sbatch`.

The user does **not** want every job auto-renewed (that wastes fairshare and
GPUs) — only ones they explicitly opt in, and with as little interaction as
possible (ideally one keystroke, zero typing).

## What "续上/continue" means here

A running process cannot be migrated to a new node. When a job dies, its work is
gone. For `占坑` scripts the "work" is just holding the allocation, so continuity
means: **keep exactly one job of the same identity alive by resubmitting its
launch script.** atmux does not recreate tmux session *contents*; the user's
script (or their own workflow) does whatever it needs on the fresh node. The
daemon discovers the new node/session through its normal `squeue` polling.

## Scope

In scope: opt-in per job, auto-detect the launch script, resubmit before expiry,
guardrails against runaway submission, status surfaced in the TUI.

Out of scope (YAGNI): migrating/checkpointing live processes, resuming
computation state, recreating tmux window contents, non-SLURM schedulers,
interactive (`salloc`/`srun`, `BatchFlag=0`) jobs.

## Identity

Each current keep-alive entry has an internal UUID and tracks one concrete
**Slurm JobID family**. Array elements (`123_4`) and heterogeneous components
(`123+1`) normalize to their leading allocation family (`123`). Job name is
display metadata, not identity, so two independent jobs named `h100x2` can be
enabled, renewed, and disabled independently.

After a successful submit, the daemon atomically advances that UUID's tracked
JobID to the replacement returned by `sbatch --parsable`. The UUID stays stable
across every renewal. Name-only entries written by older releases remain
supported as a migration fallback; only those legacy entries retain name-wide
matching until the user toggles them off and on again.

## Auto-detecting the launch script (zero input)

On toggle, atmux runs `scontrol show job <jobid>` and extracts:

- `Command=` — the batch script path (+ any args). Verified present for batch
  jobs on this cluster, e.g. `Command=/n/home12/shgao/h100x1`.
- `WorkDir=` — the directory the job was submitted from.
- `BatchFlag=` — `1` for batch jobs. If `0` (interactive), atmux refuses to
  register and shows a toast; there is no script to resubmit.

Both `Command` and `WorkDir` are captured **at toggle time** and stored in the
registry, so renewal still works after the old job ages out of `scontrol`.

Renewal command: `sbatch --parsable -- <Command...>` run with
`cwd=<WorkDir>`. The `--` keeps an option-looking script path from being parsed
as an `sbatch` option. It must be `sbatch` (not `bash`) — the script carries
`#SBATCH` directives and an infinite-loop body; running it directly on the
login node would be wrong. `Command` is split with `shlex` to preserve any
arguments.

## Architecture & data flow

Two files, with registry writers serialized by a bounded cross-process lock:

1. **Intent registry** — `~/.config/autotmux/keepalive.json` (persistent,
   alongside `config.toml`; NFS-safe since it is a plain file, not a socket).
   The TUI writes opt-in intent; after a successful renewal the daemon updates
   the same entry's replacement JobID and submit timestamps. Both use atomic
   replace under the registry lock. The daemon re-reads when file metadata
   changes.

   Before submitting, a daemon atomically claims that entry with a unique
   owner token and a bounded lease under the same lock. This is stronger than
   an in-process `in_flight` bit: two daemons on different login hosts sharing
   the registry cannot both launch the same replacement. Completion is applied
   only by the matching claim owner, so a late result cannot overwrite a newer
   claim.

   ```json
   {
     "entries": [
       {
         "entry_id": "74edecfdcce9492f9a21ee1f41025439",
         "job_id": "12345678",
         "job_name": "h100x1",
         "command": "/n/home12/shgao/h100x1",
         "workdir": "/n/home12/shgao",
         "enabled": true
       }
     ]
   }
   ```

2. **Renewal status** — the daemon keeps per-entry bookkeeping in memory and
   publishes it into the existing `daemon.json` under a new top-level key so the
   passive TUI can render it:

   ```json
   "keepalive": {
     "74edecfdcce9492f9a21ee1f41025439": {
       "state": "healthy | renewing | paused | expiring",
       "attempts": 0,
       "last_submit": "2026-07-22 10:00:00",
       "last_error": "",
       "entry_id": "74edecfdcce9492f9a21ee1f41025439",
       "job_id": "12345678",
       "job_name": "h100x1"
     }
   }
   ```

The frontend edits intent, performs the one explicit `scontrol` lookup on a
keypress, and displays status. The daemon owns periodic squeue checks and every
`sbatch` renewal.

## Daemon renewal logic

Runs inside the existing `_squeue_loop`, after the squeue parse, once per poll.
For each enabled entry `(UUID U, tracked JobID J, name N, command C, workdir W)`:

1. `matching` = squeue jobs in JobID family `J`. A name-only legacy entry uses
   `JobName == N` instead.
2. A matching job is **fresh** if its state is `PENDING` **or**
   `time_left_seconds > lead_time`.
3. If the entry is **paused** (`attempts >= max_failures`): skip until re-armed
   (user toggles off then on).
4. If any `matching` job is fresh → `state = healthy`, `attempts = 0`. Preserve
   the cooldown timestamp so a brief controller-visibility flap cannot cause a
   duplicate submit. A queued replacement counts as fresh.
5. Else (all matching are `RUNNING` with `time_left <= lead_time`, **or**
   `matching` is empty):
   - If still within `cooldown` of `last_submit` → `state = renewing`, skip.
   - Otherwise atomically acquire the entry's shared submit claim. If another
     host owns an unexpired claim, persist its defer window locally and skip.
   - The claim owner **submits** with
     `sbatch --parsable -- shlex.split(C)`, `cwd=W`, and
     `timeout=submit_timeout`; output is captured to the daemon log. Set
     `last_submit = now`, `state = renewing`; a non-zero exit, timeout, or
     unparseable sbatch output increments `attempts`. Local command-capacity
     exhaustion is not an attempt and does not start a cooldown.
   - A timeout or transport/parse ambiguity still persists the cooldown before
     releasing the claim. `sbatch` may have accepted the job before its reply
     was lost, so immediate retry would risk a duplicate allocation.
   - On success, persist the returned replacement JobID and submit timestamps
     under UUID `U`; this preserves identity and cooldown across daemon restarts.
   - When that replacement appears → back to `healthy`, `attempts = 0`.
   - When `attempts >= max_failures` with no fresh job → `state = paused` and a
     TUI banner is surfaced.

`state = expiring` is a display nuance: `RUNNING` and `time_left <= lead_time`
but a submit is imminent/just fired (drives the `renew≤15m` text). Implementation
may fold it into `renewing`.

### TIME_LEFT parsing

Parse SLURM `%L` durations into seconds: `D-HH:MM:SS`, `HH:MM:SS`, `MM:SS`.
`UNLIMITED` → treat as always-fresh (never renew). `NOT_SET` / `INVALID` /
empty → `None` (skip the entry that poll; can't decide safely).

## TUI interaction

- New binding **`k`** = toggle keep-alive on the highlighted session's job.
- **`k` on an unregistered batch job** → run `scontrol show job`, capture
  `Command`/`WorkDir`/`JobName`, write the entry, footer toast:
  `✓ keep-alive ON · <name> #<jobid> · will re-run <script> when it expires`.
- **`k` on a registered job** → remove only that UUID, toast
  `keep-alive OFF · <name> #<jobid>`.
- **`k` on an interactive job** (`BatchFlag=0`) or a row with no Slurm job
  (localhost) → toast
  `no batch script for this job — can't keep alive`; nothing registered.
- `<Start Shell>` and `<offline>` rows still retain their node's Slurm JobID,
  so they can be toggled even when no tmux session/SSH master is available.
- A **`⟳`** marker on registered rows; status is folded into the STATUS column
  (`keep-alive`, `renewing…`, `PAUSED ✕3`).
- **Paused banner** reuses the existing daemon crash-loop banner mechanism:
  `⚠ keep-alive for '<name>' paused after 3 failed submits — check the daemon
  log, fix the script, press k twice to re-arm.`

The `scontrol show job` call is fast and read-only; it runs from the TUI at the
moment of the keypress (not on a timer), so it does not add background load.

## Config additions

New `[keepalive]` table in `config.toml` (config loader extended to accept it —
today it only merges the numeric `[daemon]` table):

| key              | default | meaning                                             |
|------------------|---------|-----------------------------------------------------|
| `enabled`        | `true`  | master switch for the whole feature                 |
| `lead_time`      | `900`   | seconds before expiry to submit the replacement     |
| `cooldown`       | `600`   | seconds to suppress re-submit after a submit        |
| `max_failures`   | `3`     | consecutive failed submits before pausing an entry  |
| `submit_timeout` | `60`    | seconds to wait for `sbatch` to return              |

## Testing

Pure/functional, no live SLURM:

- **TIME_LEFT parser** — table of SLURM duration strings → seconds / `None` /
  ∞ (`UNLIMITED`).
- **Renew-decision function** — a pure function
  `decide(matching_jobs, entry_state, now, cfg) -> action`
  (`none` / `submit` / `wait_cooldown` / `paused`) exercised with synthetic
  squeue rows: fresh present, all-expiring, gone, pending replacement, within
  cooldown, at failure cap.
- **scontrol parser** — extract `Command`/`WorkDir`/`JobName`/`BatchFlag` from a
  captured `scontrol show job` text blob (batch and interactive samples).
- **Registry round-trip** — write via the TUI helper, read back in the daemon
  helper; toggle on/off idempotence.
- **Cross-host claim** — two independent managers race the same UUID; exactly
  one calls the submitter, only its matching owner token may complete the
  claim, and an ambiguous result leaves a persisted cooldown/defer window.
- **Submit invocation** — `sbatch` stubbed (fake executable on PATH, like the
  existing `ssh` stub in `test_warm_pool.py`); assert `cwd`, argv, and that a
  non-zero/timeout return increments `attempts`.

## Edge cases

- **Daemon restart** — runtime status is re-derived from squeue; the tracked
  replacement JobID and last successful-submit cooldown persist in
  `keepalive.json`, preventing a duplicate while Slurm visibility catches up.
- **Deleted script** — `sbatch` fails → failed attempt → pause after
  `max_failures`.
- **Same-name collision** — current UUID/JobID entries are independent. Only
  unmigrated name-only entries retain the old name-wide behavior.
- **Feature disabled** (`enabled=false`) — daemon ignores the registry entirely;
  the TUI still lets you toggle (intent preserved for when you re-enable).
