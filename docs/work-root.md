# Work root and local state

This page explains where Valtrion keeps its files on a host: the disposable work root, the state home with hosts, registry, leases and job index, and the artifacts root. It also covers how background jobs run on macOS, how leases behave, and the temporary setting that lets the new CLI share a lease directory with the earlier tool.

## State home

Machine-local state lives under `~/.valtrion`. `VALTRION_HOME` overrides it (for tests and isolated runs); an isolated home carries its own `work/`.

| Path | Content |
| --- | --- |
| `hosts.json` | `gdv.hosts/1` inventory. The local machine is always a host, added automatically if not listed. |
| `host.json` | Settings of this machine, below |
| `registry.json` | `gdv.registry/1` device registry |
| `leases/` | Lease files |
| `jobs/` | Job id to run folder index |
| `hostcache/` | Five-minute cache of remote `doctor` reports |
| `artifacts/` | Default artifacts root (retained results, baselines, run folders) |
| `core/<version>/` | Virtual environment created by `tools/install.py --venv` or `valtrion hosts sync`; uses the real `~/.valtrion` even when `VALTRION_HOME` is set. The local installer's `--home DIR` instead uses `DIR/.valtrion/core/<version>`. |
| `ssh/` | SSH control sockets |
| `incremental/` | Persistent build folders of managed checkouts (Xcode export, DerivedData) |
| `checkouts/` | Default place of managed build checkouts (`checkouts/<project>/<id>`), when `host.json` names no `path` |
| `checkout-records/` | One record per managed checkout a job used, so `doctor` can tell a lost checkout from one never created |

### Working state, not cache

The valtrion home and the managed checkouts (wherever their `path` points, for example `~/valtrion-checkouts`) are working state. Do not remove them with a disk cleanup: the registry holds the owner's device classes and capabilities, `leases/` and `jobs/` belong to running work, and a managed checkout's `Library/` is the Unity import cache, so a removed checkout costs a full reclone and Library import on the next build (plus a clean incremental build). Each managed checkout carries a `.valtrion-managed` marker file (excluded from git in `.git/info/exclude`) so tools and people can recognise it, and `valtrion doctor` reports a checkout that a job used before and that is gone now as "was created, now missing" with a warning. To free space, use `valtrion gc`, `valtrion workroot sweep` and the `disk` settings below; they never touch checkouts. Only the work root (below) is disposable.

To move an existing checkout (for example into `<valtrion home>/checkouts/`), wait until no job holds its `unity-project:` lease (`valtrion lease list`, `valtrion doctor`), move the folder, then change or remove its `path` in `host.json`.

### host.json

```json
{
  "artifactsRoot": "~/.valtrion/artifacts",
  "workRoot": "~/Library/Caches/com.valtrion/work",
  "retention": { "keepDays": 30 }
}
```

All keys are optional. `artifactsRoot` and `workRoot` must be non-empty; `workRoot` must be absolute (or start with `~`). `retention.keepDays` is a non-negative number (default 30) used by `valtrion gc`. `GDV_WORK_ROOT` (absolute) overrides `workRoot` for one process.

**`disk`: free space before a build.** Optional and off by default. `"disk": { "minFreeGiB": 40, "keepDaysUnderPressure": 2 }`: before a `build` stage the pipeline checks the free space of the work root's volume. Below `minFreeGiB` (a positive number) it removes what no job needs, in this order: abandoned job folders of the work root untouched for an hour (like `workroot sweep`; the folders of a running job, of a retained run and of a protected path are never touched), run folders older than `keepDaysUnderPressure` days (default 2; like `gc`, with active, retained and `--protect`ed runs kept) and published builds beyond the build-cache retention. If the volume is still below the minimum, the stage ends `BLOCKED` with the free space, the minimum and the largest consumers, and no toolchain starts. A build reused from the cache needs no space and is not checked (for a managed checkout the cache is known only after the checkout, so a disk wait comes first there). **`disk.waitSec`** (default 0: block at once, as before) makes the stage wait instead: before it takes the project and Editor leases the job cleans up and re-checks every 30 s for up to `waitSec` seconds (`--disk-wait SEC` overrides it per job), with `phase` `waiting-disk` and a `log` event with the free space and the minimum at each check; the wait does not count against the job timeout. It ends `BLOCKED` with the same reason plus the wait if nothing appeared, and at once, without waiting, when the free space plus what the other active jobs hold in their work folders would stay below the minimum. The job holds no lease while it waits, so it cannot keep the project or the slot from a job whose end frees the space; the check under the leases, just before the build, is still the last word. Independently of this setting, a job removes its build output from the work folder as soon as the build is published (or, if it could not be published, when the last stage is done: a player may run from its artifact), instead of at the end of the job. `valtrion doctor --json` reports `storage` (free bytes, the sizes of the work root, the run folders and the build cache, and whether the host is below `minFreeGiB`).

