---
spec: "adversarial-skills-refacto-8-items"
version: "1.1"
author: "adversarial-plan"
based-on: "adversarial-spec"
findings-input: true
---

# Implementation Plan

Cross-skill refactoring across five repos under `~/.hermes/skills/`:
`adversarial-common`, `adversarial-code-loop`, `adversarial-code-review`,
`adversarial-spec`, `adversarial-plan`. Paths below are absolute so a dev loop
can resolve them unambiguously regardless of CWD.

Note on the spec: Items 5 and 6 reference
`~/.hermes/skills/adversarial-plan/scripts/phases/__init__.py`, which does **not**
exist in the adversarial-plan repo today (the repo currently tracks only
`spec.md`). The dedup therefore lands for the code-loop and spec callers; the
plan repo is covered by a guard step (P9) so that if/when it gains that module it
imports the shared helpers instead of duplicating them.

## Steps

### P1: Delete 4 unused persona files (Item 1)
- **Files:** /home/chpo/.hermes/skills/adversarial-common/personas/architect.md, /home/chpo/.hermes/skills/adversarial-common/personas/inspector.md, /home/chpo/.hermes/skills/adversarial-common/personas/synthesis.md, /home/chpo/.hermes/skills/adversarial-common/personas/cross_review.md
- **Description:** Remove the four dead persona files. adversarial_review.py uses architect, inspector, cross_review, and synthesis only as role names; no Python code references these file paths.
- **Dependencies:** []
- **Tests:** Assert persona_path("architect") raises FileNotFoundError, and grep all adversarial-skill Python files for references to the four deleted filenames.
- **Risks:** None — the dedicated persona files are unused.

