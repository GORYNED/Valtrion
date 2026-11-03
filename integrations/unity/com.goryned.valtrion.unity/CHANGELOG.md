# Changelog

## Unreleased

- `ChecksDocument` reads and writes the new optional keys of `gdv.checks/1`: `runners` (name to command template) and `exclude` on an area (`ChecksArea.Exclude`). Documents without them behave as before. The package version is not changed here; a project that parses a check map with these keys needs a package release that contains this change.

## 0.7.1 (2026-10-30)

- First public release, tagged `unity-v0.7.1`: install with `https://github.com/GORYNED/Valtrion.git?path=integrations/unity/com.goryned.valtrion.unity#unity-v0.7.1`.
- Move the integration to `integrations/unity/com.goryned.valtrion.unity` and its verification project to `integrations/unity/UnityProject`. Update local and Git installation paths. Package identity, API, GUIDs and version are unchanged.
- Fix: the fixture-based contract tests (`ContractFixtureTests`) now end Ignored, not failed, when no `contracts/fixtures` folder exists above the package folder (a consumer project with the package in `testables` and no repository checkout). The repository verification project is now `integrations/unity/UnityProject`; see release readiness for current execution evidence.
- Document that `PlanTest` and `NUnitXmlResultWriter` live in the test assembly `Valtrion.Unity.PlayModeTests`, which Unity compiles only when the package is in the consumer's `testables`, with the manifest snippet.
- The Runtime assembly `Valtrion.Unity` is unchanged.

## 0.7.0 (2026-10-30)

Moved from the previous validation package `com.goryned.unity.validation` 0.6.9 into the Valtrion repository and renamed. Behaviour and the public API shape are unchanged apart from the names below; wire identifiers are unchanged.

- Package `com.goryned.unity.validation` is now `com.goryned.valtrion.unity` (display name `Valtrion Unity`).
- Assemblies: `GORYNED.Validation` -> `Valtrion.Unity`, `GORYNED.Validation.Editor` -> `Valtrion.Unity.Editor`, `GORYNED.Validation.Tests` -> `Valtrion.Unity.Tests`, `GORYNED.Validation.PlayModeTests` -> `Valtrion.Unity.PlayModeTests`. Namespaces `GORYNED.Validation[.X]` -> `Valtrion.Unity[.X]` (`Json`, `Contracts`, `Harness`, `Probes`). `Runtime/link.xml` preserves the renamed assembly.
- Unchanged: schema ids (`gdv.*/1`), step type names, launch arguments (`-gdv-*`), environment variables (`GDV_*`) and the `GDV-RESULT` log line, so plan files, their SHA-256 and baselines stay valid.
- The host CLI and the schemas no longer ship inside the package (they live in `core/` and `contracts/` of the repository). The Editor contract tests read the golden fixtures from `contracts/fixtures` of the repository checkout.
- Add the test project (now `integrations/unity/UnityProject`).
- Documentation rewritten for Valtrion, with the rename mapping.

## Earlier history (previous validation package, Runtime and contracts only)

- 0.6.7: the C# `PipelineDocument` reads and writes `prelaunchFiles` on a stage and an absent `candidate.sha`. A step that throws fails with the exception type, its message and the first stack frame (`Faults`).
- 0.6.6: the harness honours every step's `timeoutSec`; the `screenshot` step ends `BLOCKED` at once when no graphics device renders frames, and the per-frame `Heartbeat()` ends a step still running one second after its timeout as `TIMEOUT`.
- 0.6.0: the development-only Runtime harness (`PlanInbox`, `PlanRunner`, `StepRegistry`, built-in steps, probes, `ResultWriter`, `CleanupLedger`), `PlanTest.Run` and the NUnit XML writer; the package declares the built-in modules `imageconversion` and `screencapture`.
- 0.1.0: the Runtime contracts and the reflection-free JSON reader and writer.
