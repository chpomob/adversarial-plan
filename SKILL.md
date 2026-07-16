---
name: adversarial-plan
description: "Adversarial implementation planner. Takes a spec.md (from adversarial-spec) and optionally review findings, produces a plan.md with ordered steps, dependencies, files, tests, and risks. Consumable by adversarial-code-loop --plan mode."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [adversarial, planning, implementation, plan, architecture]
    related_skills: [adversarial-spec, adversarial-code-loop, adversarial-code-review]
---

# Adversarial Plan

**Spec → implementation plan.** Two-role adversarial pipeline that takes a spec.md (from
adversarial-spec) and optionally review findings (from adversarial-code-review), and
produces a plan.md with ordered steps consumable by adversarial-code-loop's `--plan`
mode.

## Workflow

```
PHASE 0 ──→ GIT SETUP (branch, stash, init)
PHASE 1 ──→ PLAN  (plan-writer reads spec.md + optional findings, writes plan.md)
PHASE 2 ──→ CHALLENGE (plan-challenger critiques risks/dependencies/order)
PHASE 3 ──→ REVISE (plan-writer amends plan.md per findings)
PHASE 4 ──→ VERIFY (plan-challenger checks findings resolved)
MERGE  ──→ squash-merge (APPROVED) or [REJECTED] commit
```

## CLI

```bash
python3 scripts/adversarial_plan.py \
  --spec <file>              # spec.md to plan (default: <workdir>/spec.md)
  --findings <file>          # optional findings.json from a review
  --dev-cmd <cmd>            # plan-writer (default: pi ... glm-5.2)
  --review-cmd <cmd>         # plan-challenger (default: pi ... deepseek)
  --workdir <dir>            # default: .
  --max-loops <N>            # default: 2
  --feature <name>           # default: from spec filename
  --timeout <N>              # default: 600
  --out <dir>                # default: .adversarial-plan
  --no-merge
```

## Output format

plan.md with YAML frontmatter + ordered steps:

```yaml
---
spec: "feature-name"
version: "1.0"
author: "adversarial-plan"
based-on: "adversarial-spec"
findings-input: false
---

## Steps

### P1: First task
- Files: [path/to/file.rs]
- Description: What changes in this file
- Dependencies: []
- Tests: What tests to write
- Risks: What could go wrong

### P2: Second task
- Files: [path/to/another.rs]
- Description: What changes
- Dependencies: [P1]
- Tests: Integration test
- Risks: Deadlock risk
```

## Personas

Loaded from adversarial-common/personas/:
- plan-writer.md — reads spec.md + optional findings, writes plan.md
- plan-challenger.md — reads plan.md, outputs JSON findings (risks, order, gaps)

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | APPROVED — plan squash-merged |
| 1 | Infrastructure failure |
| 2 | Usage error |
| 3 | REJECT |

## Integration with dev loop

**IMPORTANT: `--plan` mode is NOT wired in `adversarial-code-loop`** — pitfall #1.
The `adversarial_loop.py` argparse accepts `--spec`, not `--plan`. Do NOT try
`--plan /path/to/plan.md` (fails with "error: the following arguments are required: --spec").

Instead, execute each plan step as a separate code loop with a focused per-step
spec. See `references/run-plan-steps-without-plan-mode.md` for the workflow.

```bash
# Example: running P1 of a plan
python3 ~/.hermes/skills/adversarial-code-loop/scripts/adversarial_loop.py \
  --spec /path/to/step-P1-spec.md \
  --workdir /path/to/target-repo \
  --dev-cmd "codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check --sandbox workspace-write" \
  --review-cmd "python3 .../claude-tmux.py --timeout 900 --hard-timeout 1800 --cwd /path/to/target-repo" \
  --timeout 1800 --max-loops 3 --no-arbiter
```

