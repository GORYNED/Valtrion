# Providers

Providers handle a platform's build, installation, process lifecycle and file
transport. They are distinct from an application integration, which executes the
actual check inside an app. Unity is the only ready application integration in
this repository; the provider list does not claim support for other engines.
Ordinary Core command jobs bypass providers and need no Unity project. See the
[Core Quick Start](quick-start.md) and [architecture boundaries](ARCHITECTURE.md).

This page describes the platform providers (`ios`, `android`, `windows`, `macos`, `editor`, plus the Unity support code they share), the build profiles a project supplies, and the environment variables the providers read. A provider turns "build, install, run, collect" into the tools of one platform and reports an expected condition (a locked device, a missing tool) as `BLOCKED` or `NEEDS_PERMISSION` with a reason, never as an exception.

## Build profiles

Every provider that builds reads its settings from `<unity project>/validation/device/profiles.json`:

```json
{
  "schema": "gdv.profiles/1",
  "profiles": {
    "Validation": {
      "ios":     { "executeMethod": "MyGame.Editor.Build.BuildIos", "configuration": "ReleaseForRunning" },
      "android": { "executeMethod": "MyGame.Editor.Build.BuildAndroid", "scriptingBackend": "IL2CPP", "package": "com.example.mygame" },
      "windows": { "executeMethod": "MyGame.Editor.Build.BuildWindows", "productName": "MyGame Validation", "companyName": "MyGame Validation", "scriptingBackend": "mono" },
      "macos":   { "executeMethod": "MyGame.Editor.Build.BuildMacOs" },
      "editor":  { "runner": "../../tools/validation/run-unity-tests.py", "planAssembly": "MyGame.Validation.PlanTests" }
    }
  }
}
```

The shape is `profiles.<profile name>.<platform>`. The profile name is the candidate profile of a run (`run --profile`, `candidate.profile` of a pipeline). `executeMethod` is the Unity `-executeMethod` that builds the player; it is required for every platform that builds, and it is project code that Valtrion runs (see [trust-model.md](trust-model.md)). The method receives `-buildOutput <path>` plus the switches of its platform. The `ios` provider refuses keys it does not know.

Keys by platform:

| Platform | Keys |
| --- | --- |
| `ios` | `executeMethod`, `buildProfile`, `configuration` (default `Release`), `scheme` (default `Unity-iPhone`), `exportPath`, `xcodeProject`, `unityArgs`, `unityPath`, `xcodebuildSettings`, `exportTimeoutSec`, `buildTimeoutSec`, `stallSec`, `gitLfsPull`, `destination` (`device` or `generic`) |
| `android` | `executeMethod`, `scriptingBackend` (`Mono` or `IL2CPP`), `package`, `activity`, `timeoutSec`, `stallSec`, `extraArgs` |
| `windows` | `executeMethod`, `productName`, `companyName`, `scriptingBackend` (default `mono`), `timeoutSec`, `stallSec`, `extraArgs` |
| `macos` | `executeMethod`, `signScript`, `timeoutSec`, `stallSec` |
| `editor` | `runner`, `planAssembly`; also read: `leaseWaitSec`, `suiteTimeoutSec` |

## Common behaviour

- Every external tool call is bounded by the supervisor: a hard timeout, a stall window and a kill of the whole process tree. See [work-root.md](work-root.md) for jobs.
- Unity runs in batch mode with the Editor matching `ProjectSettings/ProjectVersion.txt`, found under the Unity Hub paths (`/Applications/Unity/Hub/Editor` on macOS, `%ProgramFiles%\Unity\Hub\Editor` on Windows, `~/Unity/Hub/Editor` elsewhere). A missing Editor or module is `BLOCKED`.
- Every Unity process (a build stage or an Editor run) holds one slot of the host's `unity-editor` capacity and a lease on its project folder (`unity-project:<hash of the folder>`, taken first). The capacity belongs to the host, not to a provider: 2 by default on every platform, or `unityEditorSlots` in `host.json` (see [work-root.md](work-root.md)). All providers count the same total, so an Editor run and a device build share it. A second run on a project that is already open is `BLOCKED`. The old `PlatformAdapter.unity_editor_slots` attribute is gone; a third-party provider that set it must use `unityEditorSlots` in `host.json`. Lowering the total does not see holders of higher slots from an older version, so in a mixed-version transition one more Unity can run until they end.
- A passing run deletes the data it created on its target. A failed or interrupted run keeps it as diagnostics, with a `retained.json` manifest, in a folder moved aside (named with `gdv.retained-<utc>`). No later run clears such a folder. When a provider keeps data, its location is listed under `unverified` in the result.

