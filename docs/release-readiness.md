# Release readiness

This is the verification record for PR #1, prepared while the repository was
private. The owner subsequently authorized its squash merge and public launch as
an open-source pre-release. Merge/publication/security-setting results are recorded
in PR #1. Packages and a stable 1.0 release were outside that launch; the process for
PyPI and GitHub pre-releases is described in [releasing](releasing.md); Core 0.7.4
and Unity package 0.7.1 were published through it after this record. Exact tested
revisions and the final review are recorded in the PR; earlier results are
identified below. Actions settings and billing are not changed by this launch.

## CURRENT — product and repository

Valtrion manages repeatable checks and retains execution evidence. Core can
supervise ordinary commands without Unity. Unity is the first and only ready
application integration; platform providers are lifecycle/transport mechanisms,
not support for other engines. Project discovery, profiles and app comparison
metadata still contain Unity assumptions.

Repository: `GORYNED/Valtrion`, ID `1441424538`, origin
`https://github.com/GORYNED/Valtrion.git`. Preparation started from the sole initial
commit `a1ff1d226a6f1bd6570127b19d0164027ef652cb`. Work stays in the same PR #1.
No old working directory or backup was consulted.

The Unity package is `integrations/unity/com.goryned.valtrion.unity`; the repository
verification project is `integrations/unity/UnityProject`. Core 0.7.4 / Unity
package 0.7.1 are paired by `compat.json`. Package identity, APIs, CLI, wire formats,
launch arguments, `gdv.*`, `GDV_*` and Unity GUIDs are preserved.

## CURRENT — fresh local verification

Environment: Windows / Python 3.14.4 / licensed Unity 6000.6.7f1, revision
`b22767f04f04`, with a real Direct3D graphics device. Unity CLI 1.0.0-beta.15 was
confirmed current by its update check. Fresh environments isolate dependencies;
they do not constitute a factory-clean operating system or uncached network.

| Check | Observed evidence |
| --- | --- |
| Core | `cd core` then `python -m unittest discover -s tests -t . -v`: 804 tests, 798 passed, six platform skips, zero failures/errors after the current CI fixes |
| Contracts/source | `python tools/check_contracts.py`: 12 schemas, zero problems; `python tools/check_release.py`: zero problems |
| Repository EditMode | 107 tests passed, zero failed/skipped/inconclusive; nonempty NUnit XML and Editor log inspected |
| Repository PlayMode | 7 tests passed, zero failed/skipped/inconclusive; nonempty NUnit XML and Editor log inspected |
| Independent clean consumer | A fresh agent with no chat/product history followed the AI prompt, generated a new project with the Editor, and installed the real Git UPM URL pinned to `c492c31fb97acb4abf57526841c1a8ca1209ca9b`; lock source/hash and package cache inspected |
| Consumer smoke | First install and repeatable runner each selected one test: one passed, zero failed/skipped/inconclusive; valid `gdv.result/1`, PASS, intended plan, editor evidence, idle/wait/mark PASS and `smoke.complete` confirmed. A final revision recheck pinned UPM to `07d68371fa435c32d259a89030e5228bbf8054b3`, freshly reinstalled Core, and passed the same evidence gate |
| Consumer compatibility | Installed CLI doctor reported `project.status=ok`, package 0.7.1 / Core 0.7.4; generated project settings stayed unchanged across repeated smoke runs |
| Core example | On `07d68371`, fresh installed CLI produced real PASS/0, duplicate-SKU FAIL/1 and TIMEOUT/3, with checked console output and timeout event; all six job/event validations and `pip check` passed |
| Packaging | On `07d68371`, offline clean-archive installation, direct wheel and sdist → wheel → fresh install passed; version/schema/plan/check-map checks and `pip check` passed, runtime dependencies absent, full MIT notices checked |
| Paths/docs | All 115 integration files moved; all 48 `.meta` files preserved. Manifest/lock sibling package paths resolve. Local Markdown targets and diff whitespace checks passed |

The Unity package code at the tested pin is unchanged by the current Core fixes.
The independent agent used the public instructions, not an unpublished runner or
copied test project. Its consumer began with empty Assets and Editor-generated
settings/manifest; no repository Library or project settings were copied. The
agent created and exercised a project-owned runner as instructed. This establishes
installation and integration wiring, not game correctness, player builds or device
performance. Unity processes ran sequentially after the machine slot became free;
none belonging to another task was stopped.

The final revision recheck reused that consumer's established Library; only the
initial creation/import is described as pristine. Installed Core's changed
`lease.py` matched the final source hash. The repository Unity package C#/assembly
inputs are unchanged between the original Unity checks and the final Core fix.

