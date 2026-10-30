"""Adapter registry. Adding a harness means adding one module here and one entry in ``LOCAL_STORES``.

Store discovery lives here as plain path logic so a query can list stores without
importing any adapter module; adapters are loaded only when a store is indexed.
"""

from __future__ import annotations

from pathlib import Path

from .base import Adapter, StoreSpec

HARNESSES = ("omp", "claude", "hermes", "cursor", "agy")


def get_adapter(harness: str) -> Adapter:
    if harness == "omp":
        from .omp import OmpAdapter

        return OmpAdapter()
    if harness == "claude":
        from .claude import ClaudeAdapter

        return ClaudeAdapter()
    if harness == "hermes":
        from .hermes import HermesAdapter

        return HermesAdapter()
    if harness == "cursor":
        from .cursor import CursorAdapter

        return CursorAdapter()
    if harness in ("agy", "antigravity"):
        from .agy import AgyAdapter

        return AgyAdapter()
    raise KeyError(f"unknown harness {harness!r}; known: {', '.join(HARNESSES)}")


def local_stores(cfg) -> list[StoreSpec]:
    """The stores each harness keeps on this machine, from config paths alone.

    Tolerates a config that lacks a harness's path (a partial config, an older machine).
    """
    found: list[StoreSpec] = []
    omp = getattr(cfg, "omp_sessions_dir", None)
    if omp:
        omp = Path(omp).expanduser()
        found.append(StoreSpec("omp", "omp", str(omp), omp.is_dir()))
    claude = getattr(cfg, "claude_projects_dir", None)
    if claude:
        claude = Path(claude).expanduser()
        found.append(StoreSpec("claude", "claude", str(claude), claude.is_dir()))
    cursor = getattr(cfg, "cursor_projects_dir", None)
    if cursor:
        cursor = Path(cursor).expanduser()
        found.append(StoreSpec("cursor", "cursor", str(cursor), cursor.is_dir(), metadata=str(getattr(cfg, "cursor_state_db", "") or "")))
    hermes = getattr(cfg, "hermes_dir", None)
    if hermes:
        hermes = Path(hermes).expanduser()
        banks = [("hermes", hermes / "state.db")]
        profiles = hermes / "profiles"
        if profiles.is_dir():
            banks.extend((f"hermes.{p.name}", p / "state.db") for p in sorted(profiles.iterdir()) if p.is_dir())
        found.extend(StoreSpec(name, "hermes", str(path), path.is_file()) for name, path in banks)
    agy = getattr(cfg, "agy_brain_dir", None)
    if agy:
        agy = Path(agy).expanduser()
        found.append(StoreSpec("agy", "agy", str(agy), agy.is_dir(), metadata=str(getattr(cfg, "agy_summaries_db", "") or "")))
    return found


def discover_stores(cfg) -> list[StoreSpec]:
    """Every store on this machine: local plus config extras, sorted by name."""
    found: dict[str, StoreSpec] = {spec.name: spec for spec in local_stores(cfg)}
    for spec in cfg.extra_stores:
        for expanded in expand_store(spec):
            found[expanded.name] = expanded
    return [found[name] for name in sorted(found)]


def expand_store(spec: StoreSpec) -> list[StoreSpec]:
    """A configured Hermes store may point at a mirrored home directory holding several banks.

    ``hermes.<host>`` with location ``~/mirrors/<host>/hermes`` becomes ``hermes.<host>``
    for ``state.db`` plus ``hermes.<host>.<profile>`` for each ``profiles/<profile>/state.db``.
    Every other store is returned unchanged.
    """
    location = Path(spec.location)
    if spec.harness != "hermes" or not location.is_dir():
        return [spec]
    banks = [(spec.name, location / "state.db")]
    profiles = location / "profiles"
    if profiles.is_dir():
        banks.extend((f"{spec.name}.{p.name}", p / "state.db") for p in sorted(profiles.iterdir()) if p.is_dir())
    return [StoreSpec(name, spec.harness, str(path), path.is_file(), spec.metadata) for name, path in banks]
