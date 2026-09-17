from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict

from mvs.common.models import utc_now_iso


class EventLogger:
    def __init__(self, path: str, node_id: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.node_id = node_id
        self._lock = threading.Lock()

    def log(self, event: str, **fields: Any) -> None:
        row: Dict[str, Any] = {"ts": utc_now_iso(), "node_id": self.node_id, "event": event}
        row.update(fields)
        line = json.dumps(row, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
