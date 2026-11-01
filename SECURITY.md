# Security policy

Report vulnerabilities privately and keep credentials, personal information and device data out of reports.

## Report a vulnerability

Use this repository's **Security → Report a vulnerability** option when it is available. Do not disclose a vulnerability in a public issue or pull request.

GitHub's Private Vulnerability Reporting feature is available for public repositories. For a private repository, an unavailable reporting button or a 404 from its reporting-status API does not establish a configuration defect. See [GitHub's setup instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository).

If private reporting is unavailable, contact the maintainer through a private channel you already use. If you have no private contact, open an issue asking only for a private security contact; omit the vulnerability details. No response-time commitment is promised.

For maintainers: as part of an explicitly authorized public launch, open **Settings → Advanced Security → Private vulnerability reporting → Enable**, then check that **Security → Advisories → Report a vulnerability** is visible. Enable security-alert notifications for the people who will handle reports. Keep the repository private until the owner approves publication; setup preparation does not authorize changing its visibility. If using a separate private reporting contact instead, document and test that contact before launch.

Include the version (`valtrion --version`) or commit, host operating system, expected behavior, observed behavior and the smallest reproduction. Use synthetic identifiers and redact credentials, signing identities, user names, internal paths and device identifiers from logs.

## Supported versions

Security fixes currently target the latest `main`. There is no stable-release support window or commitment to backport fixes to older checkouts. Pin a reviewed commit and check subsequent changes before updating.

## Scope and trust boundaries

Valtrion runs command jobs, project-defined build methods, signing scripts, test runners and app code with the invoking user's permissions. Core command jobs have this same trust boundary without Unity. It is not a sandbox for untrusted projects. Read the [trust model](docs/trust-model.md) before using it on a build host or device.

Report cases where Valtrion executes unintended commands, crosses a documented data boundary, exposes sensitive information unexpectedly, accepts malformed results as success, or defeats resource isolation. Deliberately configured project code is trusted input; document how a report crosses that boundary.

State files can redirect work to hosts and paths. Restrict their access as you would other build configuration. Results and logs are plain files and may include host identifiers, device details and absolute paths; review and redact them before sharing.

## If a secret is committed

Revoke or rotate it immediately. Removing it from the current tree does not remove it from Git history, forks, caches or existing clones. Coordinate any history cleanup with maintainers before rewriting shared history. Do not include the secret value in an issue, pull request or cleanup commit.
