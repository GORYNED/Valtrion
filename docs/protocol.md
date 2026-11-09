# App-side file protocol

This page describes how the host and the app exchange a plan and a result: which files and arguments carry them, where they live in the app's data folder, and the line the app prints when it is done. It is the interface between the core and an app-side runner such as the Unity package; neither needs to know more about the other. The core's side is in `core/valtrion/pipeline.py` and the providers; the app-side runner is versioned separately and `compat.json` maps its versions to core versions.

## Data root

All files live under `gdv/` inside the app's data root, which for Unity is `Application.persistentDataPath`:

| Path under `gdv/` | Written by | Content |
| --- | --- | --- |
| `inbox/plan.json` | host | The `gdv.plan/1` to run |
| `inbox/candidate.json` | host (optional) | `repo`, `sha`, `profile`, `dirty`, `evidence`, `deviceId`, `deviceClass`, `hostId`. Without it the commit is unknown (zeros) and the result lists that as unverified. |
| `inbox/reply-<id>.json` | host | Answer `{"status", "message"}` to an `external.ui` request in the heartbeat |
| `out/<runId>/heartbeat.json` | app | `gdv.heartbeat/1`, rewritten atomically about every 2 s and at every step |
| `out/<runId>/result.json` | app | `gdv.result/1`, written atomically when the run ends |
| `out/<runId>/...` | app | Artifacts (for example screenshots) listed in the result |
| `out/current.json` | app | The run id of the latest run |
| `ledger.json` | app | What the run created, so cleanup can undo only that |
| `work/` | app | Base folder for files a plan step writes |

The host removes data from the device only through the run's ledger or by reinstalling: some device tools cannot delete files. A consumed `plan.json` is renamed `<name>.done-<yyyyMMddTHHmmssZ>`.

On a device the host reaches the data root with the platform's own tool: `adb` for Android (`/sdcard/Android/data/<package>/files`), `devicectl` for iOS (`Documents/` of the app container), the file system for Windows and macOS players and the Editor.

## Inputs to the app

The app takes the plan, the run id and the output folder from the first source that has them, in this order:

1. Launch arguments: `-gdv-plan <path>`, `-gdv-run-id <id>`, `-gdv-out <dir>`
2. Environment variables: `GDV_PLAN`, `GDV_RUN_ID`, `GDV_OUT_ROOT`
3. Android intent extras: `gdv-plan`, `gdv-run-id`, `gdv-out`
4. For the plan only: the file `gdv/inbox/plan.json`

The host uses the launch argument `-gdv-plan` on Windows and macOS, the `gdv-plan` intent extra on Android, and the environment variables for the Editor runner. `-gdv-out`, `GDV_OUT_ROOT` or `gdv-out` replaces `gdv/out`: heartbeat, result and artifacts then go to `<out>/<runId>/`. An explicit plan path that names no file ends the run as `FAIL`; the app never falls back to another source. Without an explicit run id the app derives one from the plan id, a UTC time and four hex digits.

## During the run

The app writes the heartbeat. The host watches its `seq`: if it stops advancing for the stall window the run is `STALL`; the plan's `budgetSeconds` bounds the run (`TIMEOUT`). The host pulls `gdv/out` while the app runs and takes the newest `<runId>` folder that did not exist before the launch.

An `external.ui` step puts a `request` in the heartbeat (state `waiting-host`) and waits for `inbox/reply-<id>.json`. The step is refused inside an open probe window of a performance plan.

The plan step `app.background` (`seconds`, optional `metric`) is an `external.ui` request with `action` `app.background` and `args.seconds`. The host moves the app to the background, keeps it there for that many seconds, brings it to the foreground and replies `PASS` once the app is in the foreground again; any other status, or no reply within the step's `timeoutSec` (which must cover the time away), ends the step with that status. How the app is sent away and back is the host's: a pipeline's `externalUi.command` receives `{arg:seconds}`. Valtrion does not ship such a command.

