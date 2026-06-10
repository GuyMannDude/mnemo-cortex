"""
Mnemo Cortex — composite recall ranking (v4.1)
==============================================
Before this, /context returned results in tier order, ranked inside each tier
by raw vector similarity alone. The quality audit (2026-06-09) showed what
that does in practice: a hand-written doctrine at similarity 0.57 loses every
top-5 slot to near-identical session-noise chunks at 0.73+. Similarity knows
what *matches*; it doesn't know what *matters*.

The composite score blends four signals, each in [0, 1]:

  similarity  — what the tiers already computed (semantic match)
  recency     — exponential decay over age; yesterday beats last quarter
  importance  — category prior: a doctrine outranks a session log at equal
                similarity; perpetual categories carry the most weight
  access      — log-scaled recall frequency; memories that keep getting used
                keep earning rank (and one lucky recall can't dominate)

Weights are config (RankingConfig). Similarity keeps the majority share on
purpose — the other signals break ties and re-order the band of plausible
matches; they must never make an irrelevant memory win.

Chunks with no age/category/access data get neutral values, not penalties —
pre-v3 records must not sink just for being old-format (every existing memory
stays accessible).
"""
from __future__ import annotations

import math
from typing import Optional

from agentb.config import RankingConfig

# Category priors. Perpetual categories (never decay) are also the ones whose
# *content* earns permanence: doctrine, incident, decision, identity. The
# floor is session_log — when a caller explicitly opts INTO seeing logs they
# still rank below distilled knowledge at equal similarity.
CATEGORY_IMPORTANCE: dict[Optional[str], float] = {
    "doctrine": 1.0,
    "incident": 0.95,
    "decision": 0.95,
    "identity": 0.90,
    "relationship": 0.80,
    "topology": 0.75,
    "current_state": 0.75,
    "unknown": 0.40,
    "session_log": 0.20,
    None: 0.50,  # uncategorized / pre-v3 — neutral, not punished
}


def composite_score(
    *,
    similarity: float,
    age_days: Optional[float],
    category: Optional[str],
    access_count: int,
    cfg: RankingConfig,
) -> float:
    sim = max(0.0, min(1.0, similarity))

    if age_days is None:
        recency = 0.5  # unknown age — neutral
    else:
        recency = math.exp(-max(0.0, age_days) / cfg.recency_half_life_days * math.log(2))

    importance = CATEGORY_IMPORTANCE.get(category, 0.50)

    # log2(1+n) saturating at ~6 accesses-worth of signal: frequently-used
    # memories rise, but rank can't be bought by access count alone.
    access = min(1.0, math.log2(1 + max(0, access_count)) / math.log2(1 + 6))

    return (
        cfg.w_similarity * sim
        + cfg.w_recency * recency
        + cfg.w_importance * importance
        + cfg.w_access * access
    )
