# Installing

Install Core from PyPI with `pipx`, or from a checkout. Start with the [Quick Start](quick-start.md) for a command check without Unity or use the [AI-agent setup prompt](ai-agent-setup.md). For Unity, add the package separately using the [Unity Quick Start](unity-quick-start.md). Public HTTPS cloning needs no GitHub account or token; private forks require normal repository access.

## Requirements

Python 3.10 or newer. Nothing else is installed at runtime (standard library only). The tools a platform needs (Unity Editor, Xcode, `adb`) are probed by `valtrion doctor`; a missing one is reported, not required to install.

## From PyPI (recommended)

```
pipx install valtrion              # latest release
pipx install valtrion==0.7.4       # a specific release
pipx upgrade valtrion              # update later
valtrion --version
```

Without pipx: `python -m venv <dir>` then `<dir>/bin/pip install valtrion` (`Scripts` on Windows). `pipx` puts `valtrion` on your PATH (`pipx ensurepath`). The package has no runtime dependencies. Releases are published by the manual [Release workflow](releasing.md) through PyPI Trusted Publishing; Core `0.7.4` is the first PyPI release.

## Unity package (UPM, from Git)

The Unity package has its own version and is not on PyPI. Add it to `Packages/manifest.json` (or Package Manager → Add package from git URL):

```json
"com.goryned.valtrion.unity": "https://github.com/GORYNED/Valtrion.git?path=integrations/unity/com.goryned.valtrion.unity#unity-v0.17.0"
```

Since package 0.11.0 the `PlanTest` helper is an ordinary package assembly and no `testables` entry is needed (remove it when upgrading); the package's own tests run only in the repository's verification project. Projects that stay on package 0.10.x still need `"testables": ["com.goryned.valtrion.unity"]` so Unity compiles the helper, as in the [Unity Quick Start](unity-quick-start.md). The package and Core versions pair as listed in [compat.json](../compat.json) (package 0.11.2 works with Core 0.8.0 up to but not including 0.15). Tags are `unity-vX.Y.Z`; pin a tag or a full commit SHA for shared projects, not a branch.

## Updating

- Core: `pipx upgrade valtrion`. On SSH hosts run `valtrion hosts sync` so every host has the same version.
- Unity: change the tag in the manifest URL (for example `#unity-v0.17.0` to a newer `unity-vX.Y.Z`), let Unity resolve it, and commit `Packages/manifest.json` and `packages-lock.json`.
- Core 0.21.x is paired with packages 0.17.x and 0.18.x (Core 0.20.x with 0.17.x, Core 0.19.x with packages 0.15.x and 0.16.x, Core 0.18.0 with 0.15.x). `valtrion doctor --project` reports a project that still has package 0.15.x or 0.16.x as `BLOCKED` with Core 0.20.0, because they cannot read `byContent.assemblies` of `checks.json`. A plan that uses the steps added for device measurements (`session.offsets`, `storage.*`, `timer.*`, `counter.*`, `repeat`, `call.time`, `app.background`) needs package 0.16.0 or newer. Packages 0.13.x and 0.14.x are paired with Core below 0.18 (Core 0.17.1 pairs with both): `valtrion doctor --project` reports a project that still has one of them as `BLOCKED` with Core 0.18.0, because they cannot read the new keys of `checks.json` (`extends`, `bindings`, `overridesPreset`, `allowHeavyFallback`, `byContent`; `equivalenceRules` needs package 0.14.0). Change the manifest tag to `unity-v0.17.0`, or stay on Core 0.17.x. A project that still has package 0.12.x (or 0.11.x) gets `BLOCKED` with Core 0.16.0 or newer in the same way. The package 0.13.0 and newer is also what hides the windows that tests open on macOS (`unity run`).
- Then run `valtrion doctor --project <Unity project>`: it reports `BLOCKED` with the action to take if the pair is not listed in [compat.json](../compat.json). Read the changelogs ([Core](../CHANGELOG.md), [Unity](../integrations/unity/com.goryned.valtrion.unity/CHANGELOG.md)) before upgrading.

## From a checkout

