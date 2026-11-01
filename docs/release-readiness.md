# Release readiness

This record separates current evidence from earlier verification. Repository
visibility remains **private**. This preparation does not publish packages,
releases or tags, merge changes, restore GitHub CI or change billing.

## CURRENT — repository and scope

The current repository is `GORYNED/Valtrion`, GitHub ID `1441424538`.
The verified origin is `https://github.com/GORYNED/Valtrion.git`. Preparation began
from a clean worktree at the sole initial commit
`a1ff1d226a6f1bd6570127b19d0164027ef652cb`; fetched `main` matched it.
The GitHub repository API confirmed private visibility during this session.

The owner recreated the repository from the sanitized snapshot. Old PRs,
discussions and historical rewrite instructions do not describe this repository.
No old working directory or backup was consulted. Earlier remote-erasure checks
are owner-provided history, not a new server-wide cache audit.

Valtrion Core can supervise ordinary commands without Unity. The Python catalog
example uses the installed CLI to check real data, propagate a failure and
enforce a timeout. Its evidence is job state, events and console output, not a
fabricated app result. Unity is the only ready app integration. Project discovery,
profiles and comparison metadata retain Unity-specific assumptions.

## HISTORICAL EVIDENCE — supplied from earlier preparation

The owner reported Windows / Python 3.14.4: 799 Core tests (793 passed, six
platform skips), 12 valid schemas, release checks, offline clean installation,
wheel and source archive rebuilding, and Unity 6000.6.7f1 EditMode with 107 passed.
Those observations apply to the snapshot transferred into the initial commit.
They are not fresh checks of this PR. The previous source/history audit reported
no confirmed credential and preserved MIT notices and technical contracts.

## CURRENT — verification of this preparation

See the PR's Local verification and independent-review record for the exact
candidate commit, commands and final counts. Raw local diagnostics stay ignored
under `.verification/`; they can contain machine paths and must be redacted
before sharing. A clean virtual environment is dependency isolation, not a
factory-clean operating system.

The first full Core run after resolving sandbox TEMP permissions exposed a torn
JSON read in the Android fake-tool fixture. Its two state writers now publish
atomically using Core's existing helper. A deterministic concurrent-reader test
fails with the old writers and passes with the fix. No Android provider behavior
was changed. Earlier failed attempts remain in local diagnostics rather than
being counted as passes.

| Check | Current observation |
| --- | --- |
| Repository identity | Correct origin and initial history; GitHub ID and private visibility confirmed |
| Remote refs | `git ls-remote origin` returned only HEAD and `refs/heads/main`, both at the initial commit, before this task branch was pushed |
| Core example | Fresh offline CLI installation; catalog PASS/0, duplicate SKU FAIL/1, delayed process TIMEOUT/3; actual console/status/timeout event checked; six job/event validations and `pip check` passed |
| Core suite | Windows / Python 3.14.4: `cd core` then `python -m unittest discover -s tests -t . -v`: 800 tests, 794 passed, six platform skips, zero failures/errors after the fixture fix |
| Contracts / source / docs | `python tools/check_contracts.py`: 12 schemas, zero problems; `python tools/check_release.py`: zero problems; local Markdown targets and `git diff --check` passed |
| Packaging | Direct wheel and sdist → wheel → fresh offline installation passed; installed version/schema/example checks and `pip check` passed; runtime dependencies empty; MIT payloads inspected |
| Unity preflight | Required 6000.6.7f1 installed; package declares minimum 6000.6; test project pins 6000.6.7f1 |
| Compatibility | Installed `valtrion doctor --project unity/TestProject --json`: `project.status=ok`, package 0.7.1 / Core 0.7.4; no Editor launched |
| Unity execution | BLOCKED: unrelated batchmode test Editors occupied the machine test slot at initial inspection and repeated checks. None was stopped; no Editor was launched by this task |
| Unity CLI | Installed 1.0.0-beta.15; latest-version check could not resolve Unity CDN; no update performed |
| Unity guide / agent steps | Source walkthrough checked manifest/testables, smoke assembly references, plan and PlanTest bridge; runtime walkthrough remains unverified |
| Hosted CI | Intentionally not restored or required for this task; no billing changes |
| Other platforms/devices | No fresh real-device or macOS/Linux execution evidence; provider unit tests with fakes do not establish it |

## KNOWN GAPS — decision before publication

**NO-GO for public release at this point.** Repository PlayMode tests and the full
smoke in a clean consumer Unity project remain unverified because the shared test
resource is busy. The Unity Quick Start is a complete procedure, not a claim of a
passed runtime walkthrough. Run it when the slot is free, inspect nonempty NUnit
XML, logs, marker and validated app result, then update this record.

An operational private security-reporting channel is also not confirmed. The
read-only GitHub API probe returned HTTP 404, which does not establish an enabled
channel. The policy explains the fallback, but a dedicated public reporting contact or enabled
private vulnerability reporting needs a maintainer decision before publication.
No response-time commitment is promised.

## INTENDED — next gate

Keep the repository private. Resolve the Unity evidence gap and security contact,
record a clean independent review, and let the owner decide publication separately.
Preserve `gdv.*`, `GDV_*`, launch arguments, result formats, license notices and
Core 0.7.4 / Unity package 0.7.1 compatibility through `compat.json`.
