"""Opt-in, non-blocking diagnostics for graph execution lifecycle events."""

from __future__ import annotations

import atexit
import faulthandler
import itertools
import json
import os
import queue
import signal
import threading
import time
import uuid
from pathlib import Path
from typing import Any, TextIO

from graphon.diagnostics_config import DiagnosticsConfig

_SESSION_ID = uuid.uuid4().hex
_PID = os.getpid()
_SEQUENCE = itertools.count()
_STOP = object()
_DEFAULT_QUEUE_SIZE = 10_000
_DEFAULT_FLUSH_TIMEOUT = 0.5

_STATE: dict[str, Any] = {
    "queue": None,
    "file": None,
    "writer": None,
    "dropped": 0,
    "registered_signal": None,
    "stack_file": None,
    "flush_timeout": _DEFAULT_FLUSH_TIMEOUT,
}


def thread_fields() -> dict[str, Any]:
    current = threading.current_thread()
    return {
        "thread_name": current.name,
        "thread_ident": current.ident,
        "thread_native_id": current.native_id,
    }


def trace(event_type: str, **fields: Any) -> None:
    destination: queue.Queue[dict[str, Any] | object] | None = _STATE["queue"]
    if destination is None:
        return

    record: dict[str, Any] = {
        "session_id": _SESSION_ID,
        "pid": _PID,
        "sequence": next(_SEQUENCE),
        "monotonic_ns": time.monotonic_ns(),
        "timestamp": time.time(),
        "event": event_type,
        **thread_fields(),
        **fields,
    }
    try:
        destination.put_nowait(record)
    except queue.Full:
        _STATE["dropped"] += 1


def configure(
    file_path: str,
    *,
    signal_name: str | None = None,
    queue_size: int = _DEFAULT_QUEUE_SIZE,
    flush_timeout: float = _DEFAULT_FLUSH_TIMEOUT,
) -> None:
    if _STATE["queue"] is not None:
        shutdown()

    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    state_file: TextIO = path.open("a", encoding="utf-8", buffering=1)
    destination: queue.Queue[dict[str, Any] | object] = queue.Queue(maxsize=queue_size)

    _STATE.update(
        {
            "queue": destination,
            "file": state_file,
            "dropped": 0,
            "flush_timeout": flush_timeout,
        },
    )

    trace("trace_configured", file=str(path), queue_size=queue_size)

    writer = threading.Thread(
        target=_write_loop,
        args=(destination, state_file),
        name="GraphonDiagnosticsWriter",
        daemon=True,
    )
    _STATE["writer"] = writer
    writer.start()

    if signal_name is not None:
        _register_signal_dump(path, signal_name)

    atexit.register(shutdown)


def shutdown() -> None:
    destination: queue.Queue[dict[str, Any] | object] | None = _STATE["queue"]
    if destination is None:
        return

    dropped = _STATE["dropped"]
    if dropped:
        destination.put_nowait(
            {
                "event": "trace_incomplete",
                "dropped": dropped,
                "session_id": _SESSION_ID,
                "pid": _PID,
            },
        )

    destination.put_nowait(_STOP)
    writer: threading.Thread | None = _STATE["writer"]
    if writer is not None:
        writer.join(timeout=_STATE["flush_timeout"])

    registered_signal = _STATE["registered_signal"]
    if registered_signal is not None:
        faulthandler.unregister(registered_signal)

    stack_file: TextIO | None = _STATE["stack_file"]
    if stack_file is not None:
        stack_file.close()

    state_file: TextIO | None = _STATE["file"]
    if state_file is not None:
        state_file.close()

    _STATE.update(
        {
            "queue": None,
            "file": None,
            "writer": None,
            "dropped": 0,
            "registered_signal": None,
            "stack_file": None,
            "flush_timeout": _DEFAULT_FLUSH_TIMEOUT,
        },
    )


def enable_from_env() -> None:
    config = DiagnosticsConfig.from_env()
    if not config.GRAPHON_TRACE_FILE:
        return
    configure(
        config.GRAPHON_TRACE_FILE,
        signal_name=config.GRAPHON_TRACE_SIGNAL or None,
        queue_size=config.GRAPHON_TRACE_QUEUE_SIZE,
        flush_timeout=config.GRAPHON_TRACE_FLUSH_TIMEOUT,
    )


def _write_loop(
    destination: queue.Queue[dict[str, Any] | object], output: TextIO
) -> None:
    while True:
        item = destination.get()
        if item is _STOP:
            return
        if not isinstance(item, dict):
            continue
        try:
            output.write(
                json.dumps(item, ensure_ascii=False, default=_safe_value) + "\n"
            )
            output.flush()
        except (OSError, TypeError, ValueError):
            continue


def _safe_value(value: Any) -> str:
    return f"<{type(value).__name__}>"


def _register_signal_dump(path: Path, name: str) -> None:
    registered_signal = getattr(signal, name, None)
    if not isinstance(registered_signal, signal.Signals):
        trace("dump_register_failed", signal_name=name, reason="unknown_signal")
        return
    if signal.getsignal(registered_signal) is not signal.SIG_DFL:
        trace("dump_register_failed", signal_name=name, reason="handler_occupied")
        return

    stack_path = path.with_suffix(".stacks.txt")
    stack_file: TextIO = stack_path.open("a", encoding="utf-8", buffering=1)
    faulthandler.register(registered_signal, file=stack_file, all_threads=True)
    _STATE["registered_signal"] = registered_signal
    _STATE["stack_file"] = stack_file
    trace("dump_registered", signal_name=name, file=str(stack_path))


enable_from_env()
