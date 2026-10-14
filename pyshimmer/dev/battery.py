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
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class ChargingStatus(IntEnum):
    """The charging status byte of the response to GET_VBATT_COMMAND

    The values are those of Table 7-6 of the LogAndStream firmware manual (Rev 0.11a).
    """

    SUSPENDED = 0x00
    FULLY_CHARGED = 0x40
    PRECONDITIONING = 0x80
    BAD_BATTERY = 0xC0


@dataclass(frozen=True)
class BatteryReading:
    """What the device answers when asked for its battery

    :param voltage: The battery voltage in Volt
    :param percent: The remaining capacity estimated from the voltage (Table 6-3 of the
        Shimmer User Manual), in percent
    :param status_byte: The charging status byte as the device sent it
    """

    voltage: float
    percent: float
    status_byte: int

    @property
    def charging_status(self) -> ChargingStatus | None:
        """The charging status, or None if the byte is not one the manual lists"""
        try:
            return ChargingStatus(self.status_byte)
        except ValueError:
            return None
