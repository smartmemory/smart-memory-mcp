"""Presentation of the decision effects counts supplied by the backend."""

from typing import Mapping


def counts_summary(counts: Mapping[str, int]) -> str:
    conflicts = counts["conflicts"]
    noun = "conflict" if conflicts == 1 else "conflicts"
    return f"{conflicts} {noun} · {counts['replaced']} replaced · {counts['to_review']} to review"
