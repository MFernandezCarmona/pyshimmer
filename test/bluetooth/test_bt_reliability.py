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
"""Tests of how the Bluetooth API behaves when the link misbehaves."""

import os
import pty
import threading
import time
from typing import List, Optional
from unittest import TestCase

from serial import Serial

from pyshimmer.bluetooth.bt_api import BluetoothRequestHandler, ShimmerBluetooth
from pyshimmer.bluetooth.bt_commands import (
    GetDeviceNameCommand,
    GetSamplingRateCommand,
    SetSamplingRateCommand,
    DataPacket,
)
from pyshimmer.bluetooth.bt_crc import crc16, crc_matches
from pyshimmer.bluetooth.bt_errors import (
    CommandRefused,
    ShimmerConfigurationError,
    ShimmerConnectError,
    ShimmerError,
    ShimmerLinkError,
    ShimmerTimeoutError,
)
from pyshimmer.bluetooth.bt_lock import ConnectLock
from pyshimmer.bluetooth.bt_serial import BluetoothSerial
from pyshimmer.dev.battery import ChargingStatus
from pyshimmer.dev.channels import EChannelType
from pyshimmer.dev.exg import ExGRegister
from pyshimmer.dev.revisions import HardwareVersion, RevisionRegistry
from test.bluetooth.fake_device import FakeDevice

REVISION = RevisionRegistry.get_revision(HardwareVersion.SHIMMER3)


def ts_of(k: int) -> int:
    """Device timestamps 0x40 ticks apart whose bytes cannot be mistaken for a frame header"""
    return 0x332211 + 0x40 * k


def adc_of(k: int) -> int:
    return 0x0A0B + 0x0101 * k


class _PtyDevice:
    """A pty pair with a FakeDevice on the master side and a Serial on the slave side"""

    def __init__(self, **device_kwargs):
        self.master, self.slave = pty.openpty()
        self.serial = Serial(os.ttyname(self.slave), 115200)
        self.device = FakeDevice(self.master, **device_kwargs)

    def close(self) -> None:
        self.device.close()
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            self.serial.close()
        except Exception:  # noqa: BLE001
            pass


class CrcTest(TestCase):
    """Vectors captured from a Shimmer3 running LogAndStream 0.15.4 with the CRC mode on"""

    def test_lone_ack(self):
        self.assertEqual(crc16(b"\xff"), bytes.fromhex("f465"))

    def test_replies_cover_the_acknowledgment(self):
        self.assertEqual(
            crc16(bytes.fromhex("ff2f030000000f04")), bytes.fromhex("0cd0")
        )
        self.assertEqual(crc16(bytes.fromhex("ff041000")), bytes.fromhex("f784"))
        self.assertEqual(crc16(bytes.fromhex("ff8a7124")), bytes.fromhex("51f8"))
        self.assertEqual(
            crc16(bytes.fromhex("ff02200001ff0109030" "11d1e1f")), bytes.fromhex("80b8")
        )

    def test_data_packets(self):
        self.assertEqual(
            crc16(bytes.fromhex("00c1ea84" "80009de500a0f7")), bytes.fromhex("6d63")
        )
        self.assertEqual(
            crc16(bytes.fromhex("00e1ea84" "80009ddd00a103")), bytes.fromhex("e12f")
        )

    def test_table_driven_implementation_equals_the_reference(self):
        import random
        from pyshimmer.bluetooth.bt_crc import _CRC_INIT, _crc_step_reference

        rnd = random.Random(1234)
        for _ in range(300):
            data = bytes(rnd.randrange(256) for _ in range(rnd.randrange(0, 60)))
            ref = _CRC_INIT
            for b in data:
                ref = _crc_step_reference(ref, b)
            if len(data) % 2:
                ref = _crc_step_reference(ref, 0)
            self.assertEqual(crc16(data), bytes((ref & 0xFF, (ref >> 8) & 0xFF)))

    def test_crc_matches(self):
        frame = bytes.fromhex("00c1ea84" "80009de500a0f7")
        self.assertTrue(crc_matches(frame, bytes.fromhex("6d63"), 2))
        self.assertTrue(crc_matches(frame, bytes.fromhex("6d"), 1))
        self.assertFalse(crc_matches(frame, bytes.fromhex("6d64"), 2))
        self.assertFalse(crc_matches(frame, bytes.fromhex("6d63"), 1))
        self.assertFalse(crc_matches(frame, b"", 0))


