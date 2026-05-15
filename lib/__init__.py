"""Trace-analysis helper package (Step 1.6).

Separate from `sim/` (the KV-cache eviction simulator) because:
- `sim/` keys are model-namespaced (single dict across all models).
- `scripts/` analyze one trace at a time, no namespace needed.
- Different lifecycles: simulator evolves with eviction algorithms,
  analyzer evolves with reporting features.

See docs/step1_6_token_level_experiment_plan.md §13 for the relationship.
"""
