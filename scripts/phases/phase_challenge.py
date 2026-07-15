"""
CHALLENGE phase: the plan-challenger model reviews ``plan.md``.

The plan and spec texts are embedded in the prompt (model-agnostic: works
even for providers without file access) and both files are also on disk for
providers that can read them. Output is validated JSON findings; one retry
with a stricter instruction on invalid JSON.

Assumption: the spec sketches ``run_challenge(dev_cmd, workdir, providers,
jsonio)``; following rule 1 ("same pattern as adversarial_spec.py") the
signature is ``(review_cmd, workdir, timeout, run=None)`` — the challenger
runs with the review command, and provider/JSON machinery is reached through
the shared helpers.
"""
from pathlib import Path

from . import run_role, try_parse_json

__all__ = ["run_challenge"]

_VALID_VERDICTS = {"REQUEST_CHANGES", "APPROVE", "REJECT"}
_VALID_SEVERITIES = {"blocker", "major", "minor", "nit"}
_REQUIRED_FINDING_KEYS = {"id", "severity", "step", "summary", "evidence"}


def _validate(payload):
    """Lightweight schema check for challenger output. No jsonschema dep."""
    if not isinstance(payload, dict):
        return False
    if payload.get("verdict") not in _VALID_VERDICTS:
        return False
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return False
    for finding in findings:
        if not isinstance(finding, dict):
            return False
        if not _REQUIRED_FINDING_KEYS.issubset(finding.keys()):
            return False
        if finding.get("severity") not in _VALID_SEVERITIES:
            return False
    return True


def _build_prompt(plan_text, spec_text, branch_point=""):
    diff_base = branch_point or "<branch-point>"
    return (
        "Challenge the implementation plan below against its specification "
        "(both also on disk at `plan.md` and `spec.md` in the current "
        "directory).\n"
        f"The branch-point SHA for this review is `{diff_base}`. Inspect "
        f"the cumulative change with `git diff {diff_base}..HEAD`.\n"
        "Look for, in priority order: missing steps (uncovered spec "
        "requirements or acceptance criteria), circular or wrong "
        "dependencies, untestable steps, missing risk documentation, wrong "
        "file assignments, steps too large for one dev-loop iteration.\n\n"
        "Output ONLY valid JSON:\n"
        '{"findings": [{"id": "P1", "severity": "blocker|major|minor|nit", '
        '"step": "P2|overall", "summary": "one-line issue", '
        '"evidence": "exact plan text or step id"}], '
        '"verdict": "REQUEST_CHANGES|APPROVE|REJECT", '
        '"summary": "counts by severity"}\n\n'
        f"--- plan.md ---\n{plan_text}\n\n"
        f"--- spec.md ---\n{spec_text}"
    )


def run_challenge(review_cmd, workdir, timeout, run=None, branch_point="", *,
                 ledger=None, show_costs=False, max_retries=3,
                 max_input_chars=None, max_output_chars=None):
    """
    Run the plan-challenger against ``<workdir>/plan.md``.

    Returns ``{"phase": "challenge", "exit_code": 0, "findings": [...],
    "verdict": "..."}``; on failure ``{"phase": "challenge", "exit_code": 1,
    "error": "..."}``. *run* is injectable for tests.
    """
    run = run or run_role
    try:
        plan_text = (Path(workdir) / "plan.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"phase": "challenge", "exit_code": 1,
                "error": f"could not read plan.md: {exc}"}
    try:
        spec_text = (Path(workdir) / "spec.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"phase": "challenge", "exit_code": 1,
                "error": f"could not read spec.md: {exc}"}

    prompt = _build_prompt(plan_text, spec_text, branch_point)

    def _attempt(prompt_text):
        stdout, stderr, code = run(
            review_cmd, prompt_text, "plan-challenger", timeout, workdir,
            ledger=ledger, show_costs=show_costs,
            max_retries=max_retries,
            max_input_chars=max_input_chars,
            max_output_chars=max_output_chars)
        if code != 0:
            return None, f"CHALLENGE exited {code}: {(stderr or '')[:200]}", stdout
        return try_parse_json(stdout), None, stdout

    try:
        payload, err, stdout = _attempt(prompt)
        if err:
            return {"phase": "challenge", "exit_code": 1, "error": err,
                    "stdout": stdout}
        if not _validate(payload):
            payload, err, stdout = _attempt(
                prompt + "\n\nIMPORTANT: Respond with raw JSON only, matching "
                         "the schema exactly. No markdown, no code fences, "
                         "no explanations."
            )
            if err:
                return {"phase": "challenge", "exit_code": 1, "error": err,
                        "stdout": stdout}
            if not _validate(payload):
                return {
                    "phase": "challenge", "exit_code": 1,
                    "findings": [], "verdict": "UNKNOWN",
                    "error": "invalid JSON after retry", "stdout": stdout,
                }
        return {
            "phase": "challenge", "exit_code": 0,
            "findings": payload["findings"],
            "verdict": payload["verdict"],
            "summary": payload.get("summary", ""),
            "stdout": stdout,
        }
    except Exception as exc:  # defensive: never leak an exception to the loop
        return {"phase": "challenge", "exit_code": 1, "error": str(exc)}