class TolerantHandlerTest(TestCase):
    TYPES = [EChannelType.TIMESTAMP, EChannelType.INTERNAL_ADC_A3]

    def setUp(self) -> None:
        self.master, self.slave = pty.openpty()
        self.serial = Serial(os.ttyname(self.slave), 115200)
        self.master_f = os.fdopen(self.master, "r+b", 0)
        self.bt = BluetoothSerial(self.serial)

    def tearDown(self) -> None:
        self.serial.close()
        self.master_f.close()
        os.close(self.slave)

    def handler(self, **kw) -> BluetoothRequestHandler:
        kw.setdefault("lookahead_timeout", 0.02)
        h = BluetoothRequestHandler(self.bt, REVISION, tolerant=True, **kw)
        h.set_stream_types([(t, REVISION.get_channel_dtype(t)) for t in self.TYPES])
        return h

    def feed(self, data: bytes) -> None:
        self.master_f.write(data)

    @staticmethod
    def pkt(ts: int, adc: int, crc_len: int = 0) -> bytes:
        body = b"\x00" + ts.to_bytes(3, "little") + adc.to_bytes(2, "little")
        return body + crc16(body)[:crc_len]

    def test_waiting_for_the_rest_of_a_frame_does_not_spin(self):
        h = self.handler(crc_len=2, frame_timeout=2.0)
        got: List[DataPacket] = []
        h.add_stream_callback(got.append)
        frame = self.pkt(ts_of(0), adc_of(0), 2)

        def late_second_part():
            time.sleep(0.4)
            self.feed(frame[4:])

        self.feed(frame[:4])
        threading.Thread(target=late_second_part, daemon=True).start()

        cpu0 = time.process_time()
        h.process_single_input_event()
        spent = time.process_time() - cpu0

        self.assertEqual(len(got), 1)
        self.assertLess(
            spent, 0.1, f"{spent:.3f} s of CPU while waiting 0.4 s: busy waiting"
        )

    def test_unexpected_ack_is_ignored(self):
        h = self.handler()
        self.feed(b"\xff")
        h.process_single_input_event()
        self.assertEqual(h.stats.unexpected_acks, 1)

    def test_stray_bytes_are_skipped_and_the_next_command_works(self):
        h = self.handler()
        self.feed(b"\x37\x38")
        h.process_single_input_event()
        h.process_single_input_event()
        self.assertEqual(h.stats.skipped_bytes, 2)

        compl, resp = h.queue_command(GetDeviceNameCommand(REVISION))
        self.feed(b"\xff\x7a\x05S_PPG")
        h.process_single_input_event()
        h.process_single_input_event()
        self.assertTrue(compl.has_completed())
        self.assertEqual(resp.get_result(), "S_PPG")

    def test_late_response_does_not_confuse_the_next_command(self):
        h = self.handler()
        compl, _ = h.queue_command(GetDeviceNameCommand(REVISION))
        h.cancel(compl)  # the caller gave up

        # Without a CRC a response nobody awaits cannot be told from garbage that starts with a response code:
        # it is skipped a byte at a time (decoding it could swallow the packets that follow)
        self.feed(b"\xff\x7a\x03ABC")
        for _ in range(6):
            h.process_single_input_event()  # the acknowledgment nobody expects, then the five bytes of the reply
        self.assertEqual(h.stats.unexpected_acks, 1)
        self.assertEqual(h.stats.skipped_bytes, 5)
        self.assertEqual(h.stats.late_responses, 0)

        compl, resp = h.queue_command(GetDeviceNameCommand(REVISION))
        self.feed(b"\xff\x7a\x02ok")
        h.process_single_input_event()
        h.process_single_input_event()
        self.assertEqual(resp.get_result(), "ok")

    def test_late_response_is_consumed_when_the_crc_vouches_for_it(self):
        h = self.handler(crc_len=2)
        compl, _ = h.queue_command(GetDeviceNameCommand(REVISION))
        h.cancel(compl)  # the caller gave up

        frame = b"\xff\x7a\x03ABC"
        self.feed(frame + crc16(frame))
        h.process_single_input_event()  # one frame: acknowledgment and response
        self.assertEqual(h.stats.late_responses, 1)
        self.assertEqual(h.stats.skipped_bytes, 0)

        compl, resp = h.queue_command(GetDeviceNameCommand(REVISION))
        frame = b"\xff\x7a\x02ok"
        self.feed(frame + crc16(frame))
        h.process_single_input_event()
        self.assertEqual(resp.get_result(), "ok")

    def test_response_is_matched_by_code_when_an_acknowledgment_went_missing(self):
        h = self.handler()
        # The device ignored the first command and only answers the second
        set_compl, _ = h.queue_command(SetSamplingRateCommand(REVISION, 100.0))
        get_compl, get_resp = h.queue_command(GetSamplingRateCommand(REVISION))

        self.feed(b"\xff\x04\x40\x00")
        h.process_single_input_event()
        h.process_single_input_event()

        self.assertTrue(get_resp.has_result())
        self.assertEqual(get_resp.get_result(), 512.0)
        self.assertTrue(get_compl.has_completed())

    def test_valid_packets_are_delivered(self):
        h = self.handler()
        got: List[DataPacket] = []
        h.add_stream_callback(got.append)

        self.feed(
            self.pkt(ts_of(0), adc_of(0))
            + self.pkt(ts_of(1), adc_of(1))
            + self.pkt(ts_of(2), adc_of(2))
        )
        for _ in range(3):
            h.process_single_input_event()

        self.assertEqual(
            [p[EChannelType.INTERNAL_ADC_A3] for p in got],
            [adc_of(0), adc_of(1), adc_of(2)],
        )
        self.assertEqual(h.stats.framing_errors, 0)

    def test_a_packet_that_lost_a_byte_is_never_delivered(self):
        h = self.handler()
        got: List[DataPacket] = []
        h.add_stream_callback(got.append)

        broken = self.pkt(ts_of(1), adc_of(1))
        broken = broken[:4] + broken[5:]  # one payload byte lost
        self.feed(
            self.pkt(ts_of(0), adc_of(0))
            + broken
            + self.pkt(ts_of(2), adc_of(2))
            + self.pkt(ts_of(3), adc_of(3))
        )
        for _ in range(20):
            h.process_single_input_event()
            if len(got) == 3:
                break

        self.assertEqual(
            [p[EChannelType.INTERNAL_ADC_A3] for p in got],
            [adc_of(0), adc_of(2), adc_of(3)],
        )
        self.assertGreaterEqual(h.stats.framing_errors, 1)

    def test_callback_exception_does_not_stop_the_stream(self):
        h = self.handler()
        got: List[int] = []

        def bad(_):
            raise RuntimeError("bug in user code")

        h.add_stream_callback(bad)
        h.add_stream_callback(lambda p: got.append(p[EChannelType.INTERNAL_ADC_A3]))

        self.feed(self.pkt(ts_of(0), adc_of(5)) + self.pkt(ts_of(1), adc_of(6)))
        h.process_single_input_event()
        h.process_single_input_event()

        self.assertEqual(got, [adc_of(5), adc_of(6)])
        self.assertEqual(h.stats.callback_errors, 2)

    def test_crc_mode_ack_and_response_form_one_frame(self):
        h = self.handler(crc_len=2)
        compl, resp = h.queue_command(GetSamplingRateCommand(REVISION))
        reply = b"\xff\x04\x40\x00"
        self.feed(reply + crc16(reply))
        h.process_single_input_event()

        self.assertTrue(compl.has_completed())
        self.assertEqual(resp.get_result(), 512.0)

    def test_crc_mode_lone_acknowledgment(self):
        h = self.handler(crc_len=2)
        compl, _ = h.queue_command(SetSamplingRateCommand(REVISION, 100.0))
        self.feed(b"\xff" + crc16(b"\xff"))
        h.process_single_input_event()
        self.assertTrue(compl.has_completed())

    def test_crc_mode_corrupt_packet_is_dropped_and_the_stream_resynchronizes(self):
        h = self.handler(crc_len=2)
        got: List[DataPacket] = []
        h.add_stream_callback(got.append)

        bad = bytearray(self.pkt(ts_of(1), adc_of(1), 2))
        bad[4] ^= 0x55  # corrupted payload byte, the CRC no longer matches
        self.feed(
            self.pkt(ts_of(0), adc_of(0), 2)
            + bytes(bad)
            + self.pkt(ts_of(2), adc_of(2), 2)
            + self.pkt(ts_of(3), adc_of(3), 2)
        )
        for _ in range(20):
            h.process_single_input_event()
            if len(got) == 3:
                break

        self.assertEqual(
            [p[EChannelType.INTERNAL_ADC_A3] for p in got],
            [adc_of(0), adc_of(2), adc_of(3)],
        )
        self.assertGreaterEqual(h.stats.crc_errors, 1)

    def test_crc_mode_lost_byte_does_not_cost_the_next_valid_packet(self):
        h = self.handler(crc_len=2)
        got: List[DataPacket] = []
        h.add_stream_callback(got.append)

        broken = self.pkt(ts_of(1), adc_of(1), 2)
        broken = broken[:2] + broken[3:]
        self.feed(
            self.pkt(ts_of(0), adc_of(0), 2) + broken + self.pkt(ts_of(2), adc_of(2), 2)
        )
        for _ in range(20):
            h.process_single_input_event()
            if len(got) == 2:
                break

        self.assertEqual(
            [p[EChannelType.INTERNAL_ADC_A3] for p in got], [adc_of(0), adc_of(2)]
        )

    def test_crc_mode_status_update(self):
        h = self.handler(crc_len=2)
        status: List[List[bool]] = []
        h.add_status_callback(status.append)

        frame = b"\x8a\x71\x21"
        self.feed(frame + crc16(frame))
        h.process_single_input_event()
        self.assertEqual(
            status, [[True, False, False, False, False, True, False, False]]
        )


