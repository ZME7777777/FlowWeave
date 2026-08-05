---
name: evidence-normalization
description: Normalize evidence for a PRD-to-technical-design node.
version: 1.0.0
---

# Normalize evidence

Classify evidence as FACT, HUMAN_INPUT, INFERENCE, UNKNOWN, CONFLICT, or ACCESS_DENIED. Preserve conflicting sources and connect every inference to supporting evidence IDs.

## Output discipline

Return reviewable structured findings with requirement and evidence references. Never perform human-only start, branch, acceptance, or cancellation actions.
