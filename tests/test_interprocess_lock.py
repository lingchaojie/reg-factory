import errno
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from common import interprocess_lock
from common.interprocess_lock import (
    InterprocessFileLock,
    _is_windows_lock_contention,
)


def _crash_while_holding_lock(path, ready):
    import os
    from common.interprocess_lock import InterprocessFileLock

    with InterprocessFileLock(path):
        ready.set()
        os._exit(53)


class InterprocessFileLockTests(unittest.TestCase):
    def test_windows_retries_only_documented_contention_errors(self):
        self.assertTrue(_is_windows_lock_contention(OSError(errno.EACCES, "busy")))
        self.assertTrue(_is_windows_lock_contention(OSError(errno.EAGAIN, "busy")))
        fatal = OSError(errno.EIO, "disk failure")
        self.assertFalse(_is_windows_lock_contention(fatal))

    def test_windows_permanent_lock_error_is_raised_without_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pool.lock"
            locking = Mock(side_effect=OSError(errno.EIO, "disk failure"))
            fake_msvcrt = SimpleNamespace(
                LK_NBLCK=1,
                LK_UNLCK=2,
                locking=locking,
            )
            lock = InterprocessFileLock(path)

            with patch.dict(
                sys.modules, {"msvcrt": fake_msvcrt}
            ), patch.object(
                interprocess_lock.os, "name", "nt"
            ), patch.object(
                interprocess_lock.time, "sleep"
            ) as sleep, self.assertRaisesRegex(OSError, "disk failure"):
                lock.__enter__()

            sleep.assert_not_called()
            self.assertFalse(lock._thread_lock.locked())

    def test_stale_lock_file_without_holder_is_immediately_reusable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pool.lock"
            path.write_bytes(b"stale lock metadata")

            started = time.monotonic()
            with InterprocessFileLock(path):
                pass

            self.assertLess(time.monotonic() - started, 0.2)

    def test_crashed_holder_releases_kernel_lock_for_recovery(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pool.lock"
            worker = context.Process(
                target=_crash_while_holding_lock,
                args=(str(path), ready),
            )
            worker.start()
            self.assertTrue(ready.wait(timeout=5))
            worker.join(timeout=5)
            self.assertEqual(worker.exitcode, 53)

            started = time.monotonic()
            with InterprocessFileLock(path):
                pass
            self.assertLess(time.monotonic() - started, 0.5)


if __name__ == "__main__":
    unittest.main()