**Managed build checkouts.** Optional (off by default). `"projects": { "MyGame": { "remote": "git@github.com:owner/MyGame.git", "unityPath": ".", "default": "queue", "checkouts": [ { "id": "queue" }, { "id": "reference", "path": "~/valtrion-checkouts/MyGame/reference" } ] } }`. The key is `candidate.repo`. Each checkout has an `id` (letters, digits, `.`, `_`, `-`) and optionally an absolute `path` (or one that starts with `~`); without `path` it lives in `<valtrion home>/checkouts/<project>/<id>` (the valtrion home is outside the folders macOS guards with permission prompts and outside purgeable caches, and holds the rest of the working state, see above); `remote` is what a missing checkout is created from (optional when the folders exist), `unityPath` is the Unity project below the checkout root (default `.`), `default` is the id used by `--checkout auto` (default: the first). Valtrion owns these folders: a `build` stage resets one to the job's commit under its `unity-project:` lease, so clients never create or switch it. Checkouts must not overlap each other (same folder, one inside another). Each keeps its own persistent build folder (`<valtrion home>/incremental/<project>/<id>/`: Xcode export and DerivedData) for incremental builds. `remote` must be a git URL or path (not an option, not an `ext::` transport); an existing folder whose `origin` differs from `remote` is refused, because valtrion resets managed checkouts. Use one checkout for ordinary builds and another (for example `reference`) for long measurement runs. See [Managed checkouts and the commit at build time](cli.md#managed-checkouts-and-the-commit-at-build-time).

**`buildCache`: how long published builds are kept.** Optional. `"buildCache": { "maxEntries": 5, "keepDays": 7, "dirtyKeepDays": 1, "perSource": 1 }`; every key is optional and the defaults are the built-in limits (20 entries, 14 days, dirty builds 2 days). Each published build in `<valtrion home>/builds/<key>/` is large (an iOS `.app` is hundreds of MB), so a host that iterates on one change with many builds can lower the limits. A build is removed when it was not used for `keepDays` days (a dirty build, built from uncommitted changes, after `dirtyKeepDays`) or when more than `maxEntries` builds are newer in use. The limits apply to the cleanup after each publish, to `valtrion gc`, to the `disk.minFreeGiB` pressure cleanup and as the defaults of `valtrion builds gc` (its `--keep-days`, `--dirty-keep-days` and `--max-entries` win for one run). **`perSource`** (a positive integer, off by default) keeps only that many builds of one *source* and platform: with `1`, a new build from the same worktree replaces that worktree's previous build for the same platform instead of piling up next to it, and the other keys stay as a ceiling over all sources. The source is recorded in the build's manifest when it is published: `checkout:<project>/<id>` for a managed checkout (`--checkout`), otherwise `dir:<project folder>` (the resolved path, so each git worktree is its own source). Builds for another platform of the same folder are separate, and builds published before this setting existed have no source and are only subject to the other limits. The newest is chosen by publication time, so installing an old build by key does not make it outlive a newer one. Regardless of the limits, a build used in the last hour, the build a job has just published and the builds that active jobs depend on stay. `maxEntries` must be a non-negative integer and the day counts non-negative numbers; other values or unknown keys are a `host.json` error (a broken `buildCache` makes the cleanup after a publish skip silently, and `builds gc` and `gc` report `BLOCKED`).

