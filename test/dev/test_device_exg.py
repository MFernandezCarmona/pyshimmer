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

import random
from unittest import TestCase

from pyshimmer.dev.channels import EChannelType
from pyshimmer.dev.exg import (
    is_exg_ch,
    ExGMux,
    ExGRLDLead,
    ERLDRef,
    ExGRegister,
    exg_data_rate_for_sampling_rate,
)


def randbytes(k: int) -> bytes:
    population = list(range(256))
    seq = random.choices(population, k=k)
    return bytes(seq)


class DeviceExGTest(TestCase):

    def test_is_exg_ch(self):
        from itertools import product

        valid_ch = [
            EChannelType[f"EXG{i}_CH{j}_{k}BIT"]
            for i, j, k in product([1, 2], [1, 2], [16, 24])
        ]

        for ch in EChannelType:
            self.assertEqual(is_exg_ch(ch), ch in valid_ch)


class ExGRegisterTest(TestCase):

    def setUp(self) -> None:
        random.seed(0x42)

    def test_exg_register_fail(self):
        self.assertRaises(ValueError, ExGRegister, bytes())

    def test_exg_register(self):
        reg1 = bytes([3, 160, 16, 64, 71, 0, 0, 0, 2, 1])
        reg2 = bytes([0, 171, 16, 21, 21, 0, 0, 0, 2, 1])

        exg_reg1 = ExGRegister(reg1)
        exg_reg2 = ExGRegister(reg2)

        self.assertEqual(exg_reg1.ch1_gain, 4)
        self.assertEqual(exg_reg1.ch2_gain, 4)
        self.assertEqual(exg_reg1.ch1_mux, ExGMux.NORMAL)
        self.assertEqual(exg_reg1.get_ch_mux_bin(0), 0b0000)
        self.assertEqual(exg_reg1.ch2_mux, ExGMux.RLD_DRM)
        self.assertEqual(exg_reg1.get_ch_mux_bin(1), 0b0111)
        self.assertEqual(exg_reg1.ch1_powerdown, False)
        self.assertEqual(exg_reg1.ch2_powerdown, False)
        self.assertEqual(exg_reg1.data_rate, 1000)
        self.assertEqual(exg_reg1.binary, reg1)

        self.assertEqual(exg_reg2.ch1_gain, 1)
        self.assertEqual(exg_reg2.ch2_gain, 1)
        self.assertEqual(exg_reg2.ch1_mux, ExGMux.TEST_SIGNAL)
        self.assertEqual(exg_reg2.ch2_mux, ExGMux.TEST_SIGNAL)
        self.assertEqual(exg_reg2.ch1_powerdown, False)
        self.assertEqual(exg_reg2.ch2_powerdown, False)
        self.assertEqual(exg_reg2.data_rate, 125)
        self.assertEqual(exg_reg2.binary, reg2)

        self.assertRaises(ValueError, exg_reg1.get_ch_mux, 2)
        self.assertRaises(ValueError, exg_reg1.get_ch_mux, -1)

    def test_exg_register_powerdown(self):
        pd = 0x1 << 7
        reg_bin = bytes([3, 160, 16, pd, pd, 0, 0, 0, 2, 1])
        reg = ExGRegister(reg_bin)

        self.assertEqual(reg.ch1_powerdown, True)
        self.assertEqual(reg.ch2_powerdown, True)

    def test_exg_register_rld_powerdown(self):
        pd = 0x01 << 5
        reg_bin = bytes([0, 0, 0, 0, 0, pd, 0, 0, 0, 0])
        reg = ExGRegister(reg_bin)

        self.assertEqual(reg.rld_powerdown, False)

    def test_exg_register_rld_channels(self):
        reg_bin = bytes([0x03, 0xA8, 0x10, 0x40, 0x40, 0x2D, 0x00, 0x00, 0x02, 0x03])
        reg = ExGRegister(reg_bin)
        self.assertEqual(
            reg.rld_channels, [ExGRLDLead.RLD1P, ExGRLDLead.RLD2P, ExGRLDLead.RLD2N]
        )

        reg_bin = bytes([0x03, 0xA8, 0x10, 0x40, 0x40, 0x00, 0x00, 0x00, 0x02, 0x03])
        reg = ExGRegister(reg_bin)
        self.assertEqual(reg.rld_channels, [])

    def test_exg_register_rld_ref(self):
        reg_bin = bytes([0x03, 0xA8, 0x10, 0x40, 0x40, 0x2D, 0x00, 0x00, 0x02, 0x03])
        reg = ExGRegister(reg_bin)
        self.assertEqual(reg.rld_ref, ERLDRef.INTERNAL)

        reg_bin = bytes([0x03, 0xA8, 0x10, 0x40, 0x40, 0x2D, 0x00, 0x00, 0x02, 0x01])
        reg = ExGRegister(reg_bin)
        self.assertEqual(reg.rld_ref, ERLDRef.EXTERNAL)

    def test_exg_register_print(self):
        reg_bin = bytes([0x03, 0xA8, 0x10, 0x40, 0x40, 0x2D, 0x00, 0x00, 0x02, 0x03])
        reg = ExGRegister(reg_bin)

        str_repr = str(reg)
        self.assertTrue("Data Rate: 1000" in str_repr)
        self.assertTrue("RLD Powerdown: False" in str_repr)

    def test_equality_operator(self):
        def do_assert(a: bytes, b: bytes, result: bool) -> None:
            self.assertEqual(ExGRegister(a) == ExGRegister(b), result)

        x = randbytes(10)
        y = randbytes(10)

        do_assert(x, y, False)
        do_assert(x, x, True)
        do_assert(y, y, True)

        for i in range(len(x)):
            y = bytearray(x)
            y[i] = random.randrange(0, 256)
            do_assert(x, y, False)


