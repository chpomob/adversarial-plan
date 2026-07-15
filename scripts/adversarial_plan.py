#!/usr/bin/env python3
"""Adversarial Plan — git-native orchestrator.

PLAN -> CHALLENGE -> (REVISE -> VERIFY)^N, on a dedicated ``plan/<feature>/<N>``
branch. One model (plan-writer) writes ``plan.md`` from a ``spec.md`` (and
optional review findings), another (plan-challenger) challenges it; the writer
revises, the challenger verifies. On approval the branch is squash-merged into
the parent; otherwise a ``[REJECTED]`` marker commit is recorded.

Phase logic lives in scripts/phases/*; the shared engine (gitops, providers,
jsonio) lives in the adversarial-common sibling skill. This file only wires
phases together and maps verdicts to exit codes (same layout as
adversarial-spec's adversarial_spec.py, which mirrors adversarial-code-loop).

Optional modes (P17):
  --deep-research   run bounded external research after preflight
  --delegated       delegate high-complexity specs to worker decomposition
  --html            render an HTML report after final.json
  --ci              CI-friendly output (no banners, plain stderr, stable codes)
  --fail-on         set failure conditions (findings, severity, verdict, …)

Exit codes:
  0 APPROVED — plan squash-merged into the parent branch (or left on its
               branch with --no-merge)
  1 infrastructure failure (phase crash, git error, interrupt)
  2 usage error (bad flags, missing/empty spec, unparseable findings)
  3 REJECT   — findings unresolved after max-loops

In --ci mode, ``runner.ci_exit_code`` maps the final verdict to stable
exit codes (CI_EXIT_CLEAN, CI_EXIT_INFRASTRUCTURE, CI_EXIT_BLOCKING,
CI_EXIT_NON_BLOCKING, CI_EXIT_CONTEXT_BLOCKED).

The machine-readable contract is <out>/<feature>/final.json; the produced
``plan.md`` is directly consumable by adversarial-code-loop v4.
"""
import argparse
import html as _html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
# skill root (for `scripts.phases.*`) and the adversarial-common sibling skill
# (for `adversarial_common.*`) must both be importable.
sys.path.insert(0, str(_SCRIPTS_DIR.parent))
sys.path.insert(0, str(_SCRIPTS_DIR.parent.parent / "adversarial-common"))

from adversarial_common import costs, gates, gitops, jsonio
from adversarial_common.providers import resolve_role_cmd
from adversarial_common.runner import (
    CI_EXIT_BLOCKING, CI_EXIT_CLEAN, CI_EXIT_CONTEXT_BLOCKED,
    CI_EXIT_INFRASTRUCTURE, CI_EXIT_NON_BLOCKING,
    ci_exit_code, ci_mode, ci_print, ensure_final_payload,
    run_delegated, run_research,
)
from scripts.phases import (extract_frontmatter, phase_challenge, phase_plan,
                            phase_revise, phase_verify)

EXIT_APPROVED = 0
EXIT_INFRA = 1
EXIT_USAGE = 2
EXIT_REJECTED = 3

DEFAULT_DEV_CMD = "pi --provider zai --model glm-5.2"
DEFAULT_REVIEW_CMD = "pi --provider deepseek --model deepseek-v4-pro"
DEFAULT_RESEARCH_CMD = "pi --provider deepseek --model deepseek-v4-pro"

# Verifier statuses that no longer block approval: "resolved" (fixed) and
# "rejected" (the verifier showed the original finding was wrong).
_SETTLED_STATUSES = {"resolved", "rejected"}

# Complexity delegation threshold (R5 "high" tier).
_DELEGATE_COMPLEXITY = "high"


# --- small helpers -------------------------------------------------------------

def _banner(title, ci=False):
    """Print a phase banner (suppressed in CI mode)."""
    if ci:
        return
    print(f"\n{'=' * 60}\n  {title}\n{'=' * 60}")


def _write_json(out_dir, name, payload):
    """Persist *payload* as a pretty-printed JSON artifact under *out_dir*."""
    jsonio.save_artifact(out_dir, name, json.dumps(payload, indent=2) + "\n")


def _ensure_ids(findings):
    """Guarantee every finding has a unique, non-empty string id (in place)."""
    seen = set()
    for i, finding in enumerate(findings, 1):
        fid = str(finding.get("id") or "").strip() or f"finding-{i}"
        while fid in seen:
            fid = f"{fid}-{i}"
        finding["id"] = fid
        seen.add(fid)
    return findings


