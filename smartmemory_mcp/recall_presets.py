"""Reader section presets for core-free MCP installs (SM-RECALL-PROFILES-1).

Mirror core's PRESETS/PRESET_BUDGETS, with parity tested against core. Expand to
the existing sections/cap_tokens wire shape so hosted service needs no new route
or selector. Agent search weight profiles remain independent.
"""

PRESET_BUDGETS: dict[str, int] = {
    "brief": 2000,
    "resume": 3000,
    "review": 4000,
    "pane": 600,
}
PRESETS: dict[str, tuple[tuple[str, float], ...]] = {
    "brief": (
        ("constraints", 0.2),
        ("active_plan", 0.2),
        ("anchors", 0.15),
        ("learned", 0.15),
        ("tier1_user", 0.3),
    ),
    "resume": (
        ("active_plan", 0.2),
        ("anchors", 0.15),
        ("last_session", 0.15),
        ("snapshot", 0.2),
        ("constraints", 0.15),
        ("learned", 0.15),
    ),
    "review": (
        ("constraints", 0.3),
        ("learned", 0.3),
        ("tier1_user", 0.25),
        ("notes", 0.15),
    ),
    "pane": (
        ("constraints", 0.3),
        ("active_plan", 0.3),
        ("anchors", 0.2),
        ("last_session", 0.2),
    ),
}
