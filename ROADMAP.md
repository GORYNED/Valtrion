# Roadmap

Current implementation, known gaps and intended work; this is not a release schedule.

## CURRENT: implemented in this repository

- Core CLI and Python library: hosts and SSH, resource locks, detached jobs, pipelines, work folders, check selection, reports, baselines and metric comparison.
- Providers for `ios`, `android`, `windows`, `macos` and `editor`, with project-supplied build or test entry points.
- Unity package: development-only plan runner, custom steps, built-in steps, probes, heartbeat and structured results.
- Versioned JSON schemas and valid/invalid fixtures shared by Core and Unity contract tests.
- Checkout installation, SSH host synchronization, project discovery and Core/Unity compatibility checks.
- CI configuration for Core on Windows, macOS and Linux with Python 3.10 through 3.13, plus contract checks and tests without Unity/mobile tooling.

Implementation is not a claim that every platform passed an end-to-end run on the current commit. See the [release-readiness record](docs/release-readiness.md) for the checks actually completed and publication blockers.

## KNOWN GAPS

- No stable release or supported upgrade policy. The CLI and API may change before 1.0.
- Installation is from a repository checkout; package-index publication and release artifacts are not part of the setup instructions.
- Clean-machine verification on every supported host and real-device end-to-end coverage are incomplete until recorded with evidence.
- Adding the Unity package alone does not create project build methods or an Editor test runner.
- Core unit tests use fake platform tools. They do not replace Unity compilation, player builds, signing or device tests.
- The standalone `build`, `install`, `launch`, `push`, `pull`, `screenshot` and `logs` CLI commands are reserved placeholders. Use `run` and `pipeline`.
- There is no Linux player provider or app-side runner for another engine.
- Command jobs work without Unity, but do not generate `gdv.result/1` app results. Project discovery (`unityProject`, `GDV_UNITY_PROJECT`), profiles and comparison keys still have Unity assumptions; `run` and `pipeline` are not a general application integration API.
- There is no dedicated vulnerability-reporting contact or response-time commitment. See [SECURITY.md](SECURITY.md).

## INTENDED: before a stable release

- Define an application integration boundary beyond Unity, including project discovery and result/comparison metadata. Add a working integration example before claiming support for another engine or application. This release preparation does not redesign those contracts.

- Record reproducible installation and Unity integration checks from fresh environments, including platform-specific prerequisites and blockers.
- Maintain a tested platform matrix and an upgrade procedure for compatible Core/Unity pairs.
- Establish private vulnerability reporting and a sustainable support policy.
- Decide whether to distribute signed or integrity-checked release artifacts and package-index releases. Publication requires a separate maintainer decision.

## Out of scope for now

- Claims of support for engines other than Unity.
- A dependency on a particular AI agent.
- Running untrusted project code in a sandbox.
