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


class CommandRefused(ShimmerError):
    """The device answered a command with a NACK instead of an acknowledgment: it understood the request and
    declined it.

    A refusal is a normal part of the protocol and does not mean the connection is gone. The firmware refuses
    every command except the SD sync command and an acknowledgment while SD sync is enabled, any set command
    while the device is sensing, a sync mode mismatch, an out-of-range InfoMem or calibration write, and a
    handful of commands it accepts but has never implemented.
    """


class ShimmerConfigurationError(ShimmerError):
    """The device answered, but what it reports is not the configuration that was written to it.

    Unlike a timeout this is deterministic: writing again would give the same result.
    """
