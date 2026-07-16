"""Orchestrator unit tests + end-to-end pipeline tests with scripted CLIs."""
import json
import shlex
import subprocess
import textwrap

import pytest

from conftest import VALID_PLAN, VALID_SPEC
from scripts import adversarial_plan as orch
from scripts.phases import phase_challenge, phase_plan, phase_revise, phase_verify

from adversarial_common import (
    NoProviderAvailable,
    ProviderConfig,
    ProviderEntry,
    RunResult,
)


# --- helpers -------------------------------------------------------------------

def test_ensure_ids_fills_and_deduplicates():
    findings = [{"summary": "a"}, {"id": "P1"}, {"id": "P1"}, {"id": ""}]
    out = orch._ensure_ids(findings)
    ids = [f["id"] for f in out]
    assert len(ids) == len(set(ids))
    assert all(ids)


def test_unresolved_keeps_open_findings():
    findings = [{"id": "P1"}, {"id": "P2"}, {"id": "P3"}]
    results = [
        {"id": "P1", "status": "resolved"},
        {"id": "P2", "status": "rejected"},
        {"id": "P3", "status": "disputed"},
    ]
    assert orch._unresolved(findings, results) == [{"id": "P3"}]


def test_unresolved_ignores_results_without_id():
    findings = [{"id": "P1"}]
    results = [{"status": "resolved"}]  # no id: must not settle anything
    assert orch._unresolved(findings, results) == findings


def test_positive_int_rejects_zero_and_garbage():
    with pytest.raises(Exception):
        orch._positive_int("0")
    with pytest.raises(Exception):
        orch._positive_int("nope")
    assert orch._positive_int("3") == 3


def test_finish_merge_failure_returns_infra_and_records_error(
        tmp_path, monkeypatch):
    def fail_merge(*_args, **_kwargs):
        raise orch.gitops.GitError("squash merge plan/demo/1 -> main failed: conflict")

    monkeypatch.setattr(orch.gitops, "squash_merge", fail_merge)
    args = orch.build_parser().parse_args([])
    state = {"branch": "plan/demo/1", "parent_branch": "main"}

    code = orch._finish(args, str(tmp_path), "demo", tmp_path, state,
                        "APPROVED")

    assert code == orch.EXIT_INFRA
    final = json.loads((tmp_path / "final.json").read_text())
    assert final["merged"] is False
    assert "squash merge" in final["error"]


def test_finish_writes_collected_provider_history(tmp_path, monkeypatch):
    decision = {
        "phase": "plan", "alias": "primary", "quota_state": "OK",
        "fallback": False, "forced": False, "reason": "eligible",
        "raw_snapshot": {"primary": {"used_pct": 10}},
    }
    args = orch.build_parser().parse_args(["--no-merge"])
    state = {
        "branch": "plan/demo/1", "parent_branch": "main",
        "provider_history": [decision],
    }

    code = orch._finish(
        args, str(tmp_path), "demo", tmp_path, state, "APPROVED"
    )

    assert code == orch.EXIT_APPROVED
    payload = json.loads((tmp_path / "final.json").read_text())
    assert payload["provider_history"] == [decision]


def test_no_provider_available_exits_three_with_snapshots(
        tmp_path, monkeypatch, capsys):
    spec = tmp_path / "demo-feature.md"
    spec.write_text(VALID_SPEC)
    monkeypatch.setattr(orch, "load_provider_config", lambda _path: None)
    monkeypatch.setattr(orch.gitops, "ensure_git_available", lambda: (True, ""))
    monkeypatch.setattr(orch, "resolve_role_cmd", lambda *_args: "echo legacy")
    monkeypatch.setattr(orch, "_restore", lambda *_args: None)

    def fail(*_args, **_kwargs):
        raise NoProviderAvailable(
            "writer",
            {"one": {"used_pct": 100}, "two": {"status": 429}},
            {"one": "rate limited", "two": "rate limited"},
        )

    monkeypatch.setattr(orch, "_pipeline", fail)
    code = orch.main([
        "--spec", str(spec), "--workdir", str(tmp_path),
        "--out", str(tmp_path / "out"),
    ])

    assert code == orch.EXIT_REJECTED
    stderr = capsys.readouterr().err
    assert "no provider available" in stderr
    assert "one" in stderr and "used_pct" in stderr
    final = json.loads(
        (tmp_path / "out" / "demo-feature" / "final.json").read_text()
    )
    assert final["verdict"] == "REJECT"
    assert final["reason"] == "no provider available"
    assert final["provider_snapshots"] == {
        "one": {"used_pct": 100}, "two": {"status": 429},
    }


