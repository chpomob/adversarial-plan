"""Unit tests for the adversarial-plan phase modules and shared helpers."""
import json
from pathlib import Path

import pytest

from conftest import VALID_PLAN, VALID_SPEC, last_commit_message
from scripts import phases
from scripts.phases import phase_challenge, phase_plan, phase_revise, phase_verify


def fake_run(stdout="", stderr="", code=0, side_effect=None, calls=None):
    """Build an injectable `run(cmd, prompt, role, timeout, cwd, ...)` stub."""
    def _run(cmd, prompt, role, timeout, cwd, **kwargs):
        if calls is not None:
            calls.append({"cmd": cmd, "prompt": prompt, "role": role,
                          "timeout": timeout, "cwd": cwd})
        if side_effect:
            side_effect(cwd)
        return stdout, stderr, code
    return _run


# --- try_parse_json (3 strategies) ---------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('The verdict follows: {"a": 1} — done.', {"a": 1}),
    ('noise [1, 2, 3] noise', [1, 2, 3]),
])
def test_try_parse_json_strategies(text, expected):
    assert phases.try_parse_json(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "no json here", None, "{broken"])
def test_try_parse_json_rejects_garbage(text):
    assert phases.try_parse_json(text) is None


# --- validate_plan_file -----------------------------------------------------------

def test_validate_plan_ok(tmp_path):
    (tmp_path / "plan.md").write_text(VALID_PLAN)
    ok, err = phases.validate_plan_file(tmp_path)
    assert ok, err


def test_validate_plan_missing_file(tmp_path):
    ok, err = phases.validate_plan_file(tmp_path)
    assert not ok and "not found" in err


def test_validate_plan_empty_file(tmp_path):
    (tmp_path / "plan.md").write_text("   \n")
    ok, err = phases.validate_plan_file(tmp_path)
    assert not ok and "empty" in err


def test_validate_plan_no_frontmatter(tmp_path):
    (tmp_path / "plan.md").write_text("# Just a title\n\nBody.\n")
    ok, err = phases.validate_plan_file(tmp_path)
    assert not ok and "frontmatter" in err


def test_validate_plan_invalid_yaml(tmp_path):
    pytest.importorskip("yaml")  # the no-PyYAML fallback is deliberately lenient
    (tmp_path / "plan.md").write_text("---\nspec: [unclosed\n---\n\n# T\n")
    ok, err = phases.validate_plan_file(tmp_path)
    assert not ok


def test_validate_plan_missing_spec_key(tmp_path):
    (tmp_path / "plan.md").write_text(
        "---\nversion: '1.0'\n---\n\n# T\n\n### P1: step\n")
    ok, err = phases.validate_plan_file(tmp_path)
    assert not ok and "spec" in err


def test_validate_plan_no_steps(tmp_path):
    (tmp_path / "plan.md").write_text(
        "---\nspec: 'demo'\n---\n\n# Plan with no steps\n")
    ok, err = phases.validate_plan_file(tmp_path)
    assert not ok and "step" in err


def test_extract_frontmatter_requires_leading_marker():
    assert phases.extract_frontmatter("intro\n---\nspec: x\n---\n") is None
    assert phases.extract_frontmatter("---\nspec: x\n---\n") == "spec: x"


# --- phase_plan -------------------------------------------------------------------

def _write_plan(cwd):
    (Path(cwd) / "plan.md").write_text(VALID_PLAN)


def test_plan_success_commits(git_repo):
    run = fake_run(stdout="wrote plan.md", side_effect=_write_plan)
    result = phase_plan.run_plan(VALID_SPEC, None, "dev", str(git_repo),
                                 60, "my-feat", run=run)
    assert result["exit_code"] == 0
    assert result["commit_sha"]
    assert last_commit_message(git_repo) == "plan: my-feat — Demo feature"


def test_plan_dev_failure(git_repo):
    run = fake_run(stderr="boom", code=1)
    result = phase_plan.run_plan(VALID_SPEC, None, "dev", str(git_repo),
                                 60, "f", run=run)
    assert result["exit_code"] == 1
    assert "PLAN exited 1" in result["error"]


def test_plan_missing_plan_fails_validation(git_repo):
    run = fake_run(stdout="did nothing")
    result = phase_plan.run_plan(VALID_SPEC, None, "dev", str(git_repo),
                                 60, "f", run=run)
    assert result["exit_code"] == 1
    assert "plan validation failed" in result["error"]


def test_plan_uses_plan_writer_persona_and_embeds_spec(git_repo):
    calls = []
    run = fake_run(side_effect=_write_plan, calls=calls)
    phase_plan.run_plan(VALID_SPEC, None, "dev", str(git_repo), 60, "f", run=run)
    assert calls[0]["role"] == "plan-writer"
    assert "demo-feature" in calls[0]["prompt"]
    assert "findings-input: false" in calls[0]["prompt"]