def _unresolved(findings, results):
    """Findings whose verify status is neither resolved nor rejected."""
    settled = {
        r.get("id") for r in results
        if r.get("id") is not None and r.get("status") in _SETTLED_STATUSES
    }
    return [f for f in findings if f.get("id") not in settled]


def _log_retrospective(label, result, feature, branch, out_dir):
    """Append a pipeline failure to <out_dir>/ISSUES.md (best-effort).

    Lives in the per-feature artifacts dir, not the skill install tree, so
    logging works on read-only installs and runs don't share one file.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    entry = (
        f"\n### {now.split()[0]} — {label} failed for {feature}\n\n"
        f"- **Phase:** {label}\n"
        f"- **Branch:** {branch}\n"
        f"- **Error:** {result.get('error', 'unknown error')}\n"
        f"- **Stdout (last 200 chars):** {result.get('stdout', '')[-200:]!r}\n"
        f"- **Auto-logged by pipeline**\n"
    )
    with (Path(out_dir) / "ISSUES.md").open("a", encoding="utf-8") as fh:
        fh.write(entry)


def _phase_failed(label, result, state, out_dir):
    """Report a phase failure, log it to the retrospective. Returns EXIT_INFRA."""
    ci_print(f"X {label} failed: {result.get('error', 'unknown error')}")
    try:
        _log_retrospective(label, result, state.get("feature", "unknown"),
                           state.get("branch", ""), out_dir)
    except Exception as exc:
        ci_print(f"! could not write retrospective log: {exc}")
    return EXIT_INFRA


def _restore(workdir, state):
    """Best-effort cleanup on every exit path: back to parent, pop stash."""
    parent = state.get("parent_branch", "")
    try:
        if parent and gitops.get_current_branch(workdir) != parent:
            gitops.checkout(workdir, parent)
    except gitops.GitError as exc:
        # Never unstash onto the wrong branch.
        ci_print(f"! could not restore branch {parent!r}: {exc}")
        return
    stash_id = state.get("stash_id", "")
    if stash_id:
        try:
            gitops.unstash(workdir, stash_id)
            state["stash_id"] = ""
        except gitops.GitError as exc:
            ci_print(f"! could not pop {stash_id}: {exc}")


# --- HTML report renderer (--html) ---------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Adversarial Plan — {feature}</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: system-ui, sans-serif; max-width: 960px; margin: 2rem auto;
       padding: 0 1rem; line-height: 1.5; }}
h1 {{ border-bottom: 2px solid #ccc; padding-bottom: .3rem; }}
.verdict {{ font-weight: bold; font-size: 1.2rem; }}
.verdict.approved {{ color: #2a7d2a; }}
.verdict.rejected {{ color: #c0392b; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
th, td {{ border: 1px solid #aaa; padding: .4rem .6rem; text-align: left; }}
th {{ background: #f0f0f0; }}
details {{ margin: .5rem 0; }}
summary {{ cursor: pointer; font-weight: 600; }}
pre {{ background: #f5f5f5; padding: .5rem; overflow-x: auto; font-size: .85rem; }}
code {{ font-size: .9em; }}
</style>
</head>
<body>
<h1>Adversarial Plan — {feature}</h1>
{body}
<p><small>Generated {timestamp}</small></p>
</body>
</html>
"""