# --- findings loading ---------------------------------------------------------------

def test_load_findings_bare_array(tmp_path):
    f = tmp_path / "findings.json"
    f.write_text('[{"id": "F1", "summary": "x"}]')
    findings, raw, err = orch._load_findings(str(f))
    assert err is None
    assert findings == [{"id": "F1", "summary": "x"}]
    assert raw.strip().startswith("[")


def test_load_findings_object_wrapper(tmp_path):
    f = tmp_path / "findings.json"
    f.write_text('{"findings": [{"id": "F1"}], "verdict": "REJECT"}')
    findings, _raw, err = orch._load_findings(str(f))
    assert err is None
    assert findings == [{"id": "F1"}]


def test_load_findings_bad_json(tmp_path):
    f = tmp_path / "findings.json"
    f.write_text("{broken")
    findings, _raw, err = orch._load_findings(str(f))
    assert findings is None and "not valid JSON" in err


def test_load_findings_wrong_shape(tmp_path):
    f = tmp_path / "findings.json"
    f.write_text('"just a string"')
    findings, _raw, err = orch._load_findings(str(f))
    assert findings is None and err


def test_load_findings_missing_file(tmp_path):
    findings, _raw, err = orch._load_findings(str(tmp_path / "nope.json"))
    assert findings is None and "could not read" in err


# --- CLI parsing ------------------------------------------------------------------

def test_parser_defaults():
    args = orch.build_parser().parse_args([])
    assert args.spec is None
    assert args.findings is None
    assert args.workdir == "."
    assert args.max_loops == 2
    assert args.timeout == 600
    assert args.out == ".adversarial-plan"
    assert args.no_merge is False


def test_parser_exposes_registry_and_force_options():
    args = orch.build_parser().parse_args([
        "--provider-config", "providers.yaml",
        "--force",
        "--force-provider", "writer:primary",
        "--force-provider", "verify:fallback",
    ])
    assert args.provider_config == "providers.yaml"
    assert args.force is True
    assert args.force_provider == [
        ("writer", "primary"), ("verify", "fallback"),
    ]


def test_main_loads_config_and_constructs_one_resolver(tmp_path, monkeypatch):
    seen = {}
    entry = ProviderEntry(alias="primary", command="echo provider")
    config = ProviderConfig(
        roles={
            "writer": (entry,), "challenger": (entry,), "verify": (entry,),
        },
        quota_cmd="quota-check",
    )

    def load(path):
        seen["path"] = path
        return config

    class Resolver:
        def __init__(self, loaded, quota_cmd):
            seen["resolver_args"] = (loaded, quota_cmd)

    monkeypatch.setattr(orch, "load_provider_config", load)
    monkeypatch.setattr(orch, "QuotaResolver", Resolver)
    code = orch.main([
        "--workdir", str(tmp_path / "missing"),
        "--provider-config", str(tmp_path / "providers.yaml"),
    ])

    assert code == orch.EXIT_USAGE
    assert seen["path"] == str(tmp_path / "providers.yaml")
    assert seen["resolver_args"] == (config, config.quota_cmd)


