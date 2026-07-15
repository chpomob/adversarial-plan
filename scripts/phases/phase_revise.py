"""
REVISE phase: the plan-writer amends ``plan.md`` on disk from findings.

The findings are passed as JSON in the prompt; the model edits ``plan.md``
in place (keeping step ids ``P1``, ``P2``, ... stable). The file is
re-validated and committed as ``revise: <feature> — round N``.

Assumption: the spec sketches ``run_revise(..., loop_n, providers)``;
following rule 1 ("same pattern as adversarial_spec.py") the loop counter is
named *round_n* and provider machinery is reached through the shared
:func:`run_role` helper, injectable as *run* for tests.
"""
import json

from adversarial_common import gitops

from . import run_role, validate_plan_file

__all__ = ["run_revise"]


def run_revise(findings, dev_cmd, workdir, timeout, feature, round_n, run=None, *,
              ledger=None, show_costs=False, max_retries=3,
              max_input_chars=None, max_output_chars=None):
    """
    Run the plan-writer in FIX mode against the challenger's findings.

    Returns ``{"phase": "revise", "exit_code": 0, "commit_sha": "..."}``;
    on failure ``{"phase": "revise", "exit_code": 1, "error": "..."}``.
    *run* is injectable for tests and defaults to :func:`run_role`.
    """
    run = run or run_role
    try:
        prompt = (
            "Revise the implementation plan `plan.md` in the current working "
            "directory to address every finding below (the specification is "
            "on disk at `spec.md`). Edit the file on disk — do not rewrite "
            "it from scratch unless a blocker forces it, and keep existing "
            "step ids (P1, P2, ...) stable.\n"
            "Do not print the plan body to stdout.\n\n"
            f"Findings:\n{json.dumps(findings, indent=2)}"
        )
        stdout, stderr, code = run(dev_cmd, prompt, "plan-writer", timeout, workdir,
                                   ledger=ledger, show_costs=show_costs,
                                   max_retries=max_retries,
                                   max_input_chars=max_input_chars,
                                   max_output_chars=max_output_chars)
        if code != 0:
            return {
                "phase": "revise",
                "exit_code": 1,
                "error": f"REVISE exited {code}: {(stderr or '')[:200]}",
                "stdout": stdout,
            }
        ok, err = validate_plan_file(workdir)
        if not ok:
            return {
                "phase": "revise",
                "exit_code": 1,
                "error": f"plan validation failed after revise: {err}",
                "stdout": stdout,
            }
        gitops.commit_all(workdir, f"revise: {feature} — round {round_n}")
        return {
            "phase": "revise",
            "exit_code": 0,
            "commit_sha": gitops.head_sha(workdir),
            "stdout": stdout,
        }
    except Exception as exc:  # defensive: never leak an exception to the loop
        return {"phase": "revise", "exit_code": 1, "error": str(exc)}