class ExGRegisterHelpersTest(TestCase):
    # Read from Shimmer3 units: one configured by Consensys for EMG, one left in test
    # signal mode
    EMG = bytes.fromhex("04a810696020000002" "03")
    TEST = bytes.fromhex("03ab1065652000000203")

    def test_data_rate_for_sampling_rate_is_the_smallest_rate_that_is_not_below(self):
        expected = {
            1: 125,
            50: 125,
            125: 125,
            126: 250,
            250: 250,
            500: 500,
            512: 1000,
            1000: 1000,
            1024: 2000,
            2000: 2000,
            2048: 4000,
            4000: 4000,
            4001: 8000,
            10000: 8000,
        }
        for sr, sps in expected.items():
            self.assertEqual(exg_data_rate_for_sampling_rate(sr), sps, f"rate {sr}")

    def test_with_data_rate_changes_only_the_rate_bits(self):
        new = ExGRegister(self.TEST).with_data_rate(2000)
        self.assertEqual(new.data_rate, 2000)
        self.assertEqual(new.binary, bytes.fromhex("04ab1065652000000203"))
        self.assertEqual(ExGRegister(self.EMG).with_data_rate(125).binary[0], 0x00)
        self.assertEqual(ExGRegister(self.EMG).with_data_rate(8000).binary[0], 0x06)

    def test_with_data_rate_rejects_unsupported_rates(self):
        with self.assertRaises(ValueError):
            ExGRegister(self.EMG).with_data_rate(1024)

    def test_data_rate_index(self):
        self.assertEqual(ExGRegister(self.TEST).data_rate_index, 3)
        self.assertEqual(ExGRegister(self.EMG).data_rate_index, 4)

    def test_test_signal_detection(self):
        self.assertTrue(ExGRegister(self.TEST).uses_test_signal)
        self.assertFalse(ExGRegister(self.EMG).uses_test_signal)

    def test_emg_preset_is_what_consensys_wrote_on_the_emg_units(self):
        self.assertEqual(ExGRegister.emg_preset().binary, self.EMG)
        reg = ExGRegister.emg_preset(rate_sps=1000, gain=6)
        self.assertEqual(reg.data_rate, 1000)
        self.assertEqual((reg.ch1_gain, reg.ch2_gain), (6, 6))
        self.assertEqual((reg.ch1_mux, reg.ch2_mux), (ExGMux.INPUT3, ExGMux.NORMAL))
        self.assertFalse(reg.uses_test_signal)

    def test_emg_preset_validates_its_arguments(self):
        with self.assertRaises(ValueError):
            ExGRegister.emg_preset(gain=5)
        with self.assertRaises(ValueError):
            ExGRegister.emg_preset(rate_sps=1024)


