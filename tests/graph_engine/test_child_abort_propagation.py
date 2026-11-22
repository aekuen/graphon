from unittest.mock import MagicMock

from graphon.graph_engine.command_processing.command_handlers import (
    AbortCommandHandler,
)
from graphon.graph_engine.entities.commands import AbortCommand
from graphon.runtime.graph_runtime_state import GraphRuntimeState


def test_abort_children_reaches_registered_and_late_engines() -> None:
    state = GraphRuntimeState(variable_pool=MagicMock(), start_at=0.0)
    builder = MagicMock()
    early_engine = MagicMock()
    builder.build_child_engine.return_value = early_engine
    state.bind_child_engine_builder(builder)

    state.create_child_engine(
        workflow_id="wf",
        graph_init_params=MagicMock(),
        root_node_id="root",
    )
    state.abort_children("outer stop")

    early_engine.request_abort.assert_called_once_with("outer stop")

    late_engine = MagicMock()
    builder.build_child_engine.return_value = late_engine
    state.create_child_engine(
        workflow_id="wf",
        graph_init_params=MagicMock(),
        root_node_id="root",
    )

    late_engine.request_abort.assert_called_once_with("outer stop")


def test_abort_handler_invokes_child_fanout_callback() -> None:
    on_abort = MagicMock()
    handler = AbortCommandHandler(on_abort=on_abort)
    execution = MagicMock()

    handler.handle(AbortCommand(reason="outer stop"), execution)

    execution.abort.assert_called_once_with("outer stop")
    on_abort.assert_called_once_with("outer stop")
