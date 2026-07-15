"""Phase modules for the adversarial-plan pipeline.

One module per phase, one public function per module (same layout as
adversarial-spec's ``scripts/phases``, which mirrors adversarial-code-loop):

  phase_plan       — plan-writer writes ``plan.md`` to disk (PLAN / BUILD)
  phase_challenge  — plan-challenger reviews ``plan.md`` (CHALLENGE / REVIEW)
  phase_revise     — plan-writer amends ``plan.md`` (REVISE / FIX)
  phase_verify     — plan-challenger checks the findings (VERIFY)

Helpers shared by more than one phase live here:

  run_role()           — persona-aware CLI execution with base-name fallback
  try_parse_json()     — 3-strategy JSON extraction (fences, ``{..}``, ``[..]``)
  validate_plan_file() — ``plan.md`` existence + YAML frontmatter validation
"""

import json
import re
import sys
from pathlib import Path

# The adversarial-common sibling skill must be importable. The orchestrator
# inserts it on sys.path before importing us; the fallback below keeps the
# package importable on its own (tests, REPL).
try:
    from adversarial_common import persona_path, runner
    from adversarial_common.providers import enhance_cmd_for_project, persona_for_role
except ImportError:  # pragma: no cover - exercised only on bare imports
    _COMMON = Path(__file__).resolve().parents[3] / "adversarial-common"
    sys.path.insert(0, str(_COMMON))
    from adversarial_common import persona_path, runner
    from adversarial_common.providers import enhance_cmd_for_project, persona_for_role

__all__ = [
    "run_role",
    "resolve_persona",
    "try_parse_json",
    "extract_frontmatter",
    "validate_plan_file",
]


# --- persona-aware execution --------------------------------------------------

def resolve_persona(role, cmd):
    """Absolute persona file path for *role*, or None when none exists.

    ``persona_for_role`` may return a provider-specific variant (e.g.
    ``plan-writer-pi``); when that file does not exist we fall back to the
    base persona instead of silently running without one (unlike
    ``providers.run_cmd``, which drops the persona entirely in that case).
    """
    for name in (persona_for_role(role, cmd), role):
        try:
            return persona_path(name)
        except FileNotFoundError:
            continue
    return None


def run_role(cmd, prompt, role, timeout, cwd, *,
             ledger=None, show_costs=False, max_retries=3,
             max_input_chars=None, max_output_chars=None):
    """Run a role command with its persona injected.

    Returns ``(stdout, stderr, returncode)`` from the hardened
    ``runner.run_cli`` (temp-file IO, process-group kill on timeout).

    Accepts optional reliability/cost keyword args forwarded to
    :func:`runner.run_cli` (ledger, caps, retries, show_costs).
    """
    cmd = enhance_cmd_for_project(cmd, cwd)
    return runner.run_cli(
        cmd, stdin_text=prompt, timeout=timeout, cwd=cwd,
        persona_file=resolve_persona(role, cmd),
        ledger=ledger, show_costs=show_costs,
        max_retries=max_retries,
        max_input_chars=max_input_chars,
        max_output_chars=max_output_chars,
        phase=role, persona=role,
    )


# --- JSON extraction (via shared jsonio) ---------------------------------------
# Re-export for backward compatibility (callers import try_parse_json from phases)
from adversarial_common import jsonio

try_parse_json = jsonio.parse_json_output
# Keep extract_frontmatter as a re-export too (callers import from phases)
extract_frontmatter = jsonio.extract_frontmatter


# --- plan.md validation ---------------------------------------------------------

_PLAN_FILENAME = "plan.md"


def validate_plan_file(workdir):
    """Validate ``<workdir>/plan.md``. Returns ``(ok, error_message)``.

    Checks: file exists, is readable, non-empty, has a YAML frontmatter
    block that parses to a mapping containing a non-empty ``spec`` key,
    and contains at least one step heading (``### P1: ...``) — the minimum
    contract from the plan-writer persona.
    """
    path = Path(workdir) / _PLAN_FILENAME
    if not path.is_file():
        return False, f"{_PLAN_FILENAME} not found in {workdir}"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"{_PLAN_FILENAME} unreadable: {exc}"
    if not text.strip():
        return False, f"{_PLAN_FILENAME} is empty"

    fm_text = extract_frontmatter(text)
    if fm_text is None:
        return False, f"{_PLAN_FILENAME} has no YAML frontmatter (--- ... ---)"
    data, err = jsonio.parse_frontmatter(fm_text)
    if err:
        return False, f"{_PLAN_FILENAME} frontmatter: {err}"
    spec_name = data.get("spec")
    if not (isinstance(spec_name, str) and spec_name.strip()):
        return False, f"{_PLAN_FILENAME} frontmatter is missing a non-empty 'spec' key"
    if not re.search(r"^###\s+P\d+\s*:", text, re.MULTILINE):
        return False, f"{_PLAN_FILENAME} has no step headings (### P1: ...)"
    return True, ""
