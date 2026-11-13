# Trust model

This page states what Valtrion runs on your machines, which inputs can cause project code to run, and what it trusts. Valtrion is a tool for people who control the project they validate; it is not a sandbox for untrusted code.

## What runs project code

Valtrion starts the following, and each is chosen by the project, not by Valtrion:

| What | Where it comes from | Runs as |
| --- | --- | --- |
| **Command jobs** | argv after `job start --`, with the supplied working directory | Invoking user's permissions on the selected host; no Unity dependency |
| **Build methods** | `executeMethod` of a platform in `validation/device/profiles.json` (or `GDV_MACOS_BUILD_METHOD`) | Unity batch mode, with the user's permissions, with access to the project |
| **Sign scripts** | `signScript` in the profile, `GDV_MACOS_SIGN_SCRIPT`, or the first `tools/build/sign-macos*.py` found in the project folder or above | The Python that runs `valtrion`, on the Mac, with the login keychain when the job is in the `gui` domain |
| **Editor test runner** | `editor.runner` in the profile, or the first `tools/validation/run-unity-tests.py` found in the project folder or above | The Python that runs `valtrion` |
| **Unity batch mode** | The Editor matching the project's `ProjectVersion.txt` | The project's own editor scripts and tests run when it opens the project |
| **`externalUi.command`** | A `run` stage of a pipeline (the argv is project configuration; the `action`, `args` and request id substituted into it come from the app's heartbeat and are untrusted, see [protocol.md](protocol.md#during-the-run)) | The user running the pipeline, in the project folder, once per `external.ui` request of the plan; the program is fixed by the pipeline, and a request value that is an option, a path escape, or has a NUL byte or an absurd length is refused with a `BLOCKED` reply |
| **Plans' launch arguments** | `--launch-arg`, `extraArgs`, `unityArgs` | Passed to the app or to Unity as given |
| **The app and its plan steps** | The built player runs the `gdv.plan/1` you supply | The app, on the device |

`valtrion.json` only names folders and files (`unityProject`, `checks`); it runs nothing. `compat.json` and the schemas are data.

Consequence: run Valtrion only on projects you trust, and treat a profile, a plan and a sign script as code. A pull request that changes them changes what runs on your build hosts.

## What the host trusts

- **The project's configuration**, as above.
- **The app's result, as far as it is verified.** A result is validated against `gdv.result/1` before it is saved; an invalid one becomes a `FAIL`. A `PASS` has no failing layer and no `reason`. Evidence is recorded as what produced the result (`physical`, `emulator`, `editor`, `host`), and a number is compared only against a baseline of the same kind.
- **Its own state files** under the state home (`hosts.json`, `host.json`, `registry.json`, leases). They are machine-local and kept outside git. Anyone who can write them can redirect where Valtrion runs work.
- **Devices it discovers.** The device is always taken from live discovery (`devicectl`, `adb`), never from a UDID in a script or a stale registry.

## SSH hosts

Remote work uses OpenSSH in batch mode: no prompts, a connect timeout, no agent forwarding. Valtrion runs only short bounded commands over SSH and never holds a session open for a job; a job runs on the host in its own detached process. The command run on a host is the absolute `valtrion` path recorded by `valtrion hosts sync` (not looked up through PATH). An SSH target never starts with `-` (it would read as an ssh option); the contract rejects it. You decide which hosts are in `hosts.json`; Valtrion does not discover or add hosts by itself. `hosts sync` installs this checkout's core into the user's home on each SSH host.

## Offloaded runs

Offloading an Editor run sends the code of a working copy to another machine, where project code (editor scripts, tests, the test runner) runs as the user of that host. It extends the machines you trust with the project, so it is opt-in on both sides: the sending machine marks the host in its own `hosts.json`, the receiving host accepts the kind of work in its own `host.json` (`offload.accept`) and refuses everything else, including a request that names a project it has no managed checkout of. Valtrion does not discover or enable it by itself. Use a host whose user may run this project's code; on a Mac that holds signing identities, run it as a user that has none (the account that receives offloaded work can be a different macOS user than the one that signs).

What is sent: the commit or a git bundle of a snapshot of tracked and untracked files that git does not ignore, the plan and the pipeline file. What is not sent: ignored files (`.env`, local settings, caches), the `host.json` and `hosts.json` of the sender, Unity licenses, keychains, signing identities and `Library/`. Uncommitted Git LFS files are refused. The snapshot commit is made without touching the sender's index, branches or working tree and is deleted from the sender after the bundle is written; on the host it stays as a ref `refs/valtrion/offload/*` of the managed checkout (the newest five are kept). Nothing is pushed to any remote.

The SSH key that lets one machine run `valtrion` on the other is what protects this: use a key of its own per direction, keep `BatchMode` and no agent forwarding (already the default), and see [SSH hosts](#ssh-hosts).

## Leases

Leases are advisory locks between sessions of one host. They keep two runs from using the same device or Editor slot; they do not protect against a process that ignores them. A live lease is never taken over, and a stale one (holder dead and TTL expired) is reclaimed.

## What Valtrion does not change

- It does not change operating system security settings: no quarantine removal, no privacy (TCC) grants, no keychain unlocking, no Controlled folder access changes. When one is needed, the run ends `NEEDS_PERMISSION` with the action for the owner.
- It does not delete data outside what a run created. Validation players use their own identity (a separate company and product name, or a bundle identifier ending in `.gdv`), and the providers refuse to touch a data folder that is not a validation one.
- It does not uninstall or clear an app except for a plan with `dataPolicy: clean-install`.
- It never stops or reconfigures a local CI runner; `doctor` only reports it.

## Secrets

Valtrion has no credential store and asks for no credentials. Signing uses the identities already in the user's keychain. Plans, pipelines, results, events and logs are plain files that may be collected, stored and shared: do not put secrets in them, and do not print secrets from a build method or a plan step. Results record, among other things, the host id, the device class and artifact paths; leases record an owner.
