pyshimmer: Unofficial Python API for Shimmer Sensor devices
===========================================================

.. image:: https://github.com/seemoo-lab/pyshimmer/actions/workflows/build.yml/badge.svg
    :target: https://github.com/seemoo-lab/pyshimmer

.. contents::

General Information
-------------------

pyshimmer provides a Python API to work with the wearable sensor devices produced by Shimmer_. The API is divided into
three major components:

* The Bluetooth API: An interface to communicate with the Shimmer LogAndStream firmware via Bluetooth
* The Dock API: An interface to communicate with the Shimmer while they are placed in a dock
* The Reader API: An interface to read the binary files produced by the Shimmer devices

.. _Shimmer: http://www.shimmersensing.com/

Please note that the following README does not provide a general introduction to the Shimmer devices. For this, please
consult the corresponding `documentation page <http://www.shimmersensing.com/support/wireless-sensor-networks-documentation/>`_
of the vendor and take a closer look at:

* The Shimmer User Manual
* The LogAndStream Manual
* The SDLog Manual

Contributing
------------
All code in this repository was produced as part of my Master thesis. This means that the API is not
complete. Especially the Bluetooth and Dock API do not feature all calls supported by the devices. However, the code
provides a solid foundation to extend it where necessary. Please feel free to make contributions in case the code is
missing required calls.

Please submit pull requests against the develop branch. When a new version is released, the current state of the
develop branch is merged into master to produce the new version.

Installation
------------

The targeted plattform for this library is **Linux**. It has not been tested on other operating systems. In order to
use all aspects of the library, you need to install the package itself, set up the Bluetooth interface, and possibly
configure udev rules to ensure that the device names are consistent.

pyshimmer Package
^^^^^^^^^^^^^^^^^
The easiest and quickest way is to install the latest release with pip:

.. code-block::

    pip install pyshimmer

From Source with Tests
^^^^^^^^^^^^^^^^^^^^^^

You can also install from source in case you would like to run the tests:

.. code-block::

    git clone https://github.com/seemoo-lab/pyshimmer.git
    cd pyshimmer
    pip install .[test]

You can then run the tests from the repository root by simply issuing:

.. code-block::

    pytest

Shimmer Firmware
^^^^^^^^^^^^^^^^

As of version v0.15.4 of the `Shimmer3 firmware <https://github.com/ShimmerResearch/shimmer3>`_ the Python API is
fully compatible to the firmware. Older versions of the vanilla firmware exhibit several bugs and are incompatible.
If you intend to use a firmware version older than 0.15.4, you will need to compile and run a custom patched version of
the firmware. In the following table, the firmware versions and their compatibility are listed.

Compatibility Table
"""""""""""""""""""

============= ========= ============= ======================================================================
Firmware Type Version   Compatibility Issues
============= ========= ============= ======================================================================
LogAndStream  v0.15.4   Compatible    You will need to use pyshimmer v0.4 or newer
                        to v0.4.0
LogAndStream  v0.11.0   Incompatible  - `Issue 7 <https://github.com/ShimmerResearch/shimmer3/issues/7>`_
                                      - `Issue 10 <https://github.com/ShimmerResearch/shimmer3/issues/10>`_
SDLog         v0.21.0   Compatible    Untested
SDLog         v0.19.0   Compatible
============= ========= ============= ======================================================================

It is recommended to use the newest *LogAndStream* firmware that is compatible to the API. If you want to use an older
version with the pyshimmer library, you will need to compile and program a patched version of the firmware. We provide
a forked repository which features the necessary fixes `here <https://github.com/seemoo-lab/shimmer3/>`_.
It also contains instructions on how to compile and program the firmware.

Notes on Firmware Version 0.15.4
""""""""""""""""""""""""""""""""
Starting with Firmware version v0.15.4,
`the race condition issue <https://github.com/ShimmerResearch/shimmer3/issues/7>`_ in the Bluetooth stack has been
fixed. Additionally, the Shimmer3 now supports an additional command to disable the unsolicited status acknowledgment
byte (see `Issue 10 <https://github.com/ShimmerResearch/shimmer3/issues/10>`_). The pyshimmer Bluetooth API tries to
automatically detect if the Shimmer3 runs a firmware newer or equal to v0.15.4 and automatically issues the command
to disable the unsolicited status acknowledgment at startup. You can optionally disable this feature in the constructor.
With this new command, the state machine in the Bluetooth API of pyshimmer is compatible to the vanilla firmware
version.

