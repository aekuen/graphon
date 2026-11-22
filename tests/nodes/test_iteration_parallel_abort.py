from concurrent.futures import Future
from threading import Lock
from unittest.mock import MagicMock

import pytest

from graphon.model_runtime.entities.llm_entities import LLMUsage
from graphon.nodes.iteration.iteration_node import IterationNode


def test_abort_is_replayed_for_engine_registered_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = object.__new__(IterationNode)
    registered_engine = MagicMock()
    late_engine = MagicMock()
    engines = {1: registered_engine, 2: late_engine}

    monkeypatch.setattr(
        node, "_create_graph_engine", lambda index, _item: engines[index]
    )
    monkeypatch.setattr(
        node,
        "_execute_parallel_iteration_with_graph_engine",
        lambda **_kwargs: (0.0, [], None, LLMUsage.empty_usage()),
    )

    current_future = Future()
    registered_future = Future()
    pending_future = Future()
    pending_future.set_running_or_notify_cancel()
    future_to_index = {
        current_future: 0,
        registered_future: 1,
        pending_future: 2,
    }

    started_child_engines = {1: registered_engine}
    started_child_engines_lock = Lock()
    aborted_reasons: dict[int, str] = {}

    node._abort_parallel_siblings(
        future_to_index=future_to_index,
        current_future=current_future,
        started_child_engines=started_child_engines,
        started_child_engines_lock=started_child_engines_lock,
        aborted_reasons=aborted_reasons,
        reason="stop sibling",
    )

    node._execute_tracked_iteration_parallel(
        index=2,
        item="item-2",
        started_child_engines=started_child_engines,
        started_child_engines_lock=started_child_engines_lock,
        aborted_reasons=aborted_reasons,
    )

    registered_engine.request_abort.assert_called_once_with("stop sibling")
    late_engine.request_abort.assert_called_once_with("stop sibling")
    assert aborted_reasons == {1: "stop sibling", 2: "stop sibling"}


def test_non_cancelled_sibling_usage_is_still_merged() -> None:
    node = object.__new__(IterationNode)
    current = MagicMock()
    sibling = MagicMock(spec=Future)
    sibling.cancelled.return_value = False
    sibling.result.return_value = None
    engine = MagicMock()
    engine.graph_runtime_state.llm_usage = LLMUsage.from_metadata({"total_tokens": 7})
    usage_accumulator = [LLMUsage.empty_usage()]

    node._drain_parallel_siblings(
        future_to_index={current: -1, sibling: 0},
        current_future=current,
        started_child_engines={0: engine},
        usage_accumulator=usage_accumulator,
        merged_usage_indexes=set(),
    )

    sibling.result.assert_called_once()
    assert usage_accumulator[0].total_tokens == 7
