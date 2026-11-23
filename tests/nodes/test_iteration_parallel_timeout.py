from concurrent.futures import Future
from threading import Lock
from typing import Any
from unittest.mock import MagicMock

import pytest

from graphon.model_runtime.entities.llm_entities import LLMUsage
from graphon.nodes.iteration import iteration_node as iteration_node_module
from graphon.nodes.iteration.entities import ErrorHandleMode
from graphon.nodes.iteration.exc import (
    ParallelIterationWaitTimeoutError,
    ParallelSiblingDrainTimeoutError,
)
from graphon.nodes.iteration.iteration_node import IterationNode

TraceRecord = tuple[str, dict[str, Any]]


def _record_trace(monkeypatch: pytest.MonkeyPatch) -> list[TraceRecord]:
    records: list[TraceRecord] = []
    monkeypatch.setattr(
        iteration_node_module,
        "trace",
        lambda event, **fields: records.append((event, fields)),
    )
    return records


def _running_future() -> Future[Any]:
    future: Future[Any] = Future()
    assert future.set_running_or_notify_cancel()
    return future


def test_sibling_drain_timeout_fails_without_merging_pending_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _record_trace(monkeypatch)
    node = object.__new__(IterationNode)
    current: Future[Any] = Future()
    current.set_result(None)
    pending = _running_future()
    engine = MagicMock()
    engine.graph_runtime_state.llm_usage = LLMUsage.from_metadata({"total_tokens": 99})
    usage_accumulator = [LLMUsage.empty_usage()]

    with pytest.raises(ParallelSiblingDrainTimeoutError) as exc_info:
        node._drain_parallel_siblings(
            future_to_index={current: -1, pending: 3},
            current_future=current,
            started_child_engines={3: engine},
            aborted_reasons={3: "child graph aborted"},
            usage_accumulator=usage_accumulator,
            merged_usage_indexes=set(),
            timeout_seconds=0.01,
        )

    assert exc_info.value.pending_indexes == [3]
    assert usage_accumulator[0].total_tokens == 0
    assert not any(
        event == "sibling_wait_end" and fields.get("iteration_index") == 3
        for event, fields in records
    )
    assert any(
        event == "sibling_wait_timeout" and fields.get("iteration_index") == 3
        for event, fields in records
    )
    assert any(event == "sibling_drain_timeout" for event, _ in records)
    assert not any(event == "sibling_drain_end" for event, _ in records)


def test_completed_sibling_usage_is_merged_before_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _record_trace(monkeypatch)
    node = object.__new__(IterationNode)
    current: Future[Any] = Future()
    current.set_result(None)
    completed: Future[Any] = Future()
    completed.set_result(None)
    pending = _running_future()

    completed_engine = MagicMock()
    completed_engine.graph_runtime_state.llm_usage = LLMUsage.from_metadata({
        "total_tokens": 5
    })
    pending_engine = MagicMock()
    pending_engine.graph_runtime_state.llm_usage = LLMUsage.from_metadata({
        "total_tokens": 99
    })
    usage_accumulator = [LLMUsage.empty_usage()]

    with pytest.raises(ParallelSiblingDrainTimeoutError):
        node._drain_parallel_siblings(
            future_to_index={current: -1, completed: 0, pending: 3},
            current_future=current,
            started_child_engines={0: completed_engine, 3: pending_engine},
            aborted_reasons={0: "child graph aborted", 3: "child graph aborted"},
            usage_accumulator=usage_accumulator,
            merged_usage_indexes=set(),
            timeout_seconds=0.01,
        )

    assert usage_accumulator[0].total_tokens == 5
    assert any(
        event == "sibling_wait_end" and fields.get("iteration_index") == 0
        for event, fields in records
    )
    assert not any(
        event == "sibling_wait_end" and fields.get("iteration_index") == 3
        for event, fields in records
    )


def test_abort_pending_parallel_iterations_records_late_reason() -> None:
    node = object.__new__(IterationNode)
    pending = _running_future()
    engine = MagicMock()
    aborted_reasons: dict[int, str] = {}

    node._abort_pending_parallel_iterations(
        pending_future_to_index={pending: 3},
        started_child_engines={3: engine},
        started_child_engines_lock=Lock(),
        aborted_reasons=aborted_reasons,
        reason="timeout",
    )

    engine.request_abort.assert_called_once_with("timeout")
    assert aborted_reasons == {3: "timeout"}


def test_timeout_env_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "GRAPHON_PARALLEL_SIBLING_DRAIN_TIMEOUT"
    monkeypatch.delenv(name, raising=False)
    assert iteration_node_module._timeout_from_env(name, 30.0) == pytest.approx(30.0)

    monkeypatch.setenv(name, "0.25")
    assert iteration_node_module._timeout_from_env(name, 30.0) == pytest.approx(0.25)

    for invalid in ("bad", "0", "-1", "inf"):
        monkeypatch.setenv(name, invalid)
        assert iteration_node_module._timeout_from_env(name, 30.0) == pytest.approx(
            30.0
        )


def test_parallel_wait_timeout_shuts_executor_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = object.__new__(IterationNode)
    node.init_node_data({
        "iterator_selector": ["#start.items"],
        "output_selector": ["#iteration.item"],
        "is_parallel": True,
        "parallel_nums": 1,
        "error_handle_mode": ErrorHandleMode.TERMINATED,
    })
    monkeypatch.setenv("GRAPHON_PARALLEL_ITERATION_WAIT_TIMEOUT", "0.01")

    class FakeExecutor:
        def __init__(self, max_workers: int) -> None:
            self.max_workers = max_workers
            self.pending = _running_future()
            self.shutdown_calls: list[tuple[bool, bool]] = []
            executor_instances.append(self)

        def submit(self, _fn: Any, **_kwargs: Any) -> Future[Any]:
            return self.pending

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            self.shutdown_calls.append((wait, cancel_futures))

    executor_instances: list[FakeExecutor] = []
    monkeypatch.setattr(iteration_node_module, "ThreadPoolExecutor", FakeExecutor)

    generator = node._execute_parallel_iterations(
        iterator_list_value=[1],
        outputs=[],
        iter_run_map={},
        usage_accumulator=[LLMUsage.empty_usage()],
    )
    next(generator)

    with pytest.raises(ParallelIterationWaitTimeoutError) as exc_info:
        next(generator)

    assert exc_info.value.pending_indexes == [0]
    assert executor_instances[0].shutdown_calls == [(False, True)]
