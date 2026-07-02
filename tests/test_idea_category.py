"""
Tier-1 `idea` category (v4.8.0) — the creative-harness unlock.

Covers the full surface a new category touches: validity, perpetual decay,
regex auto-suggester (including precedence against topology), the LLM
classifier target list, and the ranking prior. The design intent under test:
creative content must have a first-class home that never decays and ranks
above operational facts but below doctrine/incident/decision.
"""
from agentb.classify import CLASSIFIABLE_CATEGORIES, _parse_category
from agentb.provenance import (
    DECAY_THRESHOLDS,
    DEFAULT_HIDDEN_CATEGORIES,
    VALID_CATEGORIES,
    compute_stale_warning,
    suggest_category,
)
from agentb.ranking import CATEGORY_IMPORTANCE, composite_score
from agentb.config import RankingConfig

import time


def test_idea_is_a_valid_category():
    assert "idea" in VALID_CATEGORIES


def test_idea_is_perpetual_no_decay():
    # Perpetual = absent from DECAY_THRESHOLDS; a ten-year-old idea must not warn.
    assert "idea" not in DECAY_THRESHOLDS
    ten_years_ago = time.time() - 10 * 365 * 86400
    assert compute_stale_warning("idea", ten_years_ago) is None


def test_idea_is_not_hidden_from_recall():
    assert "idea" not in DEFAULT_HIDDEN_CATEGORIES


def test_suggest_category_catches_ideation_phrasing():
    for text in (
        "This chord progression reminds me of that conversation about wave interference",
        "What if the dreamer could daydream as well as consolidate?",
        "Brainstorming: government equipment waste as a lens on AI fraud detection",
        "Guy was riffing on a mashup of seasonal collectibles and weather data",
        "The connection between circuit boards and stained glass — aesthetic seed",
    ):
        category, keywords = suggest_category(text)
        assert category == "idea", f"expected idea for {text!r}, got {category}"
        assert keywords


def test_topology_still_wins_over_incidental_whatif():
    # Order matters: operational facts outrank ideation phrasing when both match.
    category, _ = suggest_category("what if we move the service to port 50060")
    assert category == "topology"


def test_plain_technical_text_does_not_become_idea():
    for text in (
        "Deployed v4.7.1 to IGOR-2 and verified /health",
        "The cron job connects to the server on startup",
        "Decided to use exFAT because it works on all three OSes",
        # "connection between" / "cross-domain" are tech vocabulary and were
        # deliberately dropped from the idea pattern (review finding):
        "the connection between the two services must use TLS",
        "enable cross-domain requests in the CORS policy",
    ):
        category, _ = suggest_category(text)
        assert category != "idea", f"{text!r} wrongly classified as idea"


def test_idea_is_an_llm_classifier_target():
    names = {c for c, _ in CLASSIFIABLE_CATEGORIES}
    assert "idea" in names
    assert _parse_category("idea") == "idea"
    assert _parse_category("**idea**") == "idea"


def test_chatty_idea_mentions_are_not_answers():
    # "idea" is ordinary chat vocabulary — unlike every other category name,
    # a mention inside a chatty LLM reply must not be trusted as an answer.
    assert _parse_category("i have no idea") is None
    assert _parse_category("one idea would be to file this as topology") is None
    assert _parse_category("not topology, more of an idea") is None


def test_idea_ranking_prior_between_identity_and_relationship():
    assert CATEGORY_IMPORTANCE["identity"] > CATEGORY_IMPORTANCE["idea"] > CATEGORY_IMPORTANCE["relationship"]


def test_idea_outranks_session_log_at_equal_similarity():
    cfg = RankingConfig()
    idea = composite_score(
        similarity=0.6, age_days=21.0, category="idea", access_count=0, cfg=cfg
    )
    log_chunk = composite_score(
        similarity=0.6, age_days=1.0, category="session_log", access_count=0, cfg=cfg
    )
    # A three-week-old idea must beat yesterday's raw log at equal similarity —
    # the exact failure mode the creative-harness audit found.
    assert idea > log_chunk
