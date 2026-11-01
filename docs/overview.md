# Overview

This page explains what Valtrion does, which parts it consists of and how a validation run flows through them. Read it first; the other pages go into detail.

## What it does

A **plan** describes what to verify in an app: steps, time limits, metrics. Valtrion builds the app for a platform, installs it on a device, starts it with the plan, watches it, collects the result and compares metrics with a baseline. Every outcome is one of a small set of statuses, and every result says what kind of evidence produced it. A run that could not prove something says so (`BLOCKED`, `NEEDS_PERMISSION`) instead of passing.

## Parts

| Part | Role |
| --- | --- |
| **Core** (`valtrion` CLI and library) | Runs on the machine that drives the work and on every host that owns a resource. Selects the host, takes leases, builds, installs, launches, supervises, collects, compares. |
| **Providers** | One per platform: `ios`, `android`, `windows`, `macos`, `editor`. Each implements build, install, push/pull files, launch, terminate, screenshot and health for its platform. See [providers.md](providers.md). |
| **Contracts** | JSON schemas `gdv.*/1` for plans, pipelines, results, heartbeats, jobs, events, hosts, registry, leases, checks and baselines. See [contracts.md](contracts.md). |
| **App-side runner** | Code inside the app that reads the plan, runs the steps and writes the heartbeat and result. For Unity this is the package `com.goryned.valtrion.unity`, which has its own version; `compat.json` says which core versions each package version works with. The host and the app meet only through the files and arguments in [protocol.md](protocol.md). |

## Hosts

The machine you run `valtrion` on is always a host. You can add SSH hosts (`valtrion hosts add`). For a request (a platform, a build, a device) the core runs locally when this machine can satisfy all of it, otherwise on the first remote host, in id order, that can. A request no host can satisfy is `BLOCKED` with one reason per host. A pinned host (`--host`) is used or the request is `BLOCKED`, never replaced. Resources such as devices and Unity Editor slots are leased on the host that owns them.

## A run

1. `valtrion run <plan>` or `valtrion pipeline <file>` picks the host and starts a detached, supervised **job** there. The command returns the job id; `--watch` follows the job to its end.
2. The job builds (under an Editor slot lease), installs, pushes the plan into the app's data folder, launches the app, and watches the heartbeat. A heartbeat that stops advancing is `STALL`; the plan's time budget is `TIMEOUT`.
3. The job collects the result, validates it against `gdv.result/1`, saves it in the run folder and removes temporary build data.
4. `valtrion compare` and `valtrion baseline promote` compare against and update baselines.

State is kept as files: a run folder per job under the artifacts root holds `job.json`, `events.jsonl`, `console.log` and `result.json`. A watcher reads them from a byte offset, so a dropped connection never changes the outcome of a job.

## Statuses and exit codes

`PASS` 0, `FAIL` 1, `BLOCKED` 2, `NEEDS_PERMISSION` 2, `STALL` 3, `TIMEOUT` 3. Details are in [contracts.md](contracts.md).

## Choosing checks for a change

A project can map changed files to the checks a change needs (`valtrion select`) and print a verification block for a pull request (`valtrion report`). See [cli.md](cli.md).

## Where to go next

[install.md](install.md) to install, [cli.md](cli.md) for commands, [trust-model.md](trust-model.md) for what runs project code.
