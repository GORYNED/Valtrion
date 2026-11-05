# Contributing

Help improve Valtrion with reproducible bug reports, clear documentation and focused changes.

## Set up a checkout

You need Python 3.10 or newer and Git. Core tests use the standard library and synthetic tools; they do not require Unity, adb, Xcode or a device.

```sh
git clone https://github.com/GORYNED/Valtrion.git
cd Valtrion
python tools/install.py --venv
```

Use the CLI path printed by the installer. See [installation](docs/install.md), the [Core Quick Start](docs/quick-start.md), and the separate [Unity Quick Start](docs/unity-quick-start.md). Ordinary command checks do not require an app integration. Do not describe platform providers as support for another engine without a working example.

## Verify your change

From the repository root:

```sh
python tools/check_contracts.py
python tools/check_release.py
python tools/check_privacy.py
```

Then run the Core tests:

```sh
cd core
python -m unittest discover -s tests -t .
```

CI configures Core tests on Windows, macOS and Linux with Python 3.10 through 3.13, plus contract/source checks, an offline checkout installation, a source-archive-to-wheel installation, and a run without Unity or mobile tooling. These tests validate Core behavior with fakes; they do not prove a real device build or Unity compilation. Hosted results and current blockers are recorded in [release readiness](docs/release-readiness.md).

For Unity changes, use the Editor specified in `integrations/unity/UnityProject/ProjectSettings/ProjectVersion.txt` and run both EditMode and PlayMode tests in `integrations/unity/UnityProject`. Use batch mode and retain the logs and test XML. A skipped test or a missing Editor is unverified, not a pass. For provider changes, also test the affected platform when tools and a target are available. State every missing check in the pull request.

## Keep real names out

The repository, its commit messages and its pull requests are public. Do not name a real project, device, machine, person, address or user folder in a test, an example, a screenshot, a changelog entry or a commit message; use made-up names (`Game`, `Example Phone 5G`, `build-pc`, `dev`). `python tools/check_privacy.py` checks what a branch adds, its commit messages and branch name, and (with `--text-file`) a pull request text; it finds user folders, e-mail addresses, private addresses and device ids by pattern, and names from a private list outside the repository and a list of hashes in `tools/privacy-terms.sha256`. Look at every image yourself: the script cannot read it. [AGENTS.md](AGENTS.md) has the details.

## Brand and website materials

Logos, banners and the project facts shown on goryned.com live in [publishing](publishing). The README banners are `publishing/assets/valtrion-readme-*.png`. The folder follows the contract in the goryned.com `docs/publishing.md`; the website pins a commit of it, so keep file names stable.

## Preserve compatibility

- Treat [contracts/schemas](contracts/schemas) and their fixtures as the source of truth. Update fixtures and validators together with a contract change; see [contracts](docs/contracts.md).
- Preserve wire identifiers: `gdv.*/1`, `-gdv*` launch arguments, `GDV_*` environment variables, step types and `GDV-RESULT`. Existing apps and plans depend on them. Give a breaking document format a new major schema id.
- Keep Core's runtime free of third-party dependencies and compatible with Python 3.10 through 3.13.
- Keep Core and Unity versions consistent with [compat.json](compat.json). They need not have identical version numbers.
- Keep examples synthetic. Do not commit credentials, personal paths, real device identifiers, signing identities, customer data or unrelated private project details.
- Describe observed behavior in documentation. Label plans and unverified capabilities explicitly.
- Keep integration independent of any particular AI agent.

## Submit a change

1. Create a branch from `main` and keep the change focused.
2. Add meaningful tests for behavior changes and fixtures for contract changes.
3. Open a pull request with the problem, resulting behavior and any compatibility impact.
4. Include exact verification commands, results, platforms and missing tools or checks. Remove personal and device data from attached logs.
5. Address review findings and failing checks before the change is considered ready.

Keep GitHub Actions pinned to full commit SHAs with a version comment. Do not publish packages or releases as part of an ordinary contribution; releases are made only by the maintainers' manual Release workflow, see [releasing](docs/releasing.md).

Contributions are submitted under the repository's [MIT license](LICENSE). Include required attribution for any third-party material and explain new dependencies. Report security issues through [SECURITY.md](SECURITY.md), rather than a public bug report.
