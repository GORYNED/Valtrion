# AI-agent setup: detailed guide

The short copy-paste prompt is in [AI-agent setup](ai-agent-setup.md). Use this longer prompt when an agent needs every step, for example for a full Unity integration with an Editor smoke test. Paste it into an agent working in your project. It first identifies the task and project type, then chooses a command check or the Unity integration. It works with an agent that has file and command access and needs no agent-specific plugin.

Installation sources, best first: Core from PyPI (`pipx install valtrion`, pin `==<version>` for shared projects); the Unity package from a release tag, `https://github.com/GORYNED/Valtrion.git?path=integrations/unity/com.goryned.valtrion.unity#unity-v0.10.0` (replace the tag with the newest `unity-vX.Y.Z` that `compat.json` pairs with the installed Core); or a reviewed checkout of Valtrion's HTTPS repository URL. A public checkout needs no GitHub account or token. Private forks require existing repository access. A missing required tool or permission is a blocker to report. The [Quick Start](quick-start.md) covers command jobs; the [Unity Quick Start](unity-quick-start.md) covers Editor integration. The [readiness record](release-readiness.md) separates observed evidence from unverified work.

```text
Use Valtrion for repeatable checks in this workspace, then prove the chosen path
works. First identify my intended check, the project type and existing validation
commands. Preserve files, dependencies, tests, pins and settings.

Choose the path before installing an application integration
- For an ordinary trusted command (tests, data validation or a build check), use
  Core command jobs following docs/quick-start.md and examples/python-check.
  Install Core with `pipx install valtrion` (record `valtrion --version`). Read
  https://github.com/GORYNED/Valtrion README.md, docs/install.md and
  docs/trust-model.md and inspect installed CLI --help before running work.
  Examples (examples/python-check) need a checkout or the release's source
  archive; record the resolved SHA if you use one. Install Core only. Do not install Unity or add Unity configuration.
  Run the real command through installed `valtrion job start -- ...` with --watch,
  bounded --stall-sec and --timeout-sec, explicit --cwd and the actual --sha.
  Inspect job.json, events.jsonl and console.log. Test a genuine failing check and
  timeout in isolated data too. Record process evidence; do not invent a successful
  gdv.result/1 or claim compare/report coverage from a command job.
- For a Unity app check, follow the concrete Unity steps below. A Unity project
  contains Assets, Packages and ProjectSettings. Do not upgrade or install an
  Editor into an unrelated project.
- For another application or engine needing in-app steps, explain that Unity is
  the only ready integration. Offer an existing command check if it serves the
  task; otherwise report the unsupported boundary. Do not invent profiles,
  result adapters or engine support. The roadmap describes future integration work.
- If the intended check cannot be inferred, ask what outcome should be verified
  before changing the project. The boundaries and handover rules below apply to
  both paths; Unity-specific steps apply only to a selected Unity project.

Handover for either path
- Inspect the complete diff. State changed files, source SHA, installed version,
  exact commands, exit codes, evidence paths and limits. State PASS only for
  observed evidence. Name missing tools or unavailable integration as BLOCKED.
  For command jobs, inspect real check output as well as status and make clear
  that --sha is a supplied label, not proof of a clean checkout. After this
  handover, a command-only task is complete; do not continue into the Unity path.

Boundaries
- Follow this workspace's AGENTS.md and approval rules. Inspect Git status first.
- Do not publish anything, change repository visibility, upload packages, create
  releases or tags, merge, rewrite history, change signing/security settings, or
  send logs to an external service.
- Run only trusted project code. Keep credentials in the normal credential manager.
  Never put tokens, personal paths or real device IDs in committed files or reports.
- Use synthetic data and isolated output. Never delete app/user data or stop an
  Editor/process that you did not start. Do not upgrade Unity to satisfy this task.
- Make temporary copies (a clean checkout of a branch, a dependency's test project
  at a pinned commit) only with `valtrion workroot scratch create --name <name>`
  (add `--project <name> --sha <full sha> --source <url>` for a checkout in its
  checkout/ folder), work only in the folder it prints, and release each one in the
  same task with `valtrion workroot scratch release <id>`. Never clone or copy into
  the work root, the repository or a temp folder by other means. Report a release
  that is refused (in use, not yours) instead of deleting anything yourself.

Unity path: discover and pin
1. Find the Unity project (Assets, Packages, ProjectSettings), its exact Editor
   version, Git root, existing test assemblies and package manifest. Identify
   existing project-specific validation before adding anything.
2. Obtain https://github.com/GORYNED/Valtrion.git in a separate checkout outside
   Assets, using existing access. If access is unavailable, report BLOCKED. Use
   the user-specified ref when provided; otherwise check out the release tag that
   matches the package you install (for example unity-v0.10.0), and use main only when
   the user wants unreleased code. Record the SHA.
   Do not apply new installer flags to an older checkout. Read
   README.md, docs/unity-quick-start.md, docs/install.md, docs/providers.md,
   docs/trust-model.md and
   integrations/unity/com.goryned.valtrion.unity/Documentation~/index.md there.
   Inspect source and current CLI --help before choosing commands. Record the full
   commit SHA. Pin Core and the Unity package to that reviewed checkout; consult
   compat.json instead of assuming their version numbers must match.

Install and integrate
3. Install Core: `pipx install valtrion` (or `pipx install valtrion==<version>` to
   pin). Only to test unreleased code, run `python tools/install.py --venv
   --home .valtrion-home` from the Valtrion checkout (use the discovered Python
   3.10+ command) and use the printed CLI path explicitly. Verify --version, schemas and validation of
   examples/smoke.plan.json from the checkout.
   If venv/pip cannot access the sandbox's temporary folder, follow the approved
   execution route or use a task-owned writable temp folder for this invocation
   as described in docs/install.md. Do not change shared permissions.
4. Merge the Unity package into Packages/manifest.json. Prefer a pinned Git URL for a shared
   project: ?path=integrations/unity/com.goryned.valtrion.unity#unity-vX.Y.Z (a release
   tag whose compat.json range includes the installed Core) or #<full SHA>. A local file: path is acceptable for a disposable verification but do
   not commit an absolute path. Preserve existing dependencies and testables.
   For package 0.10.0 and earlier add com.goryned.valtrion.unity to testables for
   PlanTest; from 0.11.0 on PlanTest needs no testables entry (remove it). Use the project's
   compatible Test Framework version (Valtrion's test project uses 1.8.0).
5. Reuse suitable existing tests, or copy examples/unity-smoke into a dedicated
   Assets/Validation test folder. Its assembly is Valtrion.SmokeTests. Copy
   examples/smoke.plan.json to validation/smoke.plan.json. Avoid duplicate assembly
   names. Add valtrion.json at the Git root with unityProject relative to that file
   and checks pointing to a real checks map. Start with
   examples/quick-start-checks.json, adjust its assembly name if needed, then run
   `valtrion check-config --checks <actual checks path>` using the installed CLI.
   Select target platforms that really exist; check selection is not execution.
6. Add output/cache paths to the project's ignore rules. Keep Unity-generated
   .meta files for the added assets; do not commit Library, Temp, credentials,
   device inventory, output or personal paths. Preserve unrelated manifest data.

Verify in Unity
7. Locate the exact licensed Editor from ProjectVersion.txt. Check that this
   project is not open elsewhere and that a test slot is available. Start a
   background batchmode PlayMode test with -assemblyNames Valtrion.SmokeTests
   (or the actual reused assembly), -testResults <absolute XML> and
   -logFile <absolute log>. Set GDV_PLAN to the copied plan's absolute path,
   GDV_OUT_ROOT to an ignored output folder, and GDV_RUN_ID to a fresh safe run ID.
   Use the commands in the Unity Quick Start for the current shell. Do not add -quit.
   Supervise the complete process tree: hard timeout, a no-progress limit,
   periodic log/result checks. Stop only processes you started if it stalls.
   If the project uses Git LFS, an agent's worktree often holds LFS pointer
   files instead of assets. `valtrion unity run` materializes them before Unity
   starts, from the host's shared LFS cache, and fetches only objects that are
   missing; its LFS-PREFLIGHT line says what it did. A BLOCKED result with an
   lfs.* code (for example lfs.git-lfs-missing or lfs.network-failed) is a
   blocker to report with the listed files. Do not import a project of pointers.
8. Read the Editor log and NUnit XML. Confirm a nonempty selected test set with
   no failures or unexplained skips, not just process exit zero. Read and validate
   <output>/<run ID>/result.json with `valtrion validate <file>`. Require PASS,
   the intended plan ID, target.evidence=editor and expected steps. Report every
   unverified field. This smoke proves wiring, not game correctness or physical
   device performance. Run the project's relevant existing regression tests too.
   With package 0.10.0 in testables, its deliberately malformed test.badsignature
   fixture can also appear as an ignored registration warning under unverified.
   Record that diagnostic and unknown candidate/host metadata explicitly; do not
   hide them or treat unrelated warnings as expected. They do not replace the
   required passing XML, intended plan, step statuses and smoke.complete mark.
9. After package resolution, run `valtrion doctor --project <Unity project> --json`.
   Require the project object's status=ok with the expected package/Core versions;
   skipped is not proof of integration. Distinguish missing unrelated mobile
   tools from the prerequisites of the Editor check. Never call an unavailable
   platform validated.

Project automation and handover
10. Make this Editor smoke repeatable through the project's existing test command
    or CI. If no command exists, add a documented, bounded project-specific runner
    using the verified Unity invocation above. If integrating Valtrion's `run`
    Editor provider, implement the exact runner interface in docs/providers.md:
    Python runner accepting --editor, --output, --assemblies and --timeout, with
    <output>/PlayMode.xml and forwarded logs. Configure profiles.json only after
    that runner exists and has been tested. Do not invent a built-in init command
    or call the placeholder build/install/launch commands. Device builds need
    project-owned build methods and authorized real targets; configure and test
    those only when available and within this task's authorization.
11. Run check-config again, inspect the complete diff and ensure the shared
    manifest uses a reproducible dependency pin. Summarize changed files, pinned
    versions/commit, exact commands, exit codes, test counts, artifact locations,
    limitations and the one command I should use next. State PASS only for
    observed evidence. If anything prevents completion, report BLOCKED with the
    precise cause and completed work. Do not claim future or unrun tests passed.
```
