"""
REVISE phase: the plan-writer amends ``plan.md`` on disk from findings.

The findings are passed as JSON in the prompt; the model edits ``plan.md``
in place (keeping step ids ``P1``, ``P2``, ... stable). The file is
re-validated and committed as ``revise: <feature> — round N``.

Provider-aware execution is routed through the shared ``run_phase_cmd`` API;
the legacy *run* injection remains available for tests and downstream callers.
"""
import json

from adversarial_common import NoProviderAvailable, gitops, run_phase_cmd

from . import (enhance_cmd_for_project, provider_history,
               raise_no_provider_available, resolve_persona, runtime_metadata,
               validate_plan_file)

__all__ = ["run_revise"]


def run_revise(findings, dev_cmd, workdir, timeout, feature, round_n,
              resolver=None, run=None, *, explicit_cmd=None, force=False,
              force_provider=None, ledger=None, show_costs=False, max_retries=3,
              max_input_chars=None, max_output_chars=None):
    """
    Run the plan-writer in FIX mode against the challenger's findings.

    Returns ``{"phase": "revise", "exit_code": 0, "commit_sha": "..."}``;
    on failure ``{"phase": "revise", "exit_code": 1, "error": "..."}``.
    *run* retains the legacy injectable test interface.
    """
    if run is None and callable(resolver) and not hasattr(resolver, "resolve"):
        run, resolver = resolver, None
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
        if run is not None:
            provider_result = run(
                dev_cmd, prompt, "plan-writer", timeout, workdir,
                ledger=ledger, show_costs=show_costs,
                max_retries=max_retries, max_input_chars=max_input_chars,
                max_output_chars=max_output_chars,
            )
        else:
            legacy_cmd = enhance_cmd_for_project(dev_cmd, workdir)
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
                "stdin_text": prompt, "timeout": timeout,
                "persona_file": resolve_persona("plan-writer", persona_cmd),
                "persona": "plan-writer", "ledger": ledger,
                "show_costs": show_costs, "max_retries": max_retries,
            }
            if max_input_chars is not None:
                execution_args["max_input_chars"] = max_input_chars
            if max_output_chars is not None:
                execution_args["max_output_chars"] = max_output_chars
            provider_result = run_phase_cmd(
                phase_name="revise", role="writer", workdir=workdir,
                resolver=resolver, explicit_cmd=selected_explicit, force=force,
                force_provider=force_provider, **command_args, **execution_args,
            )
            raise_no_provider_available(provider_result, "writer", "revise")
        stdout, stderr, code = provider_result[:3]
        history = provider_history([provider_result])
        runtime = runtime_metadata(provider_result)
        if code != 0:
            return {
                "phase": "revise",
                "exit_code": 1,
                "error": f"REVISE exited {code}: {(stderr or '')[:200]}",
                "stdout": stdout,
                "execution": runtime,
                "provider_history": history,
            }
        ok, err = validate_plan_file(workdir)
        if not ok:
            return {
                "phase": "revise",
                "exit_code": 1,
                "error": f"plan validation failed after revise: {err}",
                "stdout": stdout,
                "execution": runtime,
                "provider_history": history,
            }
        gitops.commit_all(workdir, f"revise: {feature} — round {round_n}")
        return {
            "phase": "revise",
            "exit_code": 0,
            "commit_sha": gitops.head_sha(workdir),
            "stdout": stdout,
            "execution": runtime,
            "provider_history": history,
        }
    except NoProviderAvailable:
        raise
    except Exception as exc:  # defensive: never leak an exception to the loop
        return {"phase": "revise", "exit_code": 1, "error": str(exc)}
