# Codex GPT-5.6-Sol + Claude Fable 5 — Full Adversarial Pipeline

Validated 2026-07-10 on the OmniSense firmware project (ESP32-S3 + CC1101).

## Pipeline stages

All three stages completed in 1 cycle each with Codex as writer/DEV and Claude as
challenger/reviewer:

| Stage | Writer | Challenger | Findings | Result |
|-------|--------|------------|----------|--------|
| Spec | Codex GPT-5.6-Sol (reasoning=high) | Claude Fable 5 (tmux) | 11 findings | APPROVED (11/11 settled) |
| Plan | Codex GPT-5.6-Sol (reasoning=high) | Claude Fable 5 (tmux) | 4 findings | APPROVED (4/4 settled) |
| Code loop | Codex GPT-5.6-Sol (reasoning=high) | Claude Fable 5 (tmux) | 4+ findings per step | In progress (P5/8 reached) |

## Commands used

### Spec
```bash
python3 adversarial_spec.py \
  --brief /tmp/brief.md \
  --dev-cmd "codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -c model='gpt-5.6-sol' -c model_reasoning_effort='high'" \
  --review-cmd "python3 /path/to/claude-tmux.py --yolo --model best --timeout 600 --hard-timeout 1200" \
  --feature "feature-name" --timeout 1200
```

### Plan
```bash
python3 adversarial_plan.py \
  --spec spec.md \
  --dev-cmd "codex exec ..." \
  --review-cmd "python3 /path/to/claude-tmux.py ..." \
  --feature "feature-name" --timeout 1200
```

### Code loop
```bash
python3 adversarial_loop.py \
  --plan /tmp/plan.md \
  --dev-cmd "codex exec ..." \
  --review-cmd "python3 /path/to/claude-tmux.py ..." \
  --feature "feature-name" --out .adversarial-loop \
  --timeout 1200 --max-loops 2 --no-arbiter
```

## Key observations

- Claude Fable 5 via `claude-tmux.py` produced valid JSON for both the embedded-prompt
  pattern (spec/plan challenger) and the files-on-disk pattern (code loop reviewer).
  Earlier documentation claiming Claude cannot do the embedded-prompt pattern was
  pre-Fable-5 or related to an older claude-tmux wrapper version.
- claude-tmux wrapper buffers ALL output until the session completes — no partial
  output appears in `process(action='poll')`. Only `notify_on_complete` reveals the result.
- Codex with `codex exec -C <dir>` + inline prompt (`-C` for context directory,
  prompt as argument) is the preferred approach for focused reviews, avoiding the
  1 MB stdin input limit of the review script's `--project-dir` mode.
- `reasoning=high` on GPT-5.6-Sol produces deeper analysis but can cause 5+ minute
  silent pauses between actions. `reasoning=low` is faster for exploration-heavy tasks.
- The full pipeline produces real git commits at every stage, making rollback safe.
- Claude quota is the main bottleneck: ~200-300K tokens per 5h window. For long
  code loops (8+ steps), GLM-5.2 (pi --provider zai) or DeepSeek are viable
  fallbacks for the reviewer role.
