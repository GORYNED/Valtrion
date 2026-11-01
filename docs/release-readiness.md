# Public release readiness

## CURRENT — decision: NO-GO

Initial audit date: 2026-10-31. PR #22 was subsequently squash-merged with explicit owner approval. The owner also authorized history cleanup and excluded restoration of hosted CI from this task. Repository visibility remains private; no package, release or new tag was published.

The reviewed source preparation is in main. Authorized history cleanup anonymizes private context and identifiers in writable branch/tag history while retaining commits and tag names. GitHub-retained PR references, cached views and discussions remain separate surfaces; this record does not claim server-wide erasure. Publication is not authorized.

## HISTORICAL EVIDENCE — audit scope

The audit inspected tracked source, configuration, tests, examples, public documentation, package dependencies, Unity identity/signing settings and Git metadata. Credential checks covered common token formats, private-key markers, credential assignments, suspicious filenames, paths, URLs, emails, device identifiers and project provenance. Findings were reviewed rather than treating every pattern match as a secret.

Remote references were enumerated with `git ls-remote` against the verified origin. The baseline remote had one main branch, five tags and 20 retained `refs/pull/*/head` references. Fetching PR heads into a local audit namespace did not change remote branches or the checkout.

| Scope | Observed coverage |
| --- | --- |
| Initial working tree | 255 tracked files |
| Initial locally reachable history | 56 commits and 468 unique UTF-8 text blobs |
| Remote main and five tags | 23 commits and 371 unique text blobs; tags add no ancestry beyond main |
| Remote main, tags and retained PR heads | 73 commits, 260 trees and 484 unique text blobs (817 objects) |

No credential was confirmed by this inspection. This is a heuristic audit, not proof that an unfamiliar or encoded secret cannot exist. No submodules, private dependency URLs, bundled binaries or third-party source attribution banners were identified. Core runtime dependencies are empty. Hatchling and the build frontend are development/build tools; Unity dependencies are official modules and the Test Framework.

The GitHub API audit additionally covered 20 PRs, one ordinary issue and six conversation comments, including closed items. There were no inline review comments or submitted reviews. Private context was found in PRs #1, #3, #4, #5, #16, #17 and #21, issue #19, and conversation comment 6547860217. These are cleanup targets, not invitations to copy their contents into another public surface. No credential, personal email or full device identifier was confirmed in those discussions. No recognized attachment/media links were found. No LFS pointers or `.gitattributes` files were present in the 484 inspected remote-history blobs; no external LFS payload was indicated. Zero release records were returned by the releases API.

The root MIT license and same-owner import trail are visible in source. The audit cannot independently certify ownership of imported code or rights not recorded in the repository. Maintainers must resolve any known ownership restrictions before publication.

The current Unity test project uses generic company/product names, empty signing and cloud identity fields, and disabled cloud integration. Generated asset GUIDs and compatibility identifiers are not personal credentials.

## CURRENT — changes in this preparation

- Replaced an owner-associated SSH username and a plausible physical-device identifier in tests with synthetic values. Replaced a test LAN address with a documentation address.
- Removed unrelated private-project context from current documentation. Preserved `gdv.*`, `GDV_*`, launch arguments, step names, schemas and device-class contracts.
- Included the full MIT notice in installer/host-sync wheels and the Unity package. Made Python source archives rebuildable without missing parent-directory files.
- Added workspace-local installation through `tools/install.py --venv --home DIR`. Kept the package's private-upload guard.
- Rewrote the README, contributor/security policies and roadmap. Added a human Quick Start, agent prompt, a built-in-step smoke plan and a Unity test bridge. Distinguished implemented providers from actually tested platforms.
- Added source/version/license/example checks and CI coverage for offline installation and source-archive-to-wheel installation. Added issue/PR templates and recorded the test project's resolved package lock.

## HISTORICAL EVIDENCE — verification

Local environment: Windows, Python 3.14.4, Unity Editor 6000.6.7f1, Core 0.7.4 and Unity package 0.7.1. Python 3.10–3.13 remain the configured CI matrix; a local 3.14 run does not establish that whole matrix. Fresh virtual environments and a new Unity Library test dependency isolation, not a factory-clean operating system. Existing licensed tools and Unity's shared cache/Accelerator remain available.