class _ApiTestCase(TestCase):
    """Helpers to run a ShimmerBluetooth against a simulated device on a pty"""

    def setUp(self) -> None:
        self._open: List[_PtyDevice] = []
        self._devs: List[ShimmerBluetooth] = []

    def tearDown(self) -> None:
        for d in self._devs:
            d.shutdown()
        for p in self._open:
            p.close()

    def make(
        self,
        device_kwargs: Optional[dict] = None,
        initialize: bool = True,
        **api_kwargs,
    ):
        pd = _PtyDevice(**(device_kwargs or {}))
        self._open.append(pd)
        api_kwargs.setdefault("presync", False)
        api_kwargs.setdefault("crc", "off")
        dev = ShimmerBluetooth(pd.serial, **api_kwargs)
        self._devs.append(dev)
        if initialize:
            dev.initialize()
        return pd, dev

    @staticmethod
    def wait_for(cond, timeout: float = 3.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.01)
        return False

    def start_stream(self, dev: ShimmerBluetooth) -> List[DataPacket]:
        got: List[DataPacket] = []
        dev.add_stream_callback(got.append)
        dev.start_streaming()
        return got


class ShimmerBluetoothReliabilityTest(_ApiTestCase):

    # ------------------------------------------------------------------------------------- link failure
    def test_hang_up_fails_pending_and_later_requests_instead_of_blocking(self):
        pd, dev = self.make(command_timeout=10.0)
        errors: List[ShimmerError] = []
        dev.add_error_callback(errors.append)
        pd.device.ignore.add(0x03)

        result = {}

        def ask():
            try:
                dev.get_sampling_rate()
            except BaseException as e:  # noqa: BLE001
                result["exc"] = e

        t = threading.Thread(target=ask, daemon=True)
        t.start()
        time.sleep(0.2)
        t0 = time.monotonic()
        pd.device.hang_up()
        t.join(3.0)

        self.assertFalse(t.is_alive(), "a request stayed blocked after the link died")
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertIsInstance(result.get("exc"), ShimmerLinkError)
        self.assertEqual(len(errors), 1)
        self.assertFalse(dev.connected)
        self.assertIsInstance(dev.last_error, ShimmerLinkError)

        t0 = time.monotonic()
        with self.assertRaises(ShimmerLinkError):
            dev.get_sampling_rate()
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_exception_in_the_read_loop_is_reported(self):
        pd, dev = self.make()
        errors: List[ShimmerError] = []
        dev.add_error_callback(errors.append)

        def boom():
            raise MemoryError("simulated crash inside the receiver")

        dev._bluetooth.process_single_input_event = boom
        pd.device.inject_now(b"\x00")  # wake the read loop up
        self.assertTrue(self.wait_for(lambda: errors), "the crash was not reported")
        self.assertIsInstance(errors[0], ShimmerLinkError)
        self.assertIsInstance(errors[0].__cause__, MemoryError)

    # ----------------------------------------------------------------------------------------- timeouts
    def test_unanswered_command_times_out_and_the_link_stays_usable(self):
        pd, dev = self.make(command_timeout=0.3)
        pd.device.ignore.add(0x03)

        t0 = time.monotonic()
        with self.assertRaises(ShimmerTimeoutError):
            dev.get_sampling_rate()
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertEqual(dev.link_stats["timeouts"], 1)

        pd.device.ignore.discard(0x03)
        self.assertEqual(dev.get_sampling_rate(), 512.0)
        self.assertTrue(dev.connected)

    def test_a_late_reply_is_not_mistaken_for_the_next_answer(self):
        pd, dev = self.make(command_timeout=0.15)
        pd.device.reply_delay = 0.5
        with self.assertRaises(ShimmerTimeoutError):
            dev.get_sampling_rate()

        pd.device.reply_delay = 0.0
        time.sleep(0.8)  # the late acknowledgment and response arrive now
        pd.device.dr = (
            32  # so that a mix-up would be visible: the next answer must be 1024 Hz
        )
        self.assertEqual(dev.get_sampling_rate(), 1024.0)
        self.assertGreaterEqual(
            dev.link_stats["skipped_bytes"], 3
        )  # the late response, as noise (no CRC)

    def test_a_late_reply_is_consumed_when_the_crc_vouches_for_it(self):
        pd, dev = self.make(command_timeout=0.15, crc="auto")
        pd.device.reply_delay = 0.5
        with self.assertRaises(ShimmerTimeoutError):
            dev.get_sampling_rate()

        pd.device.reply_delay = 0.0
        time.sleep(0.8)  # the late acknowledgment and response arrive now
        pd.device.dr = 32
        self.assertEqual(dev.get_sampling_rate(), 1024.0)
        self.assertEqual(dev.link_stats["late_responses"], 1)
        self.assertEqual(dev.link_stats["skipped_bytes"], 0)

    def test_requests_from_several_threads_are_not_mixed_up(self):
        pd, dev = self.make()
        errors = []

        def hammer():
            for _ in range(60):
                try:
                    if dev.get_sampling_rate() != 512.0:
                        errors.append("wrong value")
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)

        threads = [threading.Thread(target=hammer) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)

        self.assertEqual(errors, [])

    # --------------------------------------------------------------------------------------- streaming
    def test_stray_byte_in_the_stream_costs_at_most_one_packet(self):
        pd, dev = self.make()
        got = self.start_stream(dev)
        self.assertTrue(self.wait_for(lambda: len(got) > 20))

        pd.device.inject(b"\x37")
        n = len(got)
        self.assertTrue(
            self.wait_for(lambda: len(got) > n + 50), "the stream did not continue"
        )
        self.assertTrue(dev.connected)
        self.assertGreaterEqual(dev.link_stats["skipped_bytes"], 1)

        inv = {FakeDevice.value(k, 0): k for k in range(20000)}
        seqs = [inv.get(p[EChannelType.EXG1_CH1_24BIT], -1) for p in got]
        self.assertNotIn(-1, seqs, "a corrupted packet was delivered as valid")
        self.assertLessEqual(
            max(b - a for a, b in zip(seqs, seqs[1:])), 3
        )  # a handful of packets at most

    def test_shutdown_while_the_stream_is_running_is_prompt(self):
        # The read loop must notice the cancellation even though data keeps arriving all the time
        for _ in range(5):
            pd, dev = self.make(device_kwargs={"sampling_dr": 16})  # 2048 Hz
            got = self.start_stream(dev)
            self.assertTrue(self.wait_for(lambda: len(got) > 50))

            t0 = time.monotonic()
            dev.shutdown()
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertFalse(dev._thread.is_alive())

    def test_exception_in_a_stream_callback_does_not_end_the_stream(self):
        pd, dev = self.make()
        good: List[DataPacket] = []

        def bad(_):
            raise RuntimeError("bug in the application callback")

        dev.add_stream_callback(bad)
        dev.add_stream_callback(good.append)
        dev.start_streaming()

        self.assertTrue(self.wait_for(lambda: len(good) > 30))
        self.assertTrue(dev.connected)
        self.assertGreater(dev.link_stats["callback_errors"], 0)

    def test_a_stalled_stream_is_detected_by_the_watchdog(self):
        pd, dev = self.make(stall_timeout=0.5)
        errors: List[ShimmerError] = []
        dev.add_error_callback(errors.append)
        got = self.start_stream(dev)
        self.assertTrue(self.wait_for(lambda: len(got) > 10))

        pd.device.mute()
        self.assertTrue(
            self.wait_for(lambda: errors, timeout=3.0), "the stall was not detected"
        )
        self.assertIsInstance(errors[0], ShimmerLinkError)
        self.assertFalse(dev.connected)

    def test_a_stream_that_never_starts_is_detected(self):
        pd, dev = self.make(start_timeout=0.6, stall_timeout=0.5)
        errors: List[ShimmerError] = []
        dev.add_error_callback(errors.append)

        pd.device.mute()
        # The start command is acknowledged by a device that then never sends data: simulate by not answering
        dev._streaming = True
        dev._stream_started = time.monotonic()
        dev._packets_at_start = dev._bluetooth.stats.data_packets
        self.assertTrue(self.wait_for(lambda: errors, timeout=3.0))

    # -------------------------------------------------------------------------------------------- presync
    def test_a_stream_left_running_by_a_previous_session_is_stopped_on_initialize(self):
        pd = _PtyDevice()
        self._open.append(pd)
        pd.device.streaming = True  # the previous session crashed without stopping it
        time.sleep(0.2)  # let data pile up in the buffer

        dev = ShimmerBluetooth(pd.serial, presync=True, crc="off")
        self._devs.append(dev)
        dev.initialize()

        self.assertFalse(pd.device.streaming)
        self.assertEqual(dev.get_sampling_rate(), 512.0)
        self.assertTrue(dev.connected)
        self.assertEqual(
            pd.device.commands[0][0], 0x20
        )  # the first thing sent was STOP_STREAMING

    # ------------------------------------------------------------------------------------------------ CRC
    def test_crc_is_enabled_on_capable_firmware_and_frames_are_verified(self):
        pd, dev = self.make(crc="auto")
        self.assertEqual(dev.crc_len, 2)
        self.assertEqual(pd.device.crc_len, 2)

        self.assertEqual(dev.get_sampling_rate(), 512.0)
        got = self.start_stream(dev)
        self.assertTrue(self.wait_for(lambda: len(got) > 30))
        self.assertEqual(dev.link_stats["crc_errors"], 0)
        self.assertEqual(dev.link_stats["framing_errors"], 0)

    def test_crc_is_not_enabled_on_old_firmware(self):
        pd, dev = self.make(device_kwargs={"fw": (3, 0, 11, 0)}, crc="auto")
        self.assertEqual(dev.crc_len, 0)
        self.assertNotIn(0x8B, [c for c, _ in pd.device.commands])

    def test_corrupt_frame_is_dropped_with_crc(self):
        pd, dev = self.make(crc="auto")
        got = self.start_stream(dev)
        self.assertTrue(self.wait_for(lambda: len(got) > 20))

        bad = bytearray(pd.device.packet(900000))
        bad[6] ^= 0xFF
        pd.device.inject(bytes(bad))
        n = len(got)
        self.assertTrue(self.wait_for(lambda: len(got) > n + 40))
        self.assertGreaterEqual(dev.link_stats["crc_errors"], 1)
        self.assertNotIn(
            FakeDevice.value(900000, 0), [p[EChannelType.EXG1_CH1_24BIT] for p in got]
        )

    # ----------------------------------------------------------------------------------------- connect()
    def test_connect_retries_after_a_failed_attempt(self):
        attempts = []

        def factory(port, baud):
            pd = _PtyDevice()
            self._open.append(pd)
            attempts.append(pd)
            if len(attempts) == 1:
                pd.device.hang_up()  # the first attempt is refused: the device hangs up at once
            return pd.serial

        dev = ShimmerBluetooth.connect(
            "fake",
            serial_factory=factory,
            retries=2,
            retry_delay=0.05,
            presync=False,
            crc="off",
        )
        self._devs.append(dev)
        self.assertEqual(len(attempts), 2)
        self.assertTrue(dev.connected)
        self.assertEqual(dev.get_sampling_rate(), 512.0)

    def test_connect_gives_up_after_the_retries(self):
        attempts = []

        def factory(port, baud):
            pd = _PtyDevice()
            self._open.append(pd)
            attempts.append(pd)
            pd.device.hang_up()
            return pd.serial

        with self.assertRaises(ShimmerConnectError):
            ShimmerBluetooth.connect(
                "fake",
                serial_factory=factory,
                retries=1,
                retry_delay=0.01,
                presync=False,
                crc="off",
            )
        self.assertEqual(len(attempts), 2)