The Editor emitted the existing `UAC0005` and empty Editor assembly warnings.
Direct smoke results retain unknown candidate/host metadata and the intentionally
malformed `test.badsignature` fixture registration diagnostic under `unverified`.
The guides now explain these observations; runs are not described as warning-free.

Raw diagnostics remain ignored under `.verification/`, including
`unity-final-20261101-082429/`, `prompt-trial/`, `release-final-core/` and
`lease-release-fix/`. They contain machine paths and must be redacted before sharing.
The earlier sandbox TEMP failures and fixture-race failure remain diagnostics,
not passing results.

## CURRENT — GitHub CI

GitHub Actions is enabled and executes real steps. [Run 41289633850](https://github.com/GORYNED/Valtrion/actions/runs/41289633850)
for `07d68371` completed successfully: **all 15 jobs passed**, including the full
Windows/macOS/Linux × Python 3.10–3.13 matrix, source/offline-install checks,
contracts and a Core job without Unity/mobile tooling.

The earlier run `41288397435` for `c492c31` had eight successful jobs and seven
failing matrix jobs. Its logs identified the issues fixed in the passing candidate:

- Windows: a test compared the short TEMP path with Git's expanded path as text.
  The assertion now compares resolved filesystem locations.
- macOS: a real-clock test required stall cleanup to finish within a 10% margin.
  The deadline assertion now uses the supervisor's injected clock; separate real
  process-tree tests still cover termination.
- macOS: an in-flight lease renewal could recreate a released file. Keeper
  publication and release now share the ownership lock. An event-barrier regression
  fails on the old code and passes with the fix.

No workflow, Actions setting or billing configuration was changed. These are code
and test fixes, not CI restoration. The older failing run is retained as failed;
it was not a billing failure. Credential-pattern checks on all 15 passing job logs
found no recognized token, private-key or credential-URL matches.

## CURRENT — public-file and history audit

The final source audit at `07d68371` covered seven local refs, all four reachable
commits, 375 Git objects, 314 unique UTF-8 blobs (2,155,889 bytes) and 277 tracked
files. PR #1's earlier body and available discussion timeline were also examined;
the final description records the new results. Pattern/entropy scanning and
manual triage found no confirmed credential, real personal path, unrelated private
project reference or external private dependency. All commit identities use GitHub
noreply. Core has no runtime dependencies; build tools, Unity modules and Test
Framework are public dependencies. License notices are included.

Unity's public sample/default-like project setting was reviewed rather than
misclassified as a live secret. Synthetic fixture identifiers and technical
contracts remain. The initial commit still contains obsolete audit/cleanup notes
and old discussion numbers; they were examined and contain no confirmed secret or
unrelated private-project disclosure. No history rewrite was performed.

This is a heuristic audit, not proof against every encoded or unfamiliar secret.
Ignored diagnostics, hidden server caches and external copies are outside the
publication candidate and the confirmed audit scope. The evidence-only readiness
update and final PR text receive a separate final consistency check. Git and
Python pattern/entropy checks were used; dedicated secret scanners were not
available. No claim of a hidden-server-cache or external-copy audit is made.

## CURRENT — private vulnerability reporting and owner action

During private preparation the repository's reporting-status API returned 404. GitHub documents Private
Vulnerability Reporting for **public repositories**; this response while private
does not prove a settings defect. See [GitHub's instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository).

At an explicitly authorized public launch, the owner must enable **Settings →
Advanced Security → Private vulnerability reporting → Enable**, confirm **Security
→ Advisories → Report a vulnerability**, and enable security-alert notifications.
A tested separate private contact is an alternative. A readiness assessment alone
does not authorize a visibility change; that requires the owner's separate launch
instruction. See [SECURITY.md](../SECURITY.md) for the reporting policy.

## CURRENT — final decision

**GO for the public pre-release source candidate.** Fresh Core, Unity EditMode,
PlayMode, actual Git UPM import and independent clean-project onboarding checks
passed. Packaging and the full hosted CI matrix passed on the final code revision.
The independent native review of `07d68371` against the initial base found no
actionable findings; its focused tests, old-code regression probe and retained
evidence inspection are recorded in PR #1. The earlier Unity-slot blocker is resolved.

This GO is not authorization to merge, publish or change visibility. The owner
still decides the public launch and must complete the reporting setup above.

Other engines, player/device targets and real application behavior are not newly
validated. There is no stable-release support/upgrade promise. Publication and the
security-reporting launch step remain the owner's separate decision. A later
evidence-only documentation commit does not change the tested code; its exact
head and review confirmation are recorded in PR #1.