## ios

Runs on a Mac. From another host it is reached over SSH; on a Mac there is no SSH. The pipeline is a detached job in the `gui` launchd domain, because `codesign` needs the login keychain: a build outside a GUI session is `BLOCKED`, signing trouble is `NEEDS_PERMISSION`.

- Build: Unity batch-mode export (with a stall watch on the Unity log), then `xcodebuild` for the discovered device, with a watch on `DerivedData`. `executeMethod` must export the Xcode project to the folder passed as `-gdv-export-path <folder>` (also given as `-buildOutput <folder>`); a method that ignores both and exports to a fixed folder names it as `exportPath`. An `exportPath` must be a new folder outside `Assets`, `Packages`, `ProjectSettings` and similar, since it is removed after the run. The whole build ends inside 14000 s. A failed build or a missing `.app` is `nativeBuild: FAIL` and nothing is installed.
- Device: taken from `xcrun devicectl list devices` on every run (connected and paired, or wired and paired). CoreDevice drops the tunnel of an idle wired device (`available (paired)`, `tunnelState: disconnected`); discovery then wakes it with `devicectl device info lockState` (30 s bound) and lists once more. A wireless device whose tunnel is down is not used. The registry only adds the owner's class, alias and capabilities; when no attached device fits, the pipeline says whether a registered device that would fit is offline or none is known. Files move with `devicectl device copy to|from --domain-type appDataContainer` into `Documents/`. A locked device is `NEEDS_PERMISSION`.
- Leases: `device:ios:<udid>` (taken before the build, so a second request is `BLOCKED` naming the holder), then the project lease and one `unity-editor` slot. A lease that the pipeline could no longer renew while the build ran makes the build `BLOCKED`.
- Device cleanup: only through the harness ledger or by reinstalling; `devicectl` cannot delete files.

## android

A physical device (or an emulator, labelled `emulator`) driven through `adb`. A plan of kind `performance` refuses an emulator. A device that is `unauthorized` or `offline` is `NEEDS_PERMISSION`; a missing device or tool is `BLOCKED`.

- Build: Unity batch mode with `-executeMethod`; the method receives `-buildOutput <apk>`, `-gdvProfile <name>` and `-gdvBackend <Mono|IL2CPP>`. `package` is required and must be a valid package name. `Mono` suits smoke plans; a performance plan names a profile with `IL2CPP`.
- Install with `adb install -r`. The plan is pushed to `/sdcard/Android/data/<package>/files/gdv/inbox/plan.json` and the app starts with `am start` and the `gdv-plan` intent extra. The harness output under `gdv/out` is polled and pulled.
- **A slow start is waited for, bounded.** `am start -S -W` stops waiting by itself after about 15 s and prints `Status: timeout`; on a slow emulator or a cold Unity IL2CPP start the application is then still starting. The launch does not fail on that status. It keeps waiting up to the *launch wait* (counted from the start of `am start`) and decides by evidence, looking every 2 s: logcat of this launch has no `FATAL EXCEPTION`, ANR or native crash of the package, `pidof <package>` finds the process, and `dumpsys activity activities` shows the launched activity as the resumed one. All three good is the launch passing. Real errors are not waited for and fail at once with their own reason: `Error:` or `Exception` in the `am start` output (for example an activity that does not exist), an unknown `Status`, a crash or ANR in logcat, a process that was running and is gone. A start still not up at the limit fails with `launch timed out after N s, process alive` (or `not alive`) and the `WaitTime` of `am start`, which tells it from a crash. What each look saw is written to `android/launch.log` in the run folder.
- **Evidence and its limits.** The process check is `pidof`, which needs Android 7.0 (API 24) or later. The resumed activity must be the one that was launched: a launcher activity that trampolines to another activity is never seen as the resumed launched activity, so such an app ends at the limit with `launch timed out after N s, process alive`. One failed or timed-out adb call in a look (`logcat -d`, `pidof`, `dumpsys`) is retried once after the normal 2 s pause; a second failure in a row ends the launch with that call's own reason (device offline or unauthorized, adb missing, logcat unreadable). A crash or ANR in logcat is never retried.
- **The launch wait** is `launchTimeoutSec`: the run stage's `args.launchTimeoutSec`, or `valtrion run --launch-timeout SEC`, else `launchTimeoutSec` in `host.json`, else 60 s. A number from 1 to 300; a larger or invalid value is refused (`BLOCKED`), never clamped, so there is no unbounded wait. The wait is paid out of the plan's budget (`budgetSeconds`, or the stage `timeoutSec`): on Android the budget counts from the start of the whole launch step (device readiness, activity resolution, the health record, the log capture, the wake key, `am start` and the wait), not only from `am start`, and the wait is cut to the budget if that is shorter. The limit itself counts from the start of `am start`. Other platforms ignore the setting.
- `adb` is taken from `GDV_ADB`, else `PATH`, else the one bundled with a Unity Android module.
- The provider never changes device settings and clears application data only for a plan with `dataPolicy: clean-install`.

