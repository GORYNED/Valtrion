# Releasing

Core (PyPI package `valtrion`) and the Unity package (`com.goryned.valtrion.unity`) have independent versions. Releases are made only by the manual [Release workflow](../.github/workflows/release.yml). It never runs on push or pull request, and it is separate from CI. Releases are marked as GitHub **Pre-release** while Valtrion is pre-1.0.

| Component | Version source | Tag | Published to |
| --- | --- | --- | --- |
| Core | `core/pyproject.toml` and `core/valtrion/__init__.py` | `core-vX.Y.Z` | PyPI and a GitHub Release |
| Unity package | `integrations/unity/com.goryned.valtrion.unity/package.json` | `unity-vX.Y.Z` | GitHub tag and Release (UPM installs from Git) |

Versions are final `X.Y.Z` (no `rc`/`a` suffix): "pre-release" is the GitHub flag and the `Development Status :: 4 - Beta` classifier. Keep `compat.json` pairing the Unity and Core versions that work together.

## One-time setup (repository owner)

1. **PyPI Trusted Publisher.** The project does not exist yet, so add a *pending publisher* at <https://pypi.org/manage/account/publishing/> with: project `valtrion`, owner `GORYNED`, repository `Valtrion`, workflow `release.yml`, environment `pypi`. No API token is created or stored anywhere. The first publish creates the project. (Use `pypi` also on TestPyPI only if you add a TestPyPI job; none exists.)
2. **GitHub Environment `pypi`** (Settings → Environments): required reviewers = the people allowed to approve a publish, deployment branches limited to `main`. The publish job waits for this approval.
3. Optional: protect `core-v*` and `unity-v*` tags (Settings → Rules → Rulesets) so they cannot be moved or deleted.

## Releasing the next version

1. In a pull request, bump the version(s) and add a `## X.Y.Z` section to `CHANGELOG.md` (Core) or the Unity `CHANGELOG.md`. For Core change both `core/pyproject.toml` and `core/valtrion/__init__.py`. Update `compat.json` if the pairing changes, and the version numbers quoted in the docs. Merge it to `main`.
2. Run **Actions → release → Run workflow** on `main` with the component and version and **dry run on** (the default). It checks that the version equals the source, runs the contract/source checks and Core tests, builds the sdist and wheel, runs `twine check --strict`, installs the wheel into a clean environment, and reports the state of PyPI, the tag and the Release. Nothing is published.
3. Run it again with **dry run off**. For Core the order is: draft GitHub Release with the exact files → **manual approval of the `pypi` environment** → upload to PyPI by OIDC → the draft is published as a Pre-release, which creates the tag at the verified commit. For Unity it creates the tag and Pre-release directly.
4. Check <https://pypi.org/project/valtrion/> and the Release page.

## Protection against repeats and partial releases

- The run fails unless it is on `main` and the requested version equals the source.
- A version already on PyPI, an existing tag, or a published Release stops the run. Tags are never moved or recreated, and PyPI versions cannot be replaced: use a new version.
- The files uploaded to PyPI are the ones attached to the draft Release before the upload.
- If the PyPI upload fails, nothing is on PyPI: re-run; the stale draft is replaced.
- If PyPI succeeded but the final Release step failed, re-run the same version. The workflow detects "on PyPI + draft Release + no tag", skips the upload, checks that the draft's files have the same SHA-256 as PyPI, and only publishes the Release.
- Any other combination (for example a tag without the PyPI version) is refused and needs manual repair.
- Only one release run at a time (`concurrency`). The workflow uses short-lived OIDC credentials; only the `pypi` job has `id-token: write`.

A broken PyPI release cannot be deleted for reuse. Yank it on PyPI, fix the problem and release the next version.
