"""Metric statistics, comparison against a baseline and baseline promotion (plan section 2.7).

Compared statistic: the median of the samples left after the plan's warmup is dropped. A baseline is comparable only with a result
of the same plan id, key (plan hash, device class, OS major, build profile, Unity version) and evidence class; otherwise the
verdict is NOT_COMPARABLE, never PASS. The baseline of a result is found among all baselines of its plan by that key
(`find_baseline`), not by a file name. Per metric: a `budget` breach is a REGRESSION; a baseline or result whose spread exceeds
`regressionPct` is NOISY (increase `repeat`); a median worse than the baseline by more than `regressionPct` is a REGRESSION and
better by more than that is IMPROVED. Overall precedence: REGRESSION, NOT_COMPARABLE, NOISY, IMPROVED, PASS.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import constants, core, nowindow
from .schema import Validator, validate_document

DEFAULT_MIN_SAMPLES = 3
DIRTY_PATHS_SHOWN = 20
_RANK = {"REGRESSION": 5, "NOT_COMPARABLE": 4, "NOISY": 3, "IMPROVED": 2, "PASS": 1}


def compute_stats(samples: Sequence[float]) -> dict:
    """`gdv.common` stats: n, median, p90 (nearest rank), max and the sample standard deviation."""
    values = sorted(float(v) for v in samples)
    if not values:
        return {"n": 0, "median": 0, "p90": 0, "max": 0, "stdev": 0}
    rank = max(1, math.ceil(0.9 * len(values)))
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    return {"n": len(values), "median": statistics.median(values), "p90": values[rank - 1], "max": values[-1], "stdev": stdev}


def drop_warmup(samples: Sequence[float], warmup: int) -> list[float]:
    return list(samples)[max(0, warmup):]


def comparability_key(plan_sha256: str, device_class: str, os_major: int, build_profile: str, unity_version: str) -> dict:
    return {"planSha256": plan_sha256, "deviceClass": device_class, "osMajor": os_major, "buildProfile": build_profile,
            "unityVersion": unity_version}


def key_of_result(result: Mapping[str, Any]) -> dict:
    return comparability_key(
        result["plan"]["sha256"], result["target"]["deviceClass"], result["target"]["osMajor"], result["candidate"]["profile"],
        result["candidate"]["unity"])


@dataclass
class Comparison:
    verdict: str
    details: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return constants.exit_code_for_verdict(self.verdict)


def _spread_pct(stats: Mapping[str, Any]) -> float:
    median = abs(float(stats["median"]))
    spread = float(stats["stdev"]) if "stdev" in stats else max(0.0, float(stats["max"]) - float(stats["median"]))
    if median == 0:
        return 0.0 if spread == 0 else math.inf
    return spread / median * 100.0


def _worse_pct(current: float, baseline: float, direction: str) -> float:
    """Percentage by which `current` is worse than `baseline` (negative when better)."""
    delta = current - baseline if direction == "lower" else baseline - current
    if baseline == 0:
        return 0.0 if delta == 0 else (math.inf if delta > 0 else -math.inf)
    return delta / abs(baseline) * 100.0


def compare_metric(
        name: str, spec: Mapping[str, Any], samples: Sequence[float], baseline: Mapping[str, Any] | None, warmup: int) -> tuple[str, str]:
    direction = spec["direction"]
    kept = drop_warmup(samples, warmup)
    minimum = int(spec.get("minSamples", DEFAULT_MIN_SAMPLES))
    if len(kept) < minimum:
        return "NOISY", f"{name}: {len(kept)} sample(s) after warmup, {minimum} needed; increase repeat"
    stats = compute_stats(kept)
    current = float(stats["median"])
    unit = spec.get("unit", "")
    budget = spec.get("budget")
    if budget is not None and _worse_pct(current, float(budget), direction) > 0:
        return "REGRESSION", f"{name}: median {current:g}{unit} breaks the budget {budget:g}{unit}"
    if baseline is None:
        return "NOT_COMPARABLE", f"{name}: the baseline has no such metric"
    base_stats = baseline["stats"]
    threshold = spec.get("regressionPct")
    if threshold is None:
        return "PASS", f"{name}: median {current:g}{unit} (no regression threshold set)"
    if _spread_pct(base_stats) > threshold:
        return "NOISY", f"{name}: the baseline spread {_spread_pct(base_stats):.1f}% exceeds {threshold:g}%; increase repeat"
    if _spread_pct(stats) > threshold:
        return "NOISY", f"{name}: the run spread {_spread_pct(stats):.1f}% exceeds {threshold:g}%; increase repeat"
    worse = _worse_pct(current, float(base_stats["median"]), direction)
    base_median = float(base_stats["median"])
    if worse > threshold:
        return "REGRESSION", f"{name}: median {current:g}{unit} is {worse:.1f}% worse than the baseline {base_median:g}{unit} (limit {threshold:g}%)"
    if worse < -threshold:
        return "IMPROVED", f"{name}: median {current:g}{unit} is {-worse:.1f}% better than the baseline {base_median:g}{unit}"
    return "PASS", f"{name}: median {current:g}{unit} vs baseline {base_median:g}{unit} ({worse:+.1f}%)"


def _budget_breaches(result: Mapping[str, Any], plan: Mapping[str, Any]) -> list[str]:
    warmup = int(plan.get("warmup", 0))
    by_name = {metric["name"]: metric for metric in result.get("metrics", [])}
    breaches = []
    for name, spec in plan.get("metrics", {}).items():
        metric, budget = by_name.get(name), spec.get("budget")
        kept = drop_warmup(metric["samples"], warmup) if metric else []
        if budget is None or not kept:
            continue
        median = float(compute_stats(kept)["median"])
        if _worse_pct(median, float(budget), spec["direction"]) > 0:
            breaches.append(f"REGRESSION {name}: median {median:g}{spec.get('unit', '')} breaks the budget {budget:g}{spec.get('unit', '')}")
    return breaches


def compare(result: Mapping[str, Any], plan: Mapping[str, Any], baseline: Mapping[str, Any] | None) -> Comparison:
    """Verdict of one result against one baseline document (or None when no baseline exists)."""
    if result.get("status") != constants.PASS:
        return Comparison("NOT_COMPARABLE", [f"the run is {result.get('status')}; only a passing run is compared"])
    # An absolute budget needs no baseline: a breach is a regression even when the run cannot be compared with one.
    breaches = _budget_breaches(result, plan)
    if baseline is None:
        return Comparison("REGRESSION" if breaches else "NOT_COMPARABLE", [*breaches, "no baseline exists for this plan and device class"])
    details: list[str] = []
    if baseline.get("planId") != result["plan"]["id"]:
        details.append(f"planId differs: result {result['plan']['id']!r}, baseline {baseline.get('planId')!r}")
    result_key = key_of_result(result)
    for field_name, expected in baseline["key"].items():
        if result_key[field_name] != expected:
            details.append(f"key field {field_name} differs: result {result_key[field_name]!r}, baseline {expected!r}")
    evidence = result["target"]["evidence"]
    if evidence != baseline["source"]["evidence"]:
        details.append(f"evidence differs: result {evidence!r}, baseline {baseline['source']['evidence']!r}")
    if evidence == constants.EVIDENCE_EDITOR:
        details.append("an editor run is never compared with a device baseline")
    if details:
        return Comparison("REGRESSION" if breaches else "NOT_COMPARABLE", [*breaches, *details])
    warmup = int(plan.get("warmup", 0))
    by_name = {metric["name"]: metric for metric in result.get("metrics", [])}
    verdicts: list[str] = []
    for name, spec in plan.get("metrics", {}).items():
        metric = by_name.get(name)
        if metric is None:
            verdicts.append("NOT_COMPARABLE")
            details.append(f"{name}: the result has no such metric")
            continue
        full_spec = dict(spec)
        verdict, text = compare_metric(name, full_spec, metric["samples"], baseline["metrics"].get(name), warmup)
        verdicts.append(verdict)
        details.append(f"{verdict} {text}")
    return Comparison(worst_verdict(verdicts), details)


# -- baseline promotion -----------------------------------------------------------------------------------------------------

#: Runs a git command in a folder and returns (returncode, stdout). Replaced in tests.
GitRunner = Callable[[Path, Sequence[str]], "tuple[int, str]"]


#: Variables that point git at a repository. A hook or a wrapper that started valtrion may have set them for its own repository;
#: inherited, they would make every read in `folder` answer for that repository instead.
_REPOSITORY_VARIABLES = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR",
    "GIT_PREFIX", "GIT_IMPLICIT_WORK_TREE", "GIT_NAMESPACE", "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE")


def git_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a read-only git call: no inherited repository location, no prompt, and no optional lock (a `status`
    refreshes the index and would otherwise fight a concurrent git command of the user for `index.lock`)."""
    import os
    environment = {key: value for key, value in (os.environ if base is None else base).items() if key not in _REPOSITORY_VARIABLES}
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "never"  # a credential manager must not wait for a window nobody sees
    return environment