## windows

Builds a standalone player and runs it under a Job Object. Evidence is `host`: its numbers are never device-performance evidence.

- The build method receives `-buildOutput <exe>`, `-gdvProductName`, `-gdvCompanyName`, `-gdvBackend` and `-gdvDevelopment`, applies the identity for that build only and restores the project settings.
- The player gets its own identity (default company `Valtrion Validation`, product `<project> Validation`), so its data folder `%USERPROFILE%\AppData\LocalLow\<company>\<product>` is never the developer's. The provider refuses an identity equal to the project's and a player whose `app.info` does not carry the expected identity.
- `scriptingBackend: il2cpp` without the IL2CPP module is `BLOCKED` with the owner action.
- The player starts with `-gdv-plan` and `-logFile`. A player that exits without a result is a `FAIL` (or the status of its `GDV-RESULT` line) at once. A session without an interactive desktop (an SSH session or a service) cannot show a rendering player: `install` and `launch` are `BLOCKED` there, while the build still works.
- `terminate` ends the process tree and captures the screen first.

## macos

Makes the Mac that runs the job a target (`macos:<host>`, class `mac-<chip>`, evidence `host`). A Mac caller runs locally; any other caller reaches the Mac over SSH by capability (`doctor` lists `macos` among the build platforms only when an Editor with the macOS module is installed).

