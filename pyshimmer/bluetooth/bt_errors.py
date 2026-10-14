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


class ShimmerError(Exception):
    """Base class of the errors the Bluetooth API raises on purpose."""


class ShimmerLinkError(ShimmerError):
    """The connection to the device is gone or unusable.

    Raised by every pending and every later request once the read loop has crashed, the device has hung up or
    the stream has stalled. The original exception, if there was one, is available as ``__cause__``. A link
    that raised this error cannot be revived: call :meth:`ShimmerBluetooth.shutdown` and connect again.
    """


class ShimmerTimeoutError(ShimmerError, TimeoutError):
    """A request did not receive its acknowledgment or response in time."""


class ShimmerConnectError(ShimmerError):
    """No usable connection could be established, even after the configured retries."""