def _render_html(out_dir, feature):
    """Read final.json from *out_dir* and write report.html alongside it."""
    final_path = Path(out_dir) / "final.json"
    if not final_path.is_file():
        return
    try:
        final = json.loads(final_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    body_parts = [_html_status(final)]
    body_parts.append(_html_complexity(final))
    body_parts.append(_html_costs(final))
    body_parts.append(_html_artifact_links(out_dir))
    body_parts.append(_html_plan_preview(out_dir))

    html = _HTML_TEMPLATE.format(
        feature=_html.escape(feature),
        body="\n".join(body_parts),
        timestamp=_html.escape(datetime.now(timezone.utc).isoformat()),
    )
    (Path(out_dir) / "report.html").write_text(html, encoding="utf-8")


def _html_status(final):
    verdict = final.get("verdict", "UNKNOWN")
    css = "approved" if verdict == "APPROVED" else "rejected"
    lines = [
        f'<p class="verdict {css}">Verdict: {_html.escape(verdict)}</p>',
        "<table>",
        f"<tr><th>Verdict</th><td>{_html.escape(verdict)}</td></tr>",
        f"<tr><th>Loops</th><td>{final.get('loops', 0)}</td></tr>",
        f"<tr><th>Reason</th><td>{_html.escape(str(final.get('reason', '')))}</td></tr>",
        f"<tr><th>Branch</th><td><code>{_html.escape(final.get('branch', ''))}</code></td></tr>",
        f"<tr><th>Merged</th><td>{final.get('merged', False)}</td></tr>",
        f"<tr><th>Artifacts</th><td><code>{_html.escape(final.get('artifacts_dir', ''))}</code></td></tr>",
        "</table>",
    ]
    return "\n".join(lines)


def _html_complexity(final):
    cx = final.get("complexity")
    if not isinstance(cx, dict):
        return ""
    lines = [
        "<h2>Complexity</h2>",
        "<table>",
        f"<tr><th>Score</th><td>{cx.get('score', '')}</td></tr>",
        f"<tr><th>Level</th><td>{_html.escape(str(cx.get('level', '')))}</td></tr>",
        f"<tr><th>Recommended Agents</th><td>{cx.get('recommended_agents', '')}</td></tr>",
        "</table>",
    ]
    return "\n".join(lines)


def _html_costs(final):
    costs_data = final.get("costs")
    if not isinstance(costs_data, dict):
        return ""
    total = costs_data.get("total", {})
    lines = [
        "<h2>Cost Summary</h2>",
        "<table>",
        "<tr><th>Prompt Tokens</th><th>Completion Tokens</th><th>Est. Cost (USD)</th></tr>",
        f"<tr><td>{total.get('prompt_tokens', 0)}</td>"
        f"<td>{total.get('completion_tokens', 0)}</td>"
        f"<td>${total.get('est_cost_usd', 0):.6f}</td></tr>",
        "</table>",
    ]
    models = costs_data.get("models", {})
    if models:
        lines.append("<h3>By Model</h3>")
        lines.append("<table><tr><th>Model</th><th>Prompt</th><th>Completion</th><th>Cost</th></tr>")
        for model, usage in sorted(models.items()):
            lines.append(
                f"<tr><td><code>{_html.escape(model)}</code></td>"
                f"<td>{usage.get('prompt_tokens', 0)}</td>"
                f"<td>{usage.get('completion_tokens', 0)}</td>"
                f"<td>${usage.get('est_cost_usd', 0):.6f}</td></tr>"
            )
        lines.append("</table>")
    return "\n".join(lines)


def _html_artifact_links(out_dir):
    artifacts = sorted(Path(out_dir).glob("*.json"))
    if not artifacts:
        return ""
    lines = ["<h2>Artifacts</h2>", "<ul>"]
    for path in artifacts:
        if path.name == "final.json":
            continue
        lines.append(f"<li><code>{_html.escape(path.name)}</code></li>")
    lines.append("</ul>")
    return "\n".join(lines)


def _html_plan_preview(out_dir):
    plan_path = Path(out_dir).parent.parent  # out_dir/feature -> workdir
    plan_md = plan_path / "plan.md"
    if not plan_md.is_file():
        return ""
    try:
        text = plan_md.read_text(encoding="utf-8")
    except OSError:
        return ""
    # Truncate to roughly 8 KB for the preview
    preview = text[:8192]
    truncated = len(text) > 8192
    lines = [
        "<h2>Plan Preview</h2>",
        "<details open>",
        "<summary>plan.md</summary>",
        f"<pre>{_html.escape(preview)}</pre>",
    ]
    if truncated:
        lines.append("<p><em>(truncated)</em></p>")
    lines.append("</details>")
    return "\n".join(lines)


# --- PHASE 0: git setup / finalize ---------------------------------------------

def _setup_git(workdir, feature, state=None):
    """Branch `plan/<feature>/<N>`, stash dirty, record branch-point, gitignore."""
    state = state if state is not None else {}
    # Establish the recovery slot before setup performs any git mutation.
    state.setdefault("stash_id", "")
    try:
        if gitops.detect_enclosing_repo(workdir):
            gitops.ensure_git_identity(workdir)
            parent = gitops.get_current_branch(workdir)
        else:
            gitops.auto_init(workdir)  # pins the initial branch to main
            parent = "main"
        state["parent_branch"] = parent
        state["stash_id"] = gitops.stash_dirty(workdir)
        branch = gitops.create_loop_branch(workdir, feature, parent, prefix="plan")
        state["branch"] = branch
        gitops.checkout(workdir, branch)
        branch_point = gitops.record_branch_point(workdir, parent)
        state["branch_point"] = branch_point
        gitops.ensure_gitignore(workdir, ".adversarial-plan/")
        return {"exit_code": 0, "parent_branch": parent, "branch": branch,
                "branch_point": branch_point, "stash_id": state["stash_id"]}
    except Exception as exc:
        return {"exit_code": 1, "error": str(exc)}


def _stage_inputs(workdir, spec_text, findings_text):
    """Materialise the inputs on the plan branch so the plan-writer (and the
    plan.md consumers) always find `spec.md` / `findings.json` in the workdir.

    Written unconditionally: a pre-existing dirty copy may have been stashed
    by PHASE 0, and an external --spec/--findings must land in the workdir.
    The plan-phase `commit_all` records them on the branch.
    """
    (Path(workdir) / "spec.md").write_text(spec_text, encoding="utf-8")
    if findings_text is not None:
        (Path(workdir) / "findings.json").write_text(findings_text,
                                                     encoding="utf-8")


def _final_md(verdict, feature, loops, reason):
    lines = [
        f"# Adversarial Plan — {feature}",
        "",
        f"- Verdict: {verdict}",
        f"- Revise/verify loops: {loops}",
        f"- Finished: {datetime.now(timezone.utc).isoformat()}",
    ]
    if reason:
        lines.append(f"- Reason: {reason}")
    return "\n".join(lines) + "\n"


def _finish(args, workdir, feature, out_dir, state, verdict, reason="", loops=0,
            costs=None, complexity=None, research_result=None, delegated_result=None,
            ci=True):
    """Squash-merge (APPROVED) or [REJECTED] marker, write final artifacts."""
    jsonio.save_artifact(out_dir, "final.md",
                         _final_md(verdict, feature, loops, reason))
    merged = False
    error = ""
    try:
        if verdict == "APPROVED":
            if not args.no_merge:
                gitops.squash_merge(
                    workdir, state["branch"], state["parent_branch"],
                    f"squash: {feature} — plan approved")
                merged = True
        else:
            gitops.reject_marker(workdir, f"{feature} — plan {verdict}")
    except gitops.GitError as exc:
        error = f"git finalize failed: {exc}"
        ci_print(f"X git finalize failed ({verdict}): {exc}")

    # Build and write final.json with optional extras
    final_kwargs = dict(
        reason=reason, loops=loops,
        branch=state.get("branch", ""),
        merged=merged,
        error=error,
        artifacts_dir=str(out_dir),
        complexity=complexity,
    )
    if costs is not None:
        final_kwargs["costs"] = costs
    if research_result is not None:
        final_kwargs["research"] = research_result
    if delegated_result is not None:
        final_kwargs["delegated"] = delegated_result
    final_payload = ensure_final_payload(
        verdict=verdict,
        infrastructure=bool(error),
        **{k: v for k, v in final_kwargs.items() if k != "verdict"},
    )
    jsonio.write_final_json(out_dir, verdict, **final_payload)

    # --html: render report after final.json
    if args.html:
        try:
            _render_html(out_dir, feature)
        except Exception as exc:
            ci_print(f"! HTML report generation failed: {exc}")

    ci_print(f"\n{verdict}" + (f" — {reason}" if reason else ""))

    # In CI mode, use ci_exit_code for stable exit codes
    if ci and args.ci:
        exit_code = ci_exit_code(
            verdict,
            infrastructure=bool(error),
            fail_on_selector=args.fail_on,
        )
        return exit_code

    if error:
        return EXIT_INFRA
    return EXIT_APPROVED if verdict == "APPROVED" else EXIT_REJECTED


# --- deep research (R10) -------------------------------------------------------

def _build_research_queries(spec_text, feature):
    """Derive research queries from the spec's frontmatter and content."""
    queries = []
    fm_text = extract_frontmatter(spec_text)
    if fm_text:
        data, _ = jsonio.parse_frontmatter(fm_text)
        if isinstance(data, dict):
            # Use frontmatter keywords/summary
            name = data.get("name", "").strip()
            if name:
                queries.append(f"current best practices for implementing: {name}")
            summary = data.get("summary", data.get("description", "")).strip()
            if summary:
                queries.append(summary)

    # Fallback: first substantive heading as query
    if not queries:
        for line in spec_text.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped and stripped != "---" and len(stripped) > 10:
                queries.append(stripped)
                break
    if not queries:
        queries.append(f"implementation plan for: {feature}")
    return queries


def _run_deep_research(args, spec_text, dev_cmd, workdir, feature, out_dir, ledger):
    """Run bounded external research and merge findings into the pipeline."""
    research_cmd = (os.environ.get("ADVERSARIAL_RESEARCH_CMD", "")
                    or args.research_cmd
                    or dev_cmd)
    queries = _build_research_queries(spec_text, feature)
    ci_print(f"  Research: {len(queries)} query(s) via {research_cmd[:60]}")

    result = run_research(
        queries,
        provider_cmd=research_cmd,
        enabled=True,
        max_queries=args.research_max_queries,
        max_results=args.research_max_results,
        timeout=args.research_timeout,
        cwd=workdir,
        ledger=ledger,
    )
    _write_json(out_dir, "00_research.json", result)
    if result is None:
        ci_print("  Research disabled (no provider configured)")
        return None

    status = result.get("status", "skipped")
    count = result.get("result_count", 0)
    ci_print(f"  Research: {status}, {count} finding(s)")
    if result.get("warnings"):
        for w in result["warnings"]:
            ci_print(f"    ! {w.get('message', str(w))}")
    return result


# --- delegated execution (R11) -------------------------------------------------

def _run_delegated_pipeline(args, spec_text, dev_cmd, workdir, feature,
                            out_dir, complexity, ledger):
    """Delegate a high-complexity spec to worker decomposition + synthesis.

    Uses runner.run_delegated which:
      1. Calls a decomposition model to split the spec into subtasks.
      2. Fans out workers (capped by complexity recommendation).
      3. Synthesizes surviving worker outputs into plan.md.
    """
    ci_print(f"  Delegating: complexity={complexity.get('level')} "
             f"(score={complexity.get('score')})")

    decomposition_call = {
        "cmd": dev_cmd,
        "timeout": args.timeout,
        "cwd": workdir,
        "ledger": ledger,
        "phase": "decomposition",
        "persona": "plan-writer",
        "max_retries": args.retries,
    }
    worker_call = {
        "cmd": dev_cmd,
        "timeout": args.timeout,
        "cwd": workdir,
        "ledger": ledger,
        "phase": "worker",
        "persona": "plan-writer",
        "max_retries": args.retries,
    }
    synthesis_call = {
        "cmd": dev_cmd,
        "timeout": args.timeout,
        "cwd": workdir,
        "ledger": ledger,
        "phase": "synthesis",
        "persona": "plan-writer",
        "max_retries": args.retries,
    }
    # Fallback: run the standard plan phase
    fallback_call = {
        "cmd": dev_cmd,
        "timeout": args.timeout,
        "cwd": workdir,
        "ledger": ledger,
        "phase": "plan",
        "persona": "plan-writer",
        "max_retries": args.retries,
    }

    result = run_delegated(
        spec_text,
        decomposition_call=decomposition_call,
        worker_call=worker_call,
        synthesis_call=synthesis_call,
        fallback_call=fallback_call,
        concurrency=args.delegated_concurrency,
        max_concurrency=6,
        complexity=complexity,
    )
    _write_json(out_dir, "00_delegated.json", result)
    status = result.get("status", "unknown")
    ci_print(f"  Delegated: {status}, mode={result.get('mode', 'direct')}")
    if result.get("reason"):
        ci_print(f"    Reason: {result['reason']}")
    return result


# --- pipeline -------------------------------------------------------------------

def _pipeline(args, dev_cmd, review_cmd, workdir, feature, out_dir,
              spec_text, findings, findings_text, state, ci=True):
    """Run the full workflow. Returns the process exit code."""
    # --- pre-flight: context check (R3) ---
    ctx = gates.check_context("spec", spec_text)
    if not ctx["ok"]:
        ci_print(f"X Spec context check failed: {ctx['reason']}")
        return EXIT_USAGE

    # --- cost ledger + complexity (R4) ---
    ledger = costs.CostLedger()
    complexity = gates.estimate_complexity(spec_text)

    # --- deep research (R10) ---
    research_result = None
    if args.deep_research:
        ci_print("  [deep-research enabled]")
        research_result = _run_deep_research(
            args, spec_text, dev_cmd, workdir, feature, out_dir, ledger)
        # Merge research findings into the findings list
        if research_result and research_result.get("findings"):
            research_findings = research_result["findings"]
            if findings is None:
                findings = []
            findings = list(findings) + list(research_findings)
            ci_print(f"  Merged {len(research_findings)} research findings")

    # PHASE 0 — GIT SETUP (must run before delegated so state is populated)
    setup = _setup_git(workdir, feature, state)
    state.update(setup)
    if setup["exit_code"] != 0:
        ci_print(f"X git setup failed: {setup.get('error', 'unknown error')}")
        return EXIT_INFRA
    state["feature"] = feature
    _banner(f"PLAN BRANCH  {setup['branch']}  (from {setup['parent_branch']})", ci)
    jsonio.save_artifact(out_dir, "00_spec.txt", spec_text)
    if findings_text is not None:
        jsonio.save_artifact(out_dir, "00_findings.json", findings_text)
    _stage_inputs(workdir, spec_text, findings_text)

    # --- delegated execution (R11) ---
    delegated_result = None
    if args.delegated:
        ci_print("  [delegated mode enabled]")
        delegated_result = _run_delegated_pipeline(
            args, spec_text, dev_cmd, workdir, feature, out_dir,
            complexity, ledger)
        # If delegated succeeded, skip the normal pipeline and finalize
        if delegated_result.get("status") in ("synthesized", "complete"):
            merged = delegated_result.get("delegated", False)
            if merged and delegated_result.get("result"):
                # Worker output may contain plan.md — validate and commit
                plan_path = Path(workdir) / "plan.md"
                if plan_path.is_file():
                    try:
                        gitops.commit_all(workdir,
                                          f"plan: {feature} — delegated synthesis")
                    except gitops.GitError as exc:
                        ci_print(f"! Delegated git commit failed: {exc}")

            cost_summary = ledger.summary()
            return _finish(args, workdir, feature, out_dir, state, "APPROVED",
                           loops=delegated_result.get("tasks_dispatched", 0),
                           costs=cost_summary, complexity=complexity,
                           research_result=research_result,
                           delegated_result=delegated_result,
                           ci=ci)
        # Fallback: delegated fell through to direct — proceed with normal pipeline
        ci_print("  Delegation fell back to direct pipeline")

    # --- tag user-provided findings (R8) ---
    if findings is not None:
        for finding in findings:
            if isinstance(finding, dict):
                finding.setdefault("origin", "user")

    # PHASE 1 — PLAN
    _banner("PLAN  (PLAN-WRITER)", ci)
    plan = phase_plan.run_plan(spec_text, findings, dev_cmd, workdir,
                               args.timeout, feature,
                               ledger=ledger, show_costs=args.show_costs,
                               max_retries=args.retries,
                               max_input_chars=args.max_input_chars,
                               max_output_chars=args.max_output_chars)
    _write_json(out_dir, "01_plan.json", plan)
    if plan["exit_code"] != 0:
        if plan.get("exit_code") == EXIT_USAGE:
            ci_print(f"X plan validation failed: {plan.get('error', 'invalid plan')}")
            return EXIT_USAGE
        return _phase_failed("plan", plan, state, out_dir)
    ci_print(f"  OK commit {plan.get('commit_sha', '')[:12]}")

    # PHASE 2 — CHALLENGE
    _banner("CHALLENGE  (PLAN-CHALLENGER)", ci)
    challenge = phase_challenge.run_challenge(
        review_cmd, workdir, args.timeout,
        branch_point=state["branch_point"],
        ledger=ledger, show_costs=args.show_costs,
        max_retries=args.retries,
        max_input_chars=args.max_input_chars,
        max_output_chars=args.max_output_chars)
    _write_json(out_dir, "02_challenge.json", challenge)
    if challenge["exit_code"] != 0:
        return _phase_failed("challenge", challenge, state, out_dir)
    # R5: normalize epistemic labels on challenge findings
    jsonio.normalize_findings(challenge)
    challenge_findings = _ensure_ids(challenge.get("findings", []))
    verdict = challenge.get("verdict", "APPROVE")
    ci_print(f"  OK {len(challenge_findings)} findings — verdict {verdict}")

    # PHASES 3/4 — REVISE / VERIFY loop. An empty findings list only approves
    # when the challenger's verdict is also APPROVE.
    approved = not challenge_findings and verdict == "APPROVE"
    loops_run = 0
    for n in range(1, args.max_loops + 1):
        if approved:
            break
        loops_run = n

        _banner(f"REVISE  (round {n}/{args.max_loops})", ci)
        revise = phase_revise.run_revise(challenge_findings, dev_cmd, workdir,
                                         args.timeout, feature, n,
                                         ledger=ledger, show_costs=args.show_costs,
                                         max_retries=args.retries,
                                         max_input_chars=args.max_input_chars,
                                         max_output_chars=args.max_output_chars)
        _write_json(out_dir, f"03_revise_{n}.json", revise)
        if revise["exit_code"] != 0:
            return _phase_failed(f"revise_{n}", revise, state, out_dir)

        _banner(f"VERIFY  (round {n}/{args.max_loops})", ci)
        verify = phase_verify.run_verify(
            challenge_findings, review_cmd, workdir, args.timeout,
            branch_point=state["branch_point"],
            ledger=ledger, show_costs=args.show_costs,
            max_retries=args.retries,
            max_input_chars=args.max_input_chars,
            max_output_chars=args.max_output_chars)
        _write_json(out_dir, f"04_verify_{n}.json", verify)
        if verify["exit_code"] != 0:
            return _phase_failed(f"verify_{n}", verify, state, out_dir)
        # R5: normalize epistemic labels on verify results
        jsonio.normalize_findings({"findings": verify.get("results", [])})

        results = verify.get("results", [])
        remaining = _unresolved(challenge_findings, results)
        ci_print(f"  Verdict {verify.get('verdict')} — "
                 f"{len(challenge_findings) - len(remaining)}"
                 f"/{len(challenge_findings)} settled")
        if verify.get("verdict") == "APPROVE" and results and not remaining:
            approved = True
            break
        # Narrow to the still-open findings for the next round; if the verifier
        # rejected overall while marking everything settled (contradiction),
        # keep the current list so the next round sees real content.
        if remaining:
            challenge_findings = remaining

    # R4: record costs + complexity in final.json
    cost_summary = ledger.summary()
    if approved:
        return _finish(args, workdir, feature, out_dir, state, "APPROVED",
                       loops=loops_run,
                       costs=cost_summary, complexity=complexity,
                       research_result=research_result,
                       delegated_result=delegated_result,
                       ci=ci)
    return _finish(args, workdir, feature, out_dir, state, "REJECT",
                   reason=f"findings unresolved after {args.max_loops} loops",
                   loops=loops_run,
                   costs=cost_summary, complexity=complexity,
                   research_result=research_result,
                   delegated_result=delegated_result,
                   ci=ci)


# --- CLI --------------------------------------------------------------------------

def _positive_int(value):
    """argparse type: strictly positive integer."""
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}")
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return ivalue