class ConnectLockTest(TestCase):

    def test_second_holder_waits_for_the_first(self):
        import tempfile

        path = os.path.join(tempfile.mkdtemp(), "bt.lock")
        order = []

        def second():
            with ConnectLock(path, timeout=5.0):
                order.append("second")

        with ConnectLock(path, timeout=5.0):
            t = threading.Thread(target=second)
            t.start()
            time.sleep(0.3)
            order.append("first")
        t.join(5.0)

        self.assertEqual(order, ["first", "second"])

    def test_lock_times_out(self):
        import tempfile

        path = os.path.join(tempfile.mkdtemp(), "bt.lock")
        with ConnectLock(path, timeout=5.0):
            with self.assertRaises(ShimmerConnectError):
                with ConnectLock(path, timeout=0.3):
                    pass


class ExGConfigurationTest(_ApiTestCase):
    """Setting the data rate of the ExG chips through the API (the device holds the registers)"""

    def test_set_exg_data_rate_writes_only_the_rate_and_reads_it_back(self):
        pd, dev = self.make()
        before = bytes(pd.device.exg_regs)

        dev.set_exg_data_rate(4000)

        after = bytes(pd.device.exg_regs)
        self.assertEqual(after[0] & 0x07, 5)
        self.assertEqual(after[1:], before[1:])
        self.assertEqual(dev.get_exg_data_rate(), 4000)

    def test_nothing_is_written_when_the_rate_is_already_set(self):
        pd, dev = self.make()
        dev.set_exg_data_rate(2000)  # the simulated device is at 2000 SPS
        self.assertNotIn(0x61, [c for c, _ in pd.device.commands])

    def test_set_sampling_rate_follows_the_official_api_when_asked(self):
        pd, dev = self.make()
        dev.set_sampling_rate(1000.0, update_exg_rate=True)
        self.assertEqual(dev.get_exg_data_rate(), 1000)
        dev.set_sampling_rate(2048.0, update_exg_rate=True)
        self.assertEqual(dev.get_exg_data_rate(), 4000)

    def test_set_sampling_rate_leaves_the_chip_alone_by_default(self):
        pd, dev = self.make()
        dev.set_sampling_rate(2048.0)
        self.assertEqual(dev.get_exg_data_rate(), 2000)
        self.assertNotIn(0x61, [c for c, _ in pd.device.commands])

    def test_a_chip_that_ignores_the_write_is_reported(self):
        pd, dev = self.make()
        pd.device.ignore.add(0x61)  # no acknowledgment: the write times out
        dev._command_timeout = 0.3
        with self.assertRaises(ShimmerError):
            dev.set_exg_data_rate(125)

    def test_start_streaming_warns_about_a_chip_slower_than_the_sampling_rate(self):
        pd, dev = self.make(
            device_kwargs={"sampling_dr": 16}
        )  # 2048 Hz, the simulated chip does 2000 SPS
        with self.assertLogs("pyshimmer.bluetooth.bt_api", level="WARNING") as logs:
            dev.start_streaming()
        self.assertTrue(any("repeats" in m for m in logs.output), logs.output)

    def test_start_streaming_warns_about_the_test_signal(self):
        pd, dev = self.make()
        pd.device.exg_regs[:] = bytes.fromhex("03ab1065652000000203")
        with self.assertLogs("pyshimmer.bluetooth.bt_api", level="WARNING") as logs:
            dev.start_streaming()
        self.assertTrue(any("test signal" in m for m in logs.output), logs.output)

    def test_start_streaming_check_can_be_skipped(self):
        pd, dev = self.make(device_kwargs={"sampling_dr": 16})
        dev.start_streaming(check_exg=False)
        self.assertNotIn(0x63, [c for c, _ in pd.device.commands])


