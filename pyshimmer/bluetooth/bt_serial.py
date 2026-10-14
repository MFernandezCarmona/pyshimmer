# pyshimmer - API for Shimmer sensor devices
# Copyright (C) 2020  Lukas Magel

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
from __future__ import annotations

import time

from serial import Serial

from pyshimmer.bluetooth.bt_const import (
    ACK_COMMAND_PROCESSED,
    NACK_COMMAND_PROCESSED,
)
from pyshimmer.serial_base import SerialBase
from pyshimmer.util import fmt_hex, resp_code_to_bytes


class BluetoothSerial(SerialBase):
    """Another wrapper layer around the Serial interface

    :arg serial: The :class:`serial.Serial` instance to wrap
    """

    def __init__(self, serial: Serial):
        super().__init__(serial)

    def read_varlen(self) -> bytes:
        """Read the number of bytes specified by the first byte read

        The function reads a single byte that it interprets as a length field with value
        L and then reads the specified number of L bytes from the stream.

        :return: A variable number of bytes
        """
        arg_len = self.read_byte()
        arg = self.read(arg_len)
        return arg

    def write_varlen(self, arg: bytes) -> None:
        """Write a variable number of bytes by prepending the data with their length

        The function first writes the length of the data as a single byte, then writes
        the data itself.

        :param arg: The data to write
        :raises ValueError: If the number of bytes exceeds the maximum length of 256
        """
        arg_len = len(arg)
        if arg_len > 255:
            raise ValueError(f"Variable-length argument is too long: {arg_len:d}")

        self.write_byte(arg_len)
        self.write(arg)

    def write_command(self, ccode: int, arg_format: str = None, *args) -> None:
        """Write a Bluetooth command to the stream

        :param ccode: The code of the command
        :param arg_format: The argument format, can be a :func:`struct.pack` string or
            `varlen` for a variable-length argument
        :param args: The arguments to write along with the command, must meet the
            requirements of the format string
        """
        self.write_byte(ccode)

        if arg_format is not None:
            if arg_format == "varlen":
                self.write_varlen(args[0])
            else:
                self.write_packed(arg_format, *args)

    def read_ack(self) -> None:
        """Read and assert that the next byte in the stream is an acknowledgment

        :raises ValueError: If the byte is not an acknowledgment

        """
        r = self.read_byte()
        if r != ACK_COMMAND_PROCESSED:
            raise ValueError("Byte received is no acknowledgment")

    def read_nack(self) -> None:
        """Read and assert that the next byte in the stream is a refusal

        :raises ValueError: If the byte is not a NACK

        """
        r = self.read_byte()
        if r != NACK_COMMAND_PROCESSED:
            raise ValueError("Byte received is no NACK")

    def read_response(
        self, rcode: int | bytes | tuple[int, ...], arg_format: str = None
    ) -> any:
        """Read a Bluetooth command response from the stream

        :param rcode: The expected response code. Can be an int for a single-byte
            response code, or a tuple of ints or a bytes instance for a multi-byte
            response code
        :param arg_format: The format string to use when decoding the response
            arguments. Can be None, a :func:`struct.unpack` string or `varlen`. If
            None, no arguments will be read.
        :raises RuntimeError: If the response code is incorrect
        :return: The arguments of the response or () if the response has no arguments
        """
        rcode = resp_code_to_bytes(rcode)

        actual_rcode = self.read(len(rcode))
        if rcode != actual_rcode:
            raise RuntimeError(
                f"Received incorrect response code: "
                f"{fmt_hex(rcode)} != {fmt_hex(actual_rcode)}"
            )

        if arg_format is not None:
            if arg_format == "varlen":
                return self.read_varlen()
            else:
                return self.read_packed(arg_format)
        return ()


class IncompleteFrame(Exception):
    """The bytes a frame needs did not arrive within the time allowed for a single
    frame"""


class FrameView(BluetoothSerial):
    """A read-only, non-consuming view on the stream that decodes one frame
    speculatively

    All the read operations of :class:`BluetoothSerial` work on the view but only *peek*
    into the stream: nothing is taken from it until the owner of the view decides that
    the frame is valid and reads :attr:`consumed` bytes. A frame that fails validation
    (CRC, framing) costs a single byte, so the receiver can resynchronize by sliding
    forward without ever losing the bytes of a valid frame that was mistaken for the
    tail of a bad one.

    :arg base: The serial interface to look into
    :arg wait: Seconds to wait for bytes that are missing in the middle of a frame
        before giving up with :class:`IncompleteFrame`. None blocks until the bytes
        arrive (or the read is cancelled).
    """

    def __init__(self, base: BluetoothSerial, wait: float | None = None):
        # Deliberately no super().__init__(): the view owns no serial of its own.
        self._base = base
        self._wait = wait
        self._pos = 0

    @property
    def consumed(self) -> int:
        """The number of bytes the frame decoded so far spans"""
        return self._pos

    def _ensure(self, end: int, wait: float | None) -> bool:
        base = self._base
        if wait is None or base._reader.buffered_len >= end:
            return True

        deadline = time.monotonic() + wait
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            base.wait_readable(remaining)  # takes the new bytes into the buffer
            if base._reader.buffered_len >= end:
                return True

    def read(self, read_len: int) -> bytes:
        end = self._pos + read_len
        if not self._ensure(end, self._wait):
            raise IncompleteFrame(
                f"Frame needs {read_len} more bytes that did not arrive"
            )

        data = self._base.peek(end)[self._pos :]
        self._pos = end
        return data

    def peek(self, n: int) -> bytes:
        end = self._pos + n
        if not self._ensure(end, self._wait):
            raise IncompleteFrame(f"Frame needs {n} more bytes that did not arrive")

        return self._base.peek(end)[self._pos :]

    def peek_if_available(self, n: int, wait: float) -> bytes | None:
        """Peek ``n`` bytes ahead, waiting at most ``wait`` seconds for them

        :return: The bytes, or None if they did not arrive in time
        """
        end = self._pos + n
        if not self._ensure(end, wait):
            return None

        return self._base.peek(end)[self._pos :]

    def raw(self) -> bytes:
        """All the bytes the frame spans so far"""
        return self._base.peek(self._pos) if self._pos else b""