def build_parser():
    p = argparse.ArgumentParser(
        description="Adversarial Plan "
                    "(PLAN -> CHALLENGE -> (REVISE -> VERIFY)^N, git-native)")
    p.add_argument("--spec", default=None,
                   help="spec.md to plan (default: <workdir>/spec.md)")
    p.add_argument("--findings", default=None,
                   help="Optional findings.json from a review")
    p.add_argument("--dev-cmd", default=None,
                   help=f"plan-writer command (default: $APLAN_DEV_CMD or "
                        f"'{DEFAULT_DEV_CMD}')")
    p.add_argument("--review-cmd", default=None,
                   help=f"plan-challenger command (default: $APLAN_REVIEW_CMD "
                        f"or '{DEFAULT_REVIEW_CMD}')")
    p.add_argument("--workdir", default=".", help="Target directory (default: .)")
    p.add_argument("--max-loops", type=_positive_int, default=2)
    p.add_argument("--feature", default=None,
                   help="Branch/artifact name (default: spec filename)")
    p.add_argument("--timeout", type=_positive_int, default=600,
                   help="Per-subprocess timeout (s)")
    p.add_argument("--out", default=".adversarial-plan", help="Artifacts directory")
    p.add_argument("--no-merge", action="store_true",
                   help="On approval, leave the plan branch unmerged")
    p.add_argument("--show-costs", action="store_true",
                   help="Print per-phase cost breakdown to stderr")
    p.add_argument("--retries", type=_positive_int, default=3,
                   help="Max CLI retries per phase call (default: 3)")
    p.add_argument("--max-input-chars", type=_positive_int, default=None,
                   help="Cap prompt input chars per phase call")
    p.add_argument("--max-output-chars", type=_positive_int, default=None,
                   help="Cap provider output chars per phase call")

    # P17: Optional modes
    p.add_argument("--html", action="store_true",
                   help="Render an HTML report after final.json (R9)")
    p.add_argument("--ci", action="store_true",
                   help="CI-friendly output: no banners, plain stderr, stable exit codes (R10)")
    p.add_argument("--fail-on", default=None,
                   help="Failure conditions (e.g. 'findings,severity:blocker') (R10)")
    p.add_argument("--deep-research", action="store_true",
                   help="Run bounded external research after preflight (R11)")
    p.add_argument("--research-cmd", default=None,
                   help="Research provider command (default: dev-cmd or $ADVERSARIAL_RESEARCH_CMD)")
    p.add_argument("--research-max-queries", type=_positive_int, default=5,
                   help="Max research queries (default: 5)")
    p.add_argument("--research-max-results", type=_positive_int, default=5,
                   help="Max research results (default: 5)")
    p.add_argument("--research-timeout", type=_positive_int, default=60,
                   help="Per-query research timeout in seconds (default: 60)")
    p.add_argument("--delegated", action="store_true",
                   help="Delegate high-complexity specs to worker decomposition (R12)")
    p.add_argument("--delegated-concurrency", type=_positive_int, default=None,
                   help="Max concurrent delegated workers (default: complexity recommendation)")
    return p


