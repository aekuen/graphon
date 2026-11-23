import json
import os
import signal
from pathlib import Path

import pytest

from graphon import DiagnosticsConfig, diagnostics


def test_trace_writes_jsonl_then_stops(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    diagnostics.shutdown()
    diagnostics.configure(str(path))
    try:
        diagnostics.trace("probe", value=1)
    finally:
        diagnostics.shutdown()

    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    events = {record["event"] for record in records}
    assert {"trace_configured", "probe"} <= events

    probe = next(record for record in records if record["event"] == "probe")
    assert probe["value"] == 1
    assert probe["pid"] == os.getpid()

    diagnostics.trace("after_shutdown")
    assert len(records) == len(
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()],
    )


def test_signal_dump_registration_is_recorded(tmp_path: Path) -> None:
    if signal.getsignal(signal.SIGUSR1) is not signal.SIG_DFL:
        pytest.skip("SIGUSR1 already occupied")

    path = tmp_path / "trace.jsonl"
    diagnostics.shutdown()
    diagnostics.configure(str(path), signal_name="SIGUSR1")
    diagnostics.shutdown()
    diagnostics.shutdown()

    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    events = {record["event"] for record in records}
    assert "dump_registered" in events
    assert "dump_register_failed" not in events


def test_diagnostics_config_reads_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trace_file = str(tmp_path / "graphon-trace.jsonl")
    monkeypatch.setenv("GRAPHON_TRACE_FILE", trace_file)
    monkeypatch.setenv("GRAPHON_TRACE_SIGNAL", "SIGUSR1")
    monkeypatch.setenv("GRAPHON_TRACE_QUEUE_SIZE", "123")
    monkeypatch.setenv("GRAPHON_TRACE_FLUSH_TIMEOUT", "1.5")

    config = DiagnosticsConfig.from_env()

    assert trace_file == config.GRAPHON_TRACE_FILE
    assert config.GRAPHON_TRACE_SIGNAL == "SIGUSR1"
    assert config.GRAPHON_TRACE_QUEUE_SIZE == 123
    assert pytest.approx(1.5) == config.GRAPHON_TRACE_FLUSH_TIMEOUT
