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

import logging
import struct
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from threading import Event, Thread

from serial import Serial, SerialException

from pyshimmer.bluetooth.bt_commands import (
    GetShimmerHardwareVersion,
    ShimmerCommand,
    GetSamplingRateCommand,
    GetConfigTimeCommand,
    SetConfigTimeCommand,
    GetRealTimeClockCommand,
    SetRealTimeClockCommand,
    GetStatusCommand,
    GetFirmwareVersionCommand,
    Shimmer3InquiryCommand,
    Shimmer3RInquiryCommand,
    StartStreamingCommand,
    StopStreamingCommand,
    DataPacket,
    GetEXGRegsCommand,
    SetEXGRegsCommand,
    StartLoggingCommand,
    StopLoggingCommand,
    GetExperimentIDCommand,
    SetExperimentIDCommand,
    GetDeviceNameCommand,
    SetDeviceNameCommand,
    DummyCommand,
    GetBatteryCommand,
    GetBatteryReadingCommand,
    GetExpansionBoardCommand,
    SetSamplingRateCommand,
    SetSensorsCommand,
    SetStatusAckCommand,
    AllCalibration,
    GetAllCalibrationCommand,
    SetCrcCommand,
    response_parsers,
)
from pyshimmer.bluetooth.bt_const import (
    ACK_COMMAND_PROCESSED,
    DATA_PACKET,
    FULL_STATUS_RESPONSE,
    INSTREAM_CMD_RESPONSE,
    NACK_COMMAND_PROCESSED,
    STOP_STREAMING_COMMAND,
)
from pyshimmer.bluetooth.bt_crc import CRC_OFF, CRC_2BYTE, crc16, crc_matches
from pyshimmer.bluetooth.bt_errors import (
    CommandRefused,
    ShimmerConfigurationError,
    ShimmerError,
    ShimmerLinkError,
    ShimmerTimeoutError,
    ShimmerConnectError,
)
from pyshimmer.bluetooth.bt_lock import ConnectLock
from pyshimmer.bluetooth.bt_serial import BluetoothSerial, FrameView, IncompleteFrame
from pyshimmer.dev.base import DEFAULT_BAUDRATE
from pyshimmer.dev.battery import BatteryReading
from pyshimmer.dev.channels import ChannelDataType, EChannelType, ESensorGroup
from pyshimmer.dev.exg import ExGRegister, exg_data_rate_for_sampling_rate, is_exg_ch
from pyshimmer.dev.expansion_board import ExpansionBoard
from pyshimmer.dev.fw_version import (
    FirmwareType,
    FirmwareVersion,
    FirmwareCapabilities,
)
from pyshimmer.dev.revisions import HardwareRevision, HardwareVersion, RevisionRegistry
from pyshimmer.serial_base import ReadAbort
from pyshimmer.util import fmt_hex

_log = logging.getLogger(__name__)

# What follows a valid data packet in a valid stream: another packet, an acknowledgment, a refusal or an in-stream
# response
_VALID_HEADERS = (
    DATA_PACKET,
    ACK_COMMAND_PROCESSED,
    NACK_COMMAND_PROCESSED,
    INSTREAM_CMD_RESPONSE,
)
# The CRC of a lone acknowledgment is a constant. Its first byte tells an acknowledgment that is followed by a
# response (whose first byte is a response code) from an acknowledgment that stands alone.
_ACK_ONLY_CRC = crc16(bytes((ACK_COMMAND_PROCESSED,)))
# Everything a speculative decode of garbage can raise
_PARSE_ERRORS = (
    ValueError,
    RuntimeError,
    struct.error,
    KeyError,
    IndexError,
    UnicodeError,
    OverflowError,
)


class RequestCompletion:
    """
    Returned by the Bluetooth API upon sending a request. Signals the completion of a request when the API receives
    the corresponding acknowledgment.
    """

    def __init__(self):
        self.__event = Event()
        self.__error: BaseException | None = None
        self.__refused = False

    def set_completed(self) -> None:
        self.__event.set()

    def set_error(self, error: BaseException) -> None:
        """Complete the request with a failure; :meth:`wait` raises the error"""
        self.__error = error
        self.__event.set()

    def set_refused(self) -> None:
        """Mark the request as refused by the device

        The request is finished either way, so :meth:`has_completed` returns True,
        but :meth:`wait` raises instead of returning.
        """
        self.__refused = True
        self.__event.set()

    def has_completed(self) -> bool:
        return self.__event.is_set()

    def was_refused(self) -> bool:
        return self.__refused

    def wait(self, timeout: float | None = None) -> None:
        """Wait for the acknowledgment

        :param timeout: Seconds to wait, None waits forever
        :raises ShimmerTimeoutError: If the acknowledgment did not arrive in time
        :raises ShimmerLinkError: If the link failed while waiting
        :raises CommandRefused: If the device refused the command
        """
        if not self.__event.wait(timeout):
            raise ShimmerTimeoutError("No acknowledgment received")
        if self.__error is not None:
            raise self.__error
        if self.__refused:
            raise CommandRefused("The Shimmer refused the command")


class RequestResponse:
    """
    Returned by the Bluetooth API upon sending a request that features a response. Returns the request response data
    upon completion.
    """

    def __init__(self):
        self.__event = Event()
        self.__r = None
        self.__error: BaseException | None = None
        self.__refused = False

    def has_result(self) -> bool:
        return self.__event.is_set()

    def get_result(self) -> any:
        return self.__r

    def set_result(self, r: any) -> None:
        self.__r = r
        self.__event.set()

    def set_error(self, error: BaseException) -> None:
        """Complete the request with a failure; :meth:`wait` raises the error"""
        self.__error = error
        self.__event.set()

    def set_refused(self) -> None:
        """Mark the request as refused, so that no result will ever arrive"""
        self.__refused = True
        self.__event.set()

    def was_refused(self) -> bool:
        return self.__refused

    def wait(self, timeout: float | None = None) -> any:
        """Wait for the response

        :param timeout: Seconds to wait, None waits forever
        :raises ShimmerTimeoutError: If the response did not arrive in time
        :raises ShimmerLinkError: If the link failed while waiting
        :raises CommandRefused: If the device refused the command
        """
        if not self.__event.wait(timeout):
            raise ShimmerTimeoutError("No response received")
        if self.__error is not None:
            raise self.__error
        if self.__refused:
            raise CommandRefused("The Shimmer refused the command")
        return self.get_result()


class LinkStats:
    """Counters of what the receiving side of the link has seen. All fields are monotonically increasing."""

    FIELDS = (
        "data_packets",
        "acks",
        "nacks",
        "responses",
        "status_updates",
        "skipped_bytes",
        "unexpected_acks",
        "unexpected_nacks",
        "late_responses",
        "crc_errors",
        "framing_errors",
        "parse_errors",
        "callback_errors",
        "timeouts",
    )

    def __init__(self):
        for f in self.FIELDS:
            setattr(self, f, 0)

    def as_dict(self) -> dict:
        return {f: getattr(self, f) for f in self.FIELDS}


class _BadFrame(Exception):
    """The bytes at the current position do not form a valid frame"""


class _Pending:
    """A command that has been sent and whose acknowledgment and/or response is still outstanding"""

    __slots__ = ("cmd", "compl", "resp", "acked")

    def __init__(
        self,
        cmd: ShimmerCommand,
        compl: RequestCompletion,
        resp: RequestResponse | None,
    ):
        self.cmd = cmd
        self.compl = compl
        self.resp = resp
        self.acked = False