class ExGRegisterBuildersTest(TestCase):
    """The register contents that Consensys and the official APIs write, and the changes the node makes to them"""

    EMG = bytes([0x04, 0xA8, 0x10, 0x69, 0x60, 0x20, 0x00, 0x00, 0x02, 0x03])
    TEST = bytes([0x03, 0xAB, 0x10, 0x65, 0x65, 0x20, 0x00, 0x00, 0x02, 0x03])
    # The defaults of the C# API (and of ShimmerCapture) for each chip
    OFFICIAL = {
        "ecg1": [0x00, 0xA0, 0x10, 0x40, 0x40, 0x2D, 0x00, 0x00, 0x02, 0x03],
        "ecg2": [0x00, 0xA0, 0x10, 0x40, 0x47, 0x00, 0x00, 0x00, 0x02, 0x01],
        "emg1": [0x00, 0xA0, 0x10, 0x69, 0x60, 0x20, 0x00, 0x00, 0x02, 0x03],
        "emg2": [0x00, 0xA0, 0x10, 0xE1, 0xE1, 0x00, 0x00, 0x00, 0x02, 0x01],
        "test1": [0x00, 0xA3, 0x10, 0x45, 0x45, 0x00, 0x00, 0x00, 0x02, 0x01],
        "test2": [0x00, 0xA3, 0x10, 0x45, 0x45, 0x00, 0x00, 0x00, 0x02, 0x01],
        "respiration1": [0x02, 0xA8, 0x10, 0x40, 0x40, 0x20, 0x00, 0x00, 0x02, 0x03],
        "respiration2": [0x02, 0xA0, 0x10, 0x40, 0x40, 0x00, 0x00, 0x00, 0xEA, 0x01],
    }

    def test_the_configurations_of_the_units_and_the_official_apis_have_no_problems(
        self,
    ):
        regs = {k: bytes(v) for k, v in self.OFFICIAL.items()}
        regs.update(emg_unit=self.EMG, test_unit=self.TEST)
        for name, reg_bin in regs.items():
            self.assertEqual(ExGRegister(reg_bin).problems(), [], name)

    def test_the_presets_have_no_problems(self):
        for rate in (125, 250, 500, 1000, 2000, 4000, 8000):
            for gain in (1, 2, 3, 4, 6, 8, 12):
                self.assertEqual(ExGRegister.emg_preset(rate, gain).problems(), [])
                self.assertEqual(ExGRegister.test_preset(rate, gain).problems(), [])

    def test_test_preset_is_what_a_unit_in_test_signal_mode_holds(self):
        self.assertEqual(ExGRegister.test_preset(rate_sps=1000).binary, self.TEST)

    def test_each_problem_is_found(self):
        def problems_with(index, value):
            reg = bytearray(self.EMG)
            reg[index] = value
            return " ".join(ExGRegister(bytes(reg)).problems())

        self.assertIn("single-shot", problems_with(0, 0x84))
        self.assertIn("reserved bits 6:3", problems_with(0, 0x0C))
        self.assertIn("DO NOT USE", problems_with(0, 0x07))
        self.assertIn("CONFIG2: bit 7", problems_with(1, 0x28))
        self.assertIn("4.033 V", problems_with(1, 0xB8))
        self.assertIn("LOFF: bit 4", problems_with(2, 0x00))
        self.assertIn("gain bits 111", problems_with(3, 0x79))
        self.assertIn("multiplexer value 10", problems_with(4, 0x6A))
        self.assertIn("RLD_SENS", problems_with(5, 0xA0))
        self.assertIn("RESP1", problems_with(8, 0x00))
        self.assertIn("RESP2: bit 0", problems_with(9, 0x02))

    def test_builders_change_only_their_bits(self):
        emg = ExGRegister(self.EMG)

        def hexed(reg):
            return reg.binary.hex()

        self.assertEqual(hexed(emg.with_gain(0, 6)), "04a810096020000002" "03")
        self.assertEqual(hexed(emg.with_gain(1, 1)), "04a810691020000002" "03")
        self.assertEqual(
            hexed(emg.with_mux(1, ExGMux.SHORTED)), "04a8106961200000" "0203"
        )
        self.assertEqual(emg.with_mux(0, ExGMux.NORMAL).binary[3], 0x60)
        self.assertEqual(emg.with_powerdown(1, True).binary[4], 0xE0)
        self.assertEqual(emg.with_powerdown(1, True).with_powerdown(1, False), emg)
        self.assertEqual(emg.with_test_signal("square").binary[1], 0xAB)
        self.assertEqual(emg.with_test_signal("dc").binary[1], 0xAA)
        self.assertEqual(emg.with_test_signal("square").with_test_signal("off"), emg)
        self.assertEqual(emg.with_data_rate(4000).binary[0], 0x05)

    def test_gain_mux_and_test_signal_are_validated(self):
        emg = ExGRegister(self.EMG)
        with self.assertRaises(ValueError):
            emg.with_gain(0, 5)
        with self.assertRaises(ValueError):
            emg.with_gain(2, 12)
        with self.assertRaises(ValueError):
            emg.with_mux(0, ExGMux.RESERVED)
        with self.assertRaises(ValueError):
            emg.with_test_signal("sine")

    def test_test_signal_property(self):
        self.assertEqual(ExGRegister(self.EMG).test_signal, "off")
        self.assertEqual(ExGRegister(self.TEST).test_signal, "square")
        self.assertEqual(ExGRegister(self.EMG).with_test_signal("dc").test_signal, "dc")
        # the frequency bit means nothing while the generator is off
        reg = bytearray(self.EMG)
        reg[1] = 0xA9
        self.assertEqual(ExGRegister(bytes(reg)).test_signal, "off")

    def test_rld(self):
        emg = ExGRegister(self.EMG)
        self.assertEqual(emg.rld_channels, [])
        self.assertFalse(emg.rld_powerdown)
        self.assertEqual(emg.rld_ref, ERLDRef.INTERNAL)

        new = emg.with_rld(channels=[ExGRLDLead.RLD1P, ExGRLDLead.RLD1N])
        self.assertEqual(new.rld_channels, [ExGRLDLead.RLD1P, ExGRLDLead.RLD1N])
        self.assertEqual(new.binary[5], 0x23)  # "Inverse of Ch1" in ShimmerCapture
        self.assertTrue(emg.with_rld(enabled=False).rld_powerdown)
        self.assertEqual(
            emg.with_rld(internal_reference=False).rld_ref, ERLDRef.EXTERNAL
        )
        self.assertEqual(emg.with_rld(), emg)

    def test_leadoff_detection_is_set_up_as_shimmercapture_does(self):
        emg = ExGRegister(self.EMG)
        self.assertFalse(emg.leadoff_detection)

        on = emg.with_leadoff(detection=True)
        self.assertTrue(on.leadoff_detection)
        self.assertEqual(on.binary[1], 0xE8)  # PDB_LOFF_COMP
        self.assertEqual(on.binary[5], 0x30)  # RLD_LOFF_SENS
        self.assertEqual(on.binary[6], 0x07)  # LOFF1P, LOFF1N and LOFF2P
        self.assertEqual(on.with_leadoff(detection=False), emg)
        self.assertEqual(on.problems(), [])

    def test_leadoff_current_and_threshold(self):
        emg = ExGRegister(self.EMG)
        self.assertEqual(emg.leadoff_current, "6nA")
        self.assertEqual(emg.leadoff_threshold, 95.0)

        new = emg.with_leadoff(current="22uA", threshold=70.0)
        self.assertEqual(new.binary[2], 0xE0 | 0x10 | 0x0C)
        self.assertEqual(new.leadoff_current, "22uA")
        self.assertEqual(new.leadoff_threshold, 70.0)
        with self.assertRaises(ValueError):
            emg.with_leadoff(current="1A")
        with self.assertRaises(ValueError):
            emg.with_leadoff(threshold=50.0)

    def test_same_configuration_ignores_what_belongs_to_the_device(self):
        emg = ExGRegister(self.EMG)
        board_bit = bytearray(self.EMG)
        board_bit[1] ^= 0x08
        status = bytearray(self.EMG)
        status[7] = 0x1F  # lead-off status of the inputs
        self.assertTrue(emg.same_configuration(ExGRegister(bytes(board_bit))))
        self.assertTrue(emg.same_configuration(ExGRegister(bytes(status))))
        self.assertFalse(emg.same_configuration(emg.with_gain(0, 6)))
        self.assertFalse(emg.same_configuration(emg.with_data_rate(125)))

    def test_the_board_bit_of_the_device_is_kept(self):
        wanted = ExGRegister.emg_preset()  # has bit 3 of CONFIG2 set
        current = bytearray(self.EMG)
        current[1] &= ~0x08 & 0xFF  # an older board revision
        kept = wanted.keeping_board_bits_of(ExGRegister(bytes(current)))
        self.assertEqual(kept.binary[1], 0xA0)
        self.assertEqual(kept.binary[0], wanted.binary[0])
        self.assertEqual(kept.binary[2:], wanted.binary[2:])

    def test_vref(self):
        self.assertAlmostEqual(ExGRegister(self.EMG).vref_volts, 2.42)
        reg = bytearray(self.EMG)
        reg[1] |= 0x10
        self.assertAlmostEqual(ExGRegister(bytes(reg)).vref_volts, 4.033)
