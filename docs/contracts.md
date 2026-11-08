# Contracts

This page describes the JSON contracts: which documents exist, what statuses and evidence kinds mean, how they are versioned and how to add or change one. The schemas in `contracts/schemas` and the fixtures in `contracts/fixtures` are the single source of truth; this page summarizes them.

## Schemas

Every document has a `schema` property holding its id. Closed objects reject unknown properties; open objects (for example step arguments and event data) are free-form.

| Schema | File | Purpose |
| --- | --- | --- |
| `gdv.plan/1` | `*.plan.json` | What to run: `id`, `kind` (`functional`, `performance`, `soak`, `smoke`), `requires` (platforms, capabilities, profile, evidence), `dataPolicy` (`isolated`, `devdata`, `clean-install`), `budgetSeconds`, `repeat`, `warmup`, `continueOnFail`, `setup[]`, `steps[]` (each a `type`, a mandatory `timeoutSec` and free-form arguments), `metrics{}` (`unit`, `direction`, `budget`, `regressionPct`, `minSamples`), `cleanup` (`ledger` or `none`) |
| `gdv.pipeline/1` | `*.json` | Build, install, run, collect: `candidate` (`repo`, optional full SHA or all zeros, `profile`), `device` (`class`, `id`, `platform`), `stages[]` (`stage` is `build`, `install`, `run` or `collect`; `plan`, `variant`, `repeat`, `coldLaunch`, `timeoutSec`, `args`, `externalUi` (`command`, `timeoutSec`), `prelaunchFiles[]`, `cleanup[]` (run stages: outputs relative to the project folder, removed when the job ends)), `compare` |
| `gdv.result/1` | `result.json` | The outcome: `runId`, `status`, `reason`, `candidate`, `target` (platform, device class, `osMajor`, `evidence`), `host` (`id`, `transport`, `selectedBy`, `tools`), `plan` (id and SHA-256), `layers` (compile, export, nativeBuild, sign, install, launch, scenario), `steps[]`, `metrics[]`, `comparison`, `cleanup` (optional: declared outputs `removed`, `kept`, `refused`), `pipelineRun` (optional, written by every pipeline job: what became of each stage, `ran`, `carried` over from an earlier run, `reused` or `not-run`, see [cli](cli.md#selected-stages-reused-results-and-repeats)), `reusedFrom` (optional: the run, job and cache key a reused run stage result came from), `artifacts[]` and `unverified[]` (both required: an empty list is a claim, omission is not). A result of a check Valtrion did not run as a plan (L0, Editor suite, player build) has the check key of `valtrion select` as `plan.id` and the SHA-256 of that text as `plan.sha256`; `valtrion result write` and `valtrion build` write it, see [cli](cli.md) |
| `gdv.heartbeat/1` | `heartbeat.json` | Rewritten atomically by the app every few seconds: `seq`, `state`, current step, `elapsedMs`, optional host `request` |
| `gdv.job/1` | `job.json` | State of a detached job: `state`, `status` (null until finished), `watchdog`, `eventsOffset`, `cleanup` (optional, once a job with declared outputs has ended: `base`, `removed`, `kept`, `refused`) |
| `gdv.event/1` | one line of `events.jsonl` | Append-only log a watcher follows |
| `gdv.hosts/1` | `hosts.json` | Machine-local host inventory: per host `id`, `transport` (`local`, or `ssh` with `sshTarget` and `connectTimeoutSec`), `os`, `roles`, declared `capabilities`, `projectsRoot`, `worktreePolicy`, `artifactsRoot`, `valtrionCommand` (absolute path of the command on an SSH host: the pipx shim, or the stable `~/.valtrion/bin/valtrion` link; older files with a versioned path stay valid and are replaced by the next `hosts sync`), `python` (absolute path of the host's Python 3.10+). Ids are unique, at most one host is local. Kept outside git. |
| `gdv.registry/1` | `registry.json` | Machine-local device registry: class, capabilities, exclusivity; each device belongs to a `hostId`; `online: false` marks a device the last discovery did not find |
| `gdv.lease/1` | lease file | Owner, session, pid, host, TTL, renewal |
| `gdv.checks/1` | project `checks.json` | Path globs (with optional `exclude` globs) to kinds; kinds to checks per level `L0` to `L4`; optional `runners` (name to command template); optional `contentRules` (cosmetic-only changes of matching paths get a cheaper kind); optional `extends` and `bindings` (a built-in versioned preset such as `valtrion:unity/1` supplies path rules and classes, the project binds its checks to the slots `editor`, `player`, `device`; an area may carry `overridesPreset`); optional `jsonRules` (a changed JSON file is classified by the keys that changed; a key may carry `allowHeavyFallback` to accept a `*` fallback whose kinds hold player or device checks); optional `equivalenceRules` (a project command can prove a change of a matching path value-preserving, which gives it a cheaper kind); `unclassified` is `widest` or `fail` |
| `gdv.baseline/1` | project `baselines/...` | Summary statistics per metric under a comparability key; editor evidence cannot be a baseline. Several baselines of one plan may share its folder; they are told apart by `planId`, `key` and `source.evidence`, not by file name (see [cli.md](cli.md)) |

`gdv.common/1` (`common.schema.json`) holds shared definitions and is not a document type. Build profiles (`profiles.json`, id `gdv.profiles/1`) are read by the providers, not by a schema in `contracts/`; see [providers.md](providers.md).

`valtrion schemas` prints the document ids, `valtrion validate FILE` checks a document.

## Statuses and exit codes

| Status | Meaning | Exit code |
| --- | --- | --- |
| `PASS` | all checks passed | 0 |
| `FAIL` | a check failed, or a metric regressed | 1 |
| `BLOCKED` | a prerequisite is missing; nothing was proven | 2 |
| `NEEDS_PERMISSION` | the owner must act (unlock, trust, grant); the text says what | 2 |
| `STALL` | no progress for the stall window | 3 |
| `TIMEOUT` | the time budget was exceeded | 3 |

A run that finds an exclusive resource leased waits a bounded time and then reports `BLOCKED` naming the holder; an exhausted wait is never `TIMEOUT`. Per-layer outcomes in a result use the same statuses; a layer that did not run is absent or `BLOCKED`, never inferred from a neighbour. A `BLOCKED` or `NEEDS_PERMISSION` result is never reported as a pass.

## Evidence

A result says what produced it: `physical` (a real device), `emulator`, `editor` (the Unity Editor; never device evidence) or `host` (the build or host machine only). Only `physical` evidence supports a device performance claim. A baseline of another kind is labelled by its evidence and never stands in for it.

Comparison verdicts are `PASS`, `REGRESSION`, `IMPROVED`, `NOISY`, `NOT_COMPARABLE`. `REGRESSION` exits 1, the two unusable verdicts exit 2.

## Rules beyond the schemas

The validator also enforces: an `external.ui` step must not run between `probe.start` and `probe.stop` in a `performance` plan; a `PASS` result has no `reason` and no layer that is not `PASS`, any other status carries a `reason`; host ids are unique, at most one host is `local`, an `ssh` host names its target and a `local` host has none, and a target never starts with `-`.

Parsing is strict: no comments, trailing commas, duplicate property names, NaN or infinity, nesting deeper than 64 levels; counts, seconds and process ids are capped at 2147483647. A text file is read as UTF-8 with at most one byte-order mark. A file that cannot be read or decoded makes `valtrion validate` exit 2; malformed content exits 1. `events.jsonl` is split on `\n` only.

## Versioning

The id ends in the major version, for example `/1`. An additive change to an open object is not a new version. A breaking change adds `/2` next to `/1`; `/1` is not edited incompatibly. The wire identifiers (`gdv.*/1`, `-gdv*` arguments, `GDV_*` variables, `GDV-RESULT`) are stable and are not renamed. The CLI has its own contract number (`valtrion version`), which changes only with a breaking change of the command surface.

The schemas exist once, in `contracts/schemas`. An installed wheel carries a copy under `valtrion/_schemas`, and the validator uses it when present.

## Fixtures

`contracts/fixtures/valid` has one document per schema (and an `events.jsonl`, and a few more for optional keys, such as `checks-runners-exclude.json`) that must be accepted. `contracts/fixtures/invalid` has files named `<schema-name>-<reason>.json` that must be rejected. `python tools/check_contracts.py` checks that schema ids match file names, every fixture is well-formed, and each fixture is accepted or rejected as its folder says.

## Adding or changing a contract

1. Write or change the schema in `contracts/schemas/<name>.schema.json`. The `$id` is `gdv.<name>/<major>`; the file name starts with `<name>`.
2. Add a valid fixture, and an invalid fixture for each new rejection, named `<name>-<reason>.json`.
3. If the rule cannot be expressed in the schema, add it to the semantic checks in `core/valtrion/schema.py`.
4. Register a new id in `core/valtrion/constants.py` (`SCHEMA_IDS`).
5. Run `python tools/check_contracts.py` and the core tests.
6. A breaking change gets a new major id; do not edit the old one incompatibly.