class WriteExGRegisterTest(_ApiTestCase):
    """Writing a whole configuration to a chip and checking what it took"""

    def test_the_whole_configuration_is_written_and_read_back(self):
        pd, dev = self.make()
        wanted = ExGRegister.emg_preset(rate_sps=4000, gain=6)

        back = dev.write_exg_register(0, wanted)

        self.assertEqual(bytes(pd.device.exg_regs), wanted.binary)
        self.assertEqual(back, wanted)
        # a single write of the ten bytes, and one read after it
        codes = [c for c, _ in pd.device.commands if c in (0x61, 0x63)]
        self.assertEqual(codes, [0x61, 0x63])

    @staticmethod
    def acknowledge_without_storing(pd) -> None:
        """Make the device acknowledge the ExG write and keep the registers it had"""
        original = pd.device._handle

        def handle(code, args):
            if code == 0x61:
                pd.device._write(pd.device._reply())
            else:
                original(code, args)

        pd.device._handle = handle

    def test_a_chip_that_took_something_else_is_reported(self):
        pd, dev = self.make()
        self.acknowledge_without_storing(pd)

        with self.assertRaises(ShimmerConfigurationError) as ctx:
            dev.write_exg_register(0, ExGRegister.emg_preset(rate_sps=4000))
        self.assertIn("did not take the configuration", str(ctx.exception))

    def test_a_refusal_is_reported(self):
        pd, dev = self.make()
        pd.device.refuse.add(0x61)  # the device answers NACK

        with self.assertRaises(CommandRefused):
            dev.write_exg_register(0, ExGRegister.emg_preset(rate_sps=4000))

    def test_a_device_that_changes_a_bit_it_owns_is_not_an_error(self):
        pd, dev = self.make()
        wanted = ExGRegister.emg_preset(rate_sps=4000)

        original_handle = pd.device._handle

        def handle(code, args):
            original_handle(code, args)
            if code == 0x61:
                pd.device.exg_regs[1] &= ~0x08  # the firmware clears the board bit
                pd.device.exg_regs[7] = 0x0F  # and a status register shows lead-off

        pd.device._handle = handle
        back = dev.write_exg_register(0, wanted)

        self.assertEqual(back.binary[1], wanted.binary[1] & ~0x08)

    def test_a_mismatch_can_be_accepted_when_not_verifying(self):
        pd, dev = self.make()
        self.acknowledge_without_storing(pd)

        wanted = ExGRegister.emg_preset(rate_sps=125)  # not what the device holds

        back = dev.write_exg_register(0, wanted, verify=False)

        self.assertNotEqual(back, wanted)


