# Installing

Install Core from a checkout. Start with the [Quick Start](quick-start.md) for a command check without Unity or use the [AI-agent setup prompt](ai-agent-setup.md). For Unity, add the package separately using the [Unity Quick Start](unity-quick-start.md). The repository remains private; the clone URL currently requires access. These instructions do not depend on a package-index release.

## Requirements

Python 3.10 or newer. Nothing else is installed at runtime (standard library only). The tools a platform needs (Unity Editor, Xcode, `adb`) are probed by `valtrion doctor`; a missing one is reported, not required to install.

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

The offline installer is the simplest route. Standard Python builds use Hatchling 1.27 or newer as a build-only dependency (`python -m pip wheel ./core`); building from the source archive is supported too. Runtime dependencies remain empty. Both distribution paths include schemas, the compatibility table and the MIT notice. The `Private :: Do Not Upload` classifier is an intentional publication guard: removing it and uploading to an index is a separate release decision.

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
- `hosts sync` builds a wheel from this installation and, for each SSH host, checks the recorded command's version, and if it differs: copies the wheel, creates a virtual environment in `~/.valtrion/core/<version>` on the host, installs the wheel there (`python3` on macOS and Linux, `python` on Windows; it must be 3.10 or newer), and records the **absolute** path of the host's `valtrion` command as `valtrionCommand` in `hosts.json`. Non-interactive SSH sessions therefore never depend on PATH. `--only HOST` limits it. A host that is unreachable or fails is reported, and the command exits 2.
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
