# AI-agent setup

Paste this prompt into Claude Code, Codex or any coding agent that can read files and run commands in your project. It works for any project and needs no plugin.

```text
Set up Valtrion in this project, then prove it works.

Valtrion runs a command as a supervised job with a timeout, stall detection,
a clear result and saved logs. Docs: https://github.com/GORYNED/Valtrion#readme

1. Read the project's AGENTS.md or contributing notes and run `git status`.
   Find the check I most likely want repeated (usually the test command) and
   how this project already runs it. If it is unclear, ask me before changing anything.
2. Install Core: `pipx install valtrion`, then `valtrion --version`.
   Needs Python 3.10+. If pipx is missing, use a virtual environment outside the repo.
3. Run my real check as a job and read the output, not just the status:
   valtrion job start --project <name> --sha <git sha> --label <label> --cwd . --timeout-sec <limit> --stall-sec <limit> --watch -- <my command>
   Project name: the repo folder name. Limits: stall 120 s and timeout 900 s
   unless the check needs different ones. The job directory it prints holds
   job.json, events.jsonl and console.log. If the command writes build output
   into the repo folder, name it with `--cleanup <relative path>` (repeatable):
   Valtrion removes it when the job ends (never tracked files) and job.json
   `cleanup` lists what was removed, kept and refused.
4. If this is a Unity project (Assets, Packages, ProjectSettings), also read
   https://github.com/GORYNED/Valtrion/blob/main/docs/ai-agent-setup-detailed.md
   and follow its Unity path. Pin the Unity package to a release tag, for example
   https://github.com/GORYNED/Valtrion.git?path=integrations/unity/com.goryned.valtrion.unity#unity-v0.16.0
   and check the pair with `valtrion doctor --project <Unity project>`.
   For any other engine only command jobs are supported: do not invent an integration.
5. Report: files changed, installed version, exact commands, exit codes, evidence
   paths and anything you could not verify. Say PASS only for what you observed.
   A command job's PASS means exit 0; it is not app or device evidence.

Rules: run only trusted code. Do not publish, tag, release, merge or change
settings, credentials or CI (the Valtrion package entry in Packages/manifest.json
and the test files this task needs are the only exceptions). Do not configure SSH
hosts or remote execution. Ask before installing global tools. Do not commit tokens, personal paths or device IDs.
Do not delete data or stop processes you did not start. Report a missing tool or
permission as BLOCKED instead of working around it.
Temporary copies (a clean checkout to reproduce something): create them only with
`valtrion workroot scratch create --name <name> [--project <name> --sha <sha> --source <url>]`,
work in the folder it prints, and release them in the same task with
`valtrion workroot scratch release <id>`. Never clone or copy elsewhere.
```

For the full step-by-step version, including a Unity Editor smoke test, use the [detailed guide](ai-agent-setup-detailed.md). A Unity project that uses Git LFS needs no extra setup in an agent's worktree: `valtrion unity run` turns LFS pointer files into their content before Unity starts, from a cache shared by every checkout on the host (see [`unity run`](cli.md#unity-run)). The [Quick Start](quick-start.md) shows the same flow by hand.