class BluetoothRequestHandler:
    """Base class for the Bluetooth API which handles serial data processing synchronously

    In contrast to the :class:`ShimmerBluetooth` class which uses Threading, this class acts as a base layer that
    operates synchronously and allows for easier testing.

    By default the handler is *strict*: it assumes a perfectly behaved byte stream and raises an exception on any
    unexpected byte, which is what the unit tests of the protocol need. :class:`ShimmerBluetooth` creates it in
    *tolerant* mode, where nothing that arrives on the wire can end the processing: stray bytes are skipped,
    frames are validated (by CRC if the device sends one, else by checking that a valid header follows), invalid
    frames are resynchronized by sliding forward one byte, and replies that nobody waits for any more are consumed
    and discarded.

    :arg serial: The serial interface to use
    :arg revision: The hardware revision of the Shimmer device. It is only known after contacting the device,
        for which this class is needed: bootstrap it with a sensible default and update it with the
        :attr:`hardware_revision` setter.
    :arg tolerant: Enable the tolerant mode described above
    :arg crc_len: The number of CRC bytes the device appends to its frames (0, 1 or 2)
    :arg lookahead: Without CRC, hold a data packet back until the byte after it has been seen to be a valid header
    :arg frame_timeout: Seconds a frame may take to arrive completely once its first byte has been seen. Bytes
        that never complete a frame are skipped, which keeps garbage from stalling the stream.
    :arg lookahead_timeout: How long to wait for the byte after a data packet before delivering it unchecked
    """

    def __init__(
        self,
        serial: BluetoothSerial,
        revision: HardwareRevision,
        tolerant: bool = False,
        crc_len: int = CRC_OFF,
        lookahead: bool = True,
        frame_timeout: float = 0.5,
        lookahead_timeout: float = 0.25,
    ):
        self._serial = serial
        self._rev = revision
        self._tolerant = tolerant
        self._crc_len = crc_len
        self._lookahead = lookahead
        self._frame_timeout = frame_timeout
        self._lookahead_timeout = lookahead_timeout

        self._lock = threading.Lock()  # protects _pending
        self._send_lock = (
            threading.Lock()
        )  # keeps the order of _pending equal to the order on the wire
        self._pending: list[_Pending] = []
        self._failure: BaseException | None = None
        self._parsers = response_parsers(revision)

        self._stream_types = []
        self._payload_len = 0
        self._ts_first = False  # the first channel is the 3-byte device timestamp
        self._period_ticks: float | None = None
        self._last_ts: int | None = None
        self._reject_streak = 0
        self._stream_cbs = []
        self._status_cbs = []

        self.stats = LinkStats()
        self.last_rx = time.monotonic()  # last time any frame was processed
        self.last_data = time.monotonic()  # last time a data packet was delivered

    # ------------------------------------------------------------------------------------------ configuration
    @property
    def hardware_revision(self) -> HardwareRevision:
        """The hardware revision of the Shimmer device that we are currently talking to"""
        return self._rev

    @hardware_revision.setter
    def hardware_revision(self, v: HardwareRevision) -> None:
        """Update the hardware revision, which may not be known precisely when the object is created"""
        self._rev = v
        self._parsers = response_parsers(v)

    @property
    def stream_types(self) -> list[tuple[EChannelType, ChannelDataType]]:
        """The data types expected to be sent by the device in a DataPacket"""
        return self._stream_types

    @stream_types.setter
    def stream_types(
        self, types: Iterable[tuple[EChannelType, ChannelDataType]]
    ) -> None:
        self.set_stream_types(list(types))

    def set_stream_types(
        self,
        types: list[tuple[EChannelType, ChannelDataType]],
        period_ticks: float | None = None,
    ) -> None:
        """Set the channel types that are streamed as part of the data packets

        :param types: A List of tuples, each containing a channel type and its corresponding data type
        :param period_ticks: The time between two samples in ticks of the 32768 Hz device clock. Without CRC
            it lets the receiver tell a valid packet from a misaligned one by the continuity of the timestamps.
        """
        self._stream_types = types
        self._payload_len = sum(dtype.size for _, dtype in types)
        self._ts_first = (
            bool(types)
            and types[0][0] == EChannelType.TIMESTAMP
            and types[0][1].size == 3
        )
        self._period_ticks = period_ticks
        self._last_ts = None
        self._reject_streak = 0

    def set_crc_len(self, crc_len: int) -> None:
        """Set the number of CRC bytes the device appends to every frame

        Must be called *before* the command that changes the mode of the device is sent, because the
        acknowledgment of that command already carries the CRC of the new mode.
        """
        if crc_len not in (0, 1, 2):
            raise ValueError(f"CRC length must be 0, 1 or 2, not {crc_len}")
        self._crc_len = crc_len

    @property
    def crc_len(self) -> int:
        return self._crc_len

    def add_stream_callback(self, cb: Callable[[DataPacket], None]) -> None:
        """Add a stream callback which is called when a new data packet arrives

        :param cb: a function with a single argument
        """
        self._stream_cbs += [cb]

    def remove_stream_callback(self, cb: Callable[[DataPacket], None]) -> None:
        """Remove the callback from the list of active callbacks

        :param cb: The callback function to remove
        """
        self._stream_cbs.remove(cb)

    def add_status_callback(self, cb: Callable[[list[bool]], None]) -> None:
        """Add a status callback which is called when a new status update from the Shimmer arrives

        :param cb: a function with a single argument. The argument is the same value is the return value of the
            :meth:`pyshimmer.bluetooth.bt_api.ShimmerBluetooth.get_status` method.
        """
        self._status_cbs += [cb]

    def remove_status_callback(self, cb: Callable[[list[bool]], None]) -> None:
        """Remove the callback from the list of active callbacks

        :param cb: The callback function to remove
        """
        self._status_cbs.remove(cb)

    # --------------------------------------------------------------------------------------- pending commands
    def _next_unacked(self, want_response: bool | None = None) -> _Pending | None:
        for e in self._pending:
            if e.acked:
                continue
            if want_response is None or (e.resp is not None) == want_response:
                return e
        return None

    def _first_awaiting(self, code: bytes | None = None) -> _Pending | None:
        """The oldest command that expects a response (with the given code), acknowledged or not"""
        for e in self._pending:
            if e.resp is None:
                continue
            if code is None and not e.acked:
                continue
            if code is None or e.cmd.get_response_code() == code:
                return e
        return None

    def _remove(self, entry: _Pending) -> None:
        try:
            self._pending.remove(entry)
        except ValueError:
            pass

    def queue_command(
        self, cmd: ShimmerCommand
    ) -> tuple[RequestCompletion, RequestResponse]:
        """Queue a command request for processing

        :param cmd: The command to send to the Shimmer device
        :return: A completion instance and a response instance. The completion instance is always returned and becomes
            true when the command has been processed by the Shimmer. The response object is only returned if the command
            features a response. It holds the response data once the response has been returned by the Shimmer.
        """
        resp_obj = RequestResponse() if cmd.has_response() else None
        compl_obj = RequestCompletion()
        entry = _Pending(cmd, compl_obj, resp_obj)

        with self._send_lock:
            if self._failure is not None:
                raise ShimmerLinkError(
                    f"The link has failed: {self._failure}"
                ) from self._failure

            with self._lock:
                self._pending.append(entry)
            try:
                cmd.send(self._serial)
            except BaseException:
                with self._lock:
                    self._remove(entry)
                raise

        return compl_obj, resp_obj

    def cancel(self, compl: RequestCompletion) -> None:
        """Forget a request whose caller gave up waiting

        The entry is dropped so that a late reply cannot be matched with a *later* request. The late reply is
        recognized by its response code and discarded when it arrives.
        """
        with self._lock:
            for e in list(self._pending):
                if e.compl is compl:
                    self._remove(e)

    def clear_queues(self) -> None:
        """Clear the internal queues and release any locks held by other threads"""
        with self._lock:
            pending, self._pending = self._pending, []

        for e in pending:
            e.compl.set_completed()
            if e.resp is not None:
                e.resp.set_result(None)

    def fail_all(self, error: BaseException) -> None:
        """Fail every outstanding request with the error and reject all later ones"""
        self._failure = (
            error  # a plain assignment: must not wait for a writer that may be blocked
        )
        with self._lock:
            pending, self._pending = self._pending, []

        for e in pending:
            e.compl.set_error(error)
            if e.resp is not None:
                e.resp.set_error(error)

    # ------------------------------------------------------------------------------------ strict processing
    def _process_ack(self):
        self._serial.read_ack()
        with self._lock:
            entry = self._next_unacked()
            if entry is not None:
                entry.acked = True
                if entry.resp is None:
                    self._remove(entry)

        if entry is None:
            if self._tolerant:
                self.stats.unexpected_acks += 1
                return
            raise ShimmerError(
                "Received an acknowledgment, but no command is waiting for one"
            )

        self.stats.acks += 1
        entry.compl.set_completed()

    def _process_nack(self):
        self._serial.read_nack()
        self._apply_nack()

    def _process_data_packet(self):
        packet = DataPacket(self._rev, self._stream_types)
        packet.receive(self._serial)
        self.stats.data_packets += 1
        self.last_data = time.monotonic()
        self._deliver_packet(packet)

    def _deliver_packet(self, packet: DataPacket) -> None:
        for cb in list(self._stream_cbs):
            if not self._tolerant:
                cb(packet)
                continue

            try:
                cb(packet)
            except (
                Exception
            ) as e:  # noqa: BLE001 - a bug in user code must not take the link down
                self.stats.callback_errors += 1
                n = self.stats.callback_errors
                if n <= 3 or n % 1000 == 0:
                    _log.error(
                        "Stream callback %r raised (%d so far): %r",
                        cb,
                        n,
                        e,
                        exc_info=e,
                    )

    def _process_in_stream_resp(self):
        peek = self._serial.peek(len(FULL_STATUS_RESPONSE))

        if peek == FULL_STATUS_RESPONSE:
            # The packet is a status response, which we need to handle separately
            self._process_status_response()
        else:
            # We don't know exactly what it is, but it must have been triggered by a command, so we simply
            # process it as part of the reqular queue handling
            self._process_resp_from_queue()

    def _process_status_response(self):
        with self._lock:
            entry = self._first_awaiting()

        if entry is not None and isinstance(entry.cmd, GetStatusCommand):
            # We have received a Status Response and have are expecting a response from a command
            # ---> Handle it like a regular command
            self._process_resp_from_queue()
        else:
            # We have received a Status Response but are not expecting one
            # ---> Handle it as a pushed Status Update
            self._process_status_update()

    def _process_status_update(self):
        # Called if the status response was not triggered by a command but sent by the Shimmer as the result of
        # an event
        status_cmd = GetStatusCommand(self._rev)
        r = status_cmd.receive(self._serial)
        self._push_status(r)

    def _push_status(self, status: list[bool]) -> None:
        self.stats.status_updates += 1
        for cb in list(self._status_cbs):
            cb(status)

    def _process_resp_from_queue(self):
        with self._lock:
            entry = self._first_awaiting()
            if entry is None:
                raise ShimmerError(
                    "Received a response, but no command is waiting for one"
                )
            self._remove(entry)

        resp_code = entry.cmd.get_response_code()
        peek = self._serial.peek(len(resp_code))

        if peek != resp_code:
            raise ValueError(
                f"Expecting response code {fmt_hex(resp_code)} but found {fmt_hex(peek)}"
            )

        result = entry.cmd.receive(self._serial)
        self.stats.responses += 1
        entry.resp.set_result(result)

    # ---------------------------------------------------------------------------------- tolerant processing
    def _process_tolerant(self) -> None:
        # The first byte may take arbitrarily long to arrive: block on it. The rest of a frame must follow
        # promptly, which is enforced by the view.
        head = self._serial.peek(1)[0]
        view = FrameView(self._serial, wait=self._frame_timeout)

        try:
            effect, consumed = self._parse_frame(view, head)
        except _BadFrame:
            effect = None
        except IncompleteFrame:
            effect = None
        except _PARSE_ERRORS:
            self.stats.parse_errors += 1
            effect = None

        if effect is None:
            # Not a frame. Drop the first byte and look again from the next one: this is the resynchronization.
            self._serial.read(1)
            self.stats.skipped_bytes += 1
            return

        self._serial.read(consumed)
        effect()

    def _parse_frame(self, view: FrameView, head: int):
        """Decode one frame without consuming it. Returns a function that applies it and the number of bytes."""
        if head == DATA_PACKET:
            return self._parse_data(view), view.consumed

        if self._crc_len:
            return self._parse_frame_crc(view, head)

        return self._parse_frame_plain(view, head), view.consumed

    # Packets the device may have dropped between two packets that arrive one after the other
    _MAX_GAP_PACKETS = 64

    def _delta_ok(self, ts: int, ref: int, max_packets: int | None = None) -> bool:
        """Does ``ts`` continue the stream after ``ref``?

        With a known sampling period the timestamps of a stream advance by a whole number of periods (the device
        clock ticks at 32768 Hz and the period is an integer number of ticks), which a misaligned read does not
        reproduce even when the data it is made of looks like a counter. Without a period only a plausible range
        can be checked.
        """
        d = (ts - ref) & 0xFFFFFF
        limit = max_packets or self._MAX_GAP_PACKETS
        p = self._period_ticks
        if p:
            k = round(d / p)
            return 1 <= k <= limit and abs(d - k * p) <= 1.5

        return 0 < d <= 16384  # half a second

    def _parse_data(self, view: FrameView) -> Callable[[], None]:
        if not self._stream_types:
            # The layout is unknown: a packet cannot be framed. Happens with data left over from a previous
            # session, which must be skipped.
            raise _BadFrame()

        n = 1 + self._payload_len
        buf = view.read(n + self._crc_len)
        ts = int.from_bytes(buf[1:4], "little") if self._ts_first else None

        if self._crc_len:
            if not crc_matches(buf[:n], buf[n:], self._crc_len):
                self.stats.crc_errors += 1
                raise _BadFrame()
            buf = buf[:n]
        else:
            self._validate_plain(view, ts)

        packet = DataPacket(self._rev, self._stream_types)
        packet.decode(buf[1:])

        def effect():
            self._last_ts = ts
            self._reject_streak = 0
            self.stats.data_packets += 1
            self.last_data = time.monotonic()
            self._deliver_packet(packet)

        return effect

    def _validate_plain(self, view: FrameView, ts: int | None) -> None:
        """Without CRC a packet is accepted when it looks like part of the stream: a valid header follows it
        and its timestamp continues the previous one. While there is no reference timestamp (start of the
        stream, after a resynchronization) the next packet must continue this one as well.
        """
        nxt = None
        if self._lookahead:
            nxt = view.peek_if_available(
                4 if ts is not None else 1, self._lookahead_timeout
            )

        problem = None
        if nxt is not None and nxt[0] not in _VALID_HEADERS:
            problem = "framing"
        elif ts is not None:
            if self._last_ts is not None:
                if not self._delta_ok(ts, self._last_ts):
                    problem = "timestamp"
            elif nxt is not None and nxt[0] == DATA_PACKET and len(nxt) >= 4:
                # Acquiring the stream: the very next packet must follow this one, no packets missing in between
                if not self._delta_ok(
                    int.from_bytes(nxt[1:4], "little"), ts, max_packets=1
                ):
                    problem = "timestamp"

        if problem is None:
            return

        self.stats.framing_errors += 1
        self._reject_streak += 1
        if self._reject_streak >= 3:
            # Whatever the reference was, it no longer predicts the stream (e.g. the device was restarted)
            self._last_ts = None
        raise _BadFrame()

    # ---- without CRC: the acknowledgment and the response are separate events, as on the wire of old firmware
    def _parse_frame_plain(self, view: FrameView, head: int) -> Callable[[], None]:
        if head == ACK_COMMAND_PROCESSED:
            view.read(1)
            return self._apply_ack

        if head == NACK_COMMAND_PROCESSED:
            view.read(1)
            return self._apply_nack

        if head == INSTREAM_CMD_RESPONSE:
            code = bytes(view.peek(2))
        else:
            code = bytes((head,))

        if code == FULL_STATUS_RESPONSE:
            status = GetStatusCommand(self._rev).receive(view)
            return lambda: self._apply_status(status)

        with self._lock:
            entry = self._first_awaiting(code)
        if entry is None:
            # Nobody is waiting for it. Without a CRC a response cannot be told from garbage that happens to start
            # with a response code (the decoders accept almost any bytes), and decoding it would swallow the
            # bytes of the packets that follow. It is skipped a byte at a time like any other noise.
            raise _BadFrame()

        result = entry.cmd.receive(view)
        return lambda: self._apply_response(code, result)

    def _apply_ack(self) -> None:
        with self._lock:
            entry = self._next_unacked()
            if entry is not None:
                entry.acked = True
                if entry.resp is None:
                    self._remove(entry)

        if entry is None:
            self.stats.unexpected_acks += 1
            return

        self.stats.acks += 1
        entry.compl.set_completed()

    def _apply_nack(self) -> None:
        """The device understood the command and declined it: nothing else will follow for it"""
        with self._lock:
            entry = self._next_unacked()
            if entry is not None:
                self._remove(entry)

        if entry is None:
            self.stats.unexpected_nacks += 1
            return

        self.stats.nacks += 1
        entry.compl.set_refused()
        if entry.resp is not None:
            entry.resp.set_refused()

    def _apply_response(self, code: bytes, result: any) -> None:
        with self._lock:
            entry = self._first_awaiting(code)
            if entry is not None:
                self._remove(entry)

        if entry is None:
            # Nobody is waiting for it any more (the request timed out): consumed and discarded
            self.stats.late_responses += 1
            return

        self.stats.responses += 1
        entry.compl.set_completed()
        entry.resp.set_result(result)

    def _apply_status(self, status: list[bool]) -> None:
        with self._lock:
            entry = self._first_awaiting(FULL_STATUS_RESPONSE)
            if entry is not None and entry.acked:
                self._remove(entry)
            else:
                entry = None

        if entry is not None:
            self.stats.responses += 1
            entry.resp.set_result(status)
            return

        self._push_status_safely(status)

    def _push_status_safely(self, status: list[bool]) -> None:
        self.stats.status_updates += 1
        for cb in list(self._status_cbs):
            try:
                cb(status)
            except Exception as e:  # noqa: BLE001
                self.stats.callback_errors += 1
                _log.error("Status callback %r raised: %r", cb, e, exc_info=e)

    # ---- with CRC: every reply is one frame, ``ack [| response] | crc``
    def _parse_frame_crc(self, view: FrameView, head: int):
        crc = self._crc_len

        if head == INSTREAM_CMD_RESPONSE:
            # A response that the device sends on its own (no acknowledgment in front): status updates
            code = bytes(view.peek(2))
            factory = self._parsers.get(code)
            if factory is None:
                raise _BadFrame()
            result = factory().receive(view)
            body = view.raw()
            if not crc_matches(body, view.read(crc), crc):
                self.stats.crc_errors += 1
                raise _BadFrame()
            if code == FULL_STATUS_RESPONSE:
                return (lambda: self._apply_status(result)), view.consumed
            return (lambda: self._apply_response(code, result)), view.consumed

        if head == NACK_COMMAND_PROCESSED:
            # A refusal is a lone byte followed by its CRC
            view.read(1)
            if not crc_matches(view.raw(), view.read(crc), crc):
                self.stats.crc_errors += 1
                raise _BadFrame()
            return self._apply_nack, view.consumed

        if head != ACK_COMMAND_PROCESSED:
            # A response without its acknowledgment cannot be verified: the CRC covers the acknowledgment
            raise _BadFrame()

        # Is the byte after the acknowledgment the start of a response, or the CRC of a lone acknowledgment?
        view.read(1)
        nxt = view.peek(1)[0]
        attempts = []
        if nxt != _ACK_ONLY_CRC[0]:
            code = (
                bytes(view.peek(2)) if nxt == INSTREAM_CMD_RESPONSE else bytes((nxt,))
            )
            if code in self._parsers:
                attempts.append(code)
        attempts.append(None)  # a lone acknowledgment

        for code in attempts:
            v = FrameView(self._serial, wait=self._frame_timeout)
            try:
                v.read(1)  # the acknowledgment
                result = self._parsers[code]().receive(v) if code is not None else None
                body = v.raw()
                if not crc_matches(body, v.read(crc), crc):
                    self.stats.crc_errors += 1
                    continue
            except (IncompleteFrame,) + _PARSE_ERRORS:
                continue

            if code is None:
                return self._apply_ack_only, v.consumed
            return (lambda c=code, r=result: self._apply_ack_response(c, r)), v.consumed

        raise _BadFrame()

    def _apply_ack_only(self) -> None:
        with self._lock:
            entry = self._next_unacked(want_response=False)
            if entry is not None:
                self._remove(entry)

        if entry is None:
            self.stats.unexpected_acks += 1
            return

        self.stats.acks += 1
        entry.compl.set_completed()

    def _apply_ack_response(self, code: bytes, result: any) -> None:
        with self._lock:
            entry = self._first_awaiting_any(code)
            if entry is not None:
                self._remove(entry)

        if entry is None:
            if code == FULL_STATUS_RESPONSE:
                self._push_status_safely(result)
            else:
                self.stats.late_responses += 1
            return

        self.stats.acks += 1
        self.stats.responses += 1
        entry.compl.set_completed()
        entry.resp.set_result(result)

    def _first_awaiting_any(self, code: bytes) -> _Pending | None:
        for e in self._pending:
            if e.resp is not None and e.cmd.get_response_code() == code:
                return e
        return None

    # ----------------------------------------------------------------------------------------------- entry
    def process_single_input_event(self) -> None:
        """Process and read a single input event

        An input event can be a single acknowledgment or a request response. The function does not return anything.
        All data is provided via the completion objects returned when queueing the command.

        """
        if self._tolerant:
            self._process_tolerant()
        else:
            peek = self._serial.peek_packed("B")

            if peek == ACK_COMMAND_PROCESSED:
                self._process_ack()
            elif peek == NACK_COMMAND_PROCESSED:
                self._process_nack()
            elif peek == DATA_PACKET:
                self._process_data_packet()
            elif peek == INSTREAM_CMD_RESPONSE:
                self._process_in_stream_resp()
            else:
                self._process_resp_from_queue()

        self.last_rx = time.monotonic()


