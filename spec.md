---
name: "adversarial-skills-refacto-8-items"
version: "1.0"
author: "Hermes Agent"
---

# Adversarial Skills Refactoring — 8 items

Consolidate, delete, and fix artifacts across adversarial-common, adversarial-code-loop, adversarial-code-review, adversarial-spec, and adversarial-plan.

## Specification

### Item 1: Delete 4 unused personas

**Files to delete:**
- `~/.hermes/skills/adversarial-common/personas/architect.md` (1586 bytes)
- `~/.hermes/skills/adversarial-common/personas/inspector.md` (1661 bytes)
- `~/.hermes/skills/adversarial-common/personas/synthesis.md` (1496 bytes)
- `~/.hermes/skills/adversarial-common/personas/cross_review.md` (1137 bytes)

**Why:** These are not referenced by any pipeline. The code-review pipeline (adversarial_review.py) uses `"architect"`, `"inspector"`, `"cross_review"`, and `"synthesis"` as role names but they fall through `persona_for_role` / `persona_path` with base-name fallback — the dedicated persona files are dead and unused.

**Files referenced from:** adversarial-code-review/scripts/adversarial_review.py (uses `architect`, `inspector`, `cross_review`, `synthesis` as role strings, not file paths).

**Verification:** `git add -A && git status` shows no staged changes for these files (they're removed). `python3 -c "from adversarial_common import persona_path; print(persona_path('architect'))"` raises FileNotFoundError.

**Risk:** None — these roles never used their dedicated file. They always fell back to the shared `review` / `critic` persona or base-name fallback.

---

### Item 2: Fix `run_arbiter` unused params (dev_cmd/review_cmd)

**File:** `~/.hermes/skills/adversarial-code-loop/scripts/phases/phase_arbiter.py`, function `run_arbiter` at line 55.

**Problem:** Signature has `dev_cmd: str, review_cmd: str` but neither is used inside the function body. They are dead parameters.

**Fix:** Remove `dev_cmd` and `review_cmd` from the function signature. Update all callers in adversarial-code-loop/scripts/adversarial_loop.py (and v4/v3 variants if applicable).

**Callers to update:**
- `~/.hermes/skills/adversarial-code-loop/scripts/adversarial_loop_v4.py` (or the main adversarial_loop.py) where `run_arbiter` is called

**Verification:** `python3 -c "import sys; sys.path.insert(0, '...'); from scripts.phases.phase_arbiter import run_arbiter; import inspect; sig = inspect.signature(run_arbiter); assert 'dev_cmd' not in sig.parameters, 'dev_cmd should be removed'; assert 'review_cmd' not in sig.parameters"`

**Risk:** Low — just removing dead params. Ensure all callers are updated (search all files under adversarial-code-loop that call `run_arbiter`).

---

### Item 3: Remove `fail_phase = _fail_phase` alias

**File:** `~/.hermes/skills/adversarial-common/adversarial_common/runner.py`, line 87.

**Problem:** `fail_phase = _fail_phase` creates a redundant public alias for `_fail_phase`. The private function and its alias are identical.

**Fix:** Delete line 87 (`fail_phase = _fail_phase`). Update all references to `runner.fail_phase` across all adversarial skills to use `runner._fail_phase` or import directly. Since `_fail_phase` is a private function (starts with underscore), it should remain as `_fail_phase` and the alias removed.

**Callers to update (search for `fail_phase` across all adversarial skills):**
- `~/.hermes/skills/adversarial-code-review/scripts/adversarial_review.py` — uses `runner.fail_phase`
- `~/.hermes/skills/adversarial-common/adversarial_common/runner.py` — line 87 itself
- Any other import of `fail_phase`

**Verification:** `grep -r '[.]fail_phase\|import.*fail_phase' ~/.hermes/skills/adversarial-*/ --include='*.py'` returns no hits (after replacing with `._fail_phase`).

**Risk:** Low — pure alias removal. Update all references to `_fail_phase` or re-export from `__init__.py`.

---

### Item 4: Consolidate `git symbolic-ref --short HEAD` into gitops.py

**Problem:** The branch name extraction `subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], ...)` is called directly in:
- `~/.hermes/skills/adversarial-code-loop/scripts/phases/phase_review.py` (lines 51-57)
- `~/.hermes/skills/adversarial-code-loop/scripts/phases/phase_verify.py` (lines 89-95)

**Fix:** `gitops.py` already has `get_current_branch(workdir)` at line 134. Replace the inline `subprocess.run` calls in both phase_review.py and phase_verify.py with `gitops.get_current_branch(workdir)`.

**Files to change:**
- `phase_review.py`: Replace lines 51-57 with `from adversarial_common import gitops` and `branch = gitops.get_current_branch(workdir)`
- `phase_verify.py`: Replace lines 89-95 similarly

**Verification:** `git diff` shows only the consolidation change. `python3 -c "from adversarial_common import gitops; print(gitops.get_current_branch('.'))"` works.

**Risk:** Low — just deduplicating. `gitops.get_current_branch` raises `GitError` on detached HEAD (same as the inline version).

---

### Item 5: Consolidate `try_parse_json` into jsonio.py

**Problem:** `try_parse_json` (3-strategy JSON extraction) is duplicated in:
- `~/.hermes/skills/adversarial-code-loop/scripts/phases/phase_verify.py` — local `_try_parse_json` function (lines 35-68)
- `~/.hermes/skills/adversarial-spec/scripts/phases/__init__.py` — `try_parse_json` (lines 77-111)
- `~/.hermes/skills/adversarial-plan/scripts/phases/__init__.py` — `try_parse_json` (lines 77-111, identical to spec version)

**Fix:** Add a `parse_json_output()` function to `~/.hermes/skills/adversarial-common/adversarial_common/jsonio.py` that implements the 3-strategy extraction. Then update all three callers to import and use it.

**jsonio.py new function:**
```python
def parse_json_output(text: str) -> dict | list | None:
    """3-strategy JSON extraction: (1) strip fences, (2) extract {..}, (3) extract [..]."""
```

**Files to change:**
- Add `parse_json_output` to jsonio.py
- `phase_verify.py` (code-loop): Replace `_try_parse_json` with `jsonio.parse_json_output`
- `phases/__init__.py` (spec): Replace `try_parse_json` with import from `jsonio`
- `phases/__init__.py` (plan): Replace `try_parse_json` with import from `jsonio`

**Verification:** All three callers work. `python3 -c "from adversarial_common.jsonio import parse_json_output; assert parse_json_output('{\"x\":1}') == {'x': 1}"`

**Risk:** Low — pure dedup. Keep the existing function name as an alias in callers for backward compat.

---

### Item 6: Consolidate `extract_frontmatter` / `_parse_frontmatter` into adversarial-common

**Problem:** `extract_frontmatter` and `_parse_frontmatter` are duplicated identically in:
- `~/.hermes/skills/adversarial-spec/scripts/phases/__init__.py` (lines 117-159)
- `~/.hermes/skills/adversarial-plan/scripts/phases/__init__.py` (lines 117-159)

Both have the same `_FRONTMATTER_RE` regex, the same `extract_frontmatter()`, and the same `_parse_frontmatter()` with PyYAML fallback.

**Fix:** Add `extract_frontmatter()` and `parse_frontmatter()` (public) to `~/.hermes/skills/adversarial-common/adversarial_common/jsonio.py` (since it's the structured-text utility module). Then import from there in both spec and plan.

**Files to change:**
- Add to jsonio.py: `extract_frontmatter(text)`, `parse_frontmatter(fm_text)`, `_FRONTMATTER_RE`
- spec's `phases/__init__.py`: Remove the functions, import from `jsonio`
- plan's `phases/__init__.py`: Remove the functions, import from `jsonio`

**Verification:** `python3 -c "from adversarial_common.jsonio import extract_frontmatter, parse_frontmatter; assert extract_frontmatter('---\\nname: x\\n---\\n') == 'name: x'"`

**Risk:** Low — pure dedup. Keep backward compat aliases in callers.

---

### Item 7: Restore `--a-cmd` / `--b-cmd` / `--synth-cmd` flags in adversarial_review.py

**File:** `~/.hermes/skills/adversarial-code-review/scripts/adversarial_review.py` — `parse_args()` function (line 423).

**Problem:** `--a-cmd`, `--b-cmd`, and `--synth-cmd` flags are missing from the CLI. These would allow overriding the command used for the Architect, Inspector (two independent review passes), and Synthesis phases separately from `--review-cmd`.

**Fix:** Add these three optional flags to `parse_args()`:
- `--a-cmd`: Architect review command (defaults to `--review-cmd`)
- `--b-cmd`: Inspector review command (defaults to `--review-cmd`)
- `--synth-cmd`: Synthesis review command (defaults to `--review-cmd`)

Update `run_adversarial_review()` to pass the appropriate command per role instead of using `args.review_cmd` for all passes.

**Files to change:**
- `adversarial_review.py`: Add argparse arguments, update `run_adversarial_review` calls to use role-specific commands

**Verification:** `python3 adversarial_review.py --help` shows the new flags. Running with `--a-cmd "..."` uses a different command for Architect vs Inspector vs Synthesis.

**Risk:** Low — purely additive. Backward compat: defaults to `--review-cmd` when not set.

---

### Item 8: Fix `_parse_frontmatter` PyYAML fallback in jsonio.py

**File:** `~/.hermes/skills/adversarial-common/adversarial_common/jsonio.py` (after moving `_parse_frontmatter` there in Item 6).

**Problem:** The `_parse_frontmatter` function uses `try: import yaml` to optionally use PyYAML, falling back to a regex-based `key: value` parser. The issue is that the fallback regex `^([A-Za-z_][\w-]*)\s*:\s*(.*)$` is fragile — it fails on quoted values with colons, multiline values, and list values.

**Fix:** Make PyYAML a required dependency (it's already available in the environment) and remove the fragile regex fallback. Import `yaml` at the top of jsonio.py.

**What to change:**
- In jsonio.py: Remove the `try/except ImportError` guard around `import yaml`
- Import `yaml` at module level
- Remove the regex fallback code
- Simplify `_parse_frontmatter` to always use `yaml.safe_load`

**Impact:** This also affects the callers in spec and plan (Items 5-6) since they'll import from jsonio.

**Verification:** `python3 -c "from adversarial_common.jsonio import parse_frontmatter; assert parse_frontmatter('name: x\\nlist:\\n  - a\\n  - b')[0] == {'name': 'x', 'list': ['a', 'b']}"` — the regex fallback couldn't parse lists, PyYAML can.

**Risk:** Low — PyYAML is already available (it's in stdlib-ish for Python 3.11+ via `pip list | grep yaml` on this system).