A pipeline `run` stage can host the request itself: with `externalUi` (`command`, an argv list; optional `timeoutSec`, default 120) the host answers each new request once, as it sees it in the heartbeat, by running the command in the project folder and writing the reply. The command is configuration the project controls, like a profile's `signScript` ([trust-model.md](trust-model.md)). The first element is the program and is never substituted (a placeholder there is a `BLOCKED` reply). Placeholders replaced inside the other arguments (no shell, no splitting): `{device}` (the platform's own id of the selected device, for example the UDID), `{app}`, `{action}`, `{request}` and `{arg:NAME}` (a scalar of the request's `args`; a missing or structured one is a `BLOCKED` reply). `action`, `args` and the request id come from the app's heartbeat, so they are untrusted data and the command must take them as data (`--flag={arg:x}`, and `--` before positionals). The host answers `BLOCKED`, without running the command, when a value contains a NUL byte or is over its cap (1024 characters per value, 8192 for the JSON of `args`), when a value that makes a whole argument starts with `-`, or when a value has `/`, a backslash or `..` and goes into an argument that has a path separator itself. The environment has `GDV_UI_DEVICE`, `GDV_UI_APP`, `GDV_UI_ACTION`, `GDV_UI_REQUEST` and `GDV_UI_ARGS` (JSON), so a device id or a signing team never has to be written into the pipeline. Exit code 0 is a `PASS` reply, another code `FAIL`, a command over its timeout `TIMEOUT` (the timeout is `timeoutSec`, capped by what is left of the stage's budget; a cancel request of the job stops the command with `BLOCKED`); only the last 64 KiB of the output is read. The last output line may be `{"status": "...", "message": "..."}` to give the message or a different status; the statuses a command may name are exactly `PASS`, `FAIL`, `BLOCKED`, `NEEDS_PERMISSION` and `TIMEOUT` (`STALL` is read as `TIMEOUT`, anything else is ignored), and it never turns a failing exit into `PASS`. A command that cannot be started is a `BLOCKED` reply. The reply is also kept as `external-ui/reply-<id>.json` in the run folder and listed in the result. While the command runs the heartbeat is not counted as stalled. A stage without `externalUi` leaves the request to a host of the caller's own.

## The result line

When the run ends the app prints one line to its log, exactly once:

```
GDV-RESULT <STATUS> run=<id>
```

`<STATUS>` is one of the statuses in [contracts.md](contracts.md). The host reads the last such line in the tail of the app's log. It is stricter than the exit code: an app that exits without a result file is judged by this line (a non-pass status is kept), and with neither it is never a `PASS`. The app stays open after the result unless a step asked it to quit.

`valtrion job watch` prints a line of the same shape for jobs: `GDV-RESULT <status> job=<id> offset=<n>`.

## Host-side additions (not seen by the app)

The app never sees these; they are listed because they extend documents that watchers and result readers parse. All are optional additions to `gdv.*/1`, so a reader that ignores unknown fields keeps working.

- `job.json` (`gdv.job/1`): `phase` is `waiting-disk`, `waiting-build`, `building`, `waiting-device` or `on-device` while a pipeline job runs that stage, and absent otherwise. Each change is also a `progress` event whose `data.phase` is the new phase (`null` when cleared).
- `lease.acquired` / `lease.released` events now occur per stage: the `unity-project` and `unity-editor` leases around the `build` stage, the `device:<id>` lease around the device stages. See [cli.md](cli.md#leases-by-stage-build-reuse) for the order and why it cannot deadlock.
- `result.json` (`gdv.result/1`): `build` describes the build artifact the run used: `path`, `sha256` (of the file, or over the sorted relative paths and file hashes of a folder), `sizeBytes`, `candidateSha`, `dirty`, `profile`, and `key`, `kind`, `platform`, `toolchain`, `reused`, `builtByJob`. A `progress` event with `data.build` carries the same object when the artifact is published or reused.
- Job notes: `incremental build: reused the previous export and DerivedData` or `incremental build: clean build (<why>)` for a managed iOS build.
- `result.json` (`gdv.result/1`): `checkout` (optional) is present when the job's `build` stage settled which commit the project folder is at (`--sha`): `folder` (the Unity project), `sha`, `switched`, `fetched`, and for a managed checkout also `id`, `created` and `previousSha`. An unmanaged folder is only verified (`switched` false, no `id`). A `log` event with `data.checkout` carries the same object. See [Managed checkouts](cli.md#managed-checkouts-and-the-commit-at-build-time).

## Environment variables passed to tools

For the Editor runner the host sets `GDV_PLAN`, `GDV_OUT_ROOT`, `GDV_RUN_ID` and `GDV_EVIDENCE=editor`. A job started with a work root also receives the variables listed in [work-root.md](work-root.md).