class DeviceInformationTest(_ApiTestCase):
    """What the node reads before it configures a device"""

    def test_the_expansion_board(self):
        pd, dev = self.make()
        board = dev.get_expansion_board()
        self.assertEqual(str(board), "ExG (SR47-4.0)")
        self.assertTrue(board.has_exg)

    def test_the_battery_with_its_charging_status(self):
        pd, dev = self.make()
        pd.device.battery_adc = 0x0B2E
        pd.device.charging = 0x40

        reading = dev.get_battery_reading()

        self.assertAlmostEqual(reading.voltage, 4.168246153846154)
        self.assertEqual(reading.charging_status, ChargingStatus.FULLY_CHARGED)

    def test_the_battery_reading_is_the_same_object_in_the_crc_mode(self):
        # with the CRC the receiver decodes a response by its code: asking in two ways must not give two types
        pd, dev = self.make(crc="auto")
        self.assertEqual(dev.crc_len, 2)
        pd.device.battery_adc = 0x0B2E
        pd.device.charging = 0x80

        reading = dev.get_battery_reading()

        self.assertAlmostEqual(reading.voltage, 4.168246153846154)
        self.assertEqual(reading.charging_status, ChargingStatus.PRECONDITIONING)
        self.assertAlmostEqual(dev.get_battery_state(False), 4.168246153846154)
        self.assertEqual(dev.get_battery_state(True), reading.percent)

    def test_the_battery_state_is_the_voltage_or_the_percent_without_the_crc_too(self):
        pd, dev = self.make(crc="off")
        pd.device.battery_adc = 0x0B2E
        self.assertAlmostEqual(dev.get_battery_state(False), 4.168246153846154)
        self.assertEqual(dev.get_battery_state(True), 100)

    def test_the_battery_can_be_read_while_streaming(self):
        pd, dev = self.make()
        got = self.start_stream(dev)
        self.assertTrue(self.wait_for(lambda: len(got) > 5))

        reading = dev.get_battery_reading()

        self.assertGreater(reading.voltage, 0)
        self.assertTrue(self.wait_for(lambda: len(got) > 20))
        dev.stop_streaming()