Creating udev rules for persistent device filenames
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

When plugging a Shimmer dock into the host, Linux will detect two new serial interfaces and a block device representing
the internal SD card of the Shimmer:

* :code:`/dev/ttyUSB0` for the serial interface to the bootloader,
* :code:`/dev/tty/USB1` for the serial interface to the device itself,
* :code:`/dev/sdX` for the block device.

When working with multiple docks and devices, keeping track of the names of the serial interfaces can be quite
cumbersome, since udev simply names the devices in the order they are plugged in to the system. You can use udev rules
to assign persistent names to the device files. Note that the rules do not actually match the Shimmer but the dock that
it is located in. **This means that you should always place the Shimmer in the same dock**.

The following section provides an example of how to handle two Shimmer docks, one of which holds an ECG and the other a
PPG device:

Distinguishing the Shimmer booloader and device interfaces based on their udev attributes is somewhat difficult because
the distinguishing attributes are spread across multiple devices in the USB device tree. The rules first check the
bInterfaceNumber of the tty device that is being processed. If the device is the bootloader device, its bInterfaceNumber
is equal to 00. If the device is the interface to the Shimmer itself, bInterfaceNumber is equal to 01.
In a second step, the rule set differentiates between the ECG dock and the PPG dock based on the serial number of
the device. The entire udev ruleset is shown in the following code snippet:

.. code-block::

    SUBSYSTEMS=="usb" ATTRS{bInterfaceNumber}!="00" GOTO="is_secondary_interface"
    SUBSYSTEM=="tty" ATTRS{idVendor}=="<id_vendor1>" ATTRS{idProduct}=="<id_product1>" ATTRS{serial}=="<id_serial1>" SYMLINK+="ttyPPGbl"
    SUBSYSTEM=="tty" ATTRS{idVendor}=="<id_vendor2>" ATTRS{idProduct}=="<id_product2>" ATTRS{serial}=="<id_serial2>" SYMLINK+="ttyECGbl"
    GOTO="end"

    LABEL="is_secondary_interface"
    SUBSYSTEM=="tty" ATTRS{idVendor}=="<id_vendor1>" ATTRS{idProduct}=="<id_product1>" ATTRS{serial}=="<id_serial1>" SYMLINK+="ttyPPGdev"
    SUBSYSTEM=="tty" ATTRS{idVendor}=="<id_vendor2>" ATTRS{idProduct}=="<id_product2>" ATTRS{serial}=="<id_serial2>" SYMLINK+="ttyECGdev"
    GOTO="end"

    LABEL="end"

You can also find the example file in :code:`conf/udev/10-shimmer.rules.example`.

In order to create a custom ruleset for your devices, create a new udev rule file
:code:`/etc/udev/rules.d/10-shimmer.rules` and add the above contents. In the file, you need to replace the
:code:`<id_vendor1>`, :code:`<id_product1>`, and :code:`<id_serial1>` of the first device, and the :code:`<id_vendor2>`,
:code:`<id_product2>`, and :code:`<id_serial2>` of the second device. You can find the values by scanning the
:code:`dmesg` command after plugging in a Shimmer device. Here is an example:

.. code-block::

    [144366.290357] usb 1-4.3: new full-speed USB device number 34 using xhci_hcd
    [144366.386661] usb 1-4.3: New USB device found, idVendor=<id_vendor>, idProduct=<id_product>, bcdDevice= 5.00
    [144366.386668] usb 1-4.3: New USB device strings: Mfr=1, Product=2, SerialNumber=3
    [144366.386674] usb 1-4.3: Product: SHIMMER DOCK
    [144366.386679] usb 1-4.3: Manufacturer: FTDI
    [144366.386684] usb 1-4.3: SerialNumber: <id_serial>

Save the file and reload the rules for them to take effect:

