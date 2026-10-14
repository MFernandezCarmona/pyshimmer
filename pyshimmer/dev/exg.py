# pyshimmer - API for Shimmer sensor devices
# Copyright (C) 2023  Lukas Magel

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

import re
from collections.abc import Iterable
from enum import Enum

from pyshimmer.util import bit_is_set, fmt_hex
from .channels import EChannelType


class ExGMux(Enum):
    NORMAL = 0x00
    SHORTED = 0x01
    RLD_MEASURE = 0x02
    MVDD = 0x03
    TEMP_SENSOR = 0x04
    TEST_SIGNAL = 0x05
    RLD_DRP = 0x06
    RLD_DRM = 0x07
    RLD_DRPM = 0x08
    INPUT3 = 0x09
    RESERVED = 0x0A


class ExGRLDLead(Enum):
    RLD1P = 0x01 << 0
    RLD1N = 0x01 << 1
    RLD2P = 0x01 << 2
    RLD2N = 0x01 << 3


class ERLDRef(Enum):
    EXTERNAL = 0x00
    INTERNAL = 0x01


class ExGRegister:
    GAIN_MAP = {
        0: 6,
        1: 1,
        2: 2,
        3: 3,
        4: 4,
        5: 8,
        6: 12,
    }
    DATA_RATE_MAP = {0: 125, 1: 250, 2: 500, 3: 1000, 4: 2000, 5: 4000, 6: 8000, 7: -1}
    # The inverse maps: sample rate in SPS -> value of the three DR bits, gain -> value
    # of the three gain bits
    DATA_RATE_INDEX = {sps: idx for idx, sps in DATA_RATE_MAP.items() if sps > 0}
    GAIN_INDEX = {gain: idx for idx, gain in GAIN_MAP.items()}
    PD_BIT = 0x01 << 7
    RLD_PD_BIT = 0x01 << 5

    def __init__(self, reg_bin: bytes):
        if len(reg_bin) < 10:
            raise ValueError("Binary register content must have length 10")

        self._reg_bin = reg_bin

    def __str__(self) -> str:
        def print_ch(ch_id: int) -> str:
            return (
                f"Channel {ch_id + 1:2d}\n"
                + f"\tPowerdown: {self.is_ch_powerdown(ch_id)}\n"
                + f"\tGain: {self.get_ch_gain(ch_id):2d}\n"
                + f"\tMultiplexer: {self.get_ch_mux(ch_id).name} "
                + f"({self.get_ch_mux_bin(ch_id):#06b})\n"
            )

        def fmt_rld_channels(ch_names) -> str:
            ch_names = [ch.name for ch in ch_names] if len(ch_names) > 0 else ["None"]
            return ", ".join(ch_names)

        reg_bin_str = fmt_hex(self._reg_bin)
        obj_str = (
            f"ExGRegister:\n"
            + f"Data Rate: {self.data_rate:4d}\n"
            + f"RLD Powerdown: {self.rld_powerdown}\n"
            + f"RLD Channels: {fmt_rld_channels(self.rld_channels)}\n"
            + f"RLD Reference: {self.rld_ref.name}\n"
            + f"Binary: {reg_bin_str}\n"
        )
        obj_str += print_ch(0)
        obj_str += print_ch(1)
        return obj_str

    def __eq__(self, other: ExGRegister) -> bool:
        return self._reg_bin == other._reg_bin

    @staticmethod
    def check_ch_id(ch_id: int) -> None:
        if not 0 <= ch_id <= 1:
            raise ValueError("Channel ID must be 0 or 1")

    def _get_ch_byte(self, ch_id: int) -> int:
        ch_offset = 3 + ch_id
        return self._reg_bin[ch_offset]

    def _get_rld_byte(self) -> int:
        return self._reg_bin[0x05]

    def get_ch_gain(self, ch_id: int) -> int:
        self.check_ch_id(ch_id)

        ch_byte = self._get_ch_byte(ch_id)
        gain_bin = (ch_byte & 0x70) >> 4

        return self.GAIN_MAP[gain_bin]

    def get_ch_mux_bin(self, ch_id: int) -> int:
        self.check_ch_id(ch_id)

        ch_byte = self._get_ch_byte(ch_id)
        return ch_byte & 0x0F

    def get_ch_mux(self, ch_id: int) -> ExGMux:
        return ExGMux(self.get_ch_mux_bin(ch_id))

    def is_ch_powerdown(self, ch_id: int) -> bool:
        self.check_ch_id(ch_id)

        ch_byte = self._get_ch_byte(ch_id)
        return bit_is_set(ch_byte, self.PD_BIT)

    @property
    def binary(self):
        return self._reg_bin

    @property
    def ch1_gain(self) -> int:
        return self.get_ch_gain(0)

    @property
    def ch2_gain(self) -> int:
        return self.get_ch_gain(1)

    @property
    def ch1_mux(self) -> ExGMux:
        return self.get_ch_mux(0)

    @property
    def ch2_mux(self) -> ExGMux:
        return self.get_ch_mux(1)

    @property
    def ch1_powerdown(self) -> bool:
        return self.is_ch_powerdown(0)

    @property
    def ch2_powerdown(self) -> bool:
        return self.is_ch_powerdown(1)

    @property
    def data_rate(self) -> int:
        dr_bin = self._reg_bin[0] & 0x07
        return self.DATA_RATE_MAP[dr_bin]

    @property
    def data_rate_index(self) -> int:
        """The value of the three data rate bits of CONFIG1 (0: 125 SPS ... 6: 8000
        SPS)"""
        return self._reg_bin[0] & 0x07

    @property
    def uses_test_signal(self) -> bool:
        """True if an input multiplexer routes the internal test signal instead of the
        electrodes"""
        try:
            return ExGMux.TEST_SIGNAL in (self.ch1_mux, self.ch2_mux)
        except ValueError:  # a multiplexer value without a name
            return False

    def with_data_rate(self, rate_sps: int) -> ExGRegister:
        """Return a copy of the registers with another data rate

        :param rate_sps: One of 125, 250, 500, 1000, 2000, 4000, 8000
        :raises ValueError: If the chip does not support the rate
        """
        if rate_sps not in self.DATA_RATE_INDEX:
            raise ValueError(
                f"The ExG chips support {sorted(self.DATA_RATE_INDEX)} SPS, "
                f"not {rate_sps}"
            )

        return self._with_bits(0, 0x07, self.DATA_RATE_INDEX[rate_sps])

    def _with_bits(self, index: int, mask: int, value: int) -> ExGRegister:
        """Return a copy of the registers with the bits of ``mask`` in byte ``index``
        replaced by those of ``value``"""
        reg = bytearray(self._reg_bin)
        reg[index] = (reg[index] & ~mask & 0xFF) | (value & mask)
        return ExGRegister(bytes(reg))

    # ---------------------------------------------------------------------------------------------
    # The bit fields below follow Table 3-1 of the EMG User Guide (Rev 1.12), the ADS1292R and the
    # way ShimmerCapture (the official application of the C# API) sets them up.

    TEST_SIGNALS = {"off": 0b00, "dc": 0b10, "square": 0b11}
    LEADOFF_CURRENTS = {"6nA": 0, "22nA": 1, "6uA": 2, "22uA": 3}
    LEADOFF_THRESHOLDS = (95.0, 92.5, 90.0, 87.5, 85.0, 80.0, 75.0, 70.0)
    # What the chip is told about the "common mode" (right leg drive) of the electrodes
    REFERENCE_VOLTS = 2.42

    # Bits that belong to the device and not to the configuration: CONFIG2 bit 3 is set by the
    # firmware according to the revision of the ExG board, and LOFF_STAT is a status register
    # (only CLK_DIV can be written)
    _CONFIGURATION_MASK = bytes(
        (0xFF, 0xF7, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x40, 0xFF, 0xFF)
    )

    @property
    def single_shot(self) -> bool:
        """True if the chip converts once and stops (never what a stream wants)"""
        return bit_is_set(self._reg_bin[0], 0x80)

    @property
    def test_signal(self) -> str:
        """The internal test signal generator: "off", "dc" or "square" (1 Hz)"""
        bits = self._reg_bin[1] & 0b11
        if not bits & 0b10:
            return "off"
        return "square" if bits & 0b01 else "dc"

    @property
    def vref_4v(self) -> bool:
        """True if the reference of the converter is 4.033 V instead of 2.42 V"""
        return bit_is_set(self._reg_bin[1], 0x10)

    @property
    def leadoff_comparators(self) -> bool:
        """True if the lead-off comparators are on"""
        return bit_is_set(self._reg_bin[1], 0x40)

    @property
    def leadoff_current(self) -> str:
        """The lead-off current: "6nA", "22nA", "6uA" or "22uA" """
        index = (self._reg_bin[2] >> 2) & 0b11
        return next(k for k, v in self.LEADOFF_CURRENTS.items() if v == index)

    @property
    def leadoff_threshold(self) -> float:
        """The lead-off comparator threshold, as the percentage of the supply that the
        positive input must stay under (the negative one over its complement)"""
        return self.LEADOFF_THRESHOLDS[(self._reg_bin[2] >> 5) & 0b111]

    @property
    def leadoff_detection(self) -> bool:
        """True if lead-off detection is on: comparators and at least one sensed input"""
        return self.leadoff_comparators and bool(self._reg_bin[6] & 0x0F)

    @property
    def vref_volts(self) -> float:
        """The reference voltage the conversions are scaled with"""
        return 4.033 if self.vref_4v else self.REFERENCE_VOLTS

    def with_gain(self, ch_id: int, gain: int) -> ExGRegister:
        """Return a copy of the registers with another gain in one channel

        :param ch_id: The channel, 0 or 1
        :param gain: One of 1, 2, 3, 4, 6, 8, 12
        :raises ValueError: If the chip does not support the gain
        """
        self.check_ch_id(ch_id)
        if gain not in self.GAIN_INDEX:
            raise ValueError(
                f"The ExG chips support the gains {sorted(self.GAIN_INDEX)}, not {gain}"
            )
        return self._with_bits(3 + ch_id, 0x70, self.GAIN_INDEX[gain] << 4)

    def with_mux(self, ch_id: int, mux: ExGMux) -> ExGRegister:
        """Return a copy of the registers with another input in one channel

        :param ch_id: The channel, 0 or 1
        :param mux: What the input multiplexer connects to the amplifier
        :raises ValueError: For the multiplexer values the chip does not define
        """
        self.check_ch_id(ch_id)
        if mux is ExGMux.RESERVED:
            raise ValueError("The multiplexer value is reserved")
        return self._with_bits(3 + ch_id, 0x0F, mux.value)

    def with_powerdown(self, ch_id: int, powerdown: bool) -> ExGRegister:
        """Return a copy of the registers with one channel powered down (or up)

        A channel that is powered down still sends its bytes in every packet.
        """
        self.check_ch_id(ch_id)
        return self._with_bits(3 + ch_id, self.PD_BIT, self.PD_BIT if powerdown else 0)

    def with_test_signal(self, mode: str) -> ExGRegister:
        """Return a copy of the registers with the internal test signal generator on or
        off

        The signal reaches a channel only if its multiplexer is :attr:`ExGMux.TEST_SIGNAL`.

        :param mode: One of "off", "dc" and "square" (a 1 Hz square wave of +-1 mV)
        """
        if mode not in self.TEST_SIGNALS:
            raise ValueError(
                f"The test signal must be one of {list(self.TEST_SIGNALS)}"
            )
        return self._with_bits(1, 0b11, self.TEST_SIGNALS[mode])

    def with_rld(
        self,
        enabled: bool | None = None,
        channels: Iterable[ExGRLDLead] | None = None,
        internal_reference: bool | None = None,
    ) -> ExGRegister:
        """Return a copy of the registers with other right leg drive settings

        :param enabled: Power of the RLD buffer (None: leave as it is)
        :param channels: The inputs the RLD amplifier senses (None: leave as they are)
        :param internal_reference: True for the internally generated RLD reference,
            False for an external one (None: leave as it is)
        """
        new = self
        if enabled is not None:
            new = new._with_bits(5, self.RLD_PD_BIT, self.RLD_PD_BIT if enabled else 0)
        if channels is not None:
            nibble = 0
            for lead in channels:
                nibble |= lead.value
            new = new._with_bits(5, 0x0F, nibble)
        if internal_reference is not None:
            new = new._with_bits(9, 0x02, 0x02 if internal_reference else 0)
        return new

    def with_leadoff(
        self,
        detection: bool | None = None,
        current: str | None = None,
        threshold: float | None = None,
    ) -> ExGRegister:
        """Return a copy of the registers with other lead-off detection settings

        Turning the detection on does what ShimmerCapture does for chip 1: DC detection with
        the comparators on, the RLD sensing the loss and the inputs 1P, 1N and 2P sensed.
        The status is then in the status byte of every ExG data packet.

        :param detection: True to turn the detection on, False to turn it off (None: leave)
        :param current: One of "6nA", "22nA", "6uA", "22uA" (None: leave)
        :param threshold: One of :attr:`LEADOFF_THRESHOLDS` (None: leave)
        """
        if current is not None and current not in self.LEADOFF_CURRENTS:
            raise ValueError(
                f"The lead-off current must be one of {list(self.LEADOFF_CURRENTS)}"
            )
        if threshold is not None and threshold not in self.LEADOFF_THRESHOLDS:
            raise ValueError(
                f"The lead-off threshold must be one of {list(self.LEADOFF_THRESHOLDS)}"
            )

        new = self
        if detection is not None:
            on = bool(detection)
            new = new._with_bits(1, 0x40, 0x40 if on else 0)  # PDB_LOFF_COMP
            new = new._with_bits(5, 0x10, 0x10 if on else 0)  # RLD_LOFF_SENS
            new = new._with_bits(6, 0x0F, 0x07 if on else 0)  # LOFF1P, LOFF1N, LOFF2P
            new = new._with_bits(2, 0x01, 0)  # DC detection
        if current is not None:
            new = new._with_bits(2, 0x0C, self.LEADOFF_CURRENTS[current] << 2)
        if threshold is not None:
            new = new._with_bits(2, 0xE0, self.LEADOFF_THRESHOLDS.index(threshold) << 5)
        return new

    def keeping_board_bits_of(self, current: ExGRegister) -> ExGRegister:
        """Return a copy of the registers with the bits the firmware sets according to the
        revision of the ExG board (CONFIG2 bit 3) as ``current`` has them

        Writing a configuration over what the device reports must not undo that.
        """
        return self._with_bits(1, 0x08, current.binary[1])

    def same_configuration(self, other: ExGRegister) -> bool:
        """True if two register contents configure the chip alike

        The bits that belong to the device (the board revision bit and the lead-off status)
        are not compared, so that what a device reports after a write can be checked against
        what was written.
        """
        return all(
            (a ^ b) & mask == 0
            for a, b, mask in zip(self._reg_bin, other.binary, self._CONFIGURATION_MASK)
        )

    def problems(self) -> list[str]:
        """What is wrong with the register contents, as readable sentences

        The checks are the bits the chip fixes (as Table 3-1 of the EMG User Guide shows
        them), the values the guide marks DO NOT USE, the single-shot mode (the chip would
        stop) and the 4.033 V reference (the library converts with 2.42 V). The
        configurations of Consensys and of the official APIs (ECG, EMG, test signal and
        respiration) have none.

        :return: The list of problems, empty if there is none
        """
        b = self._reg_bin
        found = []

        if b[0] & 0x80:
            found.append("CONFIG1: single-shot mode: the chip converts once and stops")
        if b[0] & 0x78:
            found.append("CONFIG1: the reserved bits 6:3 must be 0")
        if b[0] & 0x07 == 7:
            found.append("CONFIG1: data rate bits 111 are marked DO NOT USE")
        if not b[1] & 0x80:
            found.append("CONFIG2: bit 7 must be 1")
        if b[1] & 0x04:
            found.append("CONFIG2: bit 2 must be 0")
        if b[1] & 0x10:
            found.append(
                "CONFIG2: the 4.033 V reference is not supported, the data is "
                "converted with 2.42 V"
            )
        if not b[2] & 0x10:
            found.append("LOFF: bit 4 must be 1")
        if b[2] & 0x02:
            found.append("LOFF: bit 1 must be 0")
        for ch in (0, 1):
            name = f"CH{ch + 1}SET"
            if (b[3 + ch] >> 4) & 0x07 == 7:
                found.append(f"{name}: gain bits 111 are marked DO NOT USE")
            if b[3 + ch] & 0x0F > ExGMux.INPUT3.value:
                found.append(
                    f"{name}: multiplexer value {b[3 + ch] & 0x0F} is reserved"
                )
        if b[5] & 0xC0:
            found.append("RLD_SENS: the reserved bits 7:6 must be 0")
        if b[6] & 0xC0:
            found.append("LOFF_SENS: the reserved bits 7:6 must be 0")
        if b[7] & 0xA0:
            found.append("LOFF_STAT: the reserved bits 7 and 5 must be 0")
        if not b[8] & 0x02:
            found.append("RESP1: bit 1 must be 1")
        if b[9] & 0x78:
            found.append("RESP2: the reserved bits 6:3 must be 0")
        if not b[9] & 0x01:
            found.append("RESP2: bit 0 must be 1")
        return found

    @classmethod
    def test_preset(cls, rate_sps: int = 2000, gain: int = 12) -> ExGRegister:
        """The register contents of a Shimmer3 left in test signal mode by Consensys

        Both channels read the 1 Hz square wave of the internal generator, which is how a
        unit is checked without electrodes (EMG User Guide, 8.1).

        :param rate_sps: The data rate, see :meth:`with_data_rate`
        :param gain: The gain of both channels, one of 1, 2, 3, 4, 6, 8, 12
        """
        return (
            cls.emg_preset(rate_sps, gain)
            .with_test_signal("square")
            .with_mux(0, ExGMux.TEST_SIGNAL)
            .with_mux(1, ExGMux.TEST_SIGNAL)
        )

    @classmethod
    def emg_preset(cls, rate_sps: int = 2000, gain: int = 12) -> ExGRegister:
        """The register contents recommended by the EMG User Guide (Rev 1.12) for
        chip 1

        Channel 1 on IN3 and channel 2 on its normal inputs (table 3-4), no test
        signal, the right-leg drive buffer on with the internal reference (tables 3-5
        and 3-6). With the default arguments it equals the configuration found on
        Shimmer3 EMG units that were configured with Consensys.

        :param rate_sps: The data rate, see :meth:`with_data_rate`
        :param gain: The gain of both channels, one of 1, 2, 3, 4, 6, 8, 12
        """
        if gain not in cls.GAIN_INDEX:
            raise ValueError(
                f"The ExG chips support the gains {sorted(cls.GAIN_INDEX)}, not {gain}"
            )
        if rate_sps not in cls.DATA_RATE_INDEX:
            raise ValueError(
                f"The ExG chips support {sorted(cls.DATA_RATE_INDEX)} SPS, "
                f"not {rate_sps}"
            )

        g = cls.GAIN_INDEX[gain] << 4
        return cls(
            bytes(
                (
                    cls.DATA_RATE_INDEX[rate_sps],
                    0xA8,
                    0x10,
                    g | 0x09,
                    g | 0x00,
                    0x20,
                    0x00,
                    0x00,
                    0x02,
                    0x03,
                )
            )
        )

    @property
    def rld_powerdown(self) -> bool:
        rld_byte = self._get_rld_byte()
        return not bit_is_set(rld_byte, self.RLD_PD_BIT)

    @property
    def rld_channels(self) -> list[ExGRLDLead]:
        rld_byte = self._get_rld_byte()
        return [ch for ch in ExGRLDLead if bit_is_set(rld_byte, ch.value)]

    @property
    def rld_ref(self) -> ERLDRef:
        ref_byte = self._reg_bin[9]
        rld_ref = (ref_byte >> 1) & 0x01
        return ERLDRef(rld_ref)


def is_exg_ch(ch_type: EChannelType) -> bool:
    """
    Returns true if the signal that this channel type describes was recorded by a ExG
    chip

    Args:
        ch_type: The EChannelType of the signal

    Returns:
        True if the channel type belongs to the ExG chips, otherwise False
    """
    # This is hacky but protected by unit tests
    regex = re.compile(r"EXG\d_CH\d_\d{2}BIT")
    return regex.match(ch_type.name) is not None


def exg_data_rate_for_sampling_rate(sampling_rate_hz: float) -> int:
    """The ExG chip data rate the official Shimmer APIs select for a sampling rate

    The smallest rate that is not below the sampling rate, so that every sample read by
    the microcontroller is a new conversion. (Consensys and the C# API do this whenever
    the sampling rate changes; pyshimmer does not touch the chip unless asked to.)

    Args:
        sampling_rate_hz: The sampling rate of the device in Hz

    Returns:
        The data rate in samples per second: 125, 250, 500, 1000, 2000, 4000 or 8000
    """
    for rate in (125, 250, 500, 1000, 2000, 4000):
        if sampling_rate_hz <= rate:
            return rate
    return 8000
