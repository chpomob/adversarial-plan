"""
PLAN phase: the plan-writer model writes ``plan.md`` to disk.

The model receives the spec (and optional review findings) in the prompt
(persona ``plan-writer``) and must write ``plan.md`` into *workdir*, where
``spec.md`` — and ``findings.json`` when findings were provided — are also
staged on disk. This phase then validates the file (existence + YAML
frontmatter with a ``spec`` key + contiguous, requirement-covering steps) and stages/commits
everything as ``plan: <feature> — <summary>``.

Provider-aware execution is routed through the shared ``run_phase_cmd`` API;
the legacy *run* injection remains available for tests and downstream callers.
"""
import json
import re
from pathlib import Path

from adversarial_common import NoProviderAvailable, gitops, run_phase_cmd

from . import (enhance_cmd_for_project, provider_history,
               raise_no_provider_available, resolve_persona, runtime_metadata,
               validate_plan_file)

__all__ = ["run_plan", "validate_step_coverage"]


_STEP_RE = re.compile(
    r"^###\s+(?P<id>P\d+)\s*:[^\n]*(?:\n|\Z)"
    r"(?P<body>.*?)(?=^###\s+P\d+\s*:|^##\s+|\Z)",
    re.MULTILINE | re.DOTALL,
)
_SECTION_RE = re.compile(
    r"^##\s+(?P<name>Requirements|Specification)\s*$"
    r"(?P<body>.*?)(?=^##\s+|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_REQUIREMENT_RE = re.compile(
    r"(?:"
    r"^###\s+(?P<heading>(?:Item\s+\d+|R\d+[A-Za-z0-9_.-]*|"
    r"REQ(?:UIREMENT)?[-_ ]?\d+[A-Za-z0-9_.-]*))(?=\s*[:.-]|\s*$)"
    r"|^\s*(?:[-*+] |\d+[.)]\s+)"
    r"(?P<list>(?:Item\s+\d+|R\d+[A-Za-z0-9_.-]*|"
    r"REQ(?:UIREMENT)?[-_ ]?\d+[A-Za-z0-9_.-]*))(?=\s*[:.)-])"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


def _canonical_requirement_id(value):
    """Normalise harmless spelling differences without conflating R1/R10."""
    return re.sub(r"[\s_-]+", "", value).casefold()


def _extract_requirement_ids(spec_text):
    """Return ordered, unique requirement ids from supported spec sections."""
    requirements = []
    seen = set()
    for section in _SECTION_RE.finditer(spec_text or ""):
        for match in _REQUIREMENT_RE.finditer(section.group("body")):
            requirement = match.group("heading") or match.group("list")
            canonical = _canonical_requirement_id(requirement)
            if canonical not in seen:
                requirements.append(requirement.strip())
                seen.add(canonical)
    return requirements


def _requirement_is_referenced(requirement, text):
    """Match a requirement id as a token, allowing Item/REQ separators."""
    parts = [re.escape(part) for part in re.split(r"[\s_-]+", requirement)]
    pattern = r"(?<![A-Za-z0-9])" + r"[\s_-]+".join(parts)
    pattern += r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def validate_step_coverage(spec_text, plan_text):
    """Validate step numbering and spec-requirement coverage.

    Coverage is checked only inside Pn step blocks. A mapping claimed in prose
    or in the ordering rationale is not actionable for the code loop.
    """
    steps = list(_STEP_RE.finditer(plan_text or ""))
    step_ids = [match.group("id").upper() for match in steps]
    expected_ids = [f"P{index}" for index in range(1, len(step_ids) + 1)]
    if step_ids != expected_ids:
        found = ", ".join(step_ids) if step_ids else "none"
        return False, (
            "plan step ids must be unique and contiguous from P1 "
            f"(found: {found})"
        )

    requirements = _extract_requirement_ids(spec_text)
    if not requirements:
        return False, (
            "spec has no identifiable requirements in its Requirements or "
            "Specification section"
        )

    step_text = "\n".join(match.group(0) for match in steps)
    uncovered = [
        requirement for requirement in requirements
        if not _requirement_is_referenced(requirement, step_text)
    ]
    if uncovered:
        return False, "requirements not covered by a plan step: " + ", ".join(uncovered)
    return True, ""


def _short_summary(spec_text, limit=60):
    """Derive a one-line commit summary from the first non-empty spec line
    after the frontmatter block."""
    in_frontmatter = False
    for i, line in enumerate((spec_text or "").splitlines()):
        stripped = line.strip()
        if i == 0 and stripped == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped == "---":
                in_frontmatter = False
            continue
        stripped = stripped.lstrip("#").strip()
        if stripped:
            return stripped[:limit]
    return "implementation plan"


def _build_prompt(spec_text, findings):
    parts = [
        "Write a complete implementation plan for the specification below "
        "(also on disk at `spec.md` in the current working directory).\n"
        "Write the file `plan.md` to disk in the current working directory, "
        "with YAML frontmatter (spec, version, author, based-on, "
        "findings-input), then a `## Steps` section of ordered steps "
        "(### P1:, ### P2:, ... each with Files, Description, Dependencies, "
        "Tests, Risks) and an `## Ordering rationale` section.\n"
        "Do not print the plan body to stdout — write it to disk.\n"
        "Every step must cite the requirement ids it covers in its heading, "
        "Description, or Tests field; step ids must be contiguous from P1.\n"
    ]
    if findings:
        parts.append(
            "\nReview findings are provided below (also on disk at "
            "`findings.json`). The plan must address each finding in at "
            "least one step and set `findings-input: true` in the "
            "frontmatter.\n\n"
            f"Findings:\n{json.dumps(findings, indent=2)}\n"
        )
    else:
        parts.append(
            "\nNo review findings were provided: plan from the spec alone "
            "and set `findings-input: false` in the frontmatter.\n"
        )
    parts.append(f"\nSpecification:\n\n{spec_text}")
    return "".join(parts)


def run_plan(spec_text, findings, dev_cmd, workdir, timeout, feature,
             resolver=None, run=None, *, explicit_cmd=None, force=False,
             force_provider=None, ledger=None, show_costs=False, max_retries=3,
             max_input_chars=None, max_output_chars=None):
    """
    Run the plan-writer with the spec (+ optional findings) as input,
    validate ``plan.md``, commit.

    Returns ``{"phase": "plan", "exit_code": 0, "commit_sha": "..."}``;
    on infrastructure failure exit 1; invalid plan coverage returns exit 2.
    *run* retains the legacy injectable test interface; normal execution uses
    :func:`run_phase_cmd` and the provider registry.
    """
    if run is None and callable(resolver) and not hasattr(resolver, "resolve"):
        run, resolver = resolver, None
    try:
        prompt = _build_prompt(spec_text, findings)
        if run is not None:
            provider_result = run(
                dev_cmd, prompt, "plan-writer", timeout, workdir,
                ledger=ledger, show_costs=show_costs,
                max_retries=max_retries,
                max_input_chars=max_input_chars,
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
                "stdin_text": prompt,
                "timeout": timeout,
                "persona_file": resolve_persona("plan-writer", persona_cmd),
                "persona": "plan-writer",
                "ledger": ledger,
                "show_costs": show_costs,
                "max_retries": max_retries,
            }
            if max_input_chars is not None:
                execution_args["max_input_chars"] = max_input_chars
            if max_output_chars is not None:
                execution_args["max_output_chars"] = max_output_chars
            provider_result = run_phase_cmd(
                phase_name="plan", role="writer", workdir=workdir,
                resolver=resolver, explicit_cmd=selected_explicit, force=force,
                force_provider=force_provider, **command_args, **execution_args,
            )
            raise_no_provider_available(provider_result, "writer", "plan")
        stdout, stderr, code = provider_result[:3]
        history = provider_history([provider_result])
        runtime = runtime_metadata(provider_result)
        if code != 0:
            return {
                "phase": "plan",
                "exit_code": 1,
                "error": f"PLAN exited {code}: {(stderr or '')[:200]}",
                "stdout": stdout,
                "execution": runtime,
                "provider_history": history,
            }
        ok, err = validate_plan_file(workdir)
        if not ok:
            return {
                "phase": "plan",
                "exit_code": 1,
                "error": f"plan validation failed: {err}",
                "stdout": stdout,
                "execution": runtime,
                "provider_history": history,
            }
        try:
            plan_text = (Path(workdir) / "plan.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return {
                "phase": "plan",
                "exit_code": 2,
                "error": f"plan coverage validation failed: plan.md unreadable: {exc}",
                "stdout": stdout,
                "execution": runtime,
                "provider_history": history,
            }
        ok, err = validate_step_coverage(spec_text, plan_text)
        if not ok:
            return {
                "phase": "plan",
                "exit_code": 2,
                "error": f"plan coverage validation failed: {err}",
                "stdout": stdout,
                "execution": runtime,
                "provider_history": history,
            }
        gitops.commit_all(workdir, f"plan: {feature} — {_short_summary(spec_text)}")
        return {
            "phase": "plan",
            "exit_code": 0,
            "commit_sha": gitops.head_sha(workdir),
            "stdout": stdout,
            "execution": runtime,
            "provider_history": history,
        }
    except NoProviderAvailable:
        raise
    except Exception as exc:
        return {"phase": "plan", "exit_code": 1, "error": str(exc)}