class ChangeCrcModeTest(_ApiTestCase):
    """The CRC mode of a link that is already up"""

    def test_the_mode_can_be_dropped_and_restored_and_the_commands_keep_working(self):
        pd, dev = self.make(crc="auto")
        self.assertEqual(dev.crc_len, 2)

        dev.set_crc(0)
        self.assertEqual(dev.crc_len, 0)
        self.assertEqual(pd.device.crc_len, 0)
        self.assertEqual(dev.get_sampling_rate(), 512.0)

        dev.set_crc(2)
        self.assertEqual((dev.crc_len, pd.device.crc_len), (2, 2))
        self.assertEqual(dev.get_sampling_rate(), 512.0)

        got = self.start_stream(dev)
        self.assertTrue(self.wait_for(lambda: len(got) > 20))
        self.assertEqual(dev.link_stats["crc_errors"], 0)

    def test_the_same_mode_sends_nothing(self):
        pd, dev = self.make(crc="auto")
        before = [c for c, _ in pd.device.commands].count(0x8B)
        dev.set_crc(2)
        self.assertEqual([c for c, _ in pd.device.commands].count(0x8B), before)

    def test_it_is_refused_while_streaming_and_with_a_wrong_length(self):
        pd, dev = self.make(crc="auto")
        with self.assertRaises(ValueError):
            dev.set_crc(3)
        self.start_stream(dev)
        with self.assertRaises(ShimmerError):
            dev.set_crc(0)
        self.assertEqual(dev.crc_len, 2)

    def test_a_firmware_without_the_mode_refuses_it(self):
        pd, dev = self.make(device_kwargs={"fw": (3, 0, 11, 0)}, crc="auto")
        with self.assertRaises(ValueError):
            dev.set_crc(2)
        dev.set_crc(0)  # already off: nothing to do

    def test_a_device_that_does_not_answer_leaves_the_previous_mode(self):
        pd, dev = self.make(crc="auto", command_timeout=0.3)
        pd.device.ignore.add(0x8B)
        with self.assertRaises(ShimmerError):
            dev.set_crc(0)
        self.assertEqual(dev.crc_len, 2)


class ThroughputWarningTest(_ApiTestCase):

    def test_warns_when_the_crc_pushes_the_stream_to_the_limit(self):
        pd, dev = self.make(
            device_kwargs={"sampling_dr": 16}, crc="auto"
        )  # 2048 Hz, 13 bytes per packet
        with self.assertLogs("pyshimmer.bluetooth.bt_api", level="WARNING") as logs:
            dev.start_streaming(check_exg=False)
        self.assertTrue(
            any("CRC" in m and "kB/s" in m for m in logs.output), logs.output
        )

    def test_no_warning_at_a_comfortable_rate(self):
        pd, dev = self.make(device_kwargs={"sampling_dr": 32}, crc="auto")  # 1024 Hz
        with self.assertNoLogs("pyshimmer.bluetooth.bt_api", level="WARNING"):
            dev.start_streaming(check_exg=False)