def test_plan_findings_embedded_in_prompt(git_repo):
    calls = []
    findings = [{"id": "R1", "summary": "review found a race"}]
    run = fake_run(side_effect=_write_plan, calls=calls)
    phase_plan.run_plan(VALID_SPEC, findings, "dev", str(git_repo), 60, "f",
                        run=run)
    assert "review found a race" in calls[0]["prompt"]
    assert "findings-input: true" in calls[0]["prompt"]


# --- phase_challenge -----------------------------------------------------------------

CHALLENGE_OK = json.dumps({
    "findings": [{"id": "P1", "severity": "major", "step": "P1",
                  "summary": "Tests field is vague", "evidence": "P1"}],
    "verdict": "REQUEST_CHANGES",
    "summary": "1 major",
})


def _stage_plan_and_spec(tmp_path):
    (tmp_path / "plan.md").write_text(VALID_PLAN)
    (tmp_path / "spec.md").write_text(VALID_SPEC)


def test_challenge_parses_findings(tmp_path):
    _stage_plan_and_spec(tmp_path)
    result = phase_challenge.run_challenge("rev", str(tmp_path), 60,
                                           run=fake_run(stdout=CHALLENGE_OK))
    assert result["exit_code"] == 0
    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["findings"][0]["id"] == "P1"


def test_challenge_missing_plan(tmp_path):
    (tmp_path / "spec.md").write_text(VALID_SPEC)
    result = phase_challenge.run_challenge("rev", str(tmp_path), 60,
                                           run=fake_run(stdout=CHALLENGE_OK))
    assert result["exit_code"] == 1
    assert "plan.md" in result["error"]


def test_challenge_missing_spec(tmp_path):
    (tmp_path / "plan.md").write_text(VALID_PLAN)
    result = phase_challenge.run_challenge("rev", str(tmp_path), 60,
                                           run=fake_run(stdout=CHALLENGE_OK))
    assert result["exit_code"] == 1
    assert "spec.md" in result["error"]


def test_challenge_retries_then_fails_on_bad_json(tmp_path):
    _stage_plan_and_spec(tmp_path)
    calls = []
    result = phase_challenge.run_challenge(
        "rev", str(tmp_path), 60, run=fake_run(stdout="not json", calls=calls))
    assert result["exit_code"] == 1
    assert result["error"] == "invalid JSON after retry"
    assert len(calls) == 2  # exactly one retry


def test_challenge_rejects_bad_severity(tmp_path):
    _stage_plan_and_spec(tmp_path)
    bad = json.dumps({"findings": [{"id": "P1", "severity": "catastrophic",
                                    "step": "P1", "summary": "x",
                                    "evidence": "y"}],
                      "verdict": "REJECT"})
    result = phase_challenge.run_challenge("rev", str(tmp_path), 60,
                                           run=fake_run(stdout=bad))
    assert result["exit_code"] == 1


def test_challenge_rejects_finding_without_step(tmp_path):
    _stage_plan_and_spec(tmp_path)
    bad = json.dumps({"findings": [{"id": "P1", "severity": "major",
                                    "summary": "x", "evidence": "y"}],
                      "verdict": "REQUEST_CHANGES"})
    result = phase_challenge.run_challenge("rev", str(tmp_path), 60,
                                           run=fake_run(stdout=bad))
    assert result["exit_code"] == 1


def test_challenge_plan_and_spec_embedded_in_prompt(tmp_path):
    _stage_plan_and_spec(tmp_path)
    calls = []
    branch_point = "0123456789abcdef"
    phase_challenge.run_challenge(
        "rev", str(tmp_path), 60,
        run=fake_run(stdout=CHALLENGE_OK, calls=calls),
        branch_point=branch_point)
    assert "Implementation Plan" in calls[0]["prompt"]
    assert "demo-feature" in calls[0]["prompt"]
    assert branch_point in calls[0]["prompt"]
    assert "HEAD~1" not in calls[0]["prompt"]
    assert calls[0]["role"] == "plan-challenger"


# --- phase_revise ----------------------------------------------------------------------

def test_revise_success_commits_round(git_repo):
    (git_repo / "plan.md").write_text(VALID_PLAN)
    findings = [{"id": "P1", "summary": "fix me"}]
    run = fake_run(side_effect=_write_plan)
    result = phase_revise.run_revise(findings, "dev", str(git_repo), 60,
                                     "my-feat", 2, run=run)
    assert result["exit_code"] == 0
    assert last_commit_message(git_repo) == "revise: my-feat — round 2"


