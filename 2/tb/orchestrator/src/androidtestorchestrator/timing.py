import asyncio
import logging

from abc import ABC, abstractmethod


log = logging.getLogger(__name__)


class StopWatch(ABC):
    """
    Interface for marking start and end positions (for example, of a test)
    """

    @abstractmethod
    def mark_start(self, name: str)->None:
        """
        mark start of some activity
        :param name:  name of activity
        """

    @abstractmethod
    def mark_end(self, name: str) -> None:
        """
        mark end of activity
        :param name: name of activity
        """


class Timer(StopWatch):
    """
    A one-time timer used to raise exception if a task is taking too long, viable only in the context
    of a running asyncio loop (in other words, mark_start and mark_end are expected to be called
    within a running EventLoop)

    This is helpful when the start and stop indicators are from output of a command running on a remote device,
    where typical direct asyncio timer logic inline to the code isn't applicable
    """

    def __init__(self, duration: float) -> None:
        """
        :param duration: duration at end of which timer will expire and raise an `asyncior.TimeoutError`
        """
        self._future = asyncio.get_event_loop().create_future()
        self._timeout = duration
        self._task = None

    def mark_end(self, name: str) -> None:
        """
        Mark the end of an activity and cancel timer
        :param name: name associated with the activity
        """
        if self._future is None or self._task is None:
            raise Exception("Internal error: marking end without a start?!")
        self._future.set_result(True)
        self._task.cancel()

    def mark_start(self, name: str) -> None:
        """
        Mark the start of an activity by creating a timer (within the context of a running event loop)
        :param name: name associated with the activity
        """
        async def timer():
            try:
                await asyncio.wait_for(self._future, timeout=self._timeout)
            except asyncio.TimeoutError:
                log.error("Task %s timed out" % name)
                asyncio.get_event_loop().stop()
            self._future = None  # timer is used up

        # in principle, a loop is alread running, so just add another task to process:
        self._task = asyncio.get_event_loop().create_task(timer())