def run_git(folder: Path, arguments: Sequence[str], timeout: float = 60) -> tuple[int, str]:
    """(exit code, stdout) of `git <arguments>` in `folder`; for a failed call the second item is the error text, so a caller can say
    why. Trailing whitespace is trimmed, leading whitespace is kept (it is part of a porcelain status line)."""
    import subprocess
    try:
        done = subprocess.run(["git", *arguments], cwd=folder, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False, env=git_environment(), stdin=subprocess.DEVNULL, **nowindow.OPTIONS)
    except subprocess.TimeoutExpired:
        return 124, f"git {arguments[0] if arguments else ''} timed out after {timeout:g} s"
    except (OSError, subprocess.SubprocessError) as error:
        return 127, str(error)
    if done.returncode != 0:
        return done.returncode, (done.stderr.strip() or done.stdout.strip())
    return done.returncode, done.stdout.rstrip()


class PromotionRefused(Exception):
    """A promotion rule was not met; the message says which."""


def baseline_path(baselines_dir: Path, plan_id: str, device_class: str) -> Path:
    """The default file of a plan on a device class. Several baselines of one plan may share the folder (see `promotion_path`),
    so a baseline is found with `find_baseline`, never by this name alone."""
    return baselines_dir / core.safe_segment(plan_id) / f"{core.safe_segment(device_class)}.json"


