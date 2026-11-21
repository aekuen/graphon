import queue
import threading
from datetime import UTC, datetime
from time import time
from typing import cast
from unittest.mock import MagicMock

import pytest

from graphon.enums import BuiltinNodeTypes
from graphon.graph_engine.command_channels.redis_channel import RedisChannel
from graphon.graph_engine.entities.commands import (
    AbortCommand,
    CommandType,
    PauseCommand,
    UpdateVariablesCommand,
)
from graphon.graph_engine.event_management import EventManager
from graphon.graph_engine.event_management.event_handlers import EventHandler
from graphon.graph_engine.layers.execution_limits import (
    ExecutionLimitsLayer,
    LimitType,
)
from graphon.graph_engine.orchestration.dispatcher import Dispatcher
from graphon.graph_engine.orchestration.execution_coordinator import (
    ExecutionCoordinator,
)
from graphon.graph_engine.ready_queue.factory import create_ready_queue_from_state
from graphon.graph_engine.ready_queue.protocol import ReadyQueueState
from graphon.graph_events.base import GraphNodeEventBase
from graphon.graph_events.node import NodeRunStartedEvent, NodeRunStreamChunkEvent


@pytest.mark.parametrize(
    ("payload", "expected_command_type"),
    [
        (
            {"command_type": CommandType.ABORT.value, "reason": "stop"},
            AbortCommand,
        ),
        (
            {"command_type": CommandType.PAUSE.value, "reason": "wait"},
            PauseCommand,
        ),
        (
            {"command_type": CommandType.UPDATE_VARIABLES.value, "updates": []},
            UpdateVariablesCommand,
        ),
    ],
)
def test_redis_channel_deserializes_command_with_model_map(
    payload: dict[str, object],
    expected_command_type: type,
) -> None:
    channel = RedisChannel(redis_client=MagicMock(), channel_key="test-channel")

    command = channel.deserialize_command(payload)

    assert isinstance(command, expected_command_type)


def test_create_ready_queue_from_state_restores_queue_items() -> None:
    queue = create_ready_queue_from_state(
        ReadyQueueState(type="InMemoryReadyQueue", version="1.0", items=["a", "b"]),
    )

    assert queue.get(timeout=0.01) == "a"
    assert queue.get(timeout=0.01) == "b"


@pytest.mark.parametrize(
    ("limit_type", "expected_reason"),
    [
        (LimitType.STEP_LIMIT, "Maximum execution steps exceeded: 4 > 3"),
        (LimitType.TIME_LIMIT, "Maximum execution time exceeded:"),
    ],
)
def test_execution_limits_layer_builds_abort_reason_with_match_case(
    limit_type: LimitType,
    expected_reason: str,
) -> None:
    layer = ExecutionLimitsLayer(max_steps=3, max_time=10)
    layer.command_channel = MagicMock()
    layer.on_graph_start()
    layer.step_count = 4
    layer.start_time = time() - 20

    layer.send_abort_command(limit_type)

    abort_command = layer.command_channel.send_command.call_args.args[0]
    assert isinstance(abort_command, AbortCommand)
    assert abort_command.reason is not None
    assert abort_command.reason.startswith(expected_reason)


def test_execution_limits_layer_matches_subclassed_node_start_event() -> None:
    class CustomNodeRunStartedEvent(NodeRunStartedEvent):
        pass

    layer = ExecutionLimitsLayer(max_steps=3, max_time=10)
    layer.on_graph_start()

    layer.on_event(
        CustomNodeRunStartedEvent(
            id="node-run-1",
            node_id="node-1",
            node_type=BuiltinNodeTypes.CODE,
            node_title="Code",
            start_at=datetime.now(UTC).replace(tzinfo=None),
        ),
    )

    assert layer.step_count == 1


@pytest.mark.parametrize("stream_events", [False, True])
def test_dispatcher_processes_commands_without_terminal_events(
    stream_events: bool,
) -> None:
    completed = threading.Event()

    class FakeCoordinator:
        def __init__(self) -> None:
            self.aborted = False
            self.execution_complete = False
            self.paused = False
            self.process_calls = 0

        def process_commands(self) -> None:
            self.process_calls += 1
            if self.process_calls >= 2:
                self.aborted = True

        def check_scaling(self) -> None:
            return

        def mark_complete(self) -> None:
            return

        def mark_failed(self, error: Exception) -> None:
            raise AssertionError(error) from error

    event_queue: queue.Queue[GraphNodeEventBase] | MagicMock
    if stream_events:
        stream_event = NodeRunStreamChunkEvent(
            id="node-run-1",
            node_id="node-1",
            node_type=BuiltinNodeTypes.CODE,
            selector=["node-1", "text"],
            chunk="chunk",
        )
        event_queue = MagicMock()

        def fake_get(*_args: object, **kwargs: object) -> GraphNodeEventBase:
            if kwargs.get("block") is False:
                raise queue.Empty
            return stream_event

        event_queue.get.side_effect = fake_get
    else:
        event_queue = queue.Queue()

    coordinator = FakeCoordinator()
    event_emitter = MagicMock()
    event_emitter.mark_complete.side_effect = completed.set
    dispatcher = Dispatcher(
        event_queue=cast(queue.Queue[GraphNodeEventBase], event_queue),
        event_handler=cast(EventHandler, MagicMock()),
        execution_coordinator=cast(ExecutionCoordinator, coordinator),
        event_emitter=cast(EventManager, event_emitter),
    )

    dispatcher.start()
    try:
        assert completed.wait(timeout=2)
        assert coordinator.process_calls >= 2
    finally:
        dispatcher.stop()
