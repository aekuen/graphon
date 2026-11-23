import logging
from collections.abc import Callable
from typing import final, override

from graphon.diagnostics import trace
from graphon.entities.pause_reason import SchedulingPause
from graphon.runtime.graph_runtime_state import GraphExecutionProtocol
from graphon.runtime.variable_pool import VariablePool

from ..entities.commands import (
    AbortCommand,
    PauseCommand,
    UpdateVariablesCommand,
)
from .command_processor import CommandHandler

logger = logging.getLogger(__name__)


@final
class AbortCommandHandler(CommandHandler[AbortCommand]):
    def __init__(self, on_abort: Callable[[str], None] | None = None) -> None:
        self._on_abort = on_abort

    @override
    def handle(
        self,
        command: AbortCommand,
        execution: GraphExecutionProtocol,
    ) -> None:
        reason = command.reason or "User requested abort"
        logger.debug("Aborting workflow %s: %s", execution.workflow_id, reason)
        trace(
            "abort_handler_enter",
            execution_id=id(execution),
            workflow_id=execution.workflow_id,
            has_propagation_callback=self._on_abort is not None,
        )
        execution.abort(reason)
        trace(
            "abort_state_set",
            execution_id=id(execution),
            aborted=getattr(execution, "aborted", None),
        )
        if self._on_abort is not None:
            try:
                self._on_abort(reason)
            except Exception as error:
                trace(
                    "abort_handler_failed",
                    execution_id=id(execution),
                    error_type=type(error).__name__,
                )
                raise
        trace("abort_handler_done", execution_id=id(execution))


@final
class PauseCommandHandler(CommandHandler[PauseCommand]):
    @override
    def handle(
        self,
        command: PauseCommand,
        execution: GraphExecutionProtocol,
    ) -> None:
        logger.debug("Pausing workflow %s: %s", execution.workflow_id, command.reason)
        # Convert string reason to PauseReason if needed
        reason = command.reason
        pause_reason = SchedulingPause(message=reason)
        execution.pause(pause_reason)


@final
class UpdateVariablesCommandHandler(CommandHandler[UpdateVariablesCommand]):
    def __init__(self, variable_pool: VariablePool) -> None:
        self._variable_pool = variable_pool

    @override
    def handle(
        self,
        command: UpdateVariablesCommand,
        execution: GraphExecutionProtocol,
    ) -> None:
        for update in command.updates:
            try:
                variable = update.value
                self._variable_pool.add(variable.selector, variable)
                logger.debug(
                    "Updated variable %s for workflow %s",
                    variable.selector,
                    execution.workflow_id,
                )
            except ValueError as exc:
                logger.warning(
                    "Skipping invalid variable selector %s for workflow %s: %s",
                    getattr(update.value, "selector", None),
                    execution.workflow_id,
                    exc,
                )
