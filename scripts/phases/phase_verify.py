"""
VERIFY phase: the plan-challenger checks whether findings are resolved.

The revised ``plan.md`` and the findings are embedded in the prompt; the
model marks each finding resolved / rejected / disputed and gives an overall
APPROVE/REJECT verdict. JSON extraction uses the shared 3-strategy parser
(:func:`try_parse_json`); one retry with a stricter instruction on invalid
JSON.

Provider-aware execution is routed through the shared ``run_phase_cmd`` API;
the legacy *run* injection remains available for tests and downstream callers.
"""
import json
from pathlib import Path

from adversarial_common import NoProviderAvailable, run_phase_cmd

from . import (enhance_cmd_for_project, provider_history,
               raise_no_provider_available, resolve_persona, runtime_metadata,
               try_parse_json)

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


def run_verify(findings, review_cmd, workdir, timeout, resolver=None, run=None,
              branch_point="", *, explicit_cmd=None, force=False,
              force_provider=None, ledger=None, show_costs=False, max_retries=3,
              max_input_chars=None, max_output_chars=None):
    """
    Run the plan-challenger in VERIFY mode against the revised plan.

    Returns ``{"phase": "verify", "exit_code": 0, "results": [...],
    "verdict": "APPROVE|REJECT"}``; on failure ``{"phase": "verify",
    "exit_code": 1, "error": "..."}``. *run* is injectable for tests.
    """
    if run is None and callable(resolver) and not hasattr(resolver, "resolve"):
        run, resolver = resolver, None
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

    provider_results = []
    runtime_calls = []

    def _attempt(prompt_text):
        if run is not None:
            result = run(
                review_cmd, prompt_text, "plan-challenger", timeout, workdir,
                ledger=ledger, show_costs=show_costs,
                max_retries=max_retries, max_input_chars=max_input_chars,
                max_output_chars=max_output_chars,
            )
        else:
            legacy_cmd = enhance_cmd_for_project(review_cmd, workdir)
            selected_explicit = (
                enhance_cmd_for_project(explicit_cmd, workdir)
                if explicit_cmd is not None else None
            )
            command_args = {}
            if resolver is None and explicit_cmd is None:
                command_args["cmd"] = legacy_cmd
            persona_cmd = selected_explicit or (
                legacy_cmd if resolver is None else ""
            )
            execution_args = {
                "stdin_text": prompt_text, "timeout": timeout,
                "persona_file": resolve_persona("plan-challenger", persona_cmd),
                "persona": "plan-challenger", "ledger": ledger,
                "show_costs": show_costs, "max_retries": max_retries,
            }
            if max_input_chars is not None:
                execution_args["max_input_chars"] = max_input_chars
            if max_output_chars is not None:
                execution_args["max_output_chars"] = max_output_chars
            result = run_phase_cmd(
                phase_name="verify", role="verify", workdir=workdir,
                resolver=resolver, explicit_cmd=selected_explicit, force=force,
                force_provider=force_provider, **command_args, **execution_args,
            )
            raise_no_provider_available(result, "verify", "verify")
        provider_results.append(result)
        runtime_calls.append(runtime_metadata(result))
        stdout, stderr, code = result[:3]
        if code != 0:
            return None, f"VERIFY exited {code}: {(stderr or '')[:200]}", stdout
        return try_parse_json(stdout), None, stdout

    def _evidence():
        return {
            "execution": {"calls": runtime_calls},
            "provider_history": provider_history(provider_results),
        }

    try:
        payload, err, stdout = _attempt(prompt)
        if err:
            return {"phase": "verify", "exit_code": 1, "error": err,
                    "stdout": stdout, **_evidence()}
        if not _validate(payload):
            payload, err, stdout = _attempt(
                prompt + "\n\nIMPORTANT: Respond with raw JSON only. "
                         "No markdown, no code fences, no explanations."
            )
            if err:
                return {"phase": "verify", "exit_code": 1, "error": err,
                        "stdout": stdout, **_evidence()}
            if not _validate(payload):
                return {
                    "phase": "verify", "exit_code": 1,
                    "results": [], "verdict": "UNKNOWN",
                    "error": "invalid JSON after retry", "stdout": stdout,
                    **_evidence(),
                }
        return {
            "phase": "verify", "exit_code": 0,
            "results": payload.get("results", []),
            "verdict": payload.get("verdict", "REJECT"),
            "stdout": stdout,
            **_evidence(),
        }
    except NoProviderAvailable:
        raise
    except Exception as exc:  # defensive: never leak an exception to the loop
        return {"phase": "verify", "exit_code": 1, "error": str(exc)}
