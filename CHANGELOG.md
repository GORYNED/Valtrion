# Changelog: Valtrion Core

Core (the `valtrion` Python package) and the Unity package `com.goryned.valtrion.unity` are versioned independently. The Unity changelog is in `integrations/unity/com.goryned.valtrion.unity/CHANGELOG.md`; `compat.json` lists compatible pairs.

## 0.8.2

Host disk: a build host no longer fills up silently while builds queue.

- **Cleanup before a build (opt-in).** With `disk.minFreeGiB` in `host.json`, a `build` stage that finds less free space on the work root's volume first removes abandoned job folders (idle for an hour; running, retained and protected ones are never touched), run folders older than `disk.keepDaysUnderPressure` (default 2 days; active, retained and `--protect`ed runs are kept) and published builds beyond the cache retention.
- A published build that a still-active job depends on (it built or reused it and waits for the device, or installs it by key) is never removed by this cleanup, nor by `valtrion builds gc` / `valtrion gc` (the job records its key in its run folder). Other builds are removed by retention; they can be rebuilt.
- **Fail fast.** If the volume is still below the minimum, the stage ends `BLOCKED` with the free space, the minimum and the largest consumers; no toolchain is started (instead of `No space left on device` inside xcodebuild).
- **Early release.** The build output (and the folders the build registered as temporary) is removed from the job's work folder as soon as the build is published, or, when it could not be published, when the last stage is done (a player may run from its artifact). This applies to every host. A reused build needs no space.
- The periodic `progress` event of a running pipeline job (every `tickSec`, 15 s; it was already there and refreshes `lastProgressUtc`) now carries `phase` and `buildLogBytes` (the size of the build logs, which grows while Unity or xcodebuild work), so a watcher can tell a quiet build from a stuck one.
- `valtrion doctor --json` has `storage`: free bytes, the sizes of the work root, the run folders and the build cache, `minFreeGiB` and `belowMinimum`.

## 0.8.1

- Fix: the timeout of a pipeline job (`--timeout-sec`) no longer counts the time the job spends waiting for a lease. A long `--lease-wait` for the Editor slot or the device used to end the job with `TIMEOUT` before the run started; now a wait is bounded by `--lease-wait` alone and the timeout covers working time (the `progress` event adds `workingSec`).

## 0.8.0

Pipelines lease the device only while they use it, build while the device is busy, and reuse builds. Existing pipelines need no change.

- **Stage-scoped leases.** The `unity-project` lease and the Unity Editor slot are held for the `build` stage only and released right after it; the `device:<id>` lease is taken at the first `install`/`run` and released after the last `install`/`run`/`collect`. A job never holds the device while it waits for a build resource, nor a build resource while it waits for the device, so there is no wait cycle (see `docs/cli.md`). With one device and one Editor slot the next job builds while the previous one is on the device. New `release_one` next to `acquire_one` (and `JobContext.release`).
- A device that cannot be served at all is still `BLOCKED` before the build; with `--lease-wait 0` a device held by another job is too. With `--lease-wait N` the job builds first and waits for the device afterwards.
- **Fair lease waiting.** A waiter joins a FIFO line per resource (ticket files under `<leases>/queue/`) and only the head may take the lease, so the longest waiter wins instead of whoever polls first after a release. Tickets of dead waiters expire; a non-waiting caller does not overtake live waiters.
- `job.json` has an optional `phase` (`waiting-build`, `building`, `waiting-device`, `on-device`), mirrored by `progress` events with `data.phase`.
- **Published builds.** The artifact of a `build` stage is moved to `<home>/builds/<key>/` (outside the work folder that cleanup removes) and listed in `result.json` as `build` (path, sha256, size, candidate sha, dirty, profile, toolchain, key, reused) and in a `progress` event.
- **Build reuse.** A clean tree with the same commit, platform, profile (and its `profiles.json` entry) and toolchain (Unity version, Xcode version on iOS, the device an iOS build is signed for) is built once and reused. A dirty tree is never reused. `"args": {"reuse": false}` forces a build. `valtrion builds list|show|gc` and `valtrion gc` apply retention (14 days unused, dirty 2 days, 20 builds).
- **`valtrion build`** (build only, no device) is implemented, and `install` takes a published build by key: `stage.args.artifactKey` or `valtrion run --artifact-key KEY`.
- `gdv.result/1` gains optional `build`; `gdv.job/1` gains optional `phase`. `compat.json`: Unity package 0.7.x pairs with Core `>=0.7.0,<0.9`.

## 0.7.4

First release on PyPI (`pipx install valtrion`), published as a GitHub pre-release.

- Packaging: removed the `Private :: Do Not Upload` classifier, added Beta status, Python versions and project URLs. No runtime change.
- Releases are made by the manual Release workflow with PyPI Trusted Publishing; see `docs/releasing.md`.
