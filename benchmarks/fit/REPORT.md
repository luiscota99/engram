# Ranking-weight fit report — seed 20260717
Holdout: n=31  ΔR@5=+0.0000 (bootstrap lb +0.0000, margin -0.02)  ΔMRR=+0.0215
Verdict: PUBLISHABLE

Held (no signal in eval set — grow the label set to fit these):
  ~ recency_half_life_days
  ~ usage_boost_weight
  ~ stale_embedding_penalty
  ~ affinity_created
  ~ affinity_used
  ~ affinity_relevant
  ~ intent_mult_pattern
  ~ feedback_helped_weight
  ~ feedback_unhelpful_weight

Proposed diff:
  base_score_lexical: 50.000 -> 99.596
  recency_floor: 0.750 -> 0.677
  recency_span: 0.250 -> 0.499
  type_match_boost: 15.000 -> 8.712
  tag_match_boost: 15.000 -> 39.170
  bm25_weight: 0.300 -> 0.902
  rrf_weight: 50.000 -> 127.874
  intent_mult_mistake: 1.250 -> 1.348
  intent_mult_skill: 1.200 -> 1.308
  intent_mult_conversation: 1.150 -> 1.598
  intent_mult_prompt: 1.350 -> 1.565
