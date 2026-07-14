"""
VERIFY phase: the plan-challenger checks whether findings are resolved.

The revised ``plan.md`` and the findings are embedded in the prompt; the
model marks each finding resolved / rejected / disputed and gives an overall
APPROVE/REJECT verdict. JSON extraction uses the shared 3-strategy parser
(:func:`try_parse_json`); one retry with a stricter instruction on invalid
JSON.

Assumption: the spec sketches ``run_verify(findings, dev_cmd, workdir,
providers, jsonio)``; following rule 1 ("same pattern as
adversarial_spec.py") the signature is ``(findings, review_cmd, workdir,
timeout, run=None)`` — verification runs with the review command.
"""
import json
from pathlib import Path

from . import run_role, try_parse_json

__all__ = ["run_verify"]

_VALID_VERDICTS = {"APPROVE", "REJECT"}
_VALID_STATUS = {"resolved", "rejected", "disputed"}


def _validate(payload):
    if not isinstance(payload, dict):
        return False
    if payload.get("verdict") not in _VALID_VERDICTS:
        return False
    results = payload.get("results")
    if not isinstance(results, list):
        return False
    for item in results:
        if not isinstance(item, dict):
            return False
        if item.get("status") not in _VALID_STATUS:
            return False
    return True


def run_verify(findings, review_cmd, workdir, timeout, run=None, branch_point=""):
    """
    Run the plan-challenger in VERIFY mode against the revised plan.

    Returns ``{"phase": "verify", "exit_code": 0, "results": [...],
    "verdict": "APPROVE|REJECT"}``; on failure ``{"phase": "verify",
    "exit_code": 1, "error": "..."}``. *run* is injectable for tests.
    """
    run = run or run_role
    try:
        plan_text = (Path(workdir) / "plan.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"phase": "verify", "exit_code": 1,
                "error": f"could not read plan.md: {exc}"}

    diff_base = branch_point or "<branch-point>"
    prompt = (
        "The implementation plan `plan.md` was revised to address the "
        "findings below. For each finding, decide whether it is **resolved** "
        "(the plan now addresses it), **rejected** (the finding was wrong), "
        "or **disputed** (still open / unclear). You may also run "
        "the cumulative diff in the current directory with "
        f"`git diff {diff_base}..HEAD` to see the exact "
        "revision.\n\n"
        f"Findings:\n{json.dumps(findings, indent=2)}\n\n"
        "Output ONLY valid JSON:\n"
        '{"results": [{"id": "P1", "status": "resolved|rejected|disputed", '
        '"note": "optional"}], "verdict": "APPROVE|REJECT"}\n\n'
        f"--- revised plan.md ---\n{plan_text}"
    )

    def _attempt(prompt_text):
        stdout, stderr, code = run(
            review_cmd, prompt_text, "plan-challenger", timeout, workdir)
        if code != 0:
            return None, f"VERIFY exited {code}: {(stderr or '')[:200]}", stdout
        return try_parse_json(stdout), None, stdout

    try:
        payload, err, stdout = _attempt(prompt)
        if err:
            return {"phase": "verify", "exit_code": 1, "error": err,
                    "stdout": stdout}
        if not _validate(payload):
            payload, err, stdout = _attempt(
                prompt + "\n\nIMPORTANT: Respond with raw JSON only. "
                         "No markdown, no code fences, no explanations."
            )
            if err:
                return {"phase": "verify", "exit_code": 1, "error": err,
                        "stdout": stdout}
            if not _validate(payload):
                return {
                    "phase": "verify", "exit_code": 1,
                    "results": [], "verdict": "UNKNOWN",
                    "error": "invalid JSON after retry", "stdout": stdout,
                }
        return {
            "phase": "verify", "exit_code": 0,
            "results": payload.get("results", []),
            "verdict": payload.get("verdict", "REJECT"),
            "stdout": stdout,
        }
    except Exception as exc:  # defensive: never leak an exception to the loop
        return {"phase": "verify", "exit_code": 1, "error": str(exc)}