class RefusalTest(_ApiTestCase):
    """A NACK is a normal answer of the protocol: the command is refused and the link is fine"""

    def test_a_refused_command_raises_and_the_link_stays_usable(self):
        pd, dev = self.make()
        pd.device.refuse.add(0x92)  # START_LOGGING

        with self.assertRaises(CommandRefused):
            dev.start_logging()

        self.assertEqual(dev.get_sampling_rate(), 32768 / 64)
        self.assertTrue(dev.connected)
        self.assertEqual(dev.link_stats["nacks"], 1)

    def test_a_refused_request_with_a_response_does_not_wait_for_the_response(self):
        pd, dev = self.make(command_timeout=2.0)
        pd.device.refuse.add(0x03)  # GET_SAMPLING_RATE

        t0 = time.monotonic()
        with self.assertRaises(CommandRefused):
            dev.get_sampling_rate()
        self.assertLess(time.monotonic() - t0, 1.0)

        pd.device.refuse.clear()
        self.assertEqual(dev.get_sampling_rate(), 32768 / 64)

    def test_a_refusal_is_also_understood_with_the_crc_mode(self):
        pd, dev = self.make(crc="auto")
        self.assertEqual(dev.crc_len, 2)
        pd.device.refuse.add(0x92)

        with self.assertRaises(CommandRefused):
            dev.start_logging()

        self.assertEqual(dev.get_sampling_rate(), 32768 / 64)
        self.assertEqual(dev.link_stats["crc_errors"], 0)

    def test_a_refusal_nobody_asked_for_is_counted_and_ignored(self):
        pd, dev = self.make()
        pd.device.inject_now(b"\xfe")

        self.assertTrue(self.wait_for(lambda: dev.link_stats["unexpected_nacks"] == 1))
        self.assertEqual(dev.get_sampling_rate(), 32768 / 64)

    def test_the_refusal_is_a_shimmer_error_that_is_not_a_link_failure(self):
        self.assertTrue(issubclass(CommandRefused, ShimmerError))
        self.assertFalse(issubclass(CommandRefused, ShimmerLinkError))

    def test_a_refusal_keeps_a_stream_flowing(self):
        pd, dev = self.make()
        got = self.start_stream(dev)
        self.assertTrue(self.wait_for(lambda: len(got) > 20))
        pd.device.refuse.add(0x92)

        with self.assertRaises(CommandRefused):
            dev.start_logging()

        n = len(got)
        self.assertTrue(self.wait_for(lambda: len(got) > n + 20))
        dev.stop_streaming()


class HardwareRevisionTest(_ApiTestCase):

    def test_the_revision_is_the_one_the_device_reports(self):
        pd, dev = self.make()
        self.assertEqual(dev.hardware_version, HardwareVersion.SHIMMER3)
        self.assertIs(
            dev.hardware_revision,
            RevisionRegistry.get_revision(HardwareVersion.SHIMMER3),
        )
        self.assertEqual(str(dev.firmware_type.name), "LogAndStream")
        self.assertEqual(
            (
                dev.firmware_version.major,
                dev.firmware_version.minor,
                dev.firmware_version.rel,
            ),
            (0, 15, 4),
        )

    def test_a_shimmer3r_gets_its_own_revision(self):
        pd, dev = self.make(initialize=False)
        pd.device.hw_version = 10
        dev.initialize()
        self.assertEqual(dev.hardware_version, HardwareVersion.SHIMMER3R)
        self.assertIs(
            dev.hardware_revision,
            RevisionRegistry.get_revision(HardwareVersion.SHIMMER3R),
        )

    def test_a_hardware_version_without_revision_is_an_error_and_releases_the_port(
        self,
    ):
        pd, dev = self.make(initialize=False)
        pd.device.hw_version = 2  # a Shimmer2: no revision for it

        with self.assertRaises(ValueError):
            dev.initialize()
        self.assertFalse(dev.initialized)

    def test_a_device_that_does_not_answer_the_version_is_taken_for_a_shimmer3(self):
        pd, dev = self.make(initialize=False, command_timeout=0.5)
        pd.device.ignore.add(0x3F)

        dev.initialize()

        self.assertEqual(dev.hardware_version, HardwareVersion.UNKNOWN)
        self.assertIs(
            dev.hardware_revision,
            RevisionRegistry.get_revision(HardwareVersion.SHIMMER3),
        )
        self.assertEqual(dev.get_sampling_rate(), 32768 / 64)

    def test_a_revision_given_by_the_caller_is_kept(self):
        pd = _PtyDevice()
        self._open.append(pd)
        custom = RevisionRegistry.get_revision(HardwareVersion.SHIMMER3R)
        pd.device.hw_version = 3
        dev = ShimmerBluetooth(pd.serial, custom, presync=False, crc="off")
        self._devs.append(dev)

        dev.initialize()

        self.assertIs(dev.hardware_revision, custom)
        self.assertEqual(dev.hardware_version, HardwareVersion.SHIMMER3)