class ShimmerBluetooth:
    _CRC_THROUGHPUT_WARNING = 24000.0  # bytes per second

    def __init__(
        self,
        serial: Serial,
        revision: HardwareRevision | None = None,
        disable_status_ack: bool = True,
        *,
        command_timeout: float | None = 5.0,
        stall_timeout: float | None = 3.0,
        start_timeout: float = 10.0,
        crc: str | int | None = "auto",
        presync: bool = True,
        tolerant: bool = True,
        lookahead: bool = True,
    ):
        """API for communicating with the Shimmer via Bluetooth

        This class implements support for talking to the Shimmer LogAndStream firmware via Bluetooth.
        Each command is encapsulated as a method that can be called to invoke the corresponding command.
        All commands are executed synchronously. This means that the method call will block until the
        Shimmer has processed the request and responded.

        Nothing that happens on the link can leave a caller blocked forever or end the stream silently: a request
        that is not answered in time raises :class:`ShimmerTimeoutError`; when the link breaks (the read loop
        crashes, the device hangs up, the stream stalls) every outstanding and every later request raises
        :class:`ShimmerLinkError` and the registered error callbacks are called.

        :param serial: The serial channel that encapsulates the rfcomm Bluetooth connection to the Shimmer
        :param revision: Manually set the revision of the Shimmer device that we are communicating with. Normally
            the revision is determined during initialization.
        :param disable_status_ack: Starting with LogAndStream firmware version 0.15.4, the vanilla firmware
            supports disabling the acknowledgment byte before status messages. This removes the need for
            running a custom firmware version on the Shimmer. If this flag is set to True, the API will
            query the firmware version of the Shimmer and automatically send a command to disable the status
            acknowledgment byte at startup. You can set it to True if you don't want this or if it causes
            trouble with your firmware version.
        :param command_timeout: Seconds a request may take until its acknowledgment (and response) arrive.
            Real devices answer within ~0.1 s, the first command after a connection takes ~1.5 s and the status
            request right after starting the stream has been seen to take 2.5 s. None waits forever.
        :param stall_timeout: While streaming, seconds without any data after which the link is declared dead.
            None disables the check. The stream is not polled with commands: every command costs the device
            some samples.
        :param start_timeout: Seconds the first data may take after the stream was started
        :param crc: Bluetooth CRC mode (firmware >= 0.13.7): 'auto' uses 2 CRC bytes if the firmware supports
            it, 'off' (or None/False) never enables it, 1 or 2 selects the number of bytes explicitly. With CRC
            every frame is validated and corrupt ones are dropped and resynchronized.
        :param presync: Before anything else send STOP_STREAMING and wait for the line to go quiet. A device
            that was left streaming by a previous (crashed) session would otherwise fill the first replies with
            data the API cannot frame.
        :param tolerant: Use the tolerant receiver (see :class:`BluetoothRequestHandler`)
        :param lookahead: Without CRC, validate each data packet by looking at the byte after it, at the price
            of delaying every packet by one sampling period
        """
        if crc in (None, False, "off", 0):
            self._crc_request = CRC_OFF
        elif crc in ("auto", True):
            self._crc_request = CRC_2BYTE
        elif crc in (1, 2):
            self._crc_request = int(crc)
        else:
            raise ValueError(f'crc must be "auto", "off", 1 or 2, not {crc!r}')

        if revision is None:
            self._revision_is_custom = False
            self._revision = RevisionRegistry.get_revision(HardwareVersion.SHIMMER3)
        else:
            self._revision_is_custom = True
            self._revision = revision
        # If no specific revision is provided, we assume a Shimmer3. During initialization the actual revision
        # of the device is queried and used.

        self._raw_serial = serial
        self._serial = BluetoothSerial(serial)
        self._bluetooth = BluetoothRequestHandler(
            self._serial, self._revision, tolerant=tolerant, lookahead=lookahead
        )

        self._thread = Thread(
            target=self._run_readloop, daemon=True, name="pyshimmer-read"
        )
        self._watchdog = Thread(
            target=self._run_watchdog, daemon=True, name="pyshimmer-watchdog"
        )
        self._stop_watchdog = Event()

        self._initialized = False
        self._disable_ack = disable_status_ack
        self._command_timeout = command_timeout
        self._stall_timeout = stall_timeout
        self._start_timeout = start_timeout
        self._presync = presync

        self._error: ShimmerError | None = None
        self._error_lock = threading.Lock()
        self._error_cbs: list[Callable[[ShimmerError], None]] = []
        self._closing = False

        self._streaming = False
        self._stream_started = 0.0
        self._packets_at_start = 0

        self._fw_type: FirmwareType | None = None
        self._fw_version: FirmwareVersion | None = None
        self._fw_caps: FirmwareCapabilities | None = None
        self._hw_version: HardwareVersion | None = None

    # ------------------------------------------------------------------------------------------- properties
    @property
    def initialized(self) -> bool:
        """Specifies if the connection was initialized

        This property helps to determine if the capabilities property will return a valid value.

        :return: True if initialize() was called, otherwise False
        """
        return self._initialized

    @property
    def firmware_type(self) -> FirmwareType | None:
        """Return the firmware type being run on the device

        This property shall only be accessed after invoking initialize().
        """
        return self._fw_type

    @property
    def firmware_version(self) -> FirmwareVersion | None:
        """Return the firmware version of the device

        This property shall only be accessed after invoking initialize().
        """
        return self._fw_version

    @property
    def hardware_version(self) -> HardwareVersion | None:
        """Return the hardware version of the device

        This property shall only be accessed after invoking initialize().
        """
        return self._hw_version

    @property
    def hardware_revision(self) -> HardwareRevision:
        """Return the hardware revision instance being used for communication"""
        return self._revision

    @property
    def capabilities(self) -> FirmwareCapabilities:
        """Return the capabilities of the device firmware

        This property shall only be accessed after invoking initialize().

        :return: A FirmwareCapabilities instance representing the version and capabilities of the firmware
        """
        return self._fw_caps

    @property
    def connected(self) -> bool:
        """True while the link is believed to be usable: initialized, not failed and not shut down"""
        return self._initialized and self._error is None and not self._closing

    @property
    def last_error(self) -> ShimmerError | None:
        """The error that ended the link, if it has failed"""
        return self._error

    @property
    def crc_len(self) -> int:
        """The number of CRC bytes currently in use (0 when the CRC mode is off)"""
        return self._bluetooth.crc_len

    @property
    def link_stats(self) -> dict:
        """Counters of what the receiver has seen: packets, CRC errors, skipped bytes, timeouts..."""
        return self._bluetooth.stats.as_dict()

    @property
    def last_data_age(self) -> float:
        """Seconds since something was last received from the device"""
        return time.monotonic() - self._bluetooth.last_rx

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self.shutdown()

    # --------------------------------------------------------------------------------------- error handling
    def add_error_callback(self, cb: Callable[[ShimmerError], None]) -> None:
        """Add a callback that is called once when the link fails

        The callback runs in the thread that detected the failure (read loop or watchdog). It must not block
        and must not call API methods that wait for the device.

        :param cb: a function with a single argument, the :class:`ShimmerLinkError`
        """
        self._error_cbs += [cb]

    def remove_error_callback(self, cb: Callable[[ShimmerError], None]) -> None:
        """Remove the callback from the list of active callbacks

        :param cb: The callback function to remove
        """
        self._error_cbs.remove(cb)

    def _fail(self, error: ShimmerError) -> None:
        with self._error_lock:
            if self._error is not None:
                return
            self._error = error

        _log.error("The link to the Shimmer failed: %s", error)
        self._bluetooth.fail_all(error)
        for cb in list(self._error_cbs):
            try:
                cb(error)
            except Exception:  # noqa: BLE001
                _log.exception("Error callback %r raised", cb)

    # ------------------------------------------------------------------------------------------- lifecycle
    def _set_fw_capabilities(self) -> None:
        self._fw_type, self._fw_version = self.get_firmware_version()
        self._fw_caps = FirmwareCapabilities(self._fw_type, self._fw_version)

        reported = None
        try:
            reported = self.get_device_hardware_version()
        except (ShimmerTimeoutError, CommandRefused):
            # A firmware that does not answer must not keep us from connecting: the device is taken for a
            # Shimmer3 unless it says otherwise
            _log.warning("The device did not report its hardware version")

        self._hw_version = reported if reported is not None else HardwareVersion.UNKNOWN

        # If the revision was set manually, it is not replaced
        if not self._revision_is_custom and reported is not None:
            # ValueError: the device reports a hardware version this API has no revision for
            revision = RevisionRegistry.get_revision(reported)
            self._revision = self._bluetooth.hardware_revision = revision

    def _do_presync(self) -> None:
        """Stop a stream a previous session may have left running and wait until the line is quiet

        This runs on the raw serial port, before the read loop exists: whatever the device was sending has no
        known layout yet and could not be framed.
        """
        ser = self._raw_serial
        old_timeout = getattr(ser, "timeout", None)
        t0 = time.monotonic()
        quiet, first_reply_budget, give_up = 0.4, 3.0, t0 + 10.0
        try:
            ser.timeout = 0.1
            ser.reset_input_buffer()
            ser.write(bytes((STOP_STREAMING_COMMAND,)))

            heard, quiet_since = False, t0
            while True:
                now = time.monotonic()
                if ser.read(4096):
                    heard, quiet_since = True, time.monotonic()
                elif heard and now - quiet_since >= quiet:
                    break
                elif not heard and now - t0 >= first_reply_budget:
                    break  # a silent device: nothing to drain (the first command after a cold connect takes ~1.5 s)
                if now > give_up:
                    raise ShimmerLinkError(
                        "The device keeps sending data after STOP_STREAMING"
                    )

            ser.reset_input_buffer()
        except (SerialException, OSError) as e:
            raise ShimmerLinkError(
                f"The serial port failed while synchronizing: {e!r}"
            ) from e
        finally:
            try:
                ser.timeout = old_timeout
            except Exception:  # noqa: BLE001
                pass

    def _setup_crc(self) -> None:
        if self._crc_request == CRC_OFF or not self.capabilities.supports_crc:
            return

        crc_len = self._crc_request
        # The acknowledgment of this very command carries the CRC, so switch the receiver first.
        self._bluetooth.set_crc_len(crc_len)
        try:
            self._process_and_wait(SetCrcCommand(self._revision, crc_len))
        except ShimmerTimeoutError:
            _log.warning(
                "The device did not confirm the CRC mode, continuing without CRC"
            )
            self._bluetooth.set_crc_len(CRC_OFF)
            try:
                self._process_and_wait(SetCrcCommand(self._revision, CRC_OFF))
            except ShimmerTimeoutError:
                pass

    def initialize(self) -> None:
        """Initialize the Bluetooth connection

        This method must be invoked before sending commands to the Shimmer. It stops a stream that may still be
        running from a previous session, starts the read loop, queries the Shimmer version, optionally disables
        the status acknowledgment and enables the CRC mode.

        :raises ShimmerLinkError: If the link is unusable (e.g. the device hung up while connecting)
        :raises ShimmerTimeoutError: If the device does not answer
        """
        try:
            if self._presync:
                self._do_presync()

            self._thread.start()
            self._watchdog.start()
            self._set_fw_capabilities()

            if self.capabilities.supports_ack_disable and self._disable_ack:
                self.set_status_ack(enabled=False)

            self._setup_crc()
        except BaseException:
            self.shutdown()
            raise

        self._initialized = True

    def shutdown(self) -> None:
        """Shutdown the read loop

        Shutdown the read loop by stopping the read thread. Safe to call more than once and on a link that has
        failed. Requests that are still outstanding are released.
        """
        self._closing = True
        self._stop_watchdog.set()

        try:
            self._serial.cancel_read()
        except Exception:  # noqa: BLE001
            pass

        for t in (self._thread, self._watchdog):
            if t.ident is not None and t is not threading.current_thread():
                t.join(2.0)

        try:
            self._raw_serial.close()
        except Exception:  # noqa: BLE001
            pass
        self._bluetooth.clear_queues()

    @classmethod
    def connect(
        cls,
        port: str,
        *,
        baudrate: int = DEFAULT_BAUDRATE,
        retries: int = 3,
        retry_delay: float = 0.3,
        lock_path: str | None = None,
        lock_timeout: float = 60.0,
        min_connect_interval: float = 0.0,
        serial_factory: Callable[[str, int], Serial] | None = None,
        on_retry: Callable[[int, BaseException], None] | None = None,
        **kwargs,
    ) -> ShimmerBluetooth:
        """Open the port and initialize the device, retrying when the connection fails

        Opening a Bluetooth serial port can fail transiently: a Shimmer that has not been connected for a while
        was seen to refuse the first attempt and accept the retry immediately after. The port is closed and
        reopened for every attempt.

        :param port: The serial device, e.g. ``/dev/rfcomm0``
        :param baudrate: Baudrate to open the port with (ignored by rfcomm)
        :param retries: How many times to retry after the first attempt failed
        :param retry_delay: Seconds before the first retry; later retries wait proportionally longer
        :param lock_path: If given, connection attempts are serialized across processes with an ``flock`` on
            this file. Several Shimmers on one Bluetooth adapter cannot be connected at the same moment.
        :param lock_timeout: How long to wait for that lock
        :param min_connect_interval: With ``lock_path``, minimum seconds between two connection attempts
        :param serial_factory: Callable ``(port, baudrate) -> Serial`` to use instead of :class:`serial.Serial`
        :param on_retry: Called with the attempt number and the error after each failed attempt
        :param kwargs: Passed on to the constructor
        :return: An initialized instance
        :raises ShimmerConnectError: If every attempt failed
        """
        factory = serial_factory or (lambda p, b: Serial(p, b))
        last: BaseException | None = None

        for attempt in range(retries + 1):
            ser = None
            try:
                guard = (
                    ConnectLock(lock_path, lock_timeout, min_connect_interval)
                    if lock_path
                    else nullcontext()
                )
                with guard:
                    ser = factory(port, baudrate)
                    dev = cls(ser, **kwargs)
                    dev.initialize()
                return dev
            except (ShimmerConnectError,):
                raise
            except (ShimmerError, SerialException, OSError) as e:
                last = e
                _log.warning(
                    "Connection attempt %d/%d to %s failed: %r",
                    attempt + 1,
                    retries + 1,
                    port,
                    e,
                )
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:  # noqa: BLE001
                        pass
                if on_retry is not None:
                    on_retry(attempt + 1, e)
                if attempt < retries:
                    time.sleep(retry_delay * (attempt + 1))

        raise ShimmerConnectError(
            f"Could not connect to {port} after {retries + 1} attempts: {last!r}"
        ) from last

    # --------------------------------------------------------------------------------------- read loop
    def _run_readloop(self):
        try:
            while True:
                self._bluetooth.process_single_input_event()

        except ReadAbort:
            if self._closing:
                _log.debug("Read loop exiting after cancel request")
            else:
                self._fail(
                    ShimmerLinkError(
                        "The serial read returned early without a cancel request"
                    )
                )

        except (
            BaseException
        ) as e:  # noqa: BLE001 - whatever it is, the link is gone and callers must know
            if self._closing:
                _log.debug("Read loop ended by the shutdown: %r", e)
                return
            err = ShimmerLinkError(f"The read loop crashed: {e!r}")
            err.__cause__ = e
            self._fail(err)

    def _run_watchdog(self):
        """Declare the link dead when a running stream goes silent. Passive: it never sends anything."""
        while not self._stop_watchdog.wait(0.25):
            if not self._streaming or self._error is not None:
                continue

            now = time.monotonic()
            if self._bluetooth.stats.data_packets == self._packets_at_start:
                # The device takes ~1.5 s to start sampling after it acknowledged the start command
                if (
                    self._start_timeout
                    and now - self._stream_started > self._start_timeout
                ):
                    self._stalled(
                        f"No data arrived within {self._start_timeout} s of starting the stream"
                    )
                continue

            idle = now - self._bluetooth.last_data
            if self._stall_timeout and idle > self._stall_timeout:
                self._stalled(
                    f"No data from the device for {idle:.1f} s while streaming"
                )

    def _stalled(self, why: str) -> None:
        self._fail(ShimmerLinkError(why))
        try:
            self._serial.cancel_read()  # let the blocked read loop end
        except Exception:  # noqa: BLE001
            pass

    def _process_and_wait(self, cmd):
        if self._error is not None:
            raise self._error

        timeout = self._command_timeout
        deadline = None if timeout is None else time.monotonic() + timeout

        def remaining() -> float | None:
            return None if deadline is None else max(0.0, deadline - time.monotonic())

        try:
            compl_obj, return_obj = self._bluetooth.queue_command(cmd)
        except (SerialException, OSError) as e:
            err = ShimmerLinkError(f"Writing to the device failed: {e!r}")
            err.__cause__ = e
            self._fail(err)
            raise self._error

        try:
            compl_obj.wait(remaining())

            if return_obj is not None:
                return return_obj.wait(remaining())
            return None
        except ShimmerTimeoutError:
            self._bluetooth.cancel(compl_obj)
            self._bluetooth.stats.timeouts += 1
            raise ShimmerTimeoutError(
                f"{type(cmd).__name__} was not answered within {timeout} s"
            ) from None

    def add_stream_callback(self, cb: Callable[[DataPacket], None]) -> None:
        """Add a stream callback which is called when a new data packet arrives

        An exception raised by the callback is logged and counted (``link_stats['callback_errors']``) but does not
        end the stream.

        :param cb: a function with a single argument
        """
        self._bluetooth.add_stream_callback(cb)

    def remove_stream_callback(self, cb: Callable[[DataPacket], None]) -> None:
        """Remove the callback from the list of active callbacks

        :param cb: The callback function to remove
        """
        self._bluetooth.remove_stream_callback(cb)

    def add_status_callback(self, cb: Callable[[list[bool]], None]) -> None:
        """Add a status callback which is called when a new status update from the Shimmer arrives

        :param cb: a function with a single argument. The argument is the same value is the return value of the
            :meth:`pyshimmer.bluetooth.bt_api.ShimmerBluetooth.get_status` method.
        """
        self._bluetooth.add_status_callback(cb)

    def remove_status_callback(self, cb: Callable[[list[bool]], None]) -> None:
        """Remove the callback from the list of active callbacks

        :param cb: The callback function to remove
        """
        self._bluetooth.remove_status_callback(cb)

    def get_sampling_rate(self) -> float:
        """Retrieve the sampling rate of the device

        :return: The sampling rate as floating point value in samples per second
        """
        return self._process_and_wait(GetSamplingRateCommand(self._revision))

    def set_sampling_rate(
        self, sr: float, update_exg_rate: bool = False, exg_chips: Iterable[int] = (0,)
    ) -> None:
        """Set the active sampling rate for the device

        The command only sets the rate of the microcontroller. The ExG chips of an EMG/ECG unit keep the data
        rate they were configured with (e.g. in Consensys) and the device then returns the same conversion
        several times when the chip is slower than the sampling rate. The official APIs adapt the chip whenever
        the sampling rate changes; do the same with ``update_exg_rate=True``.

        :param sr: The sampling rate in Hertz
        :param update_exg_rate: Also set the data rate of the ExG chips to the smallest supported rate that is
            not below the sampling rate (see :func:`pyshimmer.dev.exg.exg_data_rate_for_sampling_rate`). Only for
            devices with ExG chips.
        :param exg_chips: The chips to update, 0 is the one EMG uses (the official API updates both)
        """
        self._process_and_wait(SetSamplingRateCommand(self._revision, sr))
        if update_exg_rate:
            self.set_exg_data_rate(exg_data_rate_for_sampling_rate(sr), exg_chips)

    def get_exg_data_rate(self, chip_id: int = 0) -> int:
        """Get the data rate of an ExG chip

        :param chip_id: The ID of the chip, one of [0, 1]
        :return: The conversions per second of the chip, e.g. 1000
        """
        return self.get_exg_register(chip_id).data_rate

    def set_exg_data_rate(self, rate_sps: int, chips: Iterable[int] = (0,)) -> None:
        """Set the data rate of the ExG chips, leaving the rest of their configuration as it is

        Only the three data rate bits of the CONFIG1 register are written, and the result is read back.
        The setting is volatile: it is not stored in the device configuration.

        :param rate_sps: One of 125, 250, 500, 1000, 2000, 4000, 8000
        :param chips: The IDs of the chips to configure
        :raises ShimmerError: If the device did not take the setting
        """
        for chip in chips:
            current = self.get_exg_register(chip)
            wanted = current.with_data_rate(rate_sps)
            if wanted.binary[0] == current.binary[0]:
                continue

            self.set_exg_register(chip, 0, bytes((wanted.binary[0],)))
            back = self.get_exg_register(chip)
            if back.data_rate != rate_sps:
                raise ShimmerError(
                    f"ExG chip {chip} reports {back.data_rate} SPS after setting {rate_sps} SPS"
                )

    def get_battery_state(self, in_percent: bool) -> float:
        """Retrieve the battery state of the device

        :param in_percent: True: calculate battery state in percent; False: calculate battery state in Volt
        :return: The battery state in percent / Volt
        """
        return self._process_and_wait(GetBatteryCommand(self._revision, in_percent))

    def get_battery_reading(self) -> BatteryReading:
        """Retrieve the battery voltage, the capacity estimated from it and the
        charging status

        Unlike :meth:`get_battery_state` it returns the charging status byte of the
        response as well. It can be called while the device streams; every in-stream
        command costs a few data packets (about 5 at 2048 Hz).

        :return: The reading of the battery
        """
        return self._process_and_wait(GetBatteryReadingCommand(self._revision))

    def get_expansion_board(self) -> ExpansionBoard:
        """Retrieve the identification of the expansion board under the Shimmer3

        :return: The board: for the ECG and EMG units, ``board.has_exg`` is True
        """
        return self._process_and_wait(GetExpansionBoardCommand(self._revision))

    def get_config_time(self) -> int:
        """Get the config time from the device as configured in the configuration file

        :return: The config time as integer
        """
        return self._process_and_wait(GetConfigTimeCommand(self._revision))

    def set_config_time(self, time: int) -> None:
        """Set the config time of the device

        :arg time: The configuration time that will be set in the configuration of the Shimmer
        """
        self._process_and_wait(SetConfigTimeCommand(self._revision, time))

    def set_sensors(self, sensors: Iterable[ESensorGroup]) -> None:
        """Set the active sensors for sampling

        This command will activate the specified list of sensors and deactivate all other sensors.

        :param sensors: A list of sensors to activate
        """
        self._process_and_wait(SetSensorsCommand(self._revision, sensors))

    def get_rtc(self) -> float:
        """Retrieve the current value of the onboard real-time clock

        :return: The current time of the device in seconds as UNIX timestamp
        """
        return self._process_and_wait(GetRealTimeClockCommand(self._revision))

    def set_rtc(self, time_sec: float) -> None:
        """Set the value of the onboard real-time clock

        Should be set as a UTC UNIX timestamp such that the resulting recordings have universal timestamps

        :param time_sec: The UNIX timestamp in seconds
        """
        self._process_and_wait(SetRealTimeClockCommand(self._revision, time_sec))

    def get_status(self) -> list[bool]:
        """Get the status of the device

        :return: A list of 8 bools which signal:
            dev_docked, dev_sensing, rtc_set, dev_logging, dev_streaming, sd_card_present, sd_error, status_red_led
        """
        return self._process_and_wait(GetStatusCommand(self._revision))

    def get_firmware_version(self) -> tuple[FirmwareType, FirmwareVersion]:
        """Get the version of the running firmware

        :return: The firmware type as enum, i.e. SDLog or LogAndStream
            and the numeric firmware version
        """
        fw_type, major, minor, rel = self._process_and_wait(
            GetFirmwareVersionCommand(self._revision)
        )
        fw_version = FirmwareVersion(major, minor, rel)

        return fw_type, fw_version

    def get_exg_register(self, chip_id: int) -> ExGRegister:
        """Get the current configuration of one of the two ExG registers of the device

        Note that this command only returns meaningful results if the device features ECG chips

        :param chip_id: The ID of the chip, one of [0, 1]
        :return: An ExGRegister object that presents the register contents in an easily processable manner
        """
        return self._process_and_wait(GetEXGRegsCommand(self._revision, chip_id))

    def get_all_calibration(self) -> AllCalibration:
        """Gets all calibration data from sensor
        :return: An AllCalibration object that presents the calibration contents in an easily processable manner
        """
        return self._process_and_wait(GetAllCalibrationCommand(self._revision))

    def set_exg_register(self, chip_id: int, offset: int, data: bytes) -> None:
        """Configure part of the memory of the ExG registers

        :param chip_id: The ID of the chip, one of [0, 1]
        :param offset: The offset at which to write the data bytes
        :param data: The data bytes to write
        """
        self._process_and_wait(SetEXGRegsCommand(self._revision, chip_id, offset, data))

    def write_exg_register(
        self, chip_id: int, register: ExGRegister, verify: bool = True
    ) -> ExGRegister:
        """Write the ten configuration bytes of an ExG chip and read them back

        The setting is volatile: the device goes back to the configuration it stores
        when it is switched off.

        :param chip_id: The ID of the chip, one of [0, 1]
        :param register: The configuration to write
        :param verify: Raise an error if what the device reports afterwards differs
            from what was written (the bits that belong to the device are not compared,
            see :meth:`ExGRegister.same_configuration`)
        :return: What the device reports after the write
        :raises ShimmerConfigurationError: If the device did not take the configuration
        """
        self.set_exg_register(chip_id, 0, register.binary)
        back = self.get_exg_register(chip_id)
        if verify and not register.same_configuration(back):
            raise ShimmerConfigurationError(
                f"ExG chip {chip_id} did not take the configuration: wrote "
                f"{fmt_hex(register.binary)}, it reports {fmt_hex(back.binary)}"
            )
        return back

    def get_device_name(self) -> str:
        """Retrieve the device name

        :return: The device name as string
        """
        return self._process_and_wait(GetDeviceNameCommand(self._revision))

    def set_device_name(self, dev_name: str) -> None:
        """Set the device name

        :param dev_name: The device name to set
        """
        self._process_and_wait(SetDeviceNameCommand(self._revision, dev_name))

    def get_device_hardware_version(self) -> HardwareVersion:
        """Retrieve the device hardware version

        :return: The device hardware version as string
        """
        return self._process_and_wait(GetShimmerHardwareVersion(self._revision))

    def get_experiment_id(self) -> str:
        """Retrieve the experiment id as string

        :return: The experiment ID as string
        """
        return self._process_and_wait(GetExperimentIDCommand(self._revision))

    def set_experiment_id(self, exp_id: str) -> None:
        """Set the experiment ID for the device

        :param exp_id: The id to set for the device
        """
        self._process_and_wait(SetExperimentIDCommand(self._revision, exp_id))

    def get_inquiry(self) -> tuple[float, int, list[EChannelType]]:
        """Perform inquiry command

        :return: A tuple of 3 values:
            - The sampling rate as float
            - The buf size of the device
            - The active data channels of the device as list, does not include the TIMESTAMP channel
        """
        if self._hw_version == HardwareVersion.SHIMMER3R:
            cmd = Shimmer3RInquiryCommand(self._revision)
        else:
            cmd = Shimmer3InquiryCommand(self._revision)

        return self._process_and_wait(cmd)

    def get_data_types(self):
        """Get the active data channels of the device

        These data channels will be contained in a DataPacket when streaming

        :return: A list of data channels, always containing the TIMESTAMP channel
        """
        _, _, ctypes = self.get_inquiry()
        # The Timestamp is always present in data packets
        ctypes = [EChannelType.TIMESTAMP] + ctypes

        return ctypes

    def _check_exg_configuration(self, sampling_rate: float) -> None:
        reg = self.get_exg_register(0)
        if reg.uses_test_signal:
            _log.warning(
                "The ExG chip routes its internal test signal to the inputs: the data is not EMG/ECG. "
                "Use ExGRegister.emg_preset() with set_exg_register() to switch to the electrodes."
            )
        if reg.data_rate < sampling_rate:
            _log.warning(
                "The ExG chip converts %d times per second but the sampling rate is %.0f Hz: about "
                "%.0f%% of the samples will be repeats. Use set_sampling_rate(..., update_exg_rate=True).",
                reg.data_rate,
                sampling_rate,
                100.0 * (1 - reg.data_rate / sampling_rate),
            )

    def start_streaming(
        self, wait_for_data: float | None = None, check_exg: bool = True
    ) -> None:
        """Start streaming data

        :param wait_for_data: If given, do not return before the first data packet has arrived, and fail with
            :class:`ShimmerTimeoutError` if none arrives within that many seconds. A device needs about 1.5 s
            (up to 5 s) to start sampling after it acknowledged the command, and starting several devices on
            one Bluetooth adapter is only reliable when each one is up before the next connects: hold a
            :class:`ConnectLock` until this returns.
        :param check_exg: When ExG channels are streamed, read the configuration of the ExG chip first and log
            a warning if it routes the test signal or converts less often than the sampling rate.
        """
        sr, _, ctypes = self.get_inquiry()
        # The Timestamp is always present in data packets
        ctypes = [EChannelType.TIMESTAMP] + ctypes

        stream_types = [(t, self._revision.get_channel_dtype(t)) for t in ctypes]
        clock_rate = 1.0 / self._revision.ticks2sec(
            1
        )  # ticks of the device clock per second
        self._bluetooth.set_stream_types(
            stream_types, period_ticks=clock_rate / sr if sr else None
        )

        # Measured on LogAndStream 0.15.4 (RN42 module): 22 kB/s streams without losses, 26 kB/s loses a few
        # samples per minute. The CRC adds bytes to every packet, so it is the first thing to give up at the top.
        wire_rate = (
            sr * (1 + self._bluetooth._payload_len + self._bluetooth.crc_len)
            if sr
            else 0
        )
        if self._bluetooth.crc_len and wire_rate > self._CRC_THROUGHPUT_WARNING:
            _log.warning(
                "The stream needs %.0f kB/s with the %d-byte CRC (%.0f kB/s without): close to what the "
                'Bluetooth module sustains, and packets are lost occasionally. Consider crc="off" '
                "(frames are then validated by the continuity of the timestamps) or crc=1.",
                wire_rate / 1000,
                self._bluetooth.crc_len,
                sr * (1 + self._bluetooth._payload_len) / 1000,
            )

        if check_exg and sr and any(is_exg_ch(t) for t in ctypes):
            try:
                self._check_exg_configuration(sr)
            except ShimmerError as e:  # purely informative
                _log.debug("Could not check the ExG configuration: %r", e)

        self._process_and_wait(StartStreamingCommand(self._revision))
        self._packets_at_start = self._bluetooth.stats.data_packets
        self._stream_started = time.monotonic()
        self._streaming = True

        if wait_for_data is not None:
            deadline = time.monotonic() + wait_for_data
            while self._bluetooth.stats.data_packets == self._packets_at_start:
                if self._error is not None:
                    raise self._error
                if time.monotonic() > deadline:
                    raise ShimmerTimeoutError(
                        f"No data arrived within {wait_for_data} s of starting the stream"
                    )
                time.sleep(0.01)

    def stop_streaming(self) -> None:
        """Stop streaming data

        Note that the interface will possibly return more data packets that have already been received and are in the
        input buffer.

        """
        self._streaming = False
        self._process_and_wait(StopStreamingCommand(self._revision))

    def start_logging(self) -> None:
        """Start logging data to the SD card of the device"""
        self._process_and_wait(StartLoggingCommand(self._revision))

    def stop_logging(self) -> None:
        """Stop logging data to the SD card of the device"""
        self._process_and_wait(StopLoggingCommand(self._revision))

    def send_ping(self) -> None:
        """Send a ping command to the device

        The command can be used to test the connection. It does not return anything.
        """
        self._process_and_wait(DummyCommand(self._revision))

    def set_status_ack(self, enabled: bool) -> None:
        """Send a command to enable or disable the status acknowledgment

        This command should normally not be called directly. If enabled in the constructor, the command
        will automatically be sent to the Shimmer if the firmware supports it. It can be used to make
        vanilla firmware versions compatible with the state machine of the Python API.

        :param enabled: If set to True, enable status acknowledgment byte. This will make the
            firmware incompatible to the Python API. If set to False, disable sending the status ack.
            In this state, the firmware is compatible to the Python API.
        """
        self._process_and_wait(SetStatusAckCommand(self._revision, enabled))
