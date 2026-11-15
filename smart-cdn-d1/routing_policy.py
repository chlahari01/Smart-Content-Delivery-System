"""Pure routing decisions used by the SDN controller and unit tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def choose_path(
    path_up: Mapping[str, bool],
    path_load: Mapping[str, float],
    cursor: int = 0,
    tie_margin: float = 0.05,
) -> tuple[str | None, int]:
    """Choose a live, least-loaded path; rotate among near-equal candidates."""
    live = [name for name, is_up in path_up.items() if is_up]
    if not live:
        return None, cursor

    minimum = min(max(0.0, path_load.get(name, 0.0)) for name in live)
    candidates = [
        name for name in live
        if max(0.0, path_load.get(name, 0.0)) <= minimum + tie_margin
    ]
    selected = candidates[cursor % len(candidates)]
    return selected, cursor + 1


def choose_backend(
    backends: Sequence[str],
    backend_up: Mapping[str, bool],
    cursor: int = 0,
) -> tuple[str | None, int]:
    """Choose the next live backend in stable round-robin order."""
    live = [name for name in backends if backend_up.get(name, False)]
    if not live:
        return None, cursor
    selected = live[cursor % len(live)]
    return selected, cursor + 1
