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
    """
    验证 Dispatcher 在没有终态事件时也能走到收尾流程并通知事件层完成.

    覆盖两条命令触发路径:
    - 事件队列为空
    - 队列里只有 NodeRunStreamChunkEvent, 不是终态事件

    without_terminal_events 指没有终态事件:
    NodeRunSucceededEvent / NodeRunFailedEvent / NodeRunExceptionEvent.
    """
    # 用作 dispatcher 线程与主线程之间的完成信号, 新建时为 unset。
    completed = threading.Event()

    # 测试替身: 只记录命令处理次数, 第二次起置 aborted 让 dispatcher 退出循环。
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
            # 异常路径不应被触发, 触发则立即暴露。
            raise AssertionError(error) from error

    # 两种队列形态分别覆盖: stream chunk 与 queue.Empty 两条命令触发路径。
    event_queue: queue.Queue[GraphNodeEventBase] | MagicMock
    if stream_events:
        # 只有流式事件, 没有终态事件; 它仍属于命令触发事件。
        stream_event = NodeRunStreamChunkEvent(
            id="node-run-1",
            node_id="node-1",
            node_type=BuiltinNodeTypes.CODE,
            selector=["node-1", "text"],
            chunk="chunk",
        )
        event_queue = MagicMock()

        def fake_get(*_args: object, **kwargs: object) -> GraphNodeEventBase:
            # 阻塞获取返回流式事件; 排空队列时的非阻塞获取返回 Empty。
            if kwargs.get("block") is False:
                raise queue.Empty
            return stream_event

        event_queue.get.side_effect = fake_get
    else:
        # 空队列: 依靠 queue.Empty 分支驱动命令处理。
        event_queue = queue.Queue()

    coordinator = FakeCoordinator()
    event_emitter = MagicMock()
    # side_effect 在被 mock 方法调用时执行:
    # 置位 completed 表示 dispatcher 已走到 finally。
    # 注意它改变的是 Event 状态, mark_complete() 返回值仍是 None。
    event_emitter.mark_complete.side_effect = completed.set
    # 注入替身; cast 只为满足静态类型, 不改变运行时对象。
    dispatcher = Dispatcher(
        event_queue=cast(queue.Queue[GraphNodeEventBase], event_queue),
        event_handler=cast(EventHandler, MagicMock()),
        execution_coordinator=cast(ExecutionCoordinator, coordinator),
        event_emitter=cast(EventManager, event_emitter),
    )

    dispatcher.start()
    try:
        # 等待 finally 中的完成通知; 超时说明 dispatcher 没有正常收尾。
        assert completed.wait(timeout=2)
        # 确认命令处理至少被驱动两次: 第二次置 aborted, 之后还有一次收尾调用。
        # stream_events=False: 循环前调用一次, 计数为 1.
        # 第一次 while 迭代走 queue.Empty 分支, 计数从 1 变 2, 并置 aborted=True.
        # 第二次 while 迭代在顶部读到 aborted 后直接 break, 不再走 queue.Empty 分支.
        # 循环结束后第 108 行再调用一次收尾, 计数变成 3.
        assert coordinator.process_calls >= 2
    finally:
        # 即使断言失败也停止后台线程。
        dispatcher.stop()