def _created_order(text: str) -> tuple:
    """Sort key of a `createdUtc` value: the fraction may have any number of digits, or none."""
    body, _, fraction = text.rstrip("Z").partition(".")
    return body, fraction.ljust(9, "0")


def _baseline_files(folder: Path, validator: Validator) -> tuple[list[tuple[Path, dict]], list[str]]:
    """The valid baselines in a plan folder and one line for every file that is not one."""
    found: list[tuple[Path, dict]] = []
    problems: list[str] = []
    if not folder.is_dir():
        return found, problems
    for path in sorted(folder.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            problems.append(f"{path} cannot be read as a baseline: {error}")
            continue
        _, errors = validate_document(document, validator)
        if errors:
            problems.append(f"{path} is not a valid gdv.baseline/1: " + "; ".join(errors[:2]))
            continue
        found.append((path, document))
    return found, problems


@dataclass
class BaselineLookup:
    """What `find_baseline` found: the document (None when the plan has none), the file it came from and what to tell the reader."""
    document: dict | None
    path: Path | None
    expected: Path
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # files that are not valid baselines: the lookup cannot be trusted


def find_baseline(folders: Sequence[Path], result: Mapping[str, Any], validator: Validator | None = None) -> BaselineLookup:
    """Choose the baseline of a result by its comparability key and evidence among every baseline of its plan in `folders`.

    A file name says nothing: several baselines of one plan (other OS major, build profile, Unity version or plan hash) share
    the plan folder, and ids that differ only in characters a folder name cannot hold share a folder, so a document counts only
    when it names the plan id and carries the key and evidence of the result. The newest of several exact matches wins. With no
    exact match the closest baseline is returned, so `compare` names the key fields that differ; with none the notes say where
    the search looked. Files that are not valid baselines are named in the notes, never skipped silently."""
    plan_id, key, evidence = result["plan"]["id"], key_of_result(result), result["target"]["evidence"]
    validator = validator or Validator()
    candidates: list[tuple[Path, dict]] = []
    notes: list[str] = []
    problems: list[str] = []
    searched: list[str] = []
    for folder in folders:
        plan_folder = folder / core.safe_segment(plan_id)
        searched.append(str(plan_folder))
        found, bad = _baseline_files(plan_folder, validator)
        candidates.extend(entry for entry in found if entry[1]["planId"] == plan_id)
        problems.extend(bad)
    expected = baseline_path(folders[0], plan_id, result["target"]["deviceClass"]) if folders else Path(core.safe_segment(plan_id))
    exact = [entry for entry in candidates if entry[1]["key"] == key and entry[1]["source"]["evidence"] == evidence]
    if exact:
        path, document = max(exact, key=lambda entry: (_created_order(entry[1]["createdUtc"]), str(entry[0])))
        if len(exact) > 1:
            notes.append(f"{len(exact)} baselines have the key of the result; the newest ({path.name}) is used")
            places = {entry[0].parent for entry in exact}
            if len(places) > 1 and any(entry[1] != document for entry in exact):
                notes.append("the searched folders hold different baselines for this key: " + ", ".join(sorted(str(p) for p in places)))
        return BaselineLookup(document, path, expected, notes, problems)
    if candidates:
        def closeness(entry: tuple[Path, dict]) -> tuple:
            matching = sum(1 for name, value in entry[1]["key"].items() if key.get(name) == value)
            return matching, entry[1]["source"]["evidence"] == evidence, _created_order(entry[1]["createdUtc"]), str(entry[0])
        path, document = max(candidates, key=closeness)
        notes.append(f"{len(candidates)} baseline(s) exist for plan {plan_id}, but none has the key of the result "
                     f"(closest: {path})")
        return BaselineLookup(document, path, expected, notes, problems)
    notes.append(f"no baseline for plan {plan_id} was found in: " + (", ".join(searched) or "no baselines folder"))
    return BaselineLookup(None, None, expected, notes, problems)


def compare_against_folders(
        result: Mapping[str, Any], plan: Mapping[str, Any], folders: Sequence[Path], validator: Validator | None = None
) -> tuple[Comparison, str]:
    """`compare` with the baseline `find_baseline` chooses. Returns the comparison, whose details end with the lookup notes, and
    the baseline file (or the default file name where one would be written) to show."""
    lookup = find_baseline(folders, result, validator)
    if lookup.problems:
        # Fail closed: an unreadable file may be the newest baseline of this very key, so an older one must not decide. A budget
        # breach needs no baseline and stays a REGRESSION.
        comparison = compare(result, plan, None)
        details = [*(f"{line}; the baseline cannot be chosen safely until it is fixed or removed" for line in lookup.problems),
                   *comparison.details, *lookup.notes]
        return Comparison(comparison.verdict, details), str(lookup.expected)
    comparison = compare(result, plan, lookup.document)
    return Comparison(comparison.verdict, [*comparison.details, *lookup.notes]), str(lookup.path or lookup.expected)


def worst_verdict(verdicts: Sequence[str]) -> str:
    """The most severe of several comparison verdicts (REGRESSION, NOT_COMPARABLE, NOISY, IMPROVED, PASS); PASS when there are none."""
    return max(verdicts, key=_RANK.__getitem__) if verdicts else "PASS"


def _slot(document: Mapping[str, Any]) -> tuple:
    """What a baseline stands for: the plan, the device class, OS major, build profile, Unity version and evidence. The plan
    hash is left out: a changed plan file is a new baseline of the same slot, not a second one."""
    key = document["key"]
    return (document["planId"], key["deviceClass"], key["osMajor"], key["buildProfile"], key["unityVersion"],
            document["source"]["evidence"])


def promotion_path(baselines_dir: Path, document: Mapping[str, Any], validator: Validator | None = None) -> Path:
    """The file a baseline is written to. The file of the same slot is replaced; a new slot takes `<deviceClass>.json` when that
    name is free (the documented layout) and otherwise a name that carries the OS major, build profile and a digest of the slot,
    so promoting one series never overwrites another."""
    slot = _slot(document)
    folder = baselines_dir / core.safe_segment(document["planId"])
    for path, existing in _baseline_files(folder, validator or Validator())[0]:
        if _slot(existing) == slot:
            return path
    device = core.safe_segment(document["key"]["deviceClass"])
    default = folder / f"{device}.json"
    if not default.exists():
        return default
    digest = hashlib.sha256(json.dumps(slot).encode("utf-8")).hexdigest()[:8]
    profile = core.safe_segment(document["key"]["buildProfile"], limit=40)
    return folder / f"{device}-os{document['key']['osMajor']}-{profile}-{digest}.json"


def build_baseline(result: Mapping[str, Any], plan: Mapping[str, Any], created_utc: str | None = None) -> dict:
    metrics: dict[str, Any] = {}
    by_name = {metric["name"]: metric for metric in result.get("metrics", [])}
    warmup = int(plan.get("warmup", 0))
    for name, spec in plan.get("metrics", {}).items():
        metric = by_name.get(name)
        if metric is None:
            raise PromotionRefused(f"the result has no samples for the planned metric {name}")
        kept = drop_warmup(metric["samples"], warmup)
        if len(kept) < int(spec.get("minSamples", DEFAULT_MIN_SAMPLES)):
            raise PromotionRefused(f"{name} has {len(kept)} sample(s) after warmup; the plan needs {spec.get('minSamples', DEFAULT_MIN_SAMPLES)}")
        metrics[name] = {"unit": spec["unit"], "direction": spec["direction"], "stats": compute_stats(kept)}
    if not metrics:
        raise PromotionRefused("the plan declares no metrics, so there is nothing to baseline")
    return {
        "schema": "gdv.baseline/1", "planId": result["plan"]["id"], "key": key_of_result(result),
        "createdUtc": created_utc or core.utc_now(),
        "source": {"runId": result["runId"], "repo": result["candidate"]["repo"], "sha": result["candidate"]["sha"],
                   "evidence": result["target"]["evidence"]},
        "metrics": metrics}


def promote(
        result: Mapping[str, Any], plan: Mapping[str, Any], baselines_dir: Path, repo_root: Path, git: GitRunner = run_git,
        validator: Validator | None = None, main_branch: str = "main", runner_state: Callable[[], str] | None = None) -> Path:
    """Write the baseline file for a passing run on a commit from `main` with a clean tree. Raises `PromotionRefused` otherwise;
    the written file changes through an ordinary pull request."""
    if result.get("status") != constants.PASS:
        raise PromotionRefused(f"only a PASS run can become a baseline, this one is {result.get('status')}")
    if result["target"]["evidence"] == constants.EVIDENCE_EDITOR:
        raise PromotionRefused("an editor run is never a device baseline")
    if runner_state is not None and runner_state() == "busy":
        # A busy runner competes for CPU, Unity and Xcode with the measurement; valtrion only reads its state, it never stops it.
        raise PromotionRefused("a local GitHub Actions runner is busy, so the numbers are unfit for a baseline; promote when it is idle")
    if result["candidate"]["dirty"]:
        raise PromotionRefused("the run was made on a dirty tree")
    code, status = git(repo_root, ["status", "--porcelain"])
    if code != 0:
        raise PromotionRefused(f"cannot read the git state of {repo_root}" + (f": {status}" if status else ""))
    if status:
        entries = [line.strip() for line in status.splitlines() if line.strip()]
        shown = entries[:DIRTY_PATHS_SHOWN]
        more = f", and {len(entries) - len(shown)} more" if len(entries) > len(shown) else ""
        raise PromotionRefused(f"the working tree is not clean ({len(entries)} changed path(s): {'; '.join(shown)}{more})")
    sha = result["candidate"]["sha"]
    if set(sha) == {"0"}:
        raise PromotionRefused("the run recorded no commit (all-zero sha), so it cannot be tied to a commit on main")
    code, head = git(repo_root, ["rev-parse", "HEAD"])
    if code != 0:
        raise PromotionRefused(f"cannot read HEAD of {repo_root}" + (f": {head}" if head else ""))
    if head != sha:
        raise PromotionRefused(f"HEAD ({head or 'unknown'}) is not the commit the run was made on ({sha})")
    for reference in (f"origin/{main_branch}", main_branch):
        code, _ = git(repo_root, ["merge-base", "--is-ancestor", sha, reference])
        if code == 0:
            break
    else:
        raise PromotionRefused(f"commit {sha[:8]} is not on {main_branch}")
    document = build_baseline(result, plan)
    _, errors = validate_document(document, validator or Validator())
    if errors:
        raise PromotionRefused("the baseline would be invalid: " + "; ".join(errors))
    path = promotion_path(baselines_dir, document, validator)
    core.atomic_write_json(path, document)
    return path
