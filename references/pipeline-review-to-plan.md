# Bridging adversarial-code-review → adversarial-plan

## The gap

Adversarial-review outputs `final.json` with `{verdict, findings: {blocker: N, major: N}}` — finding **counts**, not finding objects. The plan pipeline's `--findings` flag expects either a JSON array of finding objects or an object with a `findings` array. **These are not compatible formats.** You must extract structured findings from the review synthesis and craft a findings.json manually.

## Detection

After `adversarial_review.py` completes, check:

```
ls <out>/  # e.g. /tmp/acr-quota-publish/
# → 01_architect.txt  02_inspector.txt  03_cross_1.txt ...
# → 05_synthesis.txt  review.md  final.json
cat <out>/final.json
# → {"verdict":"REQUEST_CHANGES","findings":{"major":10,"minor":5,"nit":2},...}
# ⚠ These are COUNTS, not finding objects — NOT plan-consumable.
```

## Extraction procedure

### 1. Read the synthesis report (`05_synthesis.txt` or `review.md`)

It contains the complete ranked findings list with id, severity, file, line, summary, and evidence for each.

### 2. Craft a findings.json

Format accepted by `adversarial_plan.py --findings`:

**Option A — bare array (preferred):**
```json
[
  {
    "id": "C1",
    "severity": "blocker",
    "summary": "Short title",
    "detail": "Full description with root cause and fix guidance",
    "file": "file.py:42-56"
  },
  ...
]
```

**Option B — object with `findings` key (also accepted):**
```json
{
  "findings": [...]
}
```

Each finding object supports these fields:

| Field | Required | Description |
|-------|----------|-------------|
| `id` | Yes | Unique identifier, e.g. `C1`, `M2`. Used by the plan pipeline for tracking finding-to-step mapping. |
| `severity` | Yes | `blocker`, `major`, `minor`, or `nit`. |
| `summary` | Yes | One-line title. |
| `detail` | No | Multi-sentence description with root cause, impact, and fix direction. |
| `file` | No | Path and line range, e.g. `__init__.py:549-556`. |

### 3. Strip review-only metadata

Remove any fields the synthesis report adds for the human reader (e.g. "cross-review consensus", "risk level: confirmed"). Only id, severity, summary, detail, and file are consumed by the plan pipeline.

### 4. Validate

```bash
python3 -c "
import json, sys
with open('/tmp/findings.json') as f:
    data = json.load(f)
if isinstance(data, dict):
    data = data.get('findings', [])
assert isinstance(data, list), 'must be array or object with findings key'
for f in data:
    assert f.get('id'), f'missing id in {f}'
    assert f.get('severity'), f'missing severity in {f}'
    assert f.get('summary'), f'missing summary in {f}'
print(f'{len(data)} findings valid')
"
```

### 5. Feed to plan

```bash
python3 /path/to/adversarial_plan.py \
  --spec spec.md \
  --findings /tmp/findings.json \
  --workdir /path/to/project \
  ...
```

## Alternative: extract from earlier artifacts

If the synthesis report is missing or truncated, extract findings from the raw reviewer output artifacts:

```
01_architect.txt  →  JSON with "findings" array (Architect perspective)
02_inspector.txt  →  JSON with "findings" array (Inspector perspective)
```

These contain individual reviewer findings but lack the cross-validation consensus and ranking. Merge manually, deduplicate by id (the synthesis already does this in its report).

## Pitfalls

- **Do NOT feed `final.json` directly to `--findings`.** The plan pipeline will parse `{findings: {blocker: 1, major: 2}}` as a dict, try `payload.get("findings")` which returns the dict, then fail `isinstance(payload, list)` → exit 2 "must be a JSON array or an object with a 'findings' array".
- **Do NOT skip findings.** Every finding id in the findings.json must be addressed by at least one plan step or the plan-challenger will flag it as uncovered.
- **Severity drives plan urgency.** `blocker` findings should get their own step early. `nit` findings can be grouped in a cleanup step at the end.
- **File paths help the plan-writer generate accurate step descriptions.** Include them even though they're not strictly required.
- **Cross-review ADD findings (from 03_cross_1.txt / 04_cross_2.txt)** are already consolidated in the synthesis report — check the report's "Critical Findings" section which lists cross-review additions per finding.