- Build: Unity batch mode (`-buildTarget StandaloneOSX -executeMethod <method> -buildOutput <app>`). **The method comes from the profile `executeMethod` or from `GDV_MACOS_BUILD_METHOD`; there is no built-in default.** Without either, the build is `BLOCKED`. The method receives `-buildOutput`, `-gdvCompanyName` and `-gdvProductName`, and must build a development player whose bundle identifier ends in `.gdv`.
- Signing: the script is `signScript` of the profile, or `GDV_MACOS_SIGN_SCRIPT`, or else the first `tools/build/sign-macos*.py` found in the project folder or any folder above it. It runs with the current Python and receives the `.app` path. Then `codesign --verify --deep --strict` runs. A step that needs the keychain is `NEEDS_PERMISSION`; start the job with `--domain gui`.
- Isolation: a macOS player keeps its data in `~/Library/Application Support/<bundle identifier>`, so the validation player has its own identifier (the application's plus `.gdv`) and its own company (`Valtrion-Validation`) and product (`<project>-gdv`) names. `install` refuses a bundle whose identifier does not end in `.gdv` and touches only such a folder. Because the build is re-signed each run, a privacy permission granted to the player may not survive a rebuild.
- Run: `<App>.app/Contents/MacOS/<binary>` is started directly (never `open`) in its own session with `-gdv-plan <inbox>` and `-logFile <console.log>`. `terminate` kills the process group (SIGTERM, then SIGKILL) and refuses to signal a process whose start time differs from the recorded one. The screenshot is the harness's own capture, falling back to `screencapture` (which needs the Screen Recording permission, an owner action).
- Permissions: quarantine and TCC are never changed. A quarantined bundle, or a player that stops on a missing privacy permission, is `NEEDS_PERMISSION` with the action for the owner. A missing Unity license is `BLOCKED`.

## editor

Runs a plan as a Unity Test Framework PlayMode test in a background batch-mode Editor, with evidence `editor` (never a device-performance baseline). There is nothing to build.

- The runner is `editor.runner` of the profile (relative to the project), else the first `tools/validation/run-unity-tests.py` found in the project folder or above. It runs the test assembly `planAssembly` (or the `--app` value other than `editor`) while holding the project lease and a `unity-editor` slot. Both are taken before the runner starts and given back only when no process of its tree is left: if the processes survive a kill, `terminate` fails and the leases stay held. A lease that cannot be renewed while the Editor runs is taken again (the result lists it under `unverified`); if that fails, the Editor is stopped and the run is `FAIL`.
- A project runner (or a `checks.json` `editmode:` command) that only needs Unity in batch mode can call [`valtrion unity run`](cli.md#unity-run) instead of locating the Editor and managing leases and the watchdog itself. Inside a provider run the runner already runs under the provider's leases, so it must not take them again: use `unity run` for runs outside a provider (EditMode tests, `-executeMethod`).
- The plan is staged privately and passed through `GDV_PLAN`, `GDV_OUT_ROOT`, `GDV_RUN_ID` (and `GDV_EVIDENCE=editor`). The pipeline adds 15 minutes of startup allowance to the plan budget.
- The project supplies the Python runner; Valtrion does not ship a universal one. It is invoked with `--editor <executable> --output <folder> --assemblies <assembly> --timeout <seconds>` plus launch arguments, from the runner's own directory. It must know the Unity project path, preserve the `GDV_*` environment, run the selected PlayMode assembly, and write Unity's NUnit XML to `<output>/PlayMode.xml`. Forward useful logs/status to stdout or stderr, return a nonzero code on failure, and bound the Editor process tree. See the [Unity Quick Start](unity-quick-start.md) for a direct Unity invocation before adding this provider wrapper.
- A harness result stays authoritative unless it says `PASS` while the NUnit XML does not. Without a harness result the provider writes the result from the NUnit XML (one step per test case) or the exit code. A runner that ends without a result is read through its `GDV-RESULT` or `STATUS` line, so a missing prerequisite is `BLOCKED`, not a stall.

## unity-support

`valtrion.providers.unity.support` is shared code, not a platform: reading `profiles.json`, finding the Editor of a project, tolerant file copies, and turning a process exit plus its `GDV-RESULT` or `STATUS` line into a status (never a `PASS` without a result).

## Environment variables

| Variable | Read by | Meaning |
| --- | --- | --- |
| `GDV_UNITY_PROJECT` | CLI | Unity project folder (below `--project-dir`, above `valtrion.json`) |
| `GDV_UNITY_HUB_PATHS` | doctor, providers | Folders to search for Unity Editors, separated by the OS path separator; replaces the default Hub path |
| `GDV_UNITY_EDITORS` | ios | Folder of the Hub editors on the Mac (default `/Applications/Unity/Hub/Editor`) |
| `GDV_ADB` | android | Path of the `adb` to use |
| `GDV_ANDROID_BACKEND` | android | `Mono` or `IL2CPP`, overrides the profile |
| `GDV_MACOS_BUILD_METHOD` | macos | The Unity build method, overrides the profile |
| `GDV_MACOS_SIGN_SCRIPT` | macos | The signing script, overrides the profile |
| `GDV_WORK_ROOT` | core | Absolute work root, see [work-root.md](work-root.md) |
| `GDV_REMOTE_PROJECT` | CLI | Default for `--remote-project` |
| `GDV_OWNER`, `GDV_SESSION` | leases | Default lease owner and session |
| `VALTRION_HOME` | core | State home, see [work-root.md](work-root.md) |

Variables passed to an app or a tool are listed in [protocol.md](protocol.md) and [work-root.md](work-root.md).
