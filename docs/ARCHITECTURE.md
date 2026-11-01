# Architecture

This page describes the components of Valtrion, the boundaries between them and the contracts that connect them, as they exist in this repository.

## Components

### Core (`core/`)

The `valtrion` CLI and library, distribution `valtrion` 0.8.1, Python 3.10 or newer, standard library only. It is delivered in this repository. It provides:

- Host inventory and selection: this machine first, then SSH hosts by capability, or a pinned host.
- Detached, supervised jobs with hard timeouts, stall detection and process-tree kill.
- Exclusive resource leases (devices, Unity Editor slots) with explicit release, expiry and recovery. A pipeline holds the build resources only during its `build` stage and the device only during its device stages, so builds overlap device runs; see [cli.md](cli.md#leases-by-stage-build-reuse).
- A host-local cache of published builds (`<home>/builds`), reused by commit, profile and toolchain.
- Build, install, launch, collect and cleanup through platform providers: `ios`, `android`, `windows`, `macos`, `editor`.
- Result validation, baselines and comparison, check selection for a change, and a work root outside protected folders.

The core does not require Unity, `adb` or Xcode to run; a missing tool makes only the work that needs it `BLOCKED`.

There are two execution paths. `commands._job_start` creates a command payload;
`jobs.run_job` supervises its argv directly without Unity project discovery or a
platform provider. It saves `gdv.job/1`, events and console output, not an app-side
`gdv.result/1`. See the [command example](../examples/python-check/README.md).

`run` and `pipeline` create pipeline payloads, select a platform provider and use
app-side plans/results. The documented complete app integration is Unity. Providers
handle platform lifecycle and transport, while the Unity package executes assertions
inside the app. A Windows or Android provider alone does not establish support for
another engine.

Unity assumptions remain in `project.py` (`unityProject`, `GDV_UNITY_PROJECT`),
project build/test profiles, provider build paths and comparison's required
`candidate.unity` / `unityVersion` key. The schemas also enumerate platform/evidence
vocabulary. These contracts are preserved; installing Core without Unity does not
make every pipeline or comparison application-neutral.

### Contracts (`contracts/`)

JSON schemas `gdv.*/1` and fixtures. They define every document that crosses a boundary: plans, pipelines, results, heartbeats, jobs, events, hosts, registry, leases, checks and baselines. The core validates against them; app-side runners produce documents that validate against them. See [contracts.md](contracts.md).

### Unity package (`integrations/unity/`)

`com.goryned.valtrion.unity` is the app-side runner for Unity: it reads the plan, runs the steps, writes the heartbeat and result. It is a separate component with its own SemVer and release lifecycle, developed under `integrations/unity/com.goryned.valtrion.unity/`. Its test project is `integrations/unity/UnityProject/`. It does not depend on the core to build, and the core does not need Unity to run. The two meet only through the contracts and the file protocol ([protocol.md](protocol.md)).

### Compatibility (`compat.json`)

Maps versions of the Unity package to the range of core versions that supports them. `valtrion doctor --project` checks a project against it, and `valtrion doctor --host` checks that a remote host runs the same core version.

## Boundaries

- The host never links against the app, and the app never calls the host. Everything is files and launch arguments: `gdv/inbox/plan.json` in, `gdv/out/<runId>/heartbeat.json` and `result.json` out, and one `GDV-RESULT` line in the log.
- The host runs on the machine that owns a resource. A caller on another machine uses SSH to run short commands there; a job is its own detached process and survives a dropped connection.
- Project code runs only where the project configured it (build methods, sign scripts, test runners). See [trust-model.md](trust-model.md).
- Disposable data lives under the work root; retained results live under the artifacts root. See [work-root.md](work-root.md).

## Stable wire identifiers

These are shared with already built apps and projects and are not renamed: schema ids `gdv.*/1`, launch arguments `-gdv*`, environment variables `GDV_*` that apps and tools read, and the `GDV-RESULT` line. A breaking change to a document is a new major id, not an edit.

## Versioning

- The core has its own version (`valtrion --version`) and its own CLI contract number.
- The Unity package has its own version.
- Documents are versioned by their id (`/1`).
- `compat.json` is the only place where package and core versions are tied together.

## Not part of this repository

Anything private to a particular application: signing identities, credentials, device identifiers and project-specific build methods. Examples in the docs use neutral names.
