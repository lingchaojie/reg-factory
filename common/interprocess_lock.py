"""Small cross-platform advisory file lock used by durable local ledgers."""

from pathlib import Path
import errno
import os
import threading
import time


_THREAD_LOCKS = {}
_THREAD_LOCKS_GUARD = threading.Lock()


_WINDOWS_LOCK_CONTENTION_ERRNOS = {
    errno.EACCES,
    errno.EAGAIN,
    getattr(errno, "EDEADLK", errno.EACCES),
}
_WINDOWS_LOCK_CONTENTION_WINERRORS = {32, 33, 36}


def _is_windows_lock_contention(error):
    return (
        getattr(error, "errno", None) in _WINDOWS_LOCK_CONTENTION_ERRNOS
        or getattr(error, "winerror", None)
        in _WINDOWS_LOCK_CONTENTION_WINERRORS
    )


def _thread_lock(path):
    key = os.path.normcase(str(Path(path).resolve()))
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


class InterprocessFileLock:
    """Exclusive lock backed by ``flock`` on POSIX and ``locking`` on Windows."""

    def __init__(self, path, poll_interval=0.01):
        self.path = Path(path)
        self.poll_interval = max(0.001, float(poll_interval))
        self._thread_lock = _thread_lock(self.path)
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread_lock.acquire()
        try:
            self._handle = self.path.open("a+b")
            if self.path.stat().st_size == 0:
                self._handle.write(b"\0")
                self._handle.flush()
                os.fsync(self._handle.fileno())
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        self._handle.seek(0)
                        msvcrt.locking(
                            self._handle.fileno(), msvcrt.LK_NBLCK, 1
                        )
                        break
                    except OSError as exc:
                        if not _is_windows_lock_contention(exc):
                            raise
                        time.sleep(self.poll_interval)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
            return self
        except BaseException:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            self._thread_lock.release()
            raise

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self._handle is not None:
                self._handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(
                        self._handle.fileno(), msvcrt.LK_UNLCK, 1
                    )
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            self._thread_lock.release()
