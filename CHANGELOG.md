# Changelog: Valtrion Core

Core (the `valtrion` Python package) and the Unity package `com.goryned.valtrion.unity` are versioned independently. The Unity changelog is in `integrations/unity/com.goryned.valtrion.unity/CHANGELOG.md`; `compat.json` lists compatible pairs.

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
