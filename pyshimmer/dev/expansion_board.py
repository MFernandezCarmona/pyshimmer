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
from typing import ClassVar

# The board identifiers of the C# API (ExpansionBoardDetectShimmer3)
_BOARD_NAMES = {
    8: "Bridge Amplifier+",
    14: "GSR+",
    31: "None",
    36: "PROTO3 Mini",
    37: "ExG",
    38: "PROTO3 Deluxe",
    44: "High G Accel",
    46: "GPS",
    47: "ExG",
    48: "GSR+",
    49: "Bridge Amplifier+",
    59: "ExG",
}
_EXG_BOARDS = (37, 47, 59)


@dataclass(frozen=True)
class ExpansionBoard:
    """The expansion board that sits under a Shimmer3

    :param board_id: The identifier of the board (the "SR" number: 47 is the unified ExG
        board of the ECG and EMG units)
    :param revision: The revision of the board
    :param special_revision: The special revision of the board
    """

    board_id: int
    revision: int
    special_revision: int

    UNKNOWN: ClassVar[str] = "Unknown"

    @property
    def name(self) -> str:
        """The kind of board, or "Unknown" for an identifier that is not known"""
        return _BOARD_NAMES.get(self.board_id, self.UNKNOWN)

    @property
    def has_exg(self) -> bool:
        """True if the board holds the ADS1292R chips"""
        return self.board_id in _EXG_BOARDS

    def __str__(self) -> str:
        return (
            f"{self.name} (SR{self.board_id}-{self.revision}.{self.special_revision})"
        )
