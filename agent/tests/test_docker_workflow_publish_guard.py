"""The Docker publish job must stay manual; the validation job must never push.

`docker-build.yml` gained `pull_request` / `push` triggers so the pinned
docker/* actions and the Dockerfile are exercised by CI rather than discovered
broken by whoever is mid-release. That is only safe while the publishing job
stays gated to `workflow_dispatch`: without the gate, those new triggers would
publish an image to GHCR and Docker Hub on every commit that touches the
Dockerfile.

The guard is one `if:` expression, which is exactly the kind of line a later
edit drops silently — nothing else in the repository would notice. These tests
are offline and read the workflow as data.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "docker-build.yml"


def _workflow() -> dict:
    parsed = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), "workflow did not parse as a mapping"
    return parsed


def _triggers(parsed: dict) -> dict:
    # PyYAML parses a bare `on:` key as the boolean True.
    return parsed[True] if True in parsed else parsed["on"]


def _push_flags(job: dict) -> list:
    return [
        step.get("with", {}).get("push")
        for step in job["steps"]
        if "build-push-action" in str(step.get("uses", ""))
    ]


def test_publishing_job_is_workflow_dispatch_only() -> None:
    """A commit touching the Dockerfile must not be able to publish an image."""
    jobs = _workflow()["jobs"]
    publishing = {
        name: job for name, job in jobs.items() if True in _push_flags(job)
    }
    assert publishing, "no publishing job found — did the push flag change?"
    for name, job in publishing.items():
        assert job.get("if") == "github.event_name == 'workflow_dispatch'", (
            f"job {name!r} pushes images but is not gated to workflow_dispatch; "
            "with the pull_request/push triggers present this publishes on "
            "every Dockerfile change"
        )


def test_validation_job_never_pushes() -> None:
    """The job that runs on ordinary commits must build only."""
    jobs = _workflow()["jobs"]
    assert "validate-build" in jobs, "the no-push validation job was removed"
    flags = _push_flags(jobs["validate-build"])
    assert flags, "validate-build no longer runs build-push-action"
    assert all(flag is False for flag in flags), (
        f"validate-build must pass push: false, got {flags}"
    )


def test_validation_job_uses_the_same_action_pins_as_the_publish_job() -> None:
    """A validation that ran different versions would prove nothing.

    The point of this job is to exercise the pins the release build uses, so a
    drift between the two makes the check decorative.
    """
    jobs = _workflow()["jobs"]

    def pins(job: dict) -> set[str]:
        return {
            str(step["uses"])
            for step in job["steps"]
            if str(step.get("uses", "")).startswith("docker/")
        }

    validate = pins(jobs["validate-build"])
    publish = pins(jobs["build-and-push"])
    shared = {p.split("@")[0] for p in validate} & {p.split("@")[0] for p in publish}
    assert shared, "the two jobs share no docker/* actions"
    for action in shared:
        v = {p for p in validate if p.startswith(action + "@")}
        p_ = {p for p in publish if p.startswith(action + "@")}
        assert v == p_, f"{action} is pinned differently: validate={v} publish={p_}"


def test_every_docker_action_is_pinned_to_a_sha() -> None:
    """A moving tag would silently change what the release build runs."""
    parsed = _workflow()
    for name, job in parsed["jobs"].items():
        for step in job["steps"]:
            uses = str(step.get("uses", ""))
            if not uses.startswith("docker/"):
                continue
            ref = uses.split("@", 1)[1]
            assert len(ref) == 40 and all(
                c in "0123456789abcdef" for c in ref
            ), f"{name}: {uses} is not pinned to a full commit SHA"


def test_triggers_include_the_paths_that_can_break_the_image() -> None:
    """Without these paths the validation job never runs on the real changes."""
    triggers = _triggers(_workflow())
    for event in ("pull_request", "push"):
        assert event in triggers, f"{event} trigger missing"
        paths = set(triggers[event]["paths"])
        assert {"Dockerfile", ".github/workflows/docker-build.yml"} <= paths, (
            f"{event} paths do not cover the Dockerfile and this workflow: {paths}"
        )