def test_revise_findings_in_prompt(git_repo):
    (git_repo / "plan.md").write_text(VALID_PLAN)
    calls = []
    phase_revise.run_revise([{"id": "P9", "summary": "oops"}], "dev",
                            str(git_repo), 60, "f", 1,
                            run=fake_run(side_effect=_write_plan, calls=calls))
    assert "P9" in calls[0]["prompt"]
    assert calls[0]["role"] == "plan-writer"


def test_revise_broken_plan_fails_validation(git_repo):
    def _break_plan(cwd):
        (Path(cwd) / "plan.md").write_text("no frontmatter anymore")
    result = phase_revise.run_revise([], "dev", str(git_repo), 60, "f", 1,
                                     run=fake_run(side_effect=_break_plan))
    assert result["exit_code"] == 1
    assert "plan validation failed" in result["error"]


# --- phase_verify ----------------------------------------------------------------------

VERIFY_OK = json.dumps({
    "results": [{"id": "P1", "status": "resolved", "note": "tests concretised"}],
    "verdict": "APPROVE",
})


def test_verify_parses_results(tmp_path):
    (tmp_path / "plan.md").write_text(VALID_PLAN)
    result = phase_verify.run_verify([{"id": "P1"}], "rev", str(tmp_path), 60,
                                     run=fake_run(stdout=VERIFY_OK))
    assert result["exit_code"] == 0
    assert result["verdict"] == "APPROVE"
    assert result["results"][0]["status"] == "resolved"


def test_verify_prompt_uses_branch_point(tmp_path):
    (tmp_path / "plan.md").write_text(VALID_PLAN)
    calls = []
    branch_point = "fedcba9876543210"
    phase_verify.run_verify(
        [{"id": "P1"}], "rev", str(tmp_path), 60,
        run=fake_run(stdout=VERIFY_OK, calls=calls),
        branch_point=branch_point)
    assert branch_point in calls[0]["prompt"]
    assert "HEAD~1" not in calls[0]["prompt"]


def test_verify_invalid_status_rejected(tmp_path):
    (tmp_path / "plan.md").write_text(VALID_PLAN)
    bad = json.dumps({"results": [{"id": "P1", "status": "maybe"}],
                      "verdict": "APPROVE"})
    result = phase_verify.run_verify([{"id": "P1"}], "rev", str(tmp_path), 60,
                                     run=fake_run(stdout=bad))
    assert result["exit_code"] == 1


def test_verify_fenced_json_accepted(tmp_path):
    (tmp_path / "plan.md").write_text(VALID_PLAN)
    fenced = f"```json\n{VERIFY_OK}\n```"
    result = phase_verify.run_verify([{"id": "P1"}], "rev", str(tmp_path), 60,
                                     run=fake_run(stdout=fenced))
    assert result["exit_code"] == 0


def test_verify_missing_plan(tmp_path):
    result = phase_verify.run_verify([], "rev", str(tmp_path), 60,
                                     run=fake_run(stdout=VERIFY_OK))
    assert result["exit_code"] == 1
    assert "plan.md" in result["error"]


def test_verify_cli_failure(tmp_path):
    (tmp_path / "plan.md").write_text(VALID_PLAN)
    result = phase_verify.run_verify([], "rev", str(tmp_path), 60,
                                     run=fake_run(stderr="timeout", code=124))
    assert result["exit_code"] == 1
    assert "VERIFY exited 124" in result["error"]


# --- persona resolution -------------------------------------------------------------------

def test_resolve_persona_falls_back_to_base_for_pi():
    # 'pi' maps to 'plan-writer-pi', which does not exist -> base persona.
    path = phases.resolve_persona("plan-writer", "pi --provider zai --model glm-5.2")
    assert path is not None and path.endswith("plan-writer.md")


def test_resolve_persona_unknown_role_is_none():
    assert phases.resolve_persona("no-such-role", "somecli") is None


def test_plan_rejects_unaddressed_spec_requirement_with_usage_exit(git_repo):
    spec = VALID_SPEC.replace(
        "## Acceptance criteria",
        "- R2: the tool reports demo failures.\n\n## Acceptance criteria",
    )
    result = phase_plan.run_plan(
        spec, None, "dev", str(git_repo), 60, "f",
        run=fake_run(side_effect=_write_plan),
    )
    assert result["exit_code"] == 2
    assert "R2" in result["error"]


def test_plan_rejects_missing_p1_even_when_later_step_covers_requirement():
    plan = VALID_PLAN.replace("### P1:", "### P2:")
    ok, error = phase_plan.validate_step_coverage(VALID_SPEC, plan)
    assert not ok
    assert "contiguous from P1" in error
