# Claude Fable 5 Timeout Notes

When using Claude Fable 5 as plan-challenger:

- Extended thinking takes 8-12 min per response
- The default adversarial-plan timeout of 600s is often insufficient
- Increase `--timeout` to at least 1200 when Claude is the `--review-cmd`
- Pair with `--hard-timeout 1800` inside the claude-tmux command
- If Claude exits code 3 (REJECT) due to non-parseable JSON, retry with DeepSeek or GLM-5.2
- Validated 2026-07-10: 4 findings, REQUEST_CHANGES → REVISE → APPROVE, 4/4 settled
