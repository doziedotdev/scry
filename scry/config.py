"""Configuration: where transcripts live, where the index lives, which stores are searched by default.

``${XDG_CONFIG_HOME:-~/.config}/scry/config.toml``::

    [defaults]
    budget = 4000                       # output ceiling in characters
    exclude_stores = ["hermes.work"]  # indexed, but only searched when named with store=
    # default_stores = ["omp", "claude"] # allowlist form; omit to search everything not excluded

    [stores."omp.other"]                # an extra store, e.g. a synced mirror of another machine
    harness = "omp"
    location = "~/mirrors/other/.omp/agent/sessions"

``REAL_HOME`` takes precedence over ``HOME`` so the tool finds the user's transcripts
when run under a different home.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .model import StoreSpec

DEFAULT_BUDGET = 4000
MAX_BUDGET = 60_000


@dataclass(frozen=True, slots=True)
class Config:
    home: Path
    config_path: Path
    index_path: Path
    export_dir: Path
    budget: int = DEFAULT_BUDGET
    default_stores: tuple[str, ...] | None = None
    exclude_stores: tuple[str, ...] = ()
    extra_stores: tuple[StoreSpec, ...] = ()
    claude_projects_dir: Path = field(default_factory=Path)
    cursor_projects_dir: Path = field(default_factory=Path)
    cursor_state_db: Path = field(default_factory=Path)
    omp_sessions_dir: Path = field(default_factory=Path)
    hermes_dir: Path = field(default_factory=Path)
    agy_brain_dir: Path = field(default_factory=Path)
    agy_summaries_db: Path = field(default_factory=Path)

    def searched_by_default(self, name: str) -> bool:
        if name in self.exclude_stores:
            return False
        return self.default_stores is None or name in self.default_stores


_TRANSCRIPT_DIRS = (".claude/projects", ".omp/agent/sessions", ".cursor/projects", ".hermes/state.db", ".gemini/antigravity/brain")


def resolve_home() -> Path:
    """The home that holds the transcripts.

    ``REAL_HOME`` wins. Otherwise ``HOME``, unless it holds no transcript store at all
    while the account's home (from the password database) does: harness profiles run
    with ``HOME`` redirected into a profile directory, and indexing that would produce
    an empty index and a false "nothing ever happened".
    """
    real = os.environ.get("REAL_HOME")
    if real:
        return Path(real).expanduser()
    home = Path(os.environ.get("HOME") or Path.home())
    if any((home / rel).exists() for rel in _TRANSCRIPT_DIRS):
        return home
    try:
        import pwd

        account = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        return home
    if account != home and any((account / rel).exists() for rel in _TRANSCRIPT_DIRS):
        return account
    return home


def _expand(value: str, home: Path) -> Path:
    text = value.strip()
    if text.startswith("~"):
        return home / text[2:] if text.startswith("~/") else home
    return Path(text)


def load_config(config_path: Path | None = None, home: Path | None = None) -> Config:
    home = home or resolve_home()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    path = config_path or config_home / "scry" / "config.toml"
    data: dict = {}
    if path.is_file():
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    defaults = data.get("defaults", {}) if isinstance(data.get("defaults"), dict) else {}
    budget = int(defaults.get("budget", DEFAULT_BUDGET))
    default_stores = defaults.get("default_stores")
    exclude_stores = defaults.get("exclude_stores", ())
    extra: list[StoreSpec] = []
    stores = data.get("stores", {})
    if isinstance(stores, dict):
        for name, spec in stores.items():
            if not isinstance(spec, dict) or "harness" not in spec or "location" not in spec:
                continue
            location = _expand(str(spec["location"]), home)
            metadata = _expand(str(spec["metadata"]), home) if spec.get("metadata") else None
            extra.append(StoreSpec(str(name), str(spec["harness"]), str(location), location.exists(), metadata=str(metadata) if metadata else ""))
    return Config(
        home=home,
        config_path=path,
        index_path=cache_home / "scry" / "index.sqlite3",
        export_dir=cache_home / "scry" / "exports",
        budget=max(200, min(MAX_BUDGET, budget)),
        default_stores=tuple(str(s) for s in default_stores) if isinstance(default_stores, list) else None,
        exclude_stores=tuple(str(s) for s in exclude_stores) if isinstance(exclude_stores, list) else (),
        extra_stores=tuple(extra),
        claude_projects_dir=Path(os.environ.get("CLAUDE_PROJECTS_DIR") or home / ".claude" / "projects"),
        cursor_projects_dir=home / ".cursor" / "projects",
        cursor_state_db=home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb",
        omp_sessions_dir=home / ".omp" / "agent" / "sessions",
        hermes_dir=home / ".hermes",
        agy_brain_dir=home / ".gemini" / "antigravity" / "brain",
        agy_summaries_db=home / ".gemini" / "antigravity" / "conversation_summaries.db",
    )


def ensure_private_dirs(cfg: Config) -> None:
    for directory in (cfg.index_path.parent, cfg.export_dir):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