.. code-block::

    udevadm control --reload-rules && udevadm trigger

You should now have two named device files for each Shimmer dock:

* :code:`/dev/ttyPPGbl` and :code:`/dev/ttyPPGdev` for the PPG Shimmer bootloader and device interfaces,
* :code:`/dev/ttyECGbl` and :code:`/dev/ttyECGdev` for the ECG Shimmer bootloader and device interfaces.

Configuring the Bluetooth Interface
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
The library uses a :code:`tty` serial interface to communicate with the Shimmer over Bluetooth. Before you can use the
library, you need to set up the serial channel appropriately. This has only been tested under Arch Linux, but other
Linux distributions should work as well.

Requirements:

* Functioning Bluetooth stack
* :code:`bluez` command line tools (on Arch Linux use https://archlinux.org/packages/extra/x86_64/bluez-deprecated-tools/)
    - :code:`rfcomm`
    - :code:`hcitool`
    - :code:`sdptool`
* A Shimmer device with  :code:`LogAndStream` firmware

Scan for the device to find out its MAC address:

.. code-block::

    hcitool scan

The MAC address of the listed Shimmer device should end with the *BT Radio ID* (or simply *BTID*) imprinted on the back of the device.
Next, you can try and ping the device:

.. code-block::

    hcitool name <mac_addr>

The command should complete with the name listed previously during the scan.

Now verify which channel the Shimmer uses for RFCOMM:

.. code-block::

    sdptool records <mac_addr>

In the output, look for the "SPP SERVER" section. Under the "RFCOMM" you will find the "Channel" number.

.. code-block::

    ...
    Service Name: SPP SERVER
    Service RecHandle: 0x10002
    Service Class ID List:
      "Serial Port" (0x1101)
    Protocol Descriptor List:
      "L2CAP" (0x0100)
      "RFCOMM" (0x0003)
        Channel: 2
    Profile Descriptor List:
      "Serial Port" (0x1101)
        Version: 0x0102
    ...

Now you can pair the device as follows:

.. code-block::

    rfcomm <bind_id> <mac_addr> <channel>

where :code:`<bind_id>` is an arbitrary integer of your choosing
and the :code:`<channel>` is the channel reported by the `sdptool` (if not specified `rfcomm` assumes `1` but some devices might communicate via a different channel, e.g. `2`).
The command will create a new serial interface node
with the following name: :code:`/dev/rfcomm<bind_id>`.
The file acts as a regular serial device and allows you to communicate with the Shimmer. The file is also used by the
library.

Using the API
-------------

Using the Bluetooth interface
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

If you want to connect to the Bluetooth interface, use the :code:`ShimmerBluetooth` class. The API only offers blocking
calls.

.. code-block:: python

    import time

    from serial import Serial

    from pyshimmer import ShimmerBluetooth, DEFAULT_BAUDRATE, DataPacket, EChannelType


    def handler(pkt: DataPacket) -> None:
        cur_value = pkt[EChannelType.INTERNAL_ADC_13]
        print(f'Received new data point: {cur_value}')


    if __name__ == '__main__':
        with (
                Serial('/dev/rfcomm42', DEFAULT_BAUDRATE) as serial,
                ShimmerBluetooth(serial) as shim_dev
             ):

            dev_name = shim_dev.get_device_name()
            print(f'My name is: {dev_name}')

            shim_dev.add_stream_callback(handler)

            shim_dev.start_streaming()
            time.sleep(5.0)
            shim_dev.stop_streaming()

The example shows how to make simple calls and how to use the Bluetooth streaming capabilities of the device.

Reliable connections
^^^^^^^^^^^^^^^^^^^^

A Bluetooth link to a body-worn sensor is not reliable: a device refuses a connection, a byte is lost, the person
walks away. The API is built so that none of this can leave your program blocked or silently without data.

.. code-block:: python

    import logging

    from pyshimmer import ShimmerBluetooth, ShimmerError, ConnectLock, ESensorGroup, DataPacket

    logging.basicConfig(level=logging.INFO)  # the library logs through the "pyshimmer" loggers


    def handler(pkt: DataPacket) -> None:
        ...  # called in the read thread for every data packet


    def on_link_error(err: ShimmerError) -> None:
        # Runs in the thread that noticed. Do not block here: set a flag and let your main loop reconnect.
        print(f'Link lost: {err}')


    # Open + initialize with retries. Starting several devices at once: hold the lock until each one streams, and
    # keep the next one away for 2 s more (min_interval): a device that has just started is not ready to be
    # followed by another connection.
    with ConnectLock('/tmp/pyshimmer_bt.lock', min_interval=2.0):
        shim_dev = ShimmerBluetooth.connect('/dev/rfcomm42', retries=3)
        shim_dev.add_error_callback(on_link_error)

        shim_dev.set_sampling_rate(1024.0, update_exg_rate=True)   # also puts the ExG chip at 2000 SPS
        shim_dev.set_sensors([ESensorGroup.EXG1_24BIT])
        shim_dev.add_stream_callback(handler)
        shim_dev.start_streaming(wait_for_data=10.0)               # returns when the first packet arrived

    # ... shim_dev.connected, shim_dev.link_stats ...
    shim_dev.stop_streaming()
    shim_dev.shutdown()

What you get:

* **Timeouts.** A request that is not answered within :code:`command_timeout` raises :code:`ShimmerTimeoutError`;
  the link stays usable. Late replies are recognized and discarded.
* **Failure notification.** If the read loop crashes, the device hangs up or the stream stalls
  (:code:`stall_timeout`), the error callbacks are called, the outstanding requests raise :code:`ShimmerLinkError`
  and so does every later one. The link cannot be revived: :code:`shutdown()` it and connect again.
* **Frame validation.** With firmware >= 0.13.7 the device appends a CRC to every frame (:code:`crc='auto'`). A
  frame that fails is dropped and the receiver resynchronizes without losing the valid frames that follow. Without
  CRC, a data packet must be followed by a valid header and continue the device timestamps.
  :code:`link_stats` counts what the receiver saw (:code:`crc_errors`, :code:`skipped_bytes`...).
* **Stale streams.** :code:`initialize()` stops a stream that a crashed session left running.
* **Exceptions in your stream callback** are logged and counted; they do not end the stream.

Things worth knowing, all measured on Shimmer3 units with LogAndStream 0.15.4:

* The device needs about 1.5 s (up to 5 s) to start sampling after it acknowledged :code:`start_streaming`.
  Starting several devices on one adapter at the same moment fails; starting them one after the other works, also
  while the others are streaming. Hold a :code:`ConnectLock` until :code:`start_streaming(wait_for_data=...)`
  has returned **and keep the next connection away for 1-2 s more**: :code:`ConnectLock(path, min_interval=2.0)`.
  The first data packet arrives before the device has settled; a connection made then fails ("The read loop
  crashed ... returned no data"), and from then on opening the rfcomm port answers :code:`EBUSY` ("Device or
  resource busy") for about 30 s: the kernel keeps the channel of the failed attempt until the RFCOMM connection
  timer expires, and every new attempt, of any process, finds it. Three units started at the same moment at
  1024 Hz took 41 to 86 s to stream (11-18 :code:`EBUSY` per affected unit) with the lock released at the first
  data, and 15-18 s (none) with :code:`min_interval=2.0`. Holding the lock 1 s after the first data was already
  enough in the same test.
* The Bluetooth module sustains about 22 kB/s without losses. At 2048 Hz an EXG1 stream is 22.5 kB/s without CRC and
  26.6 kB/s with the 2-byte CRC: pass :code:`crc='off'` (or :code:`crc=1`) at the top of the range.
* Do not poll the device while it streams: each command costs about 7 samples at 2 kHz. Use the watchdog.
* :code:`set_sampling_rate()` only sets the microcontroller. The ExG chips keep the data rate they were configured
  with, so a chip at 1000 SPS read at 2048 Hz returns every conversion twice. Use :code:`update_exg_rate=True`.
  :code:`start_streaming()` logs a warning when the chip is slower than the sampling rate or routes the internal
  test signal (:code:`ExGRegister.uses_test_signal`); :code:`ExGRegister.emg_preset()` is the configuration of the EMG
  User Guide.
* With ExG enabled, 512 Hz and 600 Hz lose 65 % and 17 % of the packets on these devices (a firmware behavior,
  not the link), 50 Hz is fine and 768 Hz and above are clean. Other rates below 700 Hz were not measured: test
  yours with ``shimmer_reliability/device_experiment.py sweep``.
* The full set of sensors of the ROS node (accelerometers, gyroscope, magnetometer, battery and EXG) at 1024 Hz
  loses 17 % of the packets: stream the ExG channels alone at high rates.
* The battery is better asked for than streamed. As a channel of the data packet it costs 2 bytes in every packet
  (26.6 kB/s instead of 22.5 kB/s at 2048 Hz); asked for with :code:`get_battery_reading()` it costs about 5
  packets per reading, which is 0.004 % of the stream once a minute (and 0.24 % once a second). The reading also
  carries the charging status byte, which the data packet does not.

Configuring the ExG chips
^^^^^^^^^^^^^^^^^^^^^^^^^

An ExG register is built field by field from a preset, checked, written and read back:

.. code-block:: python

    from pyshimmer import ExGMux, ExGRegister

    wanted = (
        ExGRegister.emg_preset(rate_sps=4000, gain=12)
        .with_mux(1, ExGMux.SHORTED)       # to measure the noise of the chip
        .with_leadoff(detection=True)      # the status byte of every packet then shows a loose electrode
    )
    assert wanted.problems() == []         # the bits the chip fixes, DO NOT USE values, single-shot...

    current = shim_dev.get_exg_register(0)
    shim_dev.write_exg_register(0, wanted.keeping_board_bits_of(current))   # raises if the chip took something else

    board = shim_dev.get_expansion_board()   # ExG (SR47-4.0): board.has_exg
    print(shim_dev.get_battery_reading())    # voltage, capacity and the charging status byte

The writes are volatile: a device that is switched off goes back to the configuration it stores (Consensys writes
that). :code:`keeping_board_bits_of()` leaves the bit of CONFIG2 that the firmware sets for the revision of the
board as the device reports it.

Using the Dock API
^^^^^^^^^^^^^^^^^^

.. code-block:: python

    from serial import Serial

    from pyshimmer import ShimmerDock, DEFAULT_BAUDRATE, fmt_hex

    if __name__ == '__main__':
        with Serial('/dev/ttyPPGdev', DEFAULT_BAUDRATE) as serial:
            shim_dock = ShimmerDock(serial)

            mac = shim_dock.get_mac_address()
            print(f'Device MAC: {fmt_hex(mac)}')

Using the Dock API works very similar to the Bluetooth API. However, it does not require a separate initialization call
because it does not use a background thread to decode incoming messages.

Using the Reader API
^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

    from pyshimmer import ShimmerReader, EChannelType

    if __name__ == '__main__':

        with open('test/reader/resources/ecg.bin', 'rb') as f:
            reader = ShimmerReader(f)

            # Read the file contents into memory
            reader.load_file_data()

            print(f'Available data channels: {reader.channels}')
            print(f'Sampling rate: {reader.sample_rate} Hz')
            print()

            ts = reader[EChannelType.TIMESTAMP]
            ecg_ch1 = reader[EChannelType.EXG_ADS1292R_1_CH1_24BIT]
            assert len(ts) == len(ecg_ch1)

            print(f'Timestamp: {ts.shape}')
            print(f'ECG Channel: {ecg_ch1.shape}')
            print()

            exg_reg = reader.exg_reg1
            print(f'ECG Chip Sampling Rate: {exg_reg.data_rate} Hz')
            print(f'ECG Chip Gain: {exg_reg.ch1_gain}')

If the data was recorded using the :code:`SDLog` firmware and features synchronization information, the API
automatically interpolates the data to the common timestamp information of the master.

**Note**: Please be aware that although you have configured a sampling frequency f for your measurements, it can happen that observations are missing.
Usually the observed time difference is a multiple of the sampling period 1 / f.
However, this is not the case for the time difference between the first two observations.
Please take this caveat into consideration when you design your code.