def test_all_phases_route_to_registry_roles(tmp_path, git_repo, monkeypatch):
    calls = []
    (tmp_path / "plan.md").write_text(VALID_PLAN)
    (tmp_path / "spec.md").write_text(VALID_SPEC)
    (git_repo / "plan.md").write_text(VALID_PLAN)

    def run_phase_cmd(**kwargs):
        calls.append(kwargs)
        phase = kwargs["phase_name"]
        if phase == "plan":
            (git_repo / "plan.md").write_text(VALID_PLAN)
            stdout = "written"
        elif phase == "challenge":
            stdout = json.dumps({
                "findings": [], "verdict": "APPROVE", "summary": "clean",
            })
        elif phase == "revise":
            (git_repo / "plan.md").write_text(VALID_PLAN)
            stdout = "revised"
        else:
            stdout = json.dumps({"results": [], "verdict": "APPROVE"})
        return RunResult((stdout, "", 0))

    for module in (phase_plan, phase_challenge, phase_revise, phase_verify):
        monkeypatch.setattr(module, "run_phase_cmd", run_phase_cmd)

    resolver = object()
    assert phase_plan.run_plan(
        VALID_SPEC, None, "", str(git_repo), 10, "demo", resolver
    )["exit_code"] == 0
    assert phase_challenge.run_challenge(
        "", str(tmp_path), 10, resolver
    )["exit_code"] == 0
    assert phase_revise.run_revise(
        [], "", str(git_repo), 10, "demo", 1, resolver
    )["exit_code"] == 0
    assert phase_verify.run_verify(
        [], "", str(tmp_path), 10, resolver
    )["exit_code"] == 0

    assert [(call["phase_name"], call["role"]) for call in calls] == [
        ("plan", "writer"), ("challenge", "challenger"),
        ("revise", "writer"), ("verify", "verify"),
    ]


def test_all_phases_project_scope_legacy_and_explicit_commands(
        tmp_path, git_repo, monkeypatch):
    calls = []
    write_count = 0
    _stage = tmp_path / "review"
    _stage.mkdir()
    (_stage / "plan.md").write_text(VALID_PLAN)
    (_stage / "spec.md").write_text(VALID_SPEC)

    def run_phase_cmd(**kwargs):
        nonlocal write_count
        calls.append(kwargs)
        phase = kwargs["phase_name"]
        if phase in {"plan", "revise"}:
            write_count += 1
            (git_repo / "plan.md").write_text(
                VALID_PLAN + f"\n<!-- invocation {write_count} -->\n"
            )
            stdout = "written"
        elif phase == "challenge":
            stdout = json.dumps({
                "findings": [], "verdict": "APPROVE", "summary": "clean",
            })
        else:
            stdout = json.dumps({"results": [], "verdict": "APPROVE"})
        return RunResult((stdout, "", 0))

    for module in (phase_plan, phase_challenge, phase_revise, phase_verify):
        monkeypatch.setattr(module, "run_phase_cmd", run_phase_cmd)

    def invoke_all(resolver=None, dev_explicit=None, review_explicit=None):
        assert phase_plan.run_plan(
            VALID_SPEC, None, "codex exec", str(git_repo), 10, "demo",
            resolver=resolver, explicit_cmd=dev_explicit,
        )["exit_code"] == 0
        assert phase_challenge.run_challenge(
            "claude --print", str(_stage), 10, resolver=resolver,
            explicit_cmd=review_explicit,
        )["exit_code"] == 0
        assert phase_revise.run_revise(
            [], "codex exec", str(git_repo), 10, "demo", 1,
            resolver=resolver, explicit_cmd=dev_explicit,
        )["exit_code"] == 0
        assert phase_verify.run_verify(
            [], "claude --print", str(_stage), 10, resolver=resolver,
            explicit_cmd=review_explicit,
        )["exit_code"] == 0

    invoke_all()
    invoke_all(
        resolver=object(), dev_explicit="codex exec",
        review_explicit="claude --print",
    )

    codex_workdir = f"-C {shlex.quote(str(git_repo))}"
    assert codex_workdir in calls[0]["cmd"]
    assert "--allowedTools Read,Bash" in calls[1]["cmd"]
    assert codex_workdir in calls[2]["cmd"]
    assert "--allowedTools Read,Bash" in calls[3]["cmd"]

    assert calls[4]["explicit_cmd"].endswith(codex_workdir)
    assert "--allowedTools Read,Bash" in calls[5]["explicit_cmd"]
    assert calls[6]["explicit_cmd"].endswith(codex_workdir)
    assert "--allowedTools Read,Bash" in calls[7]["explicit_cmd"]
    for call in calls[4:]:
        assert "cmd" not in call


