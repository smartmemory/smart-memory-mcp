"""Lexical-contract v1 names for core-free hosted and remote installations."""

ACCEPTED_CHANNELS = frozenset(
    {
        "entity-graph",
        "ssg-traversal",
        "semantic",
        "regex-text",
        "lexical",
        "spreading-activation",
        "facts",
        "structural-semantic",
    }
)


def validate_channel_weights(weights):
    if weights is None:
        return
    if not isinstance(weights, dict):
        raise ValueError("channel_weights must be a dict")
    for channel in weights:
        if channel in {"contains", "keyword-bm25"}:
            raise ValueError(f"Channel {channel} was removed; use lexical.")
        if channel not in ACCEPTED_CHANNELS:
            raise ValueError(
                f"Unknown search channel {channel!r}. Accepted: {sorted(ACCEPTED_CHANNELS)}"
            )
