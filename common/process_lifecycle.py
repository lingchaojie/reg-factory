import asyncio
import errno
import os
import subprocess
import sys
import time


PROCESS_SHUTDOWN_TIMEOUT = 5.0
POSIX_SIGTERM = 15
POSIX_SIGKILL = 9


def process_group_kwargs():
    if sys.platform == "win32":
        creation_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": creation_flag} if creation_flag else {}
    return {"start_new_session": True}


def _windows_taskkill_tree(pid, *, force, timeout):
    if sys.platform != "win32":
        return False
    try:
        numeric_pid = int(pid)
    except (TypeError, ValueError):
        return False
    if numeric_pid <= 0:
        return False
    command = ["taskkill", "/PID", str(numeric_pid), "/T"]
    if force:
        command.append("/F")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _windows_tree_required(process):
    if sys.platform != "win32":
        return False
    try:
        return int(getattr(process, "pid", None)) > 0
    except (TypeError, ValueError):
        return False


def _posix_group_id(process):
    if sys.platform == "win32":
        return None
    try:
        pid = int(getattr(process, "pid", None))
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _signal_posix_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _posix_group_exists(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _wait_posix_group_gone_sync(pgid, timeout):
    deadline = time.monotonic() + max(0.0, float(timeout))
    while _posix_group_exists(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
    return True


async def _wait_posix_group_gone_async(pgid, timeout):
    deadline = time.monotonic() + max(0.0, float(timeout))
    while _posix_group_exists(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.05, remaining))
    return True


def _wait_sync(process, timeout):
    try:
        process.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, TimeoutError, OSError):
        return False
    return True


def _shutdown_posix_group_sync(process, pgid, timeout):
    parent_stopped = getattr(process, "returncode", None) is not None
    _signal_posix_group(pgid, POSIX_SIGTERM)
    if not parent_stopped:
        parent_stopped = _wait_sync(process, timeout)
    group_stopped = _wait_posix_group_gone_sync(pgid, timeout)
    if parent_stopped and group_stopped:
        return True

    _signal_posix_group(pgid, POSIX_SIGKILL)
    if not parent_stopped:
        parent_stopped = _wait_sync(process, timeout)
    group_stopped = _wait_posix_group_gone_sync(pgid, timeout)
    return parent_stopped and group_stopped


def shutdown_sync_process(process, timeout=PROCESS_SHUTDOWN_TIMEOUT):
    pgid = _posix_group_id(process)
    if pgid is not None:
        return _shutdown_posix_group_sync(process, pgid, timeout)
    tree_required = _windows_tree_required(process)
    if getattr(process, "returncode", None) is not None:
        if not tree_required:
            return True
        return _windows_taskkill_tree(
            getattr(process, "pid", None), force=True, timeout=timeout
        )
    tree_signalled = _windows_taskkill_tree(
        getattr(process, "pid", None), force=False, timeout=timeout
    )
    if not tree_signalled:
        try:
            process.terminate()
        except (OSError, ProcessLookupError):
            pass
    process_stopped = _wait_sync(process, timeout)
    if process_stopped and (not tree_required or tree_signalled):
        return True

    tree_killed = _windows_taskkill_tree(
        getattr(process, "pid", None), force=True, timeout=timeout
    )
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass
    process_stopped = _wait_sync(process, timeout)
    return process_stopped and (not tree_required or tree_killed)


async def _wait_async(process, timeout):
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError, OSError):
        return False
    return True


async def _shutdown_posix_group_async(process, pgid, timeout):
    parent_stopped = getattr(process, "returncode", None) is not None
    _signal_posix_group(pgid, POSIX_SIGTERM)
    if not parent_stopped:
        parent_stopped = await _wait_async(process, timeout)
    group_stopped = await _wait_posix_group_gone_async(pgid, timeout)
    if parent_stopped and group_stopped:
        return True

    _signal_posix_group(pgid, POSIX_SIGKILL)
    if not parent_stopped:
        parent_stopped = await _wait_async(process, timeout)
    group_stopped = await _wait_posix_group_gone_async(pgid, timeout)
    return parent_stopped and group_stopped


async def shutdown_async_process(process, timeout=PROCESS_SHUTDOWN_TIMEOUT):
    pgid = _posix_group_id(process)
    if pgid is not None:
        return await _shutdown_posix_group_async(process, pgid, timeout)
    tree_required = _windows_tree_required(process)
    if getattr(process, "returncode", None) is not None:
        if not tree_required:
            return True
        return await asyncio.to_thread(
            _windows_taskkill_tree,
            getattr(process, "pid", None),
            force=True,
            timeout=timeout,
        )
    tree_signalled = await asyncio.to_thread(
        _windows_taskkill_tree,
        getattr(process, "pid", None),
        force=False,
        timeout=timeout,
    )
    if not tree_signalled:
        try:
            process.terminate()
        except (OSError, ProcessLookupError):
            pass
    process_stopped = await _wait_async(process, timeout)
    if process_stopped and (not tree_required or tree_signalled):
        return True

    tree_killed = await asyncio.to_thread(
        _windows_taskkill_tree,
        getattr(process, "pid", None),
        force=True,
        timeout=timeout,
    )
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass
    process_stopped = await _wait_async(process, timeout)
    return process_stopped and (not tree_required or tree_killed)