| Check | Evidence and result |
| --- | --- |
| Contracts | `python tools/check_contracts.py`: 12 schemas, zero problems |
| Source metadata and examples | `python tools/check_release.py`: zero problems; versions pair through `compat.json` |
| Offline checkout installation | `PIP_NO_INDEX=1 python tools/install.py --venv --home .valtrion-home`: passed in a new environment; repeated from a clean Git archive without `.git`, generated environments or `PYTHONPATH`; installed version, plan/check-map validation and `pip check` passed |
| Standard packaging | `python -m build --no-isolation --wheel core --outdir .venv/direct-dist` and `python -m build --no-isolation core --outdir .venv/dist`: passed with Hatchling 1.33.0; second command rebuilt a wheel from the source archive |
| Distribution installation | Fresh environment installed the rebuilt wheel with `pip install --no-index --no-deps`; installed version, validation and `pip check` passed; inspected license payloads |
| License redistribution | Regression test builds a wheel, extracts it as an installed package and rebuilds it; both retain the full MIT notice |
| Core suite | `cd core` then `python -m unittest discover -s tests -t . -v`: 799 tests, 793 passed, six platform-dependent skips, zero failures/errors; Windows / Python 3.14.4 |
| Unity EditMode | 107 passed, zero failed/skipped, on a new `unity/TestProject/Library`; background Editor with graphics, NUnit XML inspected |
| Unity PlayMode and consumer smoke | BLOCKED: another task's batchmode test Editor holds the machine's test slot; no process belonging to that task was stopped. Not counted as passed |
| Compatibility | Installed `valtrion doctor --project unity/TestProject --json`: `project.status=ok`, Core 0.7.4 / Unity 0.7.1 |
| Documentation | Local Markdown targets exist and `git diff --check` passed |
| Hosted CI | Not verified: recent runs stop before steps because of the account billing/spending limit. No product test failure can be inferred from those runs |
| Other platforms and devices | Not verified on macOS, Linux, iOS, Android or Windows players for this candidate; provider unit tests use fakes |

Unity commands used the exact installed Editor with `-batchmode -projectPath <project> -runTests -testPlatform EditMode -assemblyNames Valtrion.Unity.Tests -testResults <xml> -logFile <log>`, under a process-tree watchdog (900-second hard limit, 180-second no-progress limit, log and CPU progress signals). No `-quit` or `-nographics` was used. The log confirmed a real graphics device. Compilation reports the existing `UAC0005` warning in `StepRegistry` and an empty Editor assembly warning; these were not represented as a warning-free build.

Raw logs are intentionally excluded from Git because they include local paths and host/device details. The PR records exact tested revisions, counts and review results without copying that data.

The final Core run used the normal short system TEMP outside Git. Earlier environment-only failures were resolved without changing executable behavior. History cleanup preserves the reviewed executable source. The independent source review and local evidence are recorded in PR #22; rewritten commit identifiers are mapped in the private backup records.

## KNOWN GAPS — publication blockers

1. **Retained GitHub history and discussions are not fully sanitized.** Writable branch and tag histories can be rewritten, but GitHub's read-only PR references and cached commit views may still retain earlier private context and owner-associated identifiers. The discussion disclosures identified by the audit also remain. Removing these requires separate handling; rewriting main alone cannot erase them.
2. **The new end-to-end Editor Quick Start still needs its final smoke evidence.** The consumer smoke and repository PlayMode suite are blocked by another task's active test Editor. Do not advertise them as verified until the consumer result and nonempty NUnit XML pass. A busy resource is not a product failure.
3. **Public security reporting needs confirmation.** The private-reporting API did not confirm an enabled channel for this private repository. Before opening access, provide an operational private contact or enable the repository's private vulnerability-reporting feature. No response SLA is promised.

## INTENDED — safe publication path

The owner authorized a coordinated history rewrite after merging PR #22. A full private mirror and verified Git bundle preserve the pre-cleanup history; the isolated rewrite retains a private commit/ref map and is rescanned and independently reviewed before updating writable refs. Existing clones still contain old history: use a fresh clone after cleanup, and do not merge or mirror-push old refs back. GitHub-retained PR references and cached commit pages need separate handling; GitHub Support determines whether a removal request qualifies. Do not publish backup bundles, raw audit output or real identifiers. See [GitHub history-removal guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).

Before GO, verify the actual history that will become accessible, sanitize the identified GitHub discussions and any retained copies, resolve the smoke-test gap, confirm a private security channel, and record the independent review of the final candidate. Server-hidden refs/caches and unrecognized attachment formats remain outside the confirmed audit scope. The authorized rewrite is limited to cleanup; publication, new tags, package uploads, releases and further merges still require owner authorization. Hosted CI is not a required gate for this task and no billing/workflow settings are changed.
