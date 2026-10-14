# Changelog

This changelog tries to follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The project uses semantic versioning.

## Next Release

Reliability of the Bluetooth API. Measured with three Shimmer3 units (LogAndStream 0.15.4) on one Bluetooth
adapter; see `shimmer_reliability/RESULTADOS.md` in the workspace for the data.

### Added
- `ShimmerError`, `ShimmerLinkError`, `ShimmerTimeoutError` and `ShimmerConnectError` (`pyshimmer.bluetooth.bt_errors`).
  `CommandRefused` (a NACK) is a `ShimmerError` too. A request
  that is not answered raises instead of blocking forever; when the link breaks every outstanding and every later
  request raises.
- `ShimmerBluetooth.add_error_callback()`, `connected`, `last_error`, `link_stats` and `last_data_age`.
- `ShimmerBluetooth.connect()`: opens the port and initializes the device, retrying (a Shimmer was seen to refuse
  the first attempt after being idle and to accept the retry at once).
- `ConnectLock`: `flock`-based lock to serialize Bluetooth connection setup across processes. Unlike a lock file it
  is released by the operating system when its holder dies. Hold it until the first data packet arrived **and keep
  the next connection 1-2 s away** (`min_interval=2.0`): a device that has just started is not ready for another
  connection, which then fails and leaves the rfcomm port `EBUSY` for about 30 s (three units started together
  took 41-86 s to stream without it, 15-18 s with it).
- Stream watchdog: a stream that goes silent (`stall_timeout`) or never starts (`start_timeout`) is reported as a
  `ShimmerLinkError`. It does not poll the device: every command sent during a stream costs the device samples.
- Bluetooth CRC mode (`crc=` argument, `SetCrcCommand`, `FirmwareCapabilities.supports_crc`) for LogAndStream
  >= 0.13.7. Every frame is validated; corrupt frames are dropped and the receiver resynchronizes.
- `start_streaming(wait_for_data=..., check_exg=...)`.
- `set_sampling_rate(..., update_exg_rate=True)`, `get_exg_data_rate()`, `set_exg_data_rate()`,
  `exg_data_rate_for_sampling_rate()`, `ExGRegister.with_data_rate()`, `ExGRegister.emg_preset()`,
  `ExGRegister.uses_test_signal`, `ExGRegister.data_rate_index`.
- `DataPacket.decode()` and `DataPacket.payload_size`; `SerialBase.available()`, `SerialBase.wait_readable()`.
- Tests: `test/bluetooth/test_bt_reliability.py` and a simulated device (`test/bluetooth/fake_device.py`) that can
  inject faults.
- A refusal (NACK) is understood by the tolerant receiver, with and without CRC: the command raises
  `CommandRefused` and the link stays usable. Unsolicited ones are counted (`link_stats['unexpected_nacks']`).
- ExG configuration as a whole. `ExGRegister` can be built field by field (`with_gain()`, `with_mux()`,
  `with_powerdown()`, `with_test_signal()`, `with_rld()`, `with_leadoff()`), checked (`problems()`: the bits the chip
  fixes, the values marked DO NOT USE, single-shot mode and the 4.033 V reference, which the conversions do not
  support) and compared (`same_configuration()` ignores the bits that belong to the device). New properties:
  `test_signal`, `leadoff_*`, `single_shot`, `vref_volts`. `ExGRegister.test_preset()`.
  `ShimmerBluetooth.write_exg_register()` writes the ten bytes of a chip and reads them back and raises
  `ShimmerConfigurationError` (a `ShimmerError`) if the chip took something else.
- `ShimmerBluetooth.get_battery_reading()`: voltage, capacity and the charging status byte of the response
  (`BatteryReading`, `ChargingStatus`). It can be used while streaming; each call costs about 5 packets at 2048 Hz.
- `ShimmerBluetooth.get_expansion_board()` (`ExpansionBoard`): the identification of the board under the Shimmer3,
  with `has_exg`.

### Changed
- Format code base with black
- Wrap long lines to 90 characters
- Replace types from typing with built-in ones
- Raise required Python version to 3.9 since PEP 604 is used in the code
- Update the `ChannelDataType` class to use the `int.from_bytes` and
  `int.to_bytes` methods
- Introduce new hardware revision classes to encapsulate all hardware-specific
  code. As a consequence, all global functions in `pyshimmer.dev.base` and
  `pyshimmer.dev.channels` were removed.
- Move the `get_exg_ch` function into the `HardwareRevision` class
- `ShimmerBluetooth` constructor: new keyword-only arguments `command_timeout` (5 s), `stall_timeout` (3 s),
  `start_timeout` (10 s), `crc` (`'auto'`), `presync` (True), `tolerant` (True), `lookahead` (True). **The defaults
  change behavior**: pass `command_timeout=None, stall_timeout=None, crc='off', presync=False, tolerant=False` for
  the previous behavior.
- `initialize()` stops a stream a previous session left running and waits for the line to go quiet before
  anything else (it also enables the CRC mode if the firmware supports it). It no longer fails when the device
  does not answer the hardware version query added by this fork.
- An exception raised by a stream callback is logged and counted instead of ending the read loop.
- Replies are matched with their request by *response code*, not only by arrival order, and replies that nobody
  waits for any more are consumed and discarded. Requests are sent under a lock.
- `ShimmerBluetooth.shutdown()` is idempotent, works on a failed link and does not block on a read loop that is
  busy with a stream.
- The library logs through `logging` (`pyshimmer.*`) instead of `print()`.
- `ShimmerBluetooth.shutdown()` closes the serial port it was given, so that the rfcomm channel is released.
- The hardware version that the device reports is only a hint when the device does not answer it (the revision is
  then a Shimmer3); a version without a revision is still a `ValueError`.

### Fixed
- The read loop died silently on any unexpected byte (stray byte, byte lost in a packet, acknowledgment nobody
  asked for, hang-up) and every later request blocked forever.
- A single lost acknowledgment shifted the matching of all later acknowledgments.
- Connecting to a device that was still streaming (e.g. after the previous process was killed) failed.
- A packet that lost a byte was delivered to the callbacks as valid data.

## 1.0.0 - 2025-10-25

This is a rebrand of release v0.7.0 as v1.0.0.

A lot of incoming changes and modifications already present on the main branch
make the future code base incompatible to previous releases, such as v0.7.0.
I would like to make the state that is v0.7.0 the official version 1.0.0 of
pyshimmer. That way, it will be easier for users to switch between this first
version and the newer modifications that are going to lead to version 2.0.0.

## 0.7.0 - 2025-01-18

First release with Changelog

### Added
- This Changelog :)

### Changed
- The CI workflow now builds and deploys the artifacts

