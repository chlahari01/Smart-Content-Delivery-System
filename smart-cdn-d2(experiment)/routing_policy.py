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
    backend_load: Mapping[str, int] | None = None,
) -> tuple[str | None, int]:
    """Choose a live backend: fewest active transfers, round-robin on ties.

    Without load information this is plain round-robin over live backends.
    """
    live = [name for name in backends if backend_up.get(name, False)]
    if not live:
        return None, cursor
    load = backend_load or {}
    minimum = min(max(0, load.get(name, 0)) for name in live)
    candidates = [name for name in live if max(0, load.get(name, 0)) == minimum]
    selected = candidates[cursor % len(candidates)]
    return selected, cursor + 1


def parse_heartbeat(payload: bytes) -> tuple[str, int, bool] | None:
    """Parse a backend heartbeat datagram.

    ``HB <server-id> <active-transfers>`` -> (id, active, True)
    ``BYE <server-id>``                   -> (id, 0, False)  (clean shutdown)
    Anything else returns None.
    """
    try:
        fields = bytes(payload).decode("ascii").split()
    except UnicodeDecodeError:
        return None
    if len(fields) == 3 and fields[0] == "HB" and fields[2].isdigit():
        return fields[1], int(fields[2]), True
    if len(fields) == 2 and fields[0] == "BYE":
        return fields[1], 0, False
    return None


def heartbeat_fresh(last_seen: float | None, now: float, timeout: float) -> bool:
    """A backend process is alive if it reported within ``timeout`` seconds."""
    return last_seen is not None and 0 <= now - last_seen <= timeout
