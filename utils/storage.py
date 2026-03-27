from __future__ import annotations

from pathlib import Path


def ensure_storage_layout(storage_path: Path) -> None:
    storage_path.mkdir(parents=True, exist_ok=True)
    (storage_path / "settings").mkdir(parents=True, exist_ok=True)