**`unityAccelerator`: the Unity cache server.** Optional (off by default). `"unityAccelerator": { "endpoint": "127.0.0.1:10080", "namespacePrefix": "MyGame", "download": true, "upload": true }`. With it, every Unity batch build of the platform adapters (`ios`, `android`, `macos`, `windows`) adds `-EnableCacheServer -cacheServerEndpoint <endpoint> [-cacheServerNamespacePrefix <prefix>] -cacheServerEnableDownload true|false -cacheServerEnableUpload true|false`, so a fresh or recloned checkout downloads imported assets from the Accelerator instead of importing them all; batch mode does not reliably take these settings from the Editor preferences. `endpoint` is `host:port` (an IPv6 host in brackets; the Accelerator's default port is 10080), `namespacePrefix` (1–64 letters, digits, `.`, `_`, `-`) keeps projects apart and defaults to Unity's own, `download` and `upload` default to `true`. `valtrion doctor` shows the endpoint and whether it accepts a connection (`unityAccelerator` in `--json`); Unity's own behavior with an endpoint that does not answer (falling back to a local import) is not verified by Valtrion's tests.

**`leasesDir` (temporary).** An absolute path (or one starting with `~`) that replaces `<home>/leases`. It exists for the move from the earlier tool: set it to the absolute path of the old tool's leases folder, and the old and new CLIs share one lease directory, so they never both hold the same device. The lease file format is unchanged. Remove the setting once the old tool is no longer used.

**`unityEditorSlots`.** A positive integer: how many Unity processes (the `unity-editor` slots) the host runs at once. Without it the host allows 2, and 1 on macOS. Every provider counts this one total. Slots above the total are not looked at: when you lower it (or on macOS, where it is 1), a holder from an older version that still sits on a higher slot is ignored until it ends, so during a mixed-version transition one more Unity can run than the new total. `PlatformAdapter.unity_editor_slots` no longer exists; a third-party provider that set it loses that limit and must use `unityEditorSlots` here.

## Work root

Everything Valtrion builds, exports or checks out is disposable and lives under one work root that is not a folder the operating system protects. macOS asks the owner for permission (TCC) when a program that is not already trusted touches Documents, Desktop, Downloads, iCloud Drive or a removable volume, and a one-shot background job has no stable identity to remember the answer for. Repositories stay where they are; Valtrion keeps its own processes out of those folders.

| System | Default work root |
| --- | --- |
| macOS | `~/Library/Caches/com.valtrion/work` (not protected, not synced, outside backups) |
| Windows | `%USERPROFILE%\.valtrion\work` (beside the state home; short, local, not under `AppData`) |
| other | `$XDG_CACHE_HOME/valtrion/work`, or `~/.cache/valtrion/work` |

`valtrion doctor` warns for each of work root, artifacts root and state home that lies in a protected folder, and for abandoned job directories.

On Windows, `doctor` also checks for virtualized paths and for a work root that may exceed the path limit. A job runs the same checks at startup. These are warnings: they do not establish that a build will fit.

- **Virtualized location.** A path resolved under `AppData\Local\Packages\<name>\LocalCache` is reported. For a folder that does not yet exist, a packaged Python running against nominal `%LOCALAPPDATA%` can also trigger a warning that the location may be redirected. Existing paths are judged by their resolved location. The check cannot detect every kind of redirection.
- **Estimated length.** When Windows long paths are known to be off, `doctor` assumes a longest job id of 66 characters and 134 characters below the Unity project root. That leaves 43 characters for the work root under the 259-character usable path limit. The 134 is an estimate; generated files and projects nested inside a repository can need more. At job start, the estimate uses the actual job id. An unreadable long-path setting produces no warning.
- **Android builds.** The warning uses a historical observation of a Gradle/IL2CPP build file 247 characters below a project root. It recommends enabling Windows long paths and Git's `core.longpaths`; a shorter root alone cannot accommodate that example. This is not a measured limit for every project. Changing the system setting needs an administrator and may need a restart; Valtrion does not change it.
- **Measured checkout length.** `valtrion workroot checkout` measures the new checkout's tracked files. Before job cleanup, the complete checkout is measured again, including generated files, and an overlong path is logged as a `path-length` warning event. Both measurements run only on Windows when long paths are known to be off. They report the actual excess beyond 259 characters; they cannot predict files a later build will generate.

Use a short absolute work root on a local drive. Enabling Windows long paths is necessary for some builds but is not a guarantee: Git and every other tool in the build must also support long paths.

**At job start.** A job started with a work root (`valtrion job start`, pipelines) returns the same warnings in a `warnings` list of its start report; the job still runs. For the length check it uses the job's real id, so it warns only when this job's checkout can exceed the limit, not for the worst-case id. Doctor keeps the worst case.

Both warnings name the fix: set `workRoot` in host.json, or `GDV_WORK_ROOT` for one process, to a short absolute path on a local drive. Nothing derived from `GDV_JOB_DIR` changes: the job layout below stays the same, only its root moves.

```
<workRoot>/jobs/<jobId>/{checkout,build,export,derived-data,tmp}   everything one job creates
<workRoot>/jobs/inbox-<id>/                                         files a caller sent to run a plan or pipeline here (`valtrion workroot inbox`); removed with its job; otherwise swept when idle for hours and no active job uses it
<workRoot>/mirrors/<project>.git                                   bare clone the checkouts hang off (kept: it is the cache)
<valtrion home>/tools/valtrion-<hash>/                              the CLI staged for background jobs (not in the work root: it must outlive a purge of the cache folder)
<workRoot>/launchd/                                                job definitions and logs of macOS background jobs
```

### What a job receives

A command job started with a work root (and no `env` of its own, which would replace the environment) gets `GDV_WORK_ROOT`, `GDV_JOB_ID`, `GDV_JOB_DIR`, `GDV_JOB_CHECKOUT`, `GDV_JOB_BUILD`, `GDV_JOB_EXPORT`, `GDV_JOB_DERIVED_DATA`, `GDV_JOB_TMP` and `GDV_RUN_DIR` (the run folder under the artifacts root). Providers write build output, the Unity export and Xcode DerivedData into the first group, and logs, results and diagnostics into `GDV_RUN_DIR`.

### Checkout

`valtrion workroot checkout --project P --sha <40 hex> --job-id $GDV_JOB_ID (--source URL | --repo PATH)` fetches the commit into the mirror (from a URL, or from the origin of `--repo`; only that one `git fetch` reads a repository folder) and adds a detached worktree at `GDV_JOB_CHECKOUT`. Its `.git` points into the work root, so Unity, Xcode and `git` never read the original repository. The Unity `Library` is not copied between checkouts. Git LFS files are restored after the checkout whenever `git lfs ls-files` lists any; a tree that declares LFS on a host without a usable git-lfs is refused (`BLOCKED`), never left as pointer files.

### Cleanup

When a job ends, passed or failed, `<workRoot>/jobs/<jobId>` is removed and the mirrors are pruned of its worktree; results and diagnostics are already in the run folder. The directories stay when the run is retained, overlaps a `--protect` path, has cleanup turned off, or is a pipeline that has not saved `result.json`.

`valtrion gc` and `valtrion workroot sweep [--max-age-hours N] [--dry-run]` also remove job directories and launchd files of runners that died (a job counts as active only while its runner is verifiably alive or was created two minutes ago) and, when no job is active, staged CLI copies unused for that long. Repositories, baselines, retained runs and the mirrors are never removed. `valtrion workroot show [--json]` prints the work root and any protected-folder warning; `valtrion workroot release --job-id ID` removes one job's directories.

## Background jobs

A job runs in its own detached process in the run folder `<artifactsRoot>/<project>/<sha8>/<runId>/` with `job.json`, `events.jsonl`, `console.log` and, for pipelines, `result.json`.

- Windows: `DETACHED_PROCESS`, breaking away from a parent Job Object when allowed.
- Other systems: `setsid`.
- macOS: a one-shot launchd agent, tried in the `user` domain and then the `gui` domain (`--domain` chooses; `gui` is needed for signing). The label is `com.valtrion.<jobId>`. The job definition, its log and its working directory are under `<workRoot>/launchd`, and the job runs the staged CLI (copied once per content hash to `<valtrion home>/tools/valtrion-<hash>`; the runner also imports its modules and reads every contract schema at start, so it does not depend on that copy staying in place).

On macOS the job runs **the interpreter that runs `valtrion`** (the pipx or virtual environment interpreter it was installed with), not the system one. The job is refused if that interpreter lies in a folder macOS protects (Documents, Desktop, Downloads, cloud storage); install with pipx or `python tools/install.py` into an unprotected folder.

## Artifacts root

Run folders, retained results and baselines live under the artifacts root (`<home>/artifacts` by default, `artifactsRoot` in `host.json`, or the `artifactsRoot` of the host in `hosts.json`). `valtrion collect JOB --host H` pulls a remote run into the local artifacts root with the same layout.

## Leases

A lease is a lock file created exclusively under `leases/`, holding a `gdv.lease/1` document. The process that supervises the work renews it every third of the TTL. A lease is stale only when its holder process is dead **and** its TTL has expired; a live lease is never taken, and a stale one is reclaimed under a short guard so two claimants cannot both win. A run that finds the resource leased waits a bounded time and then reports `BLOCKED` naming the holder. A counted resource (concurrent Editors) is `N` leases `<name>:slot1..N`. The resource `unity-editor` is counted with the host's total (see `unityEditorSlots` above) wherever no slot count is given: `valtrion lease acquire unity-editor` and `valtrion job start --lease unity-editor` take a slot of that total, and an explicit count (`--slots N`, `--lease unity-editor=N`) is used as given. The lease held is one slot, `unity-editor:slotN`: `lease renew` and `lease release` must use the `lease.resource` that `lease acquire` printed, not the bare `unity-editor`. `valtrion lease name unity-project PATH` prints the lease name of one Unity project folder. Leases are always taken on the host that owns the resource; a remote caller runs `valtrion lease --host H`.

## Supervisor

Every step runs with a hard timeout and a stall window (default 600 s; 180 s for short operations; neither can be disabled). Progress is growth of the log or of watched files, an advancing `seq` in a heartbeat file (when one is given, log growth no longer counts, so a stuck app that spams its log still stalls), and, opt-in, CPU time of the process tree. A hung step is killed with its whole tree (a Windows Job Object, a POSIX process group) and classified `STALL` or `TIMEOUT`. A `GDV-RESULT <status>` line written by the app is stricter than its exit code.
