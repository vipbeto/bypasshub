import sys
import asyncio
import logging
import inspect
from typing import Self
from signal import Signals, SIGINT, SIGTERM
from collections.abc import Callable

logger = logging.getLogger(__name__)


class Cleanup:
    """
    Clean up handler that runs registered tasks on application
    termination by the `SIGINT` or `SIGTERM` signals.
    """

    __instance = None
    __callbacks = set()

    def __new__(cls) -> Self:
        if Cleanup.__instance is None:
            Cleanup.__instance = super().__new__(cls)
            Cleanup.__instance._is_cleaning = None
            Cleanup.__instance._listen()
        return Cleanup.__instance

    async def _handler(self, signal: Signals) -> None:
        if self._is_cleaning is None:
            self._is_cleaning = True

            callbacks = []
            async_callbacks = []
            for callback in self.__callbacks:
                (
                    async_callbacks
                    if inspect.iscoroutinefunction(callback)
                    or inspect.isawaitable(callback)
                    else callbacks
                ).append(callback)

            if callbacks or async_callbacks:
                message = "Waiting for the scheduled tasks to finish"
                if signal == SIGINT:
                    message += " (Ctrl+C to skip)"
                logger.info(message)

                if callbacks:
                    for callback in callbacks:
                        callback()
                if async_callbacks:
                    await asyncio.gather(*[callback() for callback in async_callbacks])

                logger.debug("The scheduled tasks are finished successfully")

            self._is_cleaning = False
            sys.exit(0)
        else:
            if self._is_cleaning is not False:
                logger.warning("The pending tasks are cancelled")
            sys.exit(signal)

    def _listen(self) -> None:
        # For an unknown reason, storing the event loop inside a variable and
        # access it inside a for-loop will override the first signal handler!
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(
            SIGINT, lambda: asyncio.create_task(self._handler(SIGINT))
        )
        loop.add_signal_handler(
            SIGTERM, lambda: asyncio.create_task(self._handler(SIGTERM))
        )

    @staticmethod
    def add(callback: Callable) -> None:
        """Adds a callback function to the scheduled tasks."""
        Cleanup.__callbacks.add(callback)

    @staticmethod
    def remove(callback: Callable) -> None:
        """Removes a callback function from the scheduled tasks."""
        try:
            Cleanup.__callbacks.remove(callback)
        except KeyError:
            pass

    @property
    def is_cleaning(self) -> bool:
        """Whether the clean up process is running at the current time."""
        return bool(self._is_cleaning)
