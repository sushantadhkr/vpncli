from __future__ import annotations

import json
from typing import Any, Dict, Optional

from . import paths


def _load() -> Dict[str, Any]:
    try:
        data = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get(key: str, default: Optional[Any] = None) -> Optional[Any]:
    return _load().get(key, default)


def put(key: str, value: Optional[Any]) -> None:
    data = _load()
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    paths.ensure_data_dirs()
    paths.write_private(paths.settings_file(), json.dumps(data, indent=2))