def _derive_feature(args, spec_text):
    """Feature name: --feature > spec filename stem > frontmatter name >
    first heading line.

    A stem of exactly ``spec`` (the conventional filename) carries no
    information, so the frontmatter/heading fallbacks are used instead.
    """
    raw = args.feature or ""
    if not raw and args.spec:
        stem = Path(args.spec).stem
        if stem.lower() != "spec":
            raw = stem
    if not raw:
        fm = extract_frontmatter(spec_text) or ""
        for line in fm.splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip() == "name":
                raw = value.strip().strip("\"'")
                break
    if not raw:
        for line in spec_text.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped and stripped != "---":
                raw = stripped
                break
    return gitops.sanitize_feature_name(raw)


def _load_findings(path):
    """Read + parse review findings. Returns ``(findings, raw_text, error)``.

    Accepts either a bare JSON array of findings or an object with a
    ``findings`` array (the adversarial-review final.json shape).
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, None, f"could not read findings {path}: {exc}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, None, f"findings {path} is not valid JSON: {exc}"
    if isinstance(payload, dict):
        payload = payload.get("findings")
    if not isinstance(payload, list):
        return None, None, (f"findings {path} must be a JSON array or an "
                            f"object with a 'findings' array")
    return payload, raw, None


def main(argv=None):
    args = build_parser().parse_args(argv)

    workdir = str(Path(args.workdir).resolve())
    if not os.path.isdir(workdir):
        ci_print(f"X Workdir not found: {args.workdir}", enabled=args.ci)
        return EXIT_USAGE

    spec_path = Path(args.spec) if args.spec else Path(workdir) / "spec.md"
    try:
        spec_text = spec_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        ci_print(f"X Could not read spec {spec_path}: {exc}", enabled=args.ci)
        return EXIT_USAGE
    if not spec_text.strip():
        ci_print(f"X Empty spec: {spec_path}", enabled=args.ci)
        return EXIT_USAGE

    findings, findings_text = None, None
    if args.findings:
        findings, findings_text, err = _load_findings(args.findings)
        if err:
            ci_print(f"X {err}", enabled=args.ci)
            return EXIT_USAGE

    ok, info = gitops.ensure_git_available()
    if not ok:
        ci_print(f"X {info}", enabled=args.ci)
        return EXIT_INFRA

    dev_cmd = resolve_role_cmd("dev", args.dev_cmd, "APLAN_DEV_CMD",
                               DEFAULT_DEV_CMD)
    review_cmd = resolve_role_cmd("review", args.review_cmd, "APLAN_REVIEW_CMD",
                                  DEFAULT_REVIEW_CMD)

    feature = _derive_feature(args, spec_text)
    if not feature:
        ci_print("X Could not derive a feature name; pass --feature", enabled=args.ci)
        return EXIT_USAGE

    out_base = Path(args.out)
    if not out_base.is_absolute():
        out_base = Path(workdir) / out_base
    out_dir = out_base / feature
    out_dir.mkdir(parents=True, exist_ok=True)

    ci_print(f"\n{'#' * 60}\n  ADVERSARIAL PLAN\n"
             f"  Feature: {feature}\n  Max loops: {args.max_loops}\n"
             f"  Findings input: {'yes' if findings is not None else 'no'}\n"
             f"  WRITER: {dev_cmd[:60]}\n  CHALLENGER: {review_cmd[:60]}\n{'#' * 60}",
             enabled=not args.ci)

    state = {}
    code = EXIT_INFRA
    try:
        with ci_mode(enabled=args.ci):
            code = _pipeline(args, dev_cmd, review_cmd, workdir, feature,
                             out_dir, spec_text, findings, findings_text, state,
                             ci=args.ci)
    except KeyboardInterrupt:
        ci_print("\nX Interrupted — restoring workdir (plan branch kept)", enabled=args.ci)
        code = CI_EXIT_INFRASTRUCTURE if args.ci else EXIT_INFRA
    except gitops.GitError as exc:
        ci_print(f"\nX git error: {exc}", enabled=args.ci)
        code = CI_EXIT_INFRASTRUCTURE if args.ci else EXIT_INFRA
    finally:
        _restore(workdir, state)

    # In non-CI mode, fold CI exit codes back to historic 0/1/2/3
    if not args.ci and code >= 10:
        code = EXIT_INFRA

    return code


if __name__ == "__main__":
    sys.exit(main())
