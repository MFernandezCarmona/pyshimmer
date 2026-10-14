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
"""Frame check sequence of the optional Bluetooth CRC mode of the LogAndStream firmware (>= 0.13.7).

The algorithm is the one of ``ShimmerUartCrcCalc()`` in the official Shimmer C# API (a CRC-CCITT variant with
initial value 0xB0CA whose input is padded with a zero byte when its length is odd). The wire format was
verified against a Shimmer3 running LogAndStream 0.15.4:

* a data packet is ``0x00 | payload | crc``; the CRC covers the header byte and the payload
* the reply to a command is ``0xFF [| response code(s) | payload] | crc``; the CRC covers everything from the
  acknowledgment byte up to the end of the response payload, so an acknowledgment alone is ``0xFF | crc``
* ``crc`` has one (``CRC_1BYTE``: only the low byte) or two (``CRC_2BYTE``: low byte first) bytes
"""

CRC_OFF = 0
CRC_1BYTE = 1
CRC_2BYTE = 2

_CRC_INIT = 0xB0CA


def _crc_step_reference(crc: int, b: int) -> int:
    """One byte of the algorithm, written like the official C# implementation. Kept to validate the tables."""
    crc &= 0xFFFF
    crc = (crc >> 8) | (crc << 8)
    crc ^= b & 0xFF
    crc ^= (crc & 0xFF) >> 4
    crc ^= crc << 12
    crc ^= (crc & 0xFF) << 5
    return crc & 0xFFFF


def _mix(x: int) -> int:
    """The part of a step that follows the byte swap and the XOR with the input byte: linear over GF(2)"""
    x ^= (x & 0xFF) >> 4
    x ^= x << 12
    x ^= (x & 0xFF) << 5
    return x & 0xFFFF


# Being linear, _mix(x) == _mix(x & 0xFF00) ^ _mix(x & 0x00FF): two 256 entry tables replace the shifts
_MIX_HI = tuple(_mix(h << 8) for h in range(256))
_MIX_LO = tuple(_mix(l) for l in range(256))


def crc16(data: bytes) -> bytes:
    """Calculate the Shimmer UART CRC of a byte string

    :param data: The bytes the CRC covers
    :return: The two CRC bytes, least significant byte first
    """
    hi, lo = _MIX_HI, _MIX_LO
    crc = _CRC_INIT
    for b in data:
        x = ((crc >> 8) | ((crc & 0xFF) << 8)) ^ b
        crc = hi[x >> 8] ^ lo[x & 0xFF]
    if len(data) % 2:
        x = (crc >> 8) | ((crc & 0xFF) << 8)
        crc = hi[x >> 8] ^ lo[x & 0xFF]

    return bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def crc_matches(data: bytes, received: bytes, crc_len: int) -> bool:
    """Check the CRC bytes that followed a frame

    :param data: The bytes the CRC covers
    :param received: The CRC bytes as received, ``crc_len`` of them
    :param crc_len: The number of CRC bytes of the active mode, 1 or 2
    :return: True if the CRC bytes are valid for the data
    """
    if crc_len not in (CRC_1BYTE, CRC_2BYTE) or len(received) != crc_len:
        return False

    return crc16(data)[:crc_len] == received