def test_explicit_commands_bypass_only_their_role_groups():
    args = orch.build_parser().parse_args([
        "--dev-cmd", "echo writer", "--review-cmd", "echo reviewer",
        "--force-provider", "writer:w2",
        "--force-provider", "challenger:c2",
        "--force-provider", "verify:v2",
    ])
    args._force_providers = orch._force_provider_map(args.force_provider)

    assert orch._provider_call_args(args, "writer", args.dev_cmd) == {
        "explicit_cmd": "echo writer", "force": False, "force_provider": "w2",
    }
    assert orch._provider_call_args(args, "challenger", args.review_cmd) == {
        "explicit_cmd": "echo reviewer", "force": False, "force_provider": "c2",
    }
    assert orch._provider_call_args(args, "verify", args.review_cmd) == {
        "explicit_cmd": "echo reviewer", "force": False, "force_provider": "v2",
    }


def test_explicit_writer_command_skips_resolver(git_repo, tmp_path):
    class ResolverMustNotRun:
        def resolve(self, *_args, **_kwargs):
            raise AssertionError("quota resolver was consulted")

    writer = tmp_path / "writer.py"
    writer.write_text(WRITER_SCRIPT.format(plan=VALID_PLAN))
    result = phase_plan.run_plan(
        VALID_SPEC, None, "unused", str(git_repo), 30, "demo",
        ResolverMustNotRun(), explicit_cmd=f"python3 {writer}",
    )

    assert result["exit_code"] == 0
    assert result["provider_history"] == []


def test_delegated_pipeline_resolves_registry_writer_without_dev_cmd(
        tmp_path, monkeypatch):
    calls = []
    resolver = object()
    args = orch.build_parser().parse_args(["--delegated"])
    args._provider_resolver = resolver
    args._force_providers = {}

    def run_phase_cmd(**kwargs):
        calls.append(kwargs)
        phase = kwargs["phase_name"]
        stdout = (
            json.dumps({"tasks": ["task one", "task two"]})
            if phase == "decomposition" else f"{phase} output"
        )
        decision = {
            "phase": phase, "alias": "registry-writer", "quota_state": "OK",
            "fallback": False, "forced": False, "reason": "eligible",
        }
        return RunResult(
            (stdout, "", 0), {"provider_decision": decision}
        )

    monkeypatch.setattr(orch, "run_phase_cmd", run_phase_cmd)
    result = orch._run_delegated_pipeline(
        args, VALID_SPEC, "", str(tmp_path), "demo", tmp_path,
        {"level": "high", "score": 9, "recommended_agents": 2},
        ledger=None,
    )

    assert result["status"] == "synthesized"
    assert [call["phase_name"] for call in calls] == [
        "decomposition", "worker", "worker", "synthesis",
    ]
    assert all(call["role"] == "writer" for call in calls)
    assert all(call["resolver"] is resolver for call in calls)
    assert all(call["explicit_cmd"] is None for call in calls)
    assert all("cmd" not in call for call in calls)
    assert [item["phase"] for item in result["provider_history"]] == [
        "decomposition", "worker", "worker", "synthesis",
    ]


def test_main_delegated_provider_config_without_dev_cmd_uses_registry(
        tmp_path, monkeypatch):
    spec = tmp_path / "demo-feature.md"
    spec.write_text(VALID_SPEC)
    entry = ProviderEntry(alias="registry-writer", command="echo provider")
    config = ProviderConfig(
        roles={
            "writer": (entry,), "challenger": (entry,), "verify": (entry,),
        },
        quota_cmd="quota-check",
    )
    resolver = object()
    calls = []

    monkeypatch.setattr(orch, "load_provider_config", lambda _path: config)
    monkeypatch.setattr(
        orch, "QuotaResolver", lambda loaded, quota_cmd: resolver
    )
    monkeypatch.setattr(orch.gitops, "ensure_git_available", lambda: (True, ""))
    monkeypatch.setattr(orch, "_restore", lambda *_args: None)

    def run_phase_cmd(**kwargs):
        calls.append(kwargs)
        phase = kwargs["phase_name"]
        stdout = (
            json.dumps({"tasks": ["task one", "task two"]})
            if phase == "decomposition" else f"{phase} output"
        )
        return RunResult((stdout, "", 0))

    def pipeline(args, dev_cmd, _review_cmd, workdir, feature, out_dir,
                 spec_text, *_args, **_kwargs):
        assert args.delegated is True
        assert dev_cmd == ""
        result = orch._run_delegated_pipeline(
            args, spec_text, dev_cmd, workdir, feature, out_dir,
            {"level": "high", "score": 9, "recommended_agents": 2},
            ledger=None,
        )
        assert result["status"] == "synthesized"
        return orch.EXIT_APPROVED

    monkeypatch.setattr(orch, "run_phase_cmd", run_phase_cmd)
    monkeypatch.setattr(orch, "_pipeline", pipeline)

    code = orch.main([
        "--spec", str(spec), "--workdir", str(tmp_path),
        "--out", str(tmp_path / "out"), "--delegated",
        "--provider-config", str(tmp_path / "providers.yaml"),
    ])

    assert code == orch.EXIT_APPROVED
    assert [call["phase_name"] for call in calls] == [
        "decomposition", "worker", "worker", "synthesis",
    ]
    assert all(call["resolver"] is resolver for call in calls)
    assert all(call["explicit_cmd"] is None for call in calls)
    assert all("cmd" not in call for call in calls)


