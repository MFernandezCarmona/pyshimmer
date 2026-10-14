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
"""A LogAndStream device on the master side of a pty, for tests of the Bluetooth API without hardware.

It answers the commands the API needs, streams EXG packets that carry a sequence number in the CH1 value, can
switch to the CRC mode and lets a test inject faults (raw bytes, ignored commands, a stall, a hang-up).
"""
import os
import select
import struct
import threading
import time
from typing import Optional, Set

from pyshimmer.bluetooth.bt_crc import crc16

ACK = b'\xff'
NACK = b'\xfe'

# command code -> number of argument bytes
_ARGS = {0x01: 0, 0x03: 0, 0x05: 2, 0x07: 0, 0x08: 3, 0x20: 0, 0x2C: 0, 0x2E: 0, 0x3F: 0, 0x63: 3, 0x72: 0,
         0x8B: 1, 0x8F: 8, 0x91: 0, 0x92: 0, 0x93: 0, 0x95: 0, 0x96: 0, 0xA3: 1}


class FakeDevice:
    """
    :arg master_fd: The master side of the pty the API is connected to
    :arg fw: ``(type, major, minor, rel)`` reported by the firmware version command
    :arg sampling_dr: Sampling period in ticks of the 32768 Hz clock
    :arg reply_delay: Seconds the device needs to answer a command
    """

    # EXG1 24 bit: status (1 byte), CH1 (3 bytes), CH2 (3 bytes). Inquiry indices 0x1D, 0x1E, 0x1F.
    CHANNELS = b'\x1d\x1e\x1f'
    PAYLOAD_LEN = 3 + 1 + 3 + 3

    def __init__(self, master_fd: int, fw=(3, 0, 15, 4), sampling_dr: int = 64, reply_delay: float = 0.0):
        self.fd = master_fd
        self.fw = fw
        self.dr = sampling_dr
        self.reply_delay = reply_delay

        self.crc_len = 0
        self.streaming = False
        self.ignore: Set[int] = set()
        self.refuse: Set[int] = set()   # command codes the device answers with a NACK
        self.hw_version = 3             # what the hardware version command reports: 3 is a Shimmer3
        self.commands = []
        self.exg_regs = bytearray(b'\x04\xa8\x10\x69\x60\x20\x00\x00\x02\x03')
        self.sent_packets = 0

        self._seq = 0
        self._wlock = threading.Lock()
        self._stop = threading.Event()
        self._muted = False
        self._hung_up = False
        self._inject = []
        self._threads = [threading.Thread(target=self._serve, daemon=True),
                         threading.Thread(target=self._stream, daemon=True)]
        for t in self._threads:
            t.start()

    # ---------------------------------------------------------------------------------------------- test hooks
    def inject(self, data: bytes) -> None:
        """Send raw bytes the next time the stream thread runs (between two packets)"""
        self._inject.append(data)

    def inject_now(self, data: bytes) -> None:
        """Send raw bytes immediately, outside the framing"""
        self._write(data)

    def mute(self) -> None:
        """Stop sending anything, without hanging up"""
        self._muted = True

    def hang_up(self) -> None:
        """Close the master: the other side sees a hang-up"""
        self._hung_up = True
        self._stop.set()
        try:
            os.close(self.fd)
        except OSError:
            pass

    def close(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(1.0)

    # ------------------------------------------------------------------------------------------------ framing
    def _write(self, data: bytes) -> None:
        if self._muted or self._hung_up or not data:
            return
        with self._wlock:
            try:
                os.write(self.fd, data)
            except OSError:
                pass

    def _with_crc(self, frame: bytes) -> bytes:
        return frame + crc16(frame)[:self.crc_len] if self.crc_len else frame

    @staticmethod
    def value(seq: int, channel: int = 0) -> int:
        """Sample value of a packet: pseudo-random (like real EMG), but invertible by a test via a dict.

        A plain counter would be periodic and look like a timestamp, which is not what real data does.
        """
        return ((seq + 1 + channel * 7919) * 0x9E3779B1) & 0x7FFFFF | 0x010101

    def packet(self, seq: int) -> bytes:
        ts = (seq * self.dr) & 0xFFFFFF
        payload = struct.pack('<I', ts)[:3] + b'\x80' + struct.pack('>i', self.value(seq, 0))[1:] + \
            struct.pack('>i', self.value(seq, 1))[1:]
        return self._with_crc(b'\x00' + payload)

    def _reply(self, body: bytes = b'') -> bytes:
        return self._with_crc(ACK + body)

    # ------------------------------------------------------------------------------------------------ threads
    def _stream(self) -> None:
        next_t = time.monotonic()
        while not self._stop.is_set():
            if not self.streaming:
                time.sleep(0.002)
                next_t = time.monotonic()
                continue

            for raw in self._inject[:]:
                self._inject.remove(raw)
                self._write(raw)

            self._write(self.packet(self._seq))
            self._seq += 1
            self.sent_packets += 1
            next_t += self.dr / 32768.0
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.monotonic()

    def _serve(self) -> None:
        buf = bytearray()
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self.fd], [], [], 0.02)
                if not ready:
                    continue
                data = os.read(self.fd, 4096)
            except (OSError, ValueError):
                return
            if not data:
                return
            buf += data

            while buf:
                code = buf[0]
                need = 1 + _ARGS.get(code, 0)
                if code == 0x61:  # SET_EXG_REGS carries a variable number of bytes: chip, offset, length, data
                    if len(buf) < 4:
                        break
                    need = 4 + buf[3]
                if len(buf) < need:
                    break
                args, buf = bytes(buf[1:need]), buf[need:]
                self.commands.append((code, args))
                if code in self.ignore:
                    continue
                if self.reply_delay:
                    time.sleep(self.reply_delay)
                if code in self.refuse:
                    self._write(self._with_crc(NACK))
                    continue
                self._handle(code, args)

    def _handle(self, code: int, a: bytes) -> None:
        if code == 0x8B:  # SET_CRC: the acknowledgment already carries the CRC of the new mode
            self.crc_len = a[0]
            self._write(self._reply())
        elif code == 0x2E:
            self._write(self._reply(b'\x2f' + struct.pack('<HHBB', *self.fw)))
        elif code == 0x3F:
            self._write(self._reply(b'\x25' + bytes((self.hw_version,))))
        elif code == 0x03:
            self._write(self._reply(b'\x04' + struct.pack('<H', self.dr)))
        elif code == 0x05:
            self.dr = struct.unpack('<H', a)[0]
            self._write(self._reply())
        elif code == 0x01:
            self._write(self._reply(b'\x02' + struct.pack('<HIBB', self.dr, 0, len(self.CHANNELS), 8) + self.CHANNELS))
        elif code == 0x07:
            self._write(self._reply())
            self.streaming = True
        elif code == 0x20:
            self.streaming = False
            self._write(self._reply())
        elif code == 0x72:
            status = (self.streaming << 1) | (1 << 2) | (self.streaming << 4) | (1 << 5)
            self._write(self._reply(b'\x8a\x71' + bytes((status,))))
        elif code == 0x63:
            n = a[2]
            self._write(self._reply(b'\x62' + bytes((n,)) + bytes(self.exg_regs[a[1]:a[1] + n])))
        elif code == 0x61:  # SET_EXG_REGS: chip, offset, length, data
            off, ln = a[1], a[2]
            self.exg_regs[off:off + ln] = a[3:3 + ln]
            self._write(self._reply())
        elif code == 0x95:
            self._write(self._reply(b'\x8a\x94\x10\x0e\x00'))
        else:  # set commands, dummy, ack-disable...: only acknowledged
            self._write(self._reply())

    def push_status(self, status: int = 0x24) -> None:
        """An unsolicited status update (no acknowledgment in front)"""
        self._write(self._with_crc(b'\x8a\x71' + bytes((status,))))
