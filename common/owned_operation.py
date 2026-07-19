"""Bound synchronous calls without giving ownership to asyncio's executor."""

import asyncio
import concurrent.futures
import queue
import threading


class OperationUnconfirmed(BaseException):
    """The deadline elapsed while the operation may still own resources."""


class CancellationUnconfirmed(asyncio.CancelledError):
    """The caller was cancelled before the operation confirmed it stopped."""


_OWNER_UNCONFIRMED_ATTRIBUTE = "_claude_owner_unconfirmed"


def record_current_owner_unconfirmed(outcome):
    """Attach an ownership outcome before asyncio collapses cancellation type."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is not None:
        setattr(task, _OWNER_UNCONFIRMED_ATTRIBUTE, outcome)
    return outcome


def task_owner_unconfirmed(task):
    return getattr(task, _OWNER_UNCONFIRMED_ATTRIBUTE, None)


def raise_owner_unconfirmed(outcome):
    record_current_owner_unconfirmed(outcome)
    raise outcome


def _consume_future(future):
    if future.cancelled():
        return
    try:
        future.result()
    except BaseException:
        pass


class SerialDaemonCallOwner:
    """Serialize calls on a daemon thread that cannot delay asyncio shutdown."""

    _STOP = object()

    def __init__(self, name):
        self._queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            name=name,
            daemon=True,
        )
        self._thread.start()

    def _run(self):
        while True:
            item = self._queue.get()
            if item is self._STOP:
                return
            future, operation = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = operation()
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

    def submit(self, operation):
        future = concurrent.futures.Future()
        future.add_done_callback(_consume_future)
        self._queue.put((future, operation))
        return future

    async def _confirm_stopped(self, future, wrapped, grace):
        if future.done() or future.cancel():
            return True
        wait = max(0.0, float(grace))
        if wait <= 0:
            return future.done()
        done, _pending = await asyncio.wait({wrapped}, timeout=wait)
        return wrapped in done or future.done()

    async def wait(
        self,
        future,
        timeout,
        *,
        on_cancel=None,
        cancel_grace=0.0,
    ):
        wrapped = asyncio.wrap_future(future)
        try:
            done, _pending = await asyncio.wait(
                {wrapped}, timeout=max(0.0, float(timeout))
            )
        except asyncio.CancelledError:
            if on_cancel is not None:
                on_cancel()
            try:
                confirmed = await self._confirm_stopped(
                    future, wrapped, cancel_grace
                )
            except asyncio.CancelledError:
                if not future.done():
                    raise_owner_unconfirmed(CancellationUnconfirmed())
                raise
            if not confirmed:
                raise_owner_unconfirmed(CancellationUnconfirmed())
            raise
        if wrapped in done:
            return wrapped.result()

        if on_cancel is not None:
            on_cancel()
        try:
            confirmed = await self._confirm_stopped(
                future, wrapped, cancel_grace
            )
        except asyncio.CancelledError:
            if not future.done():
                raise_owner_unconfirmed(CancellationUnconfirmed())
            raise
        if not confirmed:
            raise_owner_unconfirmed(OperationUnconfirmed())
        raise asyncio.TimeoutError

    def stop(self):
        self._queue.put(self._STOP)


async def run_daemon_call(
    operation,
    timeout,
    *,
    name,
    on_cancel=None,
    cancel_grace=0.0,
):
    owner = SerialDaemonCallOwner(name)
    try:
        future = owner.submit(operation)
        return await owner.wait(
            future,
            timeout,
            on_cancel=on_cancel,
            cancel_grace=cancel_grace,
        )
    finally:
        owner.stop()