```
python tools/install.py            # pipx when it is installed, otherwise a virtual environment
python tools/install.py --venv     # always the virtual environment
python tools/install.py --venv --home .valtrion-home  # workspace-local installation
```

`tools/install.py` builds a wheel from the checkout (no network, no build backend) and installs it with `pipx install --force`, or into the virtual environment `~/.valtrion/core/<version>` with `pip install --force-reinstall --no-deps`. Python's `venv`, `ensurepip` and pip must be available. `--home DIR` with `--venv` replaces `~` for the installation only, so the environment becomes `DIR/.valtrion/core/<version>`. The last line it prints is the absolute path of the `valtrion` command. Run that command with `--version`; it is not automatically on your shell's PATH.

```
valtrion --version
```

This prints `valtrion <version> (contract <n>)`. Here and below, replace `valtrion` with the printed absolute path or add its directory to PATH. `python -m valtrion` works only with the installed environment's Python. To use the checkout without installing, put `core/` on `PYTHONPATH`.

The offline installer is the simplest route. Standard Python builds use Hatchling 1.27 or newer as a build-only dependency (`python -m pip wheel ./core`); building from the source archive is supported too. Runtime dependencies remain empty. Both distribution paths include schemas, the compatibility table and the MIT notice. The same distributions are what the Release workflow uploads to PyPI.

On macOS, install into a folder that is not protected (not under Documents, Desktop, Downloads or cloud storage): background jobs run the interpreter that runs `valtrion` and are refused otherwise. See [work-root.md](work-root.md).

If an agent sandbox reports a temporary-directory permission error during `venv` or `pip`, use its approved execution mechanism or a private writable temporary directory for that invocation (`TEMP`/`TMP` on Windows, `TMPDIR` on POSIX). Keep normal system paths unchanged and retain the failed diagnostic. A sandbox permission failure does not establish an installer defect; do not bypass approvals or change shared permissions.

## Several hosts

Work can run on other machines over SSH. The command on each host must be the same version as the one you run.

```
valtrion hosts add --id mac --ssh me@mac.example --os macos --artifacts-root /Users/me/.valtrion/artifacts
valtrion hosts sync
valtrion doctor --host mac
```