def test_derive_feature_prefers_flag_then_spec_filename():
    args = orch.build_parser().parse_args(
        ["--feature", "My Feature!", "--spec", "/x/rate_limiter.md"])
    assert orch._derive_feature(args, "ignored") == "my-feature"
    args = orch.build_parser().parse_args(["--spec", "/x/rate_limiter.md"])
    assert orch._derive_feature(args, "ignored") == "rate-limiter"


def test_derive_feature_generic_spec_stem_uses_frontmatter_name():
    args = orch.build_parser().parse_args(["--spec", "/x/spec.md"])
    assert orch._derive_feature(args, VALID_SPEC) == "demo-feature"


def test_derive_feature_falls_back_to_first_heading():
    args = orch.build_parser().parse_args([])
    assert orch._derive_feature(args, "\n# Add rate limiting\nmore") == \
        "add-rate-limiting"


def test_main_usage_errors(tmp_path, capsys):
    assert orch.main(["--spec", str(tmp_path / "missing.md")]) == orch.EXIT_USAGE
    assert orch.main(["--workdir", str(tmp_path / "nope"),
                      "--spec", str(tmp_path / "missing.md")]) == orch.EXIT_USAGE
    empty = tmp_path / "empty.md"
    empty.write_text("  \n")
    assert orch.main(["--spec", str(empty)]) == orch.EXIT_USAGE
    # missing default <workdir>/spec.md
    assert orch.main(["--workdir", str(tmp_path)]) == orch.EXIT_USAGE


def test_main_bad_findings_is_usage_error(tmp_path):
    spec = tmp_path / "demo-feature.md"
    spec.write_text(VALID_SPEC)
    bad = tmp_path / "findings.json"
    bad.write_text("{broken")
    assert orch.main(["--spec", str(spec), "--findings", str(bad),
                      "--workdir", str(tmp_path)]) == orch.EXIT_USAGE


# --- end-to-end with scripted fake CLIs ---------------------------------------------

WRITER_SCRIPT = textwrap.dedent("""\
    import pathlib, sys
    sys.stdin.read()  # consume persona + prompt
    pathlib.Path("plan.md").write_text('''{plan}''')
    print("plan.md written")
""")

APPROVE_REVIEWER = textwrap.dedent("""\
    import json, sys
    sys.stdin.read()
    print(json.dumps({"findings": [], "verdict": "APPROVE", "summary": "clean"}))
""")

# Challenges once, then never accepts the revision.
REJECT_REVIEWER = textwrap.dedent("""\
    import json, sys
    prompt = sys.stdin.read()
    if "For each finding" in prompt:
        print(json.dumps({"results": [{"id": "P1", "status": "disputed"}],
                          "verdict": "REJECT"}))
    else:
        print(json.dumps({
            "findings": [{"id": "P1", "severity": "major", "step": "P1",
                          "summary": "Tests vague", "evidence": "P1"}],
            "verdict": "REQUEST_CHANGES", "summary": "1 major"}))
""")


def _scripted_pipeline(tmp_path, reviewer_source, extra_args=()):
    workdir = tmp_path / "project"
    workdir.mkdir()
    writer = tmp_path / "writer.py"
    writer.write_text(WRITER_SCRIPT.format(plan=VALID_PLAN))
    reviewer = tmp_path / "reviewer.py"
    reviewer.write_text(reviewer_source)
    spec = tmp_path / "demo-feature.md"
    spec.write_text(VALID_SPEC)
    argv = [
        "--spec", str(spec),
        "--workdir", str(workdir),
        "--dev-cmd", f"python3 {writer}",
        "--review-cmd", f"python3 {reviewer}",
        "--max-loops", "1",
        "--timeout", "60",
        *extra_args,
    ]
    return workdir, orch.main(argv)


