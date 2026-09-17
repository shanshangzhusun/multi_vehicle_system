import os
from pathlib import Path


def configure_manual_debug(enabled: bool) -> None:
    os.environ["MVS_MANUAL_DEBUG_ENABLED"] = "1" if enabled else "0"


def manual_debug_enabled() -> bool:
    return os.environ.get("MVS_MANUAL_DEBUG_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def _append(path: str, msg: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.open("a", encoding="utf-8").write(str(msg) + "\n")


def debug_append_log(msg, path=".run/logs/manual_debug.log"):
    if path == ".run/logs/manual_debug.log" and not manual_debug_enabled():
        return
    text = str(msg)
    if path == ".run/logs/manual_debug.log" and (
        text.startswith("[transport]")
        or text.startswith("[VehicleGateway]")
        or text.startswith("GW9190_COUNT")
    ):
        _append(".run/logs/transport_debug.log", text)
        lowered = text.lower()
        if any(token in lowered for token in ("error", "failed", "conflict", "skipped")):
            _append(path, text)
        return
    _append(path, text)
