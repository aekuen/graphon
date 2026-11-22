import queue
from collections.abc import Generator
from typing import cast
from unittest.mock import MagicMock

from graphon.graph.graph import Graph
from graphon.graph_engine.domain.graph_execution import GraphExecution
from graphon.graph_engine.worker import Worker
from graphon.graph_events.node import NodeRunStreamChunkEvent


def test_worker_stops_consuming_node_events_after_abort() -> None:
    execution = GraphExecution(workflow_id="wf")
    execution.start()
    first = MagicMock(spec=NodeRunStreamChunkEvent)
    second = MagicMock(spec=NodeRunStreamChunkEvent)
    node = MagicMock()
    node.execution_id = "exec-1"

    def run() -> Generator[MagicMock, None, None]:
        yield first
        execution.abort("stop")
        yield second

    node.run.side_effect = run
    event_queue: queue.Queue = queue.Queue()
    worker = Worker(
        ready_queue=MagicMock(),
        event_queue=event_queue,
        graph=cast(Graph, MagicMock()),
        layers=[],
        graph_execution=execution,
    )

    worker._execute_node(node)

    assert list(event_queue.queue) == [first]


def test_worker_does_not_dequeue_next_node_after_abort() -> None:
    execution = GraphExecution(workflow_id="wf")
    execution.start()
    execution.abort("stop")
    ready_queue = MagicMock()
    worker = Worker(
        ready_queue=ready_queue,
        event_queue=queue.Queue(),
        graph=cast(Graph, MagicMock()),
        layers=[],
        graph_execution=execution,
    )

    worker.run()

    ready_queue.get.assert_not_called()