- `hosts add` writes an SSH host into `~/.valtrion/hosts.json`. Without `--python`, `hosts sync` tries `/opt/homebrew/bin/python3`, `/usr/local/bin/python3` and then `python3` (`python` on Windows) on the host, takes the first that is Python 3.10 or newer, and records it as `python`; a non-interactive SSH `python3` is often an older system interpreter. The found path stays in `hosts.json`; if that Python is later removed, delete the `python` entry of the host to probe again. SSH must work non-interactively (key based, batch mode).
- `hosts sync` builds a wheel from this installation and, for each SSH host, checks the recorded command's version. If it differs (or the recorded command is a versioned path of an older sync), it copies the wheel and installs it with the host's Python (`python3` on macOS and Linux, `python` on Windows; it must be 3.10 or newer):
  - **With pipx** (as a module of that Python, next to it, or at the usual places): `pipx install --force --python <that Python> <wheel>`. There is one installed copy that pipx replaces in place, and the recorded `valtrionCommand` is the short **absolute** shim pipx exposes (the host's `PIPX_BIN_DIR`, for example `<home>/.local/bin/valtrion`), the same command the Windows host gets from `pipx install valtrion`. Before it replaces anything it checks that the host is idle (no job whose runner is alive in the jobs registry, no lease whose holder is alive, no process using the pipx environment); if it is not, or if that cannot be established, the host is reported `BLOCKED`, nothing is replaced, and you sync again later. pipx replaces its environment as a whole: if the install runs but the new command does not report the version, the report says so, the recorded command is not changed, and `pipx install --force` may have to be run on the host by hand.
  - **Without pipx**: a virtual environment in `~/.valtrion/core/<version>` as before, plus a stable link `~/.valtrion/bin/valtrion` (a `valtrion.cmd` file on Windows) that is moved to the new version only after that version answers; the link is the recorded command.
  - **Old versions.** Without pipx, a sync then removes `core/<X.Y.Z>` folders strictly older than the version it installed. Never removed: folders whose name is not a plain version, newer versions (another machine may have synced a newer core), symbolic links, the target of the stable link and the recorded command's folder. Nothing is removed while the host has an active job or a lease whose holder is alive, or if that cannot be established; a folder a process has on its command line stays; a folder that cannot be removed is named in the report. The process list is read in a UTF-8 locale (`LC_ALL` `en_US.UTF-8`, else `C.UTF-8`; UTF-8 output of PowerShell on Windows), and vis (`M-P...`) or `\xNN` escapes are also matched; when the list cannot be read, is not UTF-8, is empty or does not show the helper's own arguments readably (a non-ASCII home folder on a host without a UTF-8 locale), every version stays and the report says why (`old versions were kept: the process list could not be read (...)`). With pipx nothing is removed unless you pass `--prune` (which also removes a local `tools/install.py --venv` install that is older); a stable link left from the earlier fallback is repointed to the pipx command.
  The new command must report this version before it is recorded in `hosts.json`; otherwise the old record stays. `--only HOST` limits the sync. A host that is unreachable or fails is reported, and the command exits 2.
- A non-interactive SSH session does not read the interactive PATH, so the pipx folder (or `~/.valtrion/bin`) is usually not on it, and a script on the host that runs `valtrion` by name does not find it. `valtrion doctor --host H` runs `valtrion --version` by name over SSH and warns (it does not block) with the fix when it is not found. On macOS the fix is one line on the host, `echo 'export PATH="<folder>:$PATH"' >> ~/.zshenv`; `valtrion hosts sync --ensure-path` does exactly that, only when you pass the flag (nothing edits a shell startup file otherwise; the flag is not implemented for Linux and Windows, where the warning names the file or setting). On Windows `pipx ensurepath` puts the pipx folder on the user PATH.
- `valtrion doctor --host H` runs the probe on the host and is `BLOCKED` when the host's core version differs from this one; run `hosts sync` again.

## Unity package compatibility

`compat.json` lists which versions of the Unity package `com.goryned.valtrion.unity` work with which core versions:

```json
{ "schema": "valtrion.compat/1", "package": "com.goryned.valtrion.unity",
  "ranges": [ { "package": "0.7.x", "core": ">=0.7.0,<0.8" } ] }
```

`valtrion doctor --project` reads the package version of the project from `Packages/packages-lock.json` and checks it against the table. The result is `ok`, `skipped` (the project does not use the package), or `BLOCKED` with the action to take: update the package to a listed version, or install a core that lists it. The Unity package has its own version and release lifecycle; the core and the package are not released together.

A project names its Unity project with a `valtrion.json` next to or above where you run commands; see [cli.md](cli.md).

## Moving from the earlier tool

Valtrion replaces an earlier tool with the same behaviour under different names. There are no compatibility layers: nothing is aliased, and the old names do not work.

| Earlier tool | Valtrion |
| --- | --- |
| command `gdv` | command `valtrion` (also `python -m valtrion`) |
| home directory of the earlier tool | `~/.valtrion` |
| environment variable `GDV_HOME` | `VALTRION_HOME` |
| a per-project shim script that located and ran the CLI of the pinned package | `valtrion.json` at the project root, naming the Unity project: `{"unityProject": "<path relative to the file>", "checks": "<path relative to the file>"}` (`checks` is optional: the check map when it is not at `validation/device/checks.json` under the repository root); the installed `valtrion` is used |
| CLI shipped inside the Unity package | CLI installed separately; compatibility is checked with `compat.json` |

Unchanged on purpose: the schema ids `gdv.*/1`, the `-gdv*` launch arguments, the `GDV_*` environment variables read by apps and tools, the `GDV-RESULT` line, and the format of lease files.

While both tools are in use on one machine, set `leasesDir` in `host.json` to the old tool's leases folder, so the two never hold the same device; remove it afterwards. See [work-root.md](work-root.md). State is not migrated: `hosts.json`, `host.json` and `registry.json` are created fresh in the new home (`valtrion hosts add`, `valtrion devices --refresh`).
