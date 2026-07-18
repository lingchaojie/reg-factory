import asyncio
import subprocess
import sys


PROCESS_SHUTDOWN_TIMEOUT = 5.0


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


def _wait_sync(process, timeout):
    try:
        process.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, TimeoutError, OSError):
        return False
    return True


def shutdown_sync_process(process, timeout=PROCESS_SHUTDOWN_TIMEOUT):
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


async def shutdown_async_process(process, timeout=PROCESS_SHUTDOWN_TIMEOUT):
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
