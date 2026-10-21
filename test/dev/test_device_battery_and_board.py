# pyshimmer - API for Shimmer sensor devices
# Copyright (C) 2026  Manuel Fernandez-Carmona

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
from unittest import TestCase

from pyshimmer.dev.battery import BatteryReading, ChargingStatus
from pyshimmer.dev.expansion_board import ExpansionBoard
from pyshimmer.util import battery_adc_to_voltage


class BatteryReadingTest(TestCase):

    def test_the_charging_status_values_are_those_of_the_charger_pins(self):
        # both pins high: no charger (a unit that is not docked); both low: bad battery. The manual has them swapped.
        self.assertEqual(ChargingStatus.SUSPENDED, 0xC0)
        self.assertEqual(ChargingStatus.FULLY_CHARGED, 0x40)
        self.assertEqual(ChargingStatus.PRECONDITIONING, 0x80)
        self.assertEqual(ChargingStatus.BAD_BATTERY, 0x00)

    def test_the_status_of_a_listed_byte(self):
        for status in ChargingStatus:
            reading = BatteryReading(4.0, 90.0, int(status))
            self.assertEqual(reading.charging_status, status)

    def test_a_byte_that_is_not_listed_has_no_status(self):
        self.assertIsNone(BatteryReading(4.0, 90.0, 0x01).charging_status)

    def test_adc_to_voltage(self):
        self.assertAlmostEqual(battery_adc_to_voltage(0x0B2E), 4.168246153846154)
        self.assertEqual(battery_adc_to_voltage(0), 0)


class ExpansionBoardTest(TestCase):

    def test_the_unified_exg_board_of_the_ecg_and_emg_units(self):
        board = ExpansionBoard(47, 4, 0)
        self.assertEqual(board.name, "ExG")
        self.assertTrue(board.has_exg)
        self.assertEqual(str(board), "ExG (SR47-4.0)")

    def test_a_board_without_exg(self):
        board = ExpansionBoard(14, 2, 1)
        self.assertEqual(board.name, "GSR+")
        self.assertFalse(board.has_exg)

    def test_an_unknown_board(self):
        board = ExpansionBoard(200, 1, 0)
        self.assertEqual(board.name, ExpansionBoard.UNKNOWN)
        self.assertFalse(board.has_exg)
        self.assertEqual(str(board), "Unknown (SR200-1.0)")

    def test_every_exg_board_id_has_exg(self):
        for board_id in (37, 47, 59):
            self.assertTrue(ExpansionBoard(board_id, 0, 0).has_exg)