def _git(workdir, *args):
    return subprocess.run(["git", *args], cwd=workdir,
                          capture_output=True, text=True, check=True).stdout


def test_pipeline_approved_squash_merges(tmp_path):
    workdir, code = _scripted_pipeline(tmp_path, APPROVE_REVIEWER)
    assert code == orch.EXIT_APPROVED
    assert _git(workdir, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert (workdir / "plan.md").is_file()
    assert (workdir / "spec.md").is_file()  # staged input merged with the plan
    assert "squash: demo-feature — plan approved" in _git(workdir, "log", "--format=%s")
    # plan branch was deleted after the squash-merge
    assert "plan/demo-feature/1" not in _git(workdir, "branch", "--list", "plan/*")
    final = json.loads(
        (workdir / ".adversarial-plan" / "demo-feature" / "final.json").read_text())
    assert final["verdict"] == "APPROVED"
    assert final["merged"] is True


def test_pipeline_rejected_leaves_marker(tmp_path):
    workdir, code = _scripted_pipeline(tmp_path, REJECT_REVIEWER)
    assert code == orch.EXIT_REJECTED
    # back on the parent branch, plan branch kept with a [REJECTED] marker
    assert _git(workdir, "symbolic-ref", "--short", "HEAD").strip() == "main"
    branches = _git(workdir, "branch", "--list", "plan/*")
    assert "plan/demo-feature/1" in branches
    log = _git(workdir, "log", "plan/demo-feature/1", "--format=%s")
    assert "[REJECTED]" in log
    assert "revise: demo-feature — round 1" in log
    final = json.loads(
        (workdir / ".adversarial-plan" / "demo-feature" / "final.json").read_text())
    assert final["verdict"] == "REJECT"
    assert final["merged"] is False


def test_pipeline_no_merge_keeps_branch(tmp_path):
    workdir, code = _scripted_pipeline(tmp_path, APPROVE_REVIEWER,
                                       extra_args=("--no-merge",))
    assert code == orch.EXIT_APPROVED
    assert "plan/demo-feature/1" in _git(workdir, "branch", "--list", "plan/*")
    final = json.loads(
        (workdir / ".adversarial-plan" / "demo-feature" / "final.json").read_text())
    assert final["merged"] is False


def test_pipeline_with_findings_stages_findings_json(tmp_path):
    findings_file = tmp_path / "review-findings.json"
    findings_file.write_text(json.dumps(
        {"findings": [{"id": "F1", "summary": "race in demo"}]}))
    workdir, code = _scripted_pipeline(
        tmp_path, APPROVE_REVIEWER,
        extra_args=("--findings", str(findings_file)))
    assert code == orch.EXIT_APPROVED
    # findings.json was staged in the workdir and merged with the plan
    staged = json.loads((workdir / "findings.json").read_text())
    assert staged["findings"][0]["id"] == "F1"
    artifacts = workdir / ".adversarial-plan" / "demo-feature"
    assert (artifacts / "00_findings.json").is_file()


def test_pipeline_restores_dirty_workdir(tmp_path):
    # A dirty file present before the run must be stashed and restored.
    workdir = tmp_path / "project"
    workdir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                   cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "Initial commit", "-q"],
                   cwd=workdir, check=True)
    (workdir / "wip.txt").write_text("uncommitted work")

    writer = tmp_path / "writer.py"
    writer.write_text(WRITER_SCRIPT.format(plan=VALID_PLAN))
    reviewer = tmp_path / "reviewer.py"
    reviewer.write_text(APPROVE_REVIEWER)
    spec = tmp_path / "demo-feature.md"
    spec.write_text(VALID_SPEC)
    code = orch.main([
        "--spec", str(spec), "--workdir", str(workdir),
        "--dev-cmd", f"python3 {writer}",
        "--review-cmd", f"python3 {reviewer}",
        "--max-loops", "1", "--timeout", "60",
    ])
    assert code == orch.EXIT_APPROVED
    assert (workdir / "wip.txt").read_text() == "uncommitted work"
