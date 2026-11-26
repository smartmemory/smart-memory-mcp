"""The same seven decision-attribution checks run over Lite and the hosted connector."""

import re


def check_answer(text, names):
    """Check complete role-labelled lines so a name in decision content cannot pass."""
    for label, name in zip(("Recommended by", "Decided by"), names, strict=True):
        prefix = f"{label} (as stated, not verified): "
        if name:
            assert prefix + name in text.splitlines(), text
        else:
            assert label not in text, text


def exercise(tools):
    """Write through the registered tools and read both sides of each replacement."""
    original = ("Medical Monitor", "Study Lead")
    successor = ("Safety Committee", "Sponsor CMO")
    empty = (None, None)
    for names, replacement in (
        (original, successor),
        (empty, empty),
        (original, empty),
    ):
        created = tools["decision_create"](
            content="Attribution test original", **_fields(names)
        )
        check_answer(created, names)
        old = re.search(r"Decision created: (\S+)", created)[1]
        replaced = tools["decision_supersede"](
            decision_id=old,
            new_content="Attribution test replacement",
            reason="new evidence",
            **_fields(replacement),
        )
        check_answer(replaced, replacement)
        new = re.search(r"New decision: (\S+)", replaced)[1]
        _check_reads(tools, old, names, new, replacement)


def _check_reads(tools, old, names, new, replacement):
    """Read the version itself and its place in the chain."""
    for ident, pair, state in (
        (old, names, "superseded"),
        (new, replacement, "active"),
    ):
        for tool in ("decision_get", "decision_provenance"):
            answer = tools[tool](decision_id=ident)
            assert ident in answer
            check_answer(answer, pair)
        listing = tools["decision_list"](status=state)
        row = listing.split(f"- [{ident}]", 1)[1].split("\n- [", 1)[0]
        check_answer(row, pair)
        history = tools["decision_history"](decision_id=ident)
        assert ident in history
        for version, expected in ((old, names), (new, replacement)):
            header = f"Decision {version}:\n"
            if any(expected):
                section = history.split(header, 1)[1].split("\nDecision ", 1)[0]
                check_answer(section, expected)
            else:
                assert header not in history
        if not any(names + replacement):
            check_answer(history, (None, None))


def _fields(pair):
    """Omit absent fields from the tool call."""
    return {
        key: value
        for key, value in zip(("recommended_by", "decided_by"), pair, strict=True)
        if value
    }
