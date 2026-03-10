"""Mnemo Cortex Recall — Exact-match memory search via SQLite FTS5.

Complements Mnemo's semantic/vector search with precise keyword and
entity-based recall. Originally conceived as 'claw-recall' by AL.

Two search modes, one memory system:
    - Mnemo /context  → "What do you remember about Easter?" (semantic, fuzzy)
    - Mnemo recall    → "What was the Shopify API key?" (exact, precise)
"""
