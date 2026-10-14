# pyshimmer - API for Shimmer sensor devices
# Copyright (C) 2020  Lukas Magel, 2026 Manuel Fernandez-Carmona

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
import os
import time
from typing import Optional

from pyshimmer.bluetooth.bt_errors import ShimmerConnectError

try:
    import fcntl
except ImportError:  # pragma: no cover - not available on Windows
    fcntl = None


class ConnectLock:
    """Serializes Bluetooth connection setup across processes

    A single Bluetooth adapter can only page one remote device at a time, so several processes that connect to
    different Shimmers at the same moment make each other fail. This lock is held while one process opens the
    serial port and initializes the device.

    The lock is an ``flock`` on a file: the operating system releases it when the holder exits or crashes, which
    a lock *file* that has to be removed explicitly does not do. In addition, ``min_interval`` keeps consecutive
    connection attempts (of any process) at least that many seconds apart, which gives the adapter time to settle.

    Hold the lock until the first data packet of the device has arrived (``start_streaming(wait_for_data=...)``)
    and use a ``min_interval`` of 1-2 s: the first packet comes before the device has settled, and a connection
    made then fails and leaves the rfcomm port ``EBUSY`` for about 30 s. With three Shimmer3 started at the same
    moment, ``min_interval=2.0`` took 15-18 s to stream them all where releasing at the first data took 41-86 s.

    :arg path: The lock file, shared by all the processes that must take turns
    :arg timeout: How long to wait for the lock before giving up
    :arg min_interval: Minimum time in seconds between the end of one holder and the start of the next
    """

    def __init__(self, path: str, timeout: float = 60.0, min_interval: float = 0.0):
        self._path = path
        self._timeout = timeout
        self._min_interval = min_interval
        self._fd: Optional[int] = None

    def __enter__(self) -> "ConnectLock":
        if fcntl is None:
            return self

        self._fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o666)
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    os.close(self._fd)
                    self._fd = None
                    raise ShimmerConnectError(f'Could not take the connect lock {self._path} in {self._timeout} s')
                time.sleep(0.05)

        if self._min_interval > 0:
            last = self._read_release_time()
            wait = last + self._min_interval - time.time()
            if 0 < wait <= self._min_interval:
                time.sleep(wait)

        return self

    def _read_release_time(self) -> float:
        try:
            raw = os.pread(self._fd, 64, 0)
            return float(raw.decode('ascii').strip() or 0)
        except (OSError, ValueError):
            return 0.0

    def __exit__(self, exc_type, exc_value, exc_traceback) -> None:
        if self._fd is None:
            return

        try:
            os.ftruncate(self._fd, 0)
            os.pwrite(self._fd, f'{time.time():.3f}'.encode('ascii'), 0)
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
