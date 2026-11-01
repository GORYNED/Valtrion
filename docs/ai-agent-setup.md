# AI-agent setup prompt

Copy the prompt below into an agent working in your existing Unity project. It works as an instruction for Claude Code, Codex, Copilot or another agent with file and command access. It does not require an agent-specific plugin.

The agent must have access to the Valtrion repository while it remains private. A missing licensed Editor, repository permission or tool is a blocker to report, not permission to bypass it. The [Quick Start](quick-start.md) contains the concrete installation and smoke-test commands; the [readiness record](release-readiness.md) states what was actually verified.

```text
Install and integrate Valtrion into the Unity project in this workspace, then prove
that the integration works. Perform the work, not just a plan. Preserve my existing
files, dependencies, tests, package pins and project settings.

Boundaries
- Follow this workspace's AGENTS.md and approval rules. Inspect Git status first.
- Do not publish anything, change repository visibility, upload packages, create
  releases or tags, merge, rewrite history, change signing/security settings, or
  send logs to an external service.
- Run only trusted project code. Keep credentials in the normal credential manager.
  Never put tokens, personal paths or real device IDs in committed files or reports.
- Use synthetic data and isolated output. Never delete app/user data or stop an
  Editor/process that you did not start. Do not upgrade Unity to satisfy this task.

Discover and pin
1. Find the Unity project (Assets, Packages, ProjectSettings), its exact Editor
   version, Git root, existing test assemblies and package manifest. Identify
   existing project-specific validation before adding anything.
2. Obtain https://github.com/GORYNED/Valtrion.git in a separate checkout outside
   Assets, using existing access. If access is unavailable, report BLOCKED. Use
   the user-specified ref when provided. For this preparation, if main lacks
   docs/quick-start.md, use branch codex/open-source-readiness (PR #22).
   Do not apply new installer flags to an older checkout. Read
   README.md, docs/quick-start.md, docs/install.md, docs/providers.md,
   docs/trust-model.md and the Unity package Documentation~/index.md there.
   Inspect source and current CLI --help before choosing commands. Record the full
   commit SHA. Pin Core and the Unity package to that reviewed checkout; consult
   compat.json instead of assuming their version numbers must match.

Install and integrate
3. Run `python tools/install.py --venv --home .valtrion-home` from the Valtrion
   checkout (use the discovered Python 3.10+ command). Capture the printed CLI
   path and use it explicitly. Verify --version, schemas and validation of
   examples/smoke.plan.json. No package-index publication is required.
4. Merge the Unity package into Packages/manifest.json. Prefer a full commit-pinned
   Git URL using ?path=unity/com.goryned.valtrion.unity#<full SHA> for a shared
   project. A local file: path is acceptable for a disposable verification but do
   not commit an absolute path. Preserve existing dependencies and testables.
   Add com.goryned.valtrion.unity to testables for PlanTest; use the project's
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
   Use the commands in the Quick Start for the current shell. Do not add -quit.
   Supervise the complete process tree: hard timeout, a no-progress limit,
   periodic log/result checks. Stop only processes you started if it stalls.
8. Read the Editor log and NUnit XML. Confirm a nonempty selected test set with
   no failures or unexplained skips, not just process exit zero. Read and validate
   <output>/<run ID>/result.json with `valtrion validate <file>`. Require PASS,
   the intended plan ID, target.evidence=editor and expected steps. Report every
   unverified field. This smoke proves wiring, not game correctness or physical
   device performance. Run the project's relevant existing regression tests too.
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
