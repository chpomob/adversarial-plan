# adversarial-plan

**Spec → implementation plan.** Two-role adversarial pipeline that reads a spec (from `adversarial-spec`) and optional review findings (from `adversarial-code-review`), then produces a `plan.md` with ordered steps consumable by `adversarial-code-loop --plan` mode.

For Hermes Agent, Claude Code, Codex, or any LLM CLI.

## How it works

```
PHASE 1 ──→ PLAN     (plan-writer reads spec.md + findings, writes plan.md)
PHASE 2 ──→ CHALLENGE (plan-challenger critiques risks/dependencies/order)
PHASE 3 ──→ REVISE   (plan-writer amends per findings)
PHASE 4 ──→ VERIFY   (plan-challenger checks findings resolved)
MERGE  ──→ squash-merge (APPROVED) or [REJECTED] commit
```

## Plan format

```yaml
### P1: Step title
- **Files:** /path/to/file1, /path/to/file2
- **Description:** What to implement
- **Dependencies:** []
- **Tests:** How to verify
- **Risks:** What could go wrong
```

Output plans are consumed directly by `adversarial-code-loop --plan` for multi-step execution with per-step BUILDs, REVIEWs, and squash-merges.

## Comparison

| Feature | adversarial-plan | Manual planning |
|---------|-----------------|-----------------|
| Adversarial challenge | ✅ plan-challenger critiques order, gaps, risks | ❌ |
| Git-native | ✅ branch-per-plan, squash-merge | ❌ |
| Findings-aware | ✅ consumes review JSON directly | ❌ |
| Code-loop compatible | ✅ output feeds `--plan` mode | ❌ |

## Quick start

```bash
python3 scripts/adversarial_plan.py \
  --spec spec.md \
  --findings findings.json \
  --dev-cmd "pi --provider zai --model glm-5.2" \
  --review-cmd "pi --provider deepseek --model deepseek-v4-pro"
```

## Dependencies

- Python ≥ 3.11
- Git ≥ 2.5
- Two LLM CLIs (plan-writer + plan-challenger)

Uses `adversarial-common` as the shared engine.

## License

MIT