### P2: Add shared structured-text helpers to jsonio.py + require PyYAML (Item 5, Item 6, Item 8)
- **Files:** `/home/chpo/.hermes/skills/adversarial-common/adversarial_common/jsonio.py`
- **Description:** Add four new public/semi-public functions plus a module-level
  regex, making PyYAML a hard dependency (it is already installed in this env):
  - `_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)`
  - `extract_frontmatter(text) -> str | None` — return the raw YAML block, or None (strip a leading BOM before matching, same as the spec version).
  - `parse_frontmatter(fm_text) -> (dict | None, error | None)` — always `yaml.safe_load`; drop the fragile `^([A-Za-z_][\w-]*)\s*:\s*(.*)$` regex fallback entirely. Import `yaml` at module top (no `try/except ImportError`). On `yaml.YAMLError` return `(None, "invalid YAML: ...")`; if the result is not a dict return `(None, "frontmatter is not a YAML mapping")`.
  - `parse_json_output(text) -> dict | list | None` — 3-strategy extraction matching the spec/code-loop version: (1) strip ```` ``` ```` fences, (2) `json.loads` whole, (3) extract outermost `{..}` then `[..]`. Return None on failure or empty/non-str input.
  Keep the existing `strip_json_wrapper` / `save_artifact` / `resume_artifact` / `write_final_json` untouched (callers depend on them).
- **Dependencies:** []
- **Tests:** Add `/home/chpo/.hermes/skills/adversarial-common/adversarial_common/tests/test_jsonio_shared.py` with: (a) `parse_json_output('{"x":1}') == {'x':1}`; (b) `parse_json_output('```json\n[1,2]\n```') == [1,2]`; (c) `parse_frontmatter('name: x\nlist:\n  - a\n  - b')[0] == {'name':'x','list':['a','b']}` (proves the regex fallback removal — lists now parse); (d) `extract_frontmatter('---\nname: x\n---\nbody') == 'name: x'`; (e) `extract_frontmatter('no fm') is None`. Run with `python3 -m pytest` from the adversarial-common repo root.
- **Risks:** Low. Making PyYAML hard-required is safe here (verified present). If a future stripped-down environment lacks PyYAML, add it to requirements rather than resurrecting the regex.

### P3: Remove `fail_phase` alias and update all callers (Item 3)
- **Files:** `/home/chpo/.hermes/skills/adversarial-common/adversarial_common/runner.py` (delete line 87 `fail_phase = _fail_phase`), `/home/chpo/.hermes/skills/adversarial-code-loop/scripts/adversarial_loop_v3.py` (line 21: change `from adversarial_common.runner import run_cli, fail_phase` → `from adversarial_common import runner` plus `from adversarial_common.runner import run_cli`; rename the 5 call sites `fail_phase(...)` → `runner._fail_phase(...)`), `/home/chpo/.hermes/skills/adversarial-code-review/scripts/adversarial_review.py` (line 271 `runner.fail_phase(...)` → `runner._fail_phase(...)`)
- **Description:** Delete the public alias in `runner.py`. The function stays
  private (`_fail_phase`). Update the two real callers so nothing imports the
  removed `fail_phase` symbol. Both callers now use the fully-qualified
  `runner._fail_phase(...)`: the v3 loop drops `fail_phase` from its
  `from adversarial_common.runner import ...` line, adds
  `from adversarial_common import runner`, and renames its 5 `fail_phase(...)`
  call sites to `runner._fail_phase(...)` (`run_cli` call sites are untouched —
  still imported directly); the review script changes `runner.fail_phase(...)` →
  `runner._fail_phase(...)`. This choice satisfies spec Item 3's verification
  grep `grep -r '[.]fail_phase\|import.*fail_phase'` (no hits): the import line
  no longer names `fail_phase`, and `runner._fail_phase` has an underscore
  between the dot and the name, so it does not match `[.]fail_phase`. (Earlier
  draft proposed `import _fail_phase as fail_phase` — rejected because that line
  matches `import.*fail_phase` and violates the spec criterion.)
- **Dependencies:** []
- **Tests:** `grep -rn '[.]fail_phase\|import.*fail_phase' /home/chpo/.hermes/skills/adversarial-*/ --include='*.py' | grep -v __pycache__` returns no hits (this is exactly spec Item 3's verification). `python3 -c "from adversarial_common.runner import _fail_phase; print(callable(_fail_phase))"` prints `True`; `python3 -c "from adversarial_common.runner import fail_phase"` raises ImportError. Smoke-import the v3 loop and review modules to confirm no NameError.
- **Risks:** Low. Any caller not found by the grep would NameError at runtime; the grep above is the guard.

### P4: Drop `dev_cmd` / `review_cmd` from `run_arbiter` signature (Item 2)
- **Files:** `/home/chpo/.hermes/skills/adversarial-code-loop/scripts/phases/phase_arbiter.py` (function `run_arbiter`, currently at line 55), `/home/chpo/.hermes/skills/adversarial-code-loop/scripts/adversarial_loop.py` (call site at line 452), `/home/chpo/.hermes/skills/adversarial-code-loop/scripts/adversarial_loop_v4.py` (call site at line 437), `/home/chpo/.hermes/skills/adversarial-code-loop/scripts/phases/test_phases.py` (helper `_run_arbiter` at line 155)
- **Description:** Change `run_arbiter(findings, dev_cmd, review_cmd, arbiter_cmd, providers)` to `run_arbiter(findings, arbiter_cmd, providers)`. Update the docstring signature line. Remove the two unused positional args from every caller. Both `adversarial_loop.py` and `adversarial_loop_v4.py` currently pass the loop's dev/review commands positionally — drop those two args, keeping `arbiter_cmd` and `providers`. Update the test helper to call the new 3-arg form.
- **Dependencies:** []
- **Tests:** `python3 -c "import sys; sys.path.insert(0,'/home/chpo/.hermes/skills/adversarial-code-loop/scripts'); from phases.phase_arbiter import run_arbiter; import inspect; p=inspect.signature(run_arbiter).parameters; assert set(p)=={'findings','arbiter_cmd','providers'}, p; print('ok')"`. Run `python3 -m pytest phases/test_phases.py -k arbiter` from the code-loop scripts dir.
- **Risks:** Low. A missed caller will TypeError on invocation (too many positional args) — the loop's arbiter path only fires after max-loops, so the test must exercise `_run_arbiter` to catch it before runtime.

### P5: Consolidate branch extraction into `gitops.get_current_branch` (Item 4)
- **Files:** `/home/chpo/.hermes/skills/adversarial-code-loop/scripts/phases/phase_review.py` (inline `subprocess.run(["git","symbolic-ref","--short","HEAD"], ...)` inside `_build_prompt`), `/home/chpo/.hermes/skills/adversarial-code-loop/scripts/phases/phase_verify.py` (inline `subprocess.run(["git","symbolic-ref","--short","HEAD"], ...)` near top of `run_verify`)
- **Description:** Replace both inline `subprocess.run(...)` blocks with
  `from adversarial_common import gitops` then `branch = gitops.get_current_branch(workdir)`,
  wrapped in `try/except gitops.GitError: branch = "(unknown)"`. In
  `phase_review.py` this passes `cwd=workdir` correctly (already did). In
  `phase_verify.py` the current inline call passes **no cwd** (runs against the
  process CWD) — consolidating fixes this latent bug by routing through the
  `workdir` argument the verifier already receives via prompt context. Remove the
  now-unused `import subprocess` from both modules.
- **Dependencies:** []
- **Tests:** Existing `phases/test_phases.py` review/verify tests still pass. Add `test_branch_delegation.py`: monkeypatch `gitops.get_current_branch` to (a) return `"stub-branch"` and (b) record its received argument, then assert `_build_prompt(diff, workdir)` contains `` `stub-branch` `` **and** that the recorded call received `workdir` (not `None` or the process CWD) — this is the observable check that the latent `phase_verify.py` no-`cwd` bug is fixed. Mirror the same assertion against `run_verify`'s branch lookup (route through the monkeypatched `get_current_branch(workdir)`). `python3 -c "from adversarial_common import gitops; print(gitops.get_current_branch('.'))"` returns a branch name.
- **Risks:** Low. `gitops.get_current_branch` raises `GitError` on detached HEAD — same semantics as the inline `subprocess` failure path, now caught explicitly.

### P6: Route code-loop `phase_verify._try_parse_json` through `jsonio.parse_json_output` (Item 5, code-loop caller)
- **Files:** `/home/chpo/.hermes/skills/adversarial-code-loop/scripts/phases/phase_verify.py`
- **Description:** Delete the local `_try_parse_json` function (and its dead
  `import ast`), import `from adversarial_common import jsonio`, and replace the
  one call site `payload = _try_parse_json(stdout)` with
  `payload = jsonio.parse_json_output(stdout)`. Keep a thin local alias
  `_try_parse_json = jsonio.parse_json_output` only if any other code in the
  module references it (it does not — single call site), so drop the alias too.
- **Dependencies:** [P2, P5]
- **Tests:** `python3 -c "from adversarial_common.jsonio import parse_json_output; assert parse_json_output('{\"x\":1}') == {'x':1}"` (P2 already guarantees this). Re-run `phases/test_phases.py -k verify` to confirm the verify retry path still extracts JSON.
- **Risks:** Low. The two implementations are equivalent (same 3 strategies); `parse_json_output` returns None on failure, matching the old `_try_parse_json` contract used by `_validate`.

### P7: Route adversarial-spec `phases/__init__.py` through shared jsonio helpers (Items 5, 6 — spec caller)
- **Files:** `/home/chpo/.hermes/skills/adversarial-spec/scripts/phases/__init__.py`
- **Description:** Remove the local `try_parse_json`, `extract_frontmatter`,
  `_parse_frontmatter`, and `_FRONTMATTER_RE` definitions. Add
  `from adversarial_common import jsonio` (alongside the existing
  `from adversarial_common import persona_path, runner` import block; the
  sys.path fallback already covers it). Re-export for backward compatibility:
  `try_parse_json = jsonio.parse_json_output`,
  `extract_frontmatter = jsonio.extract_frontmatter`. Update
  `validate_spec_file` to call `jsonio.parse_frontmatter(fm_text)` instead of the
  local `_parse_frontmatter`. Keep the public `__all__` entries
  (`try_parse_json`, `extract_frontmatter`, `validate_spec_file`) so external
  importers are unaffected. Behavior change from Item 8 (required PyYAML): list
  frontmatter values now parse correctly where the old regex fallback silently
  mangled them.
- **Dependencies:** [P2]
- **Tests:** `python3 -c "import sys; sys.path.insert(0,'/home/chpo/.hermes/skills/adversarial-spec/scripts'); from phases import try_parse_json, extract_frontmatter, validate_spec_file; assert try_parse_json('{\"a\":1}')=={'a':1}; assert extract_frontmatter('---\nname: x\n---\n')=='name: x'"`. Run the spec skill's existing `phases` test suite. Construct a temp dir with a `spec.md` whose frontmatter contains a YAML list and assert `validate_spec_file` succeeds (would have passed before only by luck of the regex).
- **Risks:** Low. If any other module in adversarial-spec imported the private `_parse_frontmatter` or `_FRONTMATTER_RE` directly, it breaks — guard with `grep -rn '_parse_frontmatter\|_FRONTMATTER_RE' /home/chpo/.hermes/skills/adversarial-spec/ --include='*.py'` before editing; expect only the one file.

### P8: Restore `--a-cmd` / `--b-cmd` / `--synth-cmd` flags in adversarial_review.py (Item 7)
- **Files:** `/home/chpo/.hermes/skills/adversarial-code-review/scripts/adversarial_review.py`
- **Description:** In `parse_args` (line 423) add three optional flags, each
  defaulting to `None`:
  `p.add_argument("--a-cmd", default=None, help="Architect review command (default: --review-cmd)")`,
  `p.add_argument("--b-cmd", default=None, help="Inspector/cross review command (default: --review-cmd)")`,
  `p.add_argument("--synth-cmd", default=None, help="Synthesis command (default: --review-cmd)")`.
  In `main()` after `args.review_cmd = providers.resolve_role_cmd("review", ...)`,
  resolve each override through the same `providers.resolve_role_cmd` with its
  own env var and the resolved `args.review_cmd` as default:
  `args.a_cmd = providers.resolve_role_cmd("review", args.a_cmd, "ACR_A_CMD", default=args.review_cmd)`,
  `args.b_cmd = providers.resolve_role_cmd("review", args.b_cmd, "ACR_B_CMD", default=args.review_cmd)`,
  `args.synth_cmd = providers.resolve_role_cmd("review", args.synth_cmd, "ACR_SYNTH_CMD", default=args.review_cmd)`.
  In `run_adversarial_review` route the commands per role: architect → `args.a_cmd`,
  inspector → `args.b_cmd`, the two cross_review passes → `args.b_cmd`, synthesis →
  `args.synth_cmd` (replace the five `args.review_cmd` literals at lines 307–325).
  Command mapping rationale: a-cmd = Architect pass; b-cmd = Inspector + the two
  cross-review passes that continue the inspector line (spec: "Inspector (two
  independent review passes)"); synth-cmd = Synthesis.
- **Dependencies:** [P3]
- **Tests:** `python3 /home/chpo/.hermes/skills/adversarial-code-review/scripts/adversarial_review.py --help` lists the three new flags. Unit test `test_review_flags.py`: build `args` via `parse_args(['--a-cmd','CMDA','--review-cmd','DEFR'])` and assert `main()`-equivalent resolution sets `a_cmd` to the CMDA-resolved value and `b_cmd`/`synth_cmd` to the `DEFR`-resolved value when not overridden. Monkeypatch `_run_role` to record the command per role and assert architect got CMDA while inspector/cross/synthesis got the default.
- **Risks:** Low — purely additive. Backward compat preserved: unset flags fall through to `--review-cmd`. Confirm `providers.resolve_role_cmd` signature supports the `default=` kwarg (it is used this way for `review` already, so it does).

### P9: Guard adversarial-plan against reintroducing the duplicated helpers (Items 5, 6 — plan caller)
- **Files:** `/home/chpo/.hermes/skills/adversarial-plan/scripts/phases/__init__.py` (does not exist yet), `/home/chpo/.hermes/skills/adversarial-plan/.gitignore` (ensure the line `__pycache__/` is present — append it if the file lacks it; create the file if it does not yet exist)
- **Description:** The adversarial-plan repo currently has no
  `scripts/phases/__init__.py`, so there is no duplicated `try_parse_json` /
  `extract_frontmatter` / `_parse_frontmatter` to remove here. This step is a
  **guard**: assert the absence of those definitions today, and document the
  contract that if/when adversarial-plan gains a `phases/__init__.py`, it must
  import `parse_json_output`, `extract_frontmatter`, and `parse_frontmatter` from
  `adversarial_common.jsonio` rather than redefining them. Add a one-line module
  docstring contract in a new (empty-body) `scripts/phases/__init__.py` that
  re-exports the shared helpers, so the plan skill imports them from one place.
- **Dependencies:** [P2]
- **Tests:** `grep -rn 'def try_parse_json\|def extract_frontmatter\|def _parse_frontmatter\|_FRONTMATTER_RE' /home/chpo/.hermes/skills/adversarial-plan/ --include='*.py'` returns no redefinitions (only an import/re-export, if any). `python3 -c "import sys; sys.path.insert(0,'/home/chpo/.hermes/skills/adversarial-plan/scripts'); import phases; from phases import try_parse_json, extract_frontmatter; assert try_parse_json('{\"x\":1}')=={'x':1}"` (passes through to jsonio).
- **Risks:** Low. Creating a near-empty `__init__.py` only matters if the plan skill later gains real phase modules; the re-export keeps that future code honest.

## Ordering rationale
- **P1 (personas), P2 (jsonio helpers), P3 (alias), P4 (arbiter sig), P5 (branch)
  have no inter-dependencies** — they touch disjoint files, so any order among
  them is valid. **P8 (review flags) depends on P3**: both edit
  `adversarial-code-review/scripts/adversarial_review.py` (P3 at line 271,
  P8 at lines 307–325 and 423); the explicit dependency prevents two diffs from
  colliding in the same file. Steps are sequenced P1→P9 to give the dev loop a
  linear run.
- **P2 before P6 and P7 and P9** because P6/P7/P9 import the shared
  `parse_json_output` / `extract_frontmatter` / `parse_frontmatter` that P2 adds.
  P2 also carries Item 8 (require PyYAML), so the frontmatter fix is in place
  before any caller is rewired to use it.
- **P6 depends on P5** (encoded in P6's Dependencies as `[P2, P5]`) because both
  edit `phase_verify.py`; the explicit dependency keeps the branch-extraction and
  JSON-parsing diffs in the same file from colliding.
- **P9 last** as a verification/guard step that depends on P2 and confirms the
  plan repo never reintroduces the duplication it was created to remove.
- No step depends on a later step; no circular dependencies.
- Spec coverage: Item 1 → P1; Item 2 → P4; Item 3 → P3; Item 4 → P5;
  Item 5 → P2+P6+P7+P9; Item 6 → P2+P7+P9; Item 7 → P8; Item 8 → P2.
