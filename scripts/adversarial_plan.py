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

Exit codes:
  0 APPROVED — plan squash-merged into the parent branch (or left on its
               branch with --no-merge)
  1 infrastructure failure (phase crash, git error, interrupt)
  2 usage error (bad flags, missing/empty spec, unparseable findings)
  3 REJECT   — findings unresolved after max-loops

The machine-readable contract is <out>/<feature>/final.json; the produced
``plan.md`` is directly consumable by adversarial-code-loop v4.
"""
import argparse
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
from scripts.phases import (extract_frontmatter, phase_challenge, phase_plan,
                            phase_revise, phase_verify)

EXIT_APPROVED = 0
EXIT_INFRA = 1
EXIT_USAGE = 2
EXIT_REJECTED = 3

DEFAULT_DEV_CMD = "pi --provider zai --model glm-5.2"
DEFAULT_REVIEW_CMD = "pi --provider deepseek --model deepseek-v4-pro"

# Verifier statuses that no longer block approval: "resolved" (fixed) and
# "rejected" (the verifier showed the original finding was wrong).
_SETTLED_STATUSES = {"resolved", "rejected"}


# --- small helpers -------------------------------------------------------------

def _banner(title):
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
    print(f"X {label} failed: {result.get('error', 'unknown error')}")
    try:
        _log_retrospective(label, result, state.get("feature", "unknown"),
                           state.get("branch", ""), out_dir)
    except Exception as exc:
        print(f"! could not write retrospective log: {exc}")
    return EXIT_INFRA


def _restore(workdir, state):
    """Best-effort cleanup on every exit path: back to parent, pop stash."""
    parent = state.get("parent_branch", "")
    try:
        if parent and gitops.get_current_branch(workdir) != parent:
            gitops.checkout(workdir, parent)
    except gitops.GitError as exc:
        # Never unstash onto the wrong branch.
        print(f"! could not restore branch {parent!r}: {exc}")
        return
    stash_id = state.get("stash_id", "")
    if stash_id:
        try:
            gitops.unstash(workdir, stash_id)
            state["stash_id"] = ""
        except gitops.GitError as exc:
            print(f"! could not pop {stash_id}: {exc}")


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
            costs=None, complexity=None):
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
        print(f"X git finalize failed ({verdict}): {exc}")
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
    jsonio.write_final_json(out_dir, verdict, **final_kwargs)
    print(f"\n{verdict}" + (f" — {reason}" if reason else ""))
    if error:
        return EXIT_INFRA
    return EXIT_APPROVED if verdict == "APPROVED" else EXIT_REJECTED


# --- pipeline -------------------------------------------------------------------

def _pipeline(args, dev_cmd, review_cmd, workdir, feature, out_dir,
              spec_text, findings, findings_text, state):
    """Run the full workflow. Returns the process exit code."""
    # --- pre-flight: context check (R3) ---
    ctx = gates.check_context("spec", spec_text)
    if not ctx["ok"]:
        print(f"X Spec context check failed: {ctx['reason']}")
        return EXIT_USAGE

    # --- cost ledger + complexity (R4) ---
    ledger = costs.CostLedger()
    complexity = gates.estimate_complexity(spec_text)

    # PHASE 0 — GIT SETUP
    setup = _setup_git(workdir, feature, state)
    state.update(setup)
    if setup["exit_code"] != 0:
        print(f"X git setup failed: {setup.get('error', 'unknown error')}")
        return EXIT_INFRA
    state["feature"] = feature
    _banner(f"PLAN BRANCH  {setup['branch']}  (from {setup['parent_branch']})")
    jsonio.save_artifact(out_dir, "00_spec.txt", spec_text)
    if findings_text is not None:
        jsonio.save_artifact(out_dir, "00_findings.json", findings_text)
    _stage_inputs(workdir, spec_text, findings_text)

    # --- tag user-provided findings (R8) ---
    if findings is not None:
        for finding in findings:
            if isinstance(finding, dict):
                finding.setdefault("origin", "user")

    # PHASE 1 — PLAN
    _banner("PLAN  (PLAN-WRITER)")
    plan = phase_plan.run_plan(spec_text, findings, dev_cmd, workdir,
                               args.timeout, feature,
                               ledger=ledger, show_costs=args.show_costs,
                               max_retries=args.retries,
                               max_input_chars=args.max_input_chars,
                               max_output_chars=args.max_output_chars)
    _write_json(out_dir, "01_plan.json", plan)
    if plan["exit_code"] != 0:
        if plan.get("exit_code") == EXIT_USAGE:
            print(f"X plan validation failed: {plan.get('error', 'invalid plan')}")
            return EXIT_USAGE
        return _phase_failed("plan", plan, state, out_dir)
    print(f"  OK commit {plan.get('commit_sha', '')[:12]}")

    # PHASE 2 — CHALLENGE
    _banner("CHALLENGE  (PLAN-CHALLENGER)")
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
    print(f"  OK {len(challenge_findings)} findings — verdict {verdict}")

    # PHASES 3/4 — REVISE / VERIFY loop. An empty findings list only approves
    # when the challenger's verdict is also APPROVE.
    approved = not challenge_findings and verdict == "APPROVE"
    loops_run = 0
    for n in range(1, args.max_loops + 1):
        if approved:
            break
        loops_run = n

        _banner(f"REVISE  (round {n}/{args.max_loops})")
        revise = phase_revise.run_revise(challenge_findings, dev_cmd, workdir,
                                         args.timeout, feature, n,
                                         ledger=ledger, show_costs=args.show_costs,
                                         max_retries=args.retries,
                                         max_input_chars=args.max_input_chars,
                                         max_output_chars=args.max_output_chars)
        _write_json(out_dir, f"03_revise_{n}.json", revise)
        if revise["exit_code"] != 0:
            return _phase_failed(f"revise_{n}", revise, state, out_dir)

        _banner(f"VERIFY  (round {n}/{args.max_loops})")
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
        print(f"  Verdict {verify.get('verdict')} — "
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
                       costs=cost_summary, complexity=complexity)
    return _finish(args, workdir, feature, out_dir, state, "REJECT",
                   reason=f"findings unresolved after {args.max_loops} loops",
                   loops=loops_run,
                   costs=cost_summary, complexity=complexity)


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
        print(f"X Workdir not found: {args.workdir}")
        return EXIT_USAGE

    spec_path = Path(args.spec) if args.spec else Path(workdir) / "spec.md"
    try:
        spec_text = spec_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"X Could not read spec {spec_path}: {exc}")
        return EXIT_USAGE
    if not spec_text.strip():
        print(f"X Empty spec: {spec_path}")
        return EXIT_USAGE

    findings, findings_text = None, None
    if args.findings:
        findings, findings_text, err = _load_findings(args.findings)
        if err:
            print(f"X {err}")
            return EXIT_USAGE

    ok, info = gitops.ensure_git_available()
    if not ok:
        print(f"X {info}")
        return EXIT_INFRA

    dev_cmd = resolve_role_cmd("dev", args.dev_cmd, "APLAN_DEV_CMD",
                               DEFAULT_DEV_CMD)
    review_cmd = resolve_role_cmd("review", args.review_cmd, "APLAN_REVIEW_CMD",
                                  DEFAULT_REVIEW_CMD)

    feature = _derive_feature(args, spec_text)
    if not feature:
        print("X Could not derive a feature name; pass --feature")
        return EXIT_USAGE

    out_base = Path(args.out)
    if not out_base.is_absolute():
        out_base = Path(workdir) / out_base
    out_dir = out_base / feature
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'#' * 60}\n  ADVERSARIAL PLAN\n"
          f"  Feature: {feature}\n  Max loops: {args.max_loops}\n"
          f"  Findings input: {'yes' if findings is not None else 'no'}\n"
          f"  WRITER: {dev_cmd[:60]}\n  CHALLENGER: {review_cmd[:60]}\n{'#' * 60}")

    state = {}
    try:
        code = _pipeline(args, dev_cmd, review_cmd, workdir, feature,
                         out_dir, spec_text, findings, findings_text, state)
    except KeyboardInterrupt:
        print("\nX Interrupted — restoring workdir (plan branch kept)")
        code = EXIT_INFRA
    except gitops.GitError as exc:
        print(f"\nX git error: {exc}")
        code = EXIT_INFRA
    finally:
        _restore(workdir, state)

    return code


if __name__ == "__main__":
    sys.exit(main())