Steps without dependencies targeting different repos can run in parallel (safe).
Parallel on the same repo is forbidden (adversarial-code-loop pitfall #8).

## Plan format constraints

- **Files and Dependencies must be on a single line.** The plan parser uses
  `- **Key:** value` format and does NOT support multi-line indented lists.
  Use comma-separated values: `- **Files:** /path/a, /path/b` not multi-line.
- **Dependencies** are parsed as a Python literal list, so `[P1, P2]` works but
  `[\"P1\", \"P2\"]` also works. Keep to the simple bracket format.
- **Step IDs** must be unique and alphanumeric (matching `P\d+` or similar).

## Pitfalls

- **The `--findings` flag does NOT accept adversarial-review's `final.json` as-is.** That file contains finding *counts* (`{blocker: 1, major: 2}`), not finding objects. You must extract structured findings from the review synthesis report and craft a findings.json manually.
- Each step must have explicit dependencies (or empty list). Circular deps cause validation failure.
- If review findings are provided via --findings, the plan must address each finding in at least one step.
- Step order should respect dependencies (topological sort is automatic).
- The same code patterns as adversarial-code-loop v4: git branch isolation, phase modules, squash merge.
- **Multi-repo plans are not reliable.** Despite `_resolve_step_workdir()` in the dev loop,
  cross-repo steps have not been validated end-to-end. One plan = one repo.
- **`--plan` mode re-runs completed steps.** If a step was already applied (e.g. files deleted
  in a previous run), BUILD still runs the DEV model, wasting time and quota. Either edit the
  plan to remove completed steps, or accept the wasted cycle (BUILD → empty diff → auto-APPROVED).
- **Reduced plans (resume after partial execution) must strip cross-step deps that reference removed steps.** When creating a reduced plan from steps P1–P13 where P1–P7 are already merged, steps like P13 that originally listed `[P8, P14, P10, ...]` will fail `validate_steps()` with `"step P13 depends on unknown step P8"` if P8 isn't in the reduced plan. **Fix:** remove all dependency IDs that reference steps not present in the reduced file. The parser checks only the plan's own step IDs — it does not know about previously executed steps. Validated 2026-07-13 on a 15-step plan resumed from step 10.
- **Plan parser is strict about bullet format.** Files and Dependencies must be on a single
  line: `- **Files:** /path1, /path2`. Indented sub-lists are NOT parsed.
- **CHALLENGE phase was embedding the full plan + spec text in the prompt (1000+ lines). Fixed 2026-07-14: reduced to 724 chars by removing the text embedding.** The `_build_prompt()` function in `phase_challenge.py` was concatenating `plan_text` and `spec_text` into the prompt — making the model read redundant text and causing extended-thinking timeouts on large documents. **Fix:** patch `_build_prompt()` to only reference file paths (`plan.md` and `spec.md` are in the current directory) and remove the `--- plan.md ---\n{plan_text}\n--- spec.md ---\n{spec_text}` suffix. The model reads files from disk via `--cwd`. This reduces total prompt size from ~1000+ lines to ~724 chars and eliminates extended-thinking timeouts. **Validated 2026-07-15:** plan challenge completed in ~2 min with Claude Sonnet (vs. 20+ min timeout before the fix).
- **Claude Fable 5 (2026-07) succeeded as plan-challenger** — a real run produced 4 findings, REQUEST_CHANGES → REVISE → APPROVE with 4/4 settled. If Claude exits code 3 (non-parseable output), fall back to DeepSeek (`pi --provider deepseek --model deepseek-v4-pro`).
- **Validated end-to-end pairing (2026-07): Codex DEV + Claude Fable 5 REVIEW across ALL stages** — spec (11 findings), plan (4 findings), and code loop. All three stages completed in 1 cycle each. Fable 5 succeeded at both the embedded-prompt JSON pattern (spec/plan challenger) and the files-on-disk pattern (code loop reviewer). Fallback: Codex DEV + DeepSeek REVIEW for spec/plan when Claude quota is exhausted.
- **`write_final_json()` may crash with TypeError for 'verdict'** at pipeline end. When the stash pop fails (see next pitfall), the error handling puts the payload dict in an unexpected state: `verdict` is passed both as a keyword argument and inside `**final_payload`. **Symptom:** the log shows "APPROVED" but exit code is 1 with `TypeError: write_final_json() got multiple values for argument 'verdict'`. The squash-merge ALREADY happened before `_finish()`, so the approved plan IS merged — only the final artifact write is lost. **Fix:** check stash state before `_finish()` and skip the pop when conflicts are detected.
- **Untracked `--findings` file in the workdir causes stash pop conflict.** PHASE 0 stashes the entire workdir including `findings.json`. At pipeline end, `git stash pop` fails because `findings.json` already exists as an untracked file (the pipeline created it). **Symptom:** `git stash pop <sha> failed (possible conflict): findings.json already exists, no checkout`. **Fix:** `git add` the findings file before launching the pipeline, or pass `--findings` from a path outside the workdir (e.g., `/tmp/findings.json`). After the crash: `git stash drop` and `git add findings.json` to clean up.
