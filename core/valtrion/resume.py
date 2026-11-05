"""Stage selection of `valtrion pipeline`: run some stages of a pipeline and carry the others over from an earlier run.

A stage is named by its position, `N` (1-based) or `N:kind` as in the job's events (`3:run`). One of three selections is given:
`--stages LIST` (those stages), `--from-stage ID` (that stage and every later one) or `--only-failed` (the stages of the earlier run
that did not pass or did not run). Every stage that is not selected is *carried over* from the earlier run: its status there, and the
run it came from, are recorded in the new result (`pipelineRun.stages`), and it counts towards the job's status like a stage that
ran. The earlier run is `--resume-from JOB`, or else the newest finished run of the same pipeline id and commit on this host.

A resume is refused (`BLOCKED`, nothing runs) when the earlier run cannot be identified with the current one: it is missing, still
active, has no stage record (made before stage selection existed), is for another project, profile or commit, was made from a dirty
tree, has other stages, or a carried stage (or its plan or pre-launch files) changed since; when a carried `build` or `install` that a
selected stage depends on did not pass; when a selected `install` needs the earlier build and it is no longer in the build cache; and
when a selected `run` needs the earlier install and the device is unknown or differs from the one the pipeline names.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import buildcache, constants, core, resultcache
from .schema import Validator, validate_document

DISPOSITIONS = ("ran", "carried", "reused", "not-run")


class ResumeError(ValueError):
    """A selection or resume that cannot be done; the message says why and what to do instead."""


def stage_name(index: int, stage: Mapping[str, Any]) -> str:
    return f"{index + 1}:{stage['stage']}"


def parse_selector(text: str, stages: Sequence[Mapping[str, Any]]) -> int:
    """The 0-based index of the stage `text` names (`N` or `N:kind`)."""
    raw = str(text).strip()
    number, _, kind = raw.partition(":")
    if not number.isdigit() or not 1 <= int(number) <= len(stages):
        raise ResumeError(f"stage {raw!r} is not one of 1..{len(stages)} (name a stage as N or N:kind, for example 3 or 3:run)")
    index = int(number) - 1
    if kind and kind != stages[index]["stage"]:
        raise ResumeError(f"stage {raw!r}: stage {number} is a {stages[index]['stage']} stage, not {kind}")
    return index


def split_list(values: Sequence[str]) -> list[str]:
    return [item.strip() for value in values for item in str(value).split(",") if item.strip()]


def check_selection(selection: Mapping[str, Any] | None, stages: Sequence[Mapping[str, Any]]) -> None:
    """Raise ResumeError for a selection whose stage names do not exist (before a job is created)."""
    if not selection:
        return
    for item in selection.get("stages") or ():
        parse_selector(item, stages)
    if selection.get("fromStage") is not None:
        parse_selector(selection["fromStage"], stages)


def stage_sha(stage: Mapping[str, Any], base: Path | None) -> str:
    """What identifies a stage between two runs: for a run stage its fingerprint (`resultcache.stage_fingerprint`), otherwise the
    stage definition without `cleanup`."""
    if stage.get("stage") == "run":
        return resultcache.stage_fingerprint(stage, base)
    definition = {key: value for key, value in stage.items() if key != "cleanup"}
    return core.sha256_bytes(json.dumps(definition, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def plan_sha(stage: Mapping[str, Any], base: Path | None) -> str | None:
    if not stage.get("plan"):
        return None
    path = Path(stage["plan"])
    if not path.is_absolute() and base is not None:
        path = base / path
    try:
        return core.sha256_file(path)
    except OSError:
        return None


@dataclass
class Prior:
    job_id: str
    result: dict

    @property
    def records(self) -> list[dict]:
        return list(self.result["pipelineRun"]["stages"])

    def record(self, index: int) -> dict:
        return next((r for r in self.records if r.get("index") == index + 1), {"index": index + 1, "disposition": "not-run"})


def _read_prior(directory: Path, job_id: str, validator: Validator) -> Prior:
    try:
        job = core.read_json(directory / "job.json", attempts=3)
    except (OSError, ValueError) as error:
        raise ResumeError(f"the earlier run {job_id} cannot be read: {error}") from None
    if job.get("state") in ("queued", "running"):
        raise ResumeError(f"the earlier run {job_id} is still {job['state']}; wait for it to end")
    try:
        result = core.read_json(directory / "result.json", attempts=3)
    except FileNotFoundError:
        raise ResumeError(f"the earlier run {job_id} has no result.json") from None
    except (OSError, ValueError) as error:
        raise ResumeError(f"the result of the earlier run {job_id} cannot be read: {error}") from None
    schema_id, errors = validate_document(result, validator)
    if schema_id != "gdv.result/1" or errors:
        raise ResumeError(f"the result of the earlier run {job_id} is not a valid gdv.result/1")
    if not isinstance(result.get("pipelineRun"), dict):
        raise ResumeError(f"the earlier run {job_id} has no stage record (it was made by a Valtrion without stage selection); "
                          "run the whole pipeline once")
    return Prior(job_id, result)


def find_prior(job_id: str | None, pipeline: Mapping[str, Any], sha: str, home: Path | None = None) -> Prior:
    """The earlier run to carry stages from: `job_id`, else the newest ended run of this pipeline id, project and commit."""
    from .jobs import JobError, find_job_directory
    validator = Validator()
    if job_id:
        try:
            directory = find_job_directory(job_id, home)
        except JobError:
            raise ResumeError(f"--resume-from {job_id}: no such job on this host") from None
        return _read_prior(directory, job_id, validator)
    folder = (home or core.valtrion_home()) / "jobs"
    found: list[tuple[str, Prior]] = []
    for entry in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            directory = Path(core.read_json(entry, attempts=2)["dir"])
            result = core.read_json(directory / "result.json", attempts=2)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        run = result.get("pipelineRun") if isinstance(result, dict) else None
        try:
            if (not isinstance(run, dict) or run.get("pipelineId") != pipeline["id"] or result["candidate"]["sha"] != sha
                    or result["candidate"]["repo"] != pipeline["candidate"]["repo"]):
                continue
            stamp = str(result["finishedUtc"])
        except (KeyError, TypeError):
            continue
        try:
            found.append((stamp, _read_prior(directory, entry.stem, validator)))
        except ResumeError:
            continue
    if not found:
        raise ResumeError(f"no earlier run of pipeline {pipeline['id']} for commit {sha[:12]} with a stage record on this host; "
                          "run the whole pipeline, or name the run with --resume-from JOB")
    return max(found, key=lambda item: core.parse_utc(item[0]))[1]


@dataclass
class ResumePlan:
    """What a job with a stage selection runs, and what it carries over."""
    selected: frozenset[int]
    prior: Prior | None = None
    build: dict | None = None  # the earlier run's build, recorded in the new results
    build_entry: buildcache.Entry | None = None  # the published build a selected install installs
    device_id: str | None = None  # the device of the earlier install, for a selected run that depends on it
    notes: list[str] = field(default_factory=list)

    def carried_record(self, index: int, stage: Mapping[str, Any], base: Path | None) -> dict:
        assert self.prior is not None
        before = self.prior.record(index)
        record: dict[str, Any] = {"index": index + 1, "stage": stage["stage"], "disposition": "carried",
                                  "fromRun": before.get("fromRun") if before.get("disposition") == "carried" and before.get("fromRun")
                                  else self.prior.job_id, "stageSha256": stage_sha(stage, base)}
        for name in ("status", "reason", "plan", "planSha256", "runId", "deviceId"):
            if before.get(name) is not None:
                record[name] = before[name]
        return record


def plan_resume(selection: Mapping[str, Any] | None, resume_from: str | None, pipeline: Mapping[str, Any], sha: str | None,
                base: Path | None, platform: str | None, home: Path | None = None) -> ResumePlan | None:
    """The stages to run and what to carry, or None without a selection; ResumeError when it cannot be done (see the module text)."""
    if not selection:
        if resume_from:
            raise ResumeError("--resume-from needs --stages, --from-stage or --only-failed")
        return None
    stages = pipeline["stages"]
    everything = frozenset(range(len(stages)))
    unknown_commit = not sha or set(sha) == {"0"}
    prior: Prior | None = None
    if selection.get("onlyFailed"):
        if unknown_commit:
            raise ResumeError("--only-failed needs the commit of the earlier run, and the commit of the checkout is unknown")
        prior = find_prior(resume_from, pipeline, sha, home)
        _check_shape(prior, pipeline)
        selected = frozenset(i for i in everything if prior.record(i).get("status") != constants.PASS)
        if not selected:
            raise ResumeError(f"every stage of the earlier run {prior.job_id} passed; there is nothing to run again")
    elif selection.get("fromStage") is not None:
        start = parse_selector(selection["fromStage"], stages)
        selected = frozenset(range(start, len(stages)))
    else:
        selected = frozenset(parse_selector(item, stages) for item in selection.get("stages") or ())
        if not selected:
            raise ResumeError("--stages names no stage")
    if selected == everything:
        return ResumePlan(selected)  # nothing is carried: an ordinary run
    if unknown_commit:
        raise ResumeError("a stage selection needs the commit of the earlier run, and the commit of the checkout is unknown")
    if prior is None:
        prior = find_prior(resume_from, pipeline, sha, home)
        _check_shape(prior, pipeline)
    assert sha is not None
    _check_identity(prior, pipeline, sha)
    plan = ResumePlan(selected, prior)
    first_selected = min(selected)
    for index, stage in enumerate(stages):
        if index in selected:
            continue
        before = prior.record(index)
        if before.get("stageSha256") != stage_sha(stage, base):
            raise ResumeError(f"stage {stage_name(index, stage)} changed since the earlier run {prior.job_id} (its definition or "
                              "pre-launch files); select it too")
        if stage["stage"] == "run" and before.get("planSha256") and before.get("planSha256") != plan_sha(stage, base):
            raise ResumeError(f"the plan of stage {stage_name(index, stage)} changed since the earlier run {prior.job_id}; select it too")
        needed = any(later in selected and stages[later]["stage"] in ("install", "run") for later in range(index + 1, len(stages)))
        if stage["stage"] in ("build", "install") and needed and before.get("status") != constants.PASS:
            shown = before.get("status") or "did not run"
            raise ResumeError(f"stage {stage_name(index, stage)} of the earlier run {prior.job_id} is {shown}, and a selected stage "
                              f"after it depends on it; select it too (for example --from-stage {index + 1})")
    if prior.result.get("build"):
        plan.build = dict(prior.result["build"])
    _plan_install(plan, pipeline, sha, platform, home)
    _plan_device(plan, pipeline)
    if first_selected > 0:
        plan.notes.append(f"stages {', '.join(stage_name(i, s) for i, s in enumerate(stages) if i not in selected)} were carried over "
                          f"from run {prior.job_id}; they did not run in this job")
    return plan


def _check_shape(prior: Prior, pipeline: Mapping[str, Any]) -> None:
    stages = pipeline["stages"]
    run = prior.result["pipelineRun"]
    if run.get("pipelineId") != pipeline["id"]:
        raise ResumeError(f"the earlier run {prior.job_id} is of pipeline {run.get('pipelineId')}, not {pipeline['id']}")
    kinds = [record.get("stage") for record in sorted(prior.records, key=lambda r: r.get("index", 0))]
    if kinds != [stage["stage"] for stage in stages]:
        raise ResumeError(f"the stages of the earlier run {prior.job_id} ({', '.join(map(str, kinds))}) differ from this pipeline's "
                          f"({', '.join(s['stage'] for s in stages)})")


def _check_identity(prior: Prior, pipeline: Mapping[str, Any], sha: str) -> None:
    before = prior.result["candidate"]
    wanted = pipeline["candidate"]
    if before["repo"] != wanted["repo"] or before["profile"] != wanted["profile"]:
        raise ResumeError(f"the earlier run {prior.job_id} is of {before['repo']} profile {before['profile']}, not {wanted['repo']} "
                          f"profile {wanted['profile']}")
    if before["sha"] != sha:
        raise ResumeError(f"the earlier run {prior.job_id} tested commit {before['sha'][:12]}, not {sha[:12]}; its build and install "
                          "are not this commit's, so run the whole pipeline")
    if before["dirty"]:
        raise ResumeError(f"the earlier run {prior.job_id} was made from a dirty tree; its build cannot be identified with a commit, "
                          "so run the whole pipeline")


def _plan_install(plan: ResumePlan, pipeline: Mapping[str, Any], sha: str, platform: str | None, home: Path | None) -> None:
    """A selected install whose build is carried installs the earlier run's published build: find it and check it is this commit's."""
    stages = pipeline["stages"]
    assert plan.prior is not None
    for index, stage in enumerate(stages):
        if stage["stage"] != "install" or index not in plan.selected or (stage.get("args") or {}).get("artifactKey") or \
                (stage.get("args") or {}).get("artifact"):
            continue
        builds = [i for i in range(index) if stages[i]["stage"] == "build"]
        if not builds or builds[-1] in plan.selected:
            continue
        key = (plan.build or {}).get("key")
        if not key:
            raise ResumeError(f"stage {stage_name(index, stage)} needs the build of the earlier run {plan.prior.job_id}, which was not "
                              "published under a key; select the build stage too")
        entry = buildcache.find(buildcache.builds_root(home), str(key))
        if entry is None or not buildcache.is_intact(entry):
            raise ResumeError(f"stage {stage_name(index, stage)} needs the build {key} of the earlier run {plan.prior.job_id}, which is "
                              "no longer in the build cache (or is damaged); select the build stage too")
        if entry.manifest.get("sha") != sha or entry.manifest.get("dirty") or (platform and entry.manifest.get("platform") != platform):
            raise ResumeError(f"the build {key} of the earlier run {plan.prior.job_id} is not a clean {platform or ''} build of "
                              f"{sha[:12]}; select the build stage too")
        plan.build_entry = entry
        return


def _plan_device(plan: ResumePlan, pipeline: Mapping[str, Any]) -> None:
    """A selected run after a carried install runs on the device of that install."""
    stages = pipeline["stages"]
    assert plan.prior is not None
    for index, stage in enumerate(stages):
        if stage["stage"] != "run" or index not in plan.selected:
            continue
        installs = [i for i in range(index) if stages[i]["stage"] == "install"]
        if not installs or installs[-1] in plan.selected:
            continue
        device_id = plan.prior.record(installs[-1]).get("deviceId")
        if not device_id:
            raise ResumeError(f"stage {stage_name(index, stage)} needs the install of the earlier run {plan.prior.job_id}, and that run "
                              "does not say which device it installed on; select the install stage too")
        named = pipeline.get("device", {}).get("id")
        if named and named != device_id:
            raise ResumeError(f"the earlier run {plan.prior.job_id} installed on {device_id}, not on the device {named} the pipeline "
                              "names; select the install stage too")
        plan.device_id = device_id
        plan.notes.append(f"the application installed by run {plan.prior.job_id} on {device_id} was not installed again; another "
                          "job may have replaced it on the device since, which this job does not detect")
        return
