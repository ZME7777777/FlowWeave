---
name: review-reconciliation
description: Reconcile review comments for a PRD-to-technical-design node.
version: 1.0.0
---

# Reconcile review comments

Address each comment by ID. Mark it resolved only with a specific revision or human disposition; otherwise keep it open and choose the earliest rerun point.

## Output discipline

Return reviewable structured findings with requirement and evidence references. Never perform human-only start, branch, acceptance, or cancellation actions.
