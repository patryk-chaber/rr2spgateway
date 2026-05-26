#!/usr/bin/python3
"""SLMP (Seamless Message Protocol) 3E frame builder and parser for Mitsubishi PLCs.

Implements the subset of the SLMP specification needed to read and write device
registers over a TCP connection. Only the 3E frame format (without serial 
number) and 4-byte device addressing are fully supported; other variants raise
NotImplementedError.

Key classes:
    SlmpProtocol -- builds outgoing request frames and decodes incoming responses

Key helpers:
    _parse_device_string -- converts a device address string (e.g. "D100") to a
                            ParsedDevice named tuple
    DEVICES              -- dict mapping device name prefixes to DeviceSpec entries
"""

import re
from struct import pack, unpack
from typing import NamedTuple, Optional


SUBHEADER_WITH_SERIAL_NO_FIXED_1        = 0x0054
SUBHEADER_WITH_SERIAL_NO_FIXED_2        = 0x0000
SUBHEADER_WITHOUT_SERIAL_NO_FIXED       = 0x0050

DEVICE_READ_CMD                         = 0x0401
DEVICE_WRITE_CMD                        = 0x1401
DEVICE_READ_RANDOM_CMD                  = 0x0403
DEVICE_WRITE_RANDOM_CMD                 = 0x1402
DEVICE_ENTRY_MONITOR_DEVICE_CMD         = 0x0801
DEVICE_EXECUTE_MONITOR_CMD              = 0x0802
DEVICE_READ_BLOCK_CMD                   = 0x0406
DEVICE_WRITE_BLOCK_CMD                  = 0x1406
LABEL_ARRAY_LABEL_READ_CMD              = 0x041A
LABEL_ARRAY_LABEL_WRITE_CMD             = 0x141A
LABEL_LABEL_READ_RANDOM_CMD             = 0x041C
LABEL_LABEL_WRITE_RANDOM_CMD            = 0x141B
MEMORY_READ_CMD                         = 0x0613
MEMORY_WRITE_CMD                        = 0x1613
EXTEND_UNIT_READ_CMD                    = 0x0601
EXTEND_UNIT_WRITE_CMD                   = 0x1601
REMOTE_CONTROL_REMOTE_RUN_CMD           = 0x1001
REMOTE_CONTROL_REMOTE_STOP_CMD          = 0x1002
REMOTE_CONTROL_REMOTE_PAUSE_CMD         = 0x1003
REMOTE_CONTROL_REMOTE_LATCH_CLEAR_CMD   = 0x1005
REMOTE_CONTROL_REMOTE_RESET_CMD         = 0x1006
REMOTE_CONTROL_READ_TYPE_NAME_CMD       = 0x0101
REMOTE_PASSWORD_LOCK_CMD                = 0x1631
REMOTE_PASSWORD_UNLOCK_CMD              = 0x1630
FILE_READ_DIRECTORY_FILE_CMD            = 0x1810
FILE_SEARCH_DIRECTORY_FILE_CMD          = 0x1811
FILE_NEW_FILE_CMD                       = 0x1820
FILE_DELETE_FILE_CMD                    = 0x1822
FILE_COPY_FILE_CMD                      = 0x1824
FILE_CHANGE_FILE_STATE_CMD              = 0x1825
FILE_CHANGE_FILE_DATE_CMD               = 0x1826
FILE_OPEN_FILE_CMD                      = 0x1827
FILE_READ_FILE_CMD                      = 0x1828
FILE_WRITE_FILE_CMD                     = 0x1829
FILE_CLOSE_FILE_CMD                     = 0x182A
SELF_TEST_CMD                           = 0x0619
CLEAR_ERROR_CMD                         = 0x1617
ONDEMAND_CMD                            = 0x2101

BIT = 0
WORD = 1
DWORD = 2


class DeviceSpec(NamedTuple):
    """Static specification for a PLC device type, looked up from DEVICES by name prefix.

    Attributes:
        code: Binary device code used in SLMP frames (None if the device has no
              binary code and can only be addressed symbolically).
        data_type: BIT, WORD, or DWORD -- governs how raw response bytes are decoded.
        is_hex: True if the register number in address strings is hexadecimal
                (e.g. "X1F"), False if decimal (e.g. "D100").
    """
    code: Optional[int]
    data_type: int
    is_hex: bool


class ParsedDevice(NamedTuple):
    """A device address string decomposed into its constituent fields.

    Produced by _parse_device_string(). Carries everything needed to build
    an SLMP request frame for a specific register.

    Attributes:
        name: Device name prefix (e.g. "D", "M", "X").
        code: Binary device code (from DeviceSpec), or None.
        register_no: Numeric register address (already converted from hex if needed).
        data_type: BIT, WORD, or DWORD.
        is_hex: Whether the original address used hexadecimal numbering.
    """
    name: str
    code: Optional[int]
    register_no: int
    data_type: int
    is_hex: bool


# DEVICE -> DeviceSpec(code, data_type, is_hex)
DEVICES: dict[str, DeviceSpec] = {
    "FX"  : DeviceSpec(None  , BIT  , True ),
    "FY"  : DeviceSpec(None  , BIT  , True ),
    "FD"  : DeviceSpec(None  , WORD , False),
    "SM"  : DeviceSpec(0x0091, BIT  , False),
    "SD"  : DeviceSpec(0x00A9, WORD , False),
    "X"   : DeviceSpec(0x009C, BIT  , True ),
    "Y"   : DeviceSpec(0x009D, BIT  , True ),
    "M"   : DeviceSpec(0x0090, BIT  , False),
    "L"   : DeviceSpec(0x0092, BIT  , False),
    "F"   : DeviceSpec(0x0093, BIT  , False),
    "V"   : DeviceSpec(0x0094, BIT  , False),
    "B"   : DeviceSpec(0x00A0, BIT  , True ),
    "D"   : DeviceSpec(0x00A8, WORD , False),
    "W"   : DeviceSpec(0x00B4, WORD , True ),
    "TS"  : DeviceSpec(0x00C1, BIT  , False),
    "TC"  : DeviceSpec(0x00C0, BIT  , False),
    "TN"  : DeviceSpec(0x00C2, WORD , False),
    "LTS" : DeviceSpec(0x0051, BIT  , False),
    "LTC" : DeviceSpec(0x0050, BIT  , False),
    "LTN" : DeviceSpec(0x0052, DWORD, False),
    "STS" : DeviceSpec(0x00C7, BIT  , False),
    "STC" : DeviceSpec(0x00C6, BIT  , False),
    "STN" : DeviceSpec(0x00C8, WORD , False),
    "LSTS": DeviceSpec(0x0059, BIT  , False),
    "LSTC": DeviceSpec(0x0058, BIT  , False),
    "LSTN": DeviceSpec(0x005A, DWORD, False),
    "CS"  : DeviceSpec(0x00C4, BIT  , False),
    "CC"  : DeviceSpec(0x00C3, BIT  , False),
    "CN"  : DeviceSpec(0x00C5, WORD , False),
    "LCS" : DeviceSpec(0x0055, BIT  , False),
    "LCC" : DeviceSpec(0x0054, BIT  , False),
    "LCN" : DeviceSpec(0x0056, DWORD, False),
    "SB"  : DeviceSpec(0x00A1, BIT  , True ),
    "SW"  : DeviceSpec(0x00B5, WORD , True ),
    "S"   : DeviceSpec(None  , BIT  , False),
    "DX"  : DeviceSpec(0x00A2, BIT  , True ),
    "DY"  : DeviceSpec(0x00A3, BIT  , True ),
    "Z"   : DeviceSpec(0x00CC, WORD , False),
    "LZ"  : DeviceSpec(0x0062, DWORD, False),
    "R"   : DeviceSpec(0x00AF, WORD , False),
    "ZR"  : DeviceSpec(0x00B0, WORD , True ),
    "?1"  : DeviceSpec(0xA8  , WORD , False), # Extended data register (D)
    "?2"  : DeviceSpec(0xB4  , WORD , True ), # Extended link register (W)
    "RD"  : DeviceSpec(0x002C, WORD , False),
    "?3"  : DeviceSpec(None  , WORD , False), # Link direct device (no symbol)
    "?4"  : DeviceSpec(None  , WORD , False), # Module access device (no symbol)
    "?5"  : DeviceSpec(None  , WORD , False), # CPU buffer memory access device (no symbol)
}

#FOUR_BYTES_SUBCMD  = 0x0003 # BIT 
FOUR_BYTES_SUBCMD  = 0x0002 # WORD
#THREE_BYTES_SUBCMD = 0x0001 # BIT
THREE_BYTES_SUBCMD = 0x0000 # WORD

ACCESS_DESTINATION_CONNECTED_STATION_REQUEST_DESTINATION_NETWORK_NO = 0x00
ACCESS_DESTINATION_ANOTHER_STATION_REQUEST_DESTINATION_NETWORK_NO = 0x01 # up to 0xEF

ACCESS_DESTINATION_CONNECTED_STATION_REQUEST_DESTINATION_STATION_NO = 0xFF
ACCESS_DESTINATION_ANOTHER_STATION_REQUEST_DESTINATION_STATION_NO_ASSIGNED_CS_MS = 0x7D
ACCESS_DESTINATION_ANOTHER_STATION_REQUEST_DESTINATION_STATION_NO_PRESENT_CS_MS = 0x7E
ACCESS_DESTINATION_ANOTHER_STATION_REQUEST_DESTINATION_STATION_NO = 0x01 # up to 0x78 or ASSIGNED/PRESENT CS/MS

ACCESS_DESTINATION_OWN_STATION = 0x03FF
ACCESS_DESTINATION_CONTROL_CPU = 0x03FF
ACCESS_DESTINATION_MULTIPLE_SYSTEM_CPU_NO1 = 0x03E0
ACCESS_DESTINATION_MULTIPLE_SYSTEM_CPU_NO2 = 0x03E1
ACCESS_DESTINATION_MULTIPLE_SYSTEM_CPU_NO3 = 0x03E2
ACCESS_DESTINATION_MULTIPLE_SYSTEM_CPU_NO4 = 0x03E3
ACCESS_DESTINATION_MULTIDROP_CONNECTION_STATION_VIA_A_CPU_MODULE_IN_MULTIDROP_CONNECTION = 0x0000 # up to 0x1FF

ACCESS_DESTINATION_OF_EXTERNAL_DEVICE_REQUEST_DESTINATION_MULTIDROP_STATION_NO_MULTIDROP_CONNECTION_STATION = 0x00 # up to 0x1F
ACCESS_DESTINATION_OF_EXTERNAL_DEVICE_REQUEST_DESTINATION_MULTIDROP_STATION_NO_THE_STATION_THAT_RELAYS_THE_MULTIDROP_CONNECTION_AND_NETWORK = 0x00
ACCESS_DESTINATION_OF_EXTERNAL_DEVICE_REQUEST_DESTINATION_MULTIDROP_STATION_NO_STATION_OTHER_THAN_THE_MULTIDROP_CONNECTION_STATION = 0x00

MONITORING_TIMER_UNLIMITED_WAIT = 0x0000
MONITORING_TIMER_UNIT_MS = 250


class SlmpProtocol:
    """Builds SLMP 3E request frames and decodes responses for Mitsubishi PLCs.

    Only the 3E frame format (without serial number), connected-station routing,
    and 4-byte device addressing are fully implemented. Other combinations raise
    NotImplementedError.
    """

    monitoring_timer_in_ms: int
    number_of_bytes: int
    subheader_with_serial_no: bool
    access_destination_own_station: bool
    access_destination_connected_station: bool
    access_link_direct_device_and_more: bool

    def __init__(self,
                 monitoring_timer_in_ms: int = 1000,
                 number_of_bytes: int = 3,
                 subheader_with_serial_no: bool = False,
                 access_destination_own_station: bool = True,
                 access_destination_connected_station: bool = True,
                 access_link_direct_device_and_more: bool = False):
        """Configure the protocol parameters.

        Args:
            monitoring_timer_in_ms: PLC watchdog timeout in milliseconds.
                Valid range is 250-10000 ms for own-station access.
            number_of_bytes: Device address width -- 3 or 4. Only 4 is fully
                supported; 3 raises NotImplementedError.
            subheader_with_serial_no: Use the 3E subheader variant that includes
                a serial number. Currently raises NotImplementedError if True.
            access_destination_own_station: Route to the station the TCP
                connection is made to (the typical case).
            access_destination_connected_station: Use connected-station network
                and station numbers. Must be True (other routing not implemented).
            access_link_direct_device_and_more: Enable link-direct device access.
                Currently raises NotImplementedError if True.
        """
        self.monitoring_timer_in_ms = monitoring_timer_in_ms
        self.number_of_bytes = number_of_bytes
        self.subheader_with_serial_no = subheader_with_serial_no
        self.access_destination_own_station = access_destination_own_station
        self.access_destination_connected_station = access_destination_connected_station
        self.access_link_direct_device_and_more = access_link_direct_device_and_more

    @staticmethod
    def device_address_add(device_str: str, offset: int) -> str:
        """Return a new device address string shifted by offset registers.

        Preserves the original numbering base (decimal or hexadecimal).

        Args:
            device_str: Base device address (e.g. "D100", "X1A").
            offset: Number of registers to add.

        Returns:
            New address string (e.g. device_address_add("D100", 3) -> "D103").
        """
        parsed = _parse_device_string(device_str)
        if parsed.is_hex:
            return f"{parsed.name}{parsed.register_no + offset:X}"
        else:
            return f"{parsed.name}{parsed.register_no + offset}"

    @staticmethod
    def bytes_to_hex_string(data: bytes) -> str:
        """Return a space-separated hex string representation of data.

        Args:
            data: Raw bytes to format.

        Returns:
            String of the form "0x50 0x00 0x00 ...".
        """
        return " ".join(f"{byte:#04x}" for byte in data)

    @staticmethod
    def decode_response_device_read_n_word_message(
        device_str: str, number: int, resp: bytes
    ) -> list[int]:
        """Decode a raw SLMP read response into a list of register values.

        For BIT devices each word is expanded into 16 individual bit values.
        For WORD devices each word is returned as-is. DWORD is not yet supported.

        Args:
            device_str: Device address that was read (used to determine data type).
            number: Number of words that were requested.
            resp: Raw response bytes received from the PLC.

        Returns:
            List of integer register values. Length is number for WORD devices
            or number * 16 for BIT devices.

        Raises:
            RuntimeError: If the PLC returned a non-zero end code.
            ValueError: If the response length does not match the request.
            NotImplementedError: If the device type is DWORD.
        """
        parsed = _parse_device_string(device_str)

        # For now I am ignoring first 7 fields, as those should be identical
        # with the request even if command failed (apart from subheader)
        response_data = resp[7:]
        response_data_length, = unpack("<H", response_data[0:2])
        end_code, = unpack("<H", response_data[2:4])

        return_values = []

        if end_code != 0:
            raise RuntimeError(f"PLC reported error code: {end_code:#06x}")

        values = response_data[4:]
        if response_data_length != len(response_data) - 2:
            raise ValueError(
                f"Response header says {response_data_length} bytes"
                f" but got {len(response_data) - 2}"
            )

        if response_data_length != number * 2 + 2:
            raise ValueError(
                f"Response length {response_data_length} does not"
                f" match expected {number * 2 + 2}"
            )

        for n in range(number):
            value, = unpack("<H", values[(n * 2):(n * 2 + 2)])
            if parsed.data_type == BIT:
                bit_values = "{:016b}".format(value)
                for i in range(16):
                    return_values.append(int(bit_values[16 - i - 1]))
            elif parsed.data_type == WORD:
                return_values.append(value)
            elif parsed.data_type == DWORD:
                raise NotImplementedError(
                    "DWORD register size not yet supported"
                )
            else:
                raise ValueError(f"Unknown register size: {parsed.data_type}")
        return return_values

    def _build_routing_prefix(self) -> bytes:
        """Build the SLMP frame header: subheader + network, station, and destination bytes.

        Returns:
            7-byte routing prefix ready to prepend to the data-length field.

        Raises:
            NotImplementedError: For subheader-with-serial-no or non-connected-station routing.
        """
        if self.subheader_with_serial_no:
            raise NotImplementedError
        subheader_bytes = pack('<H', SUBHEADER_WITHOUT_SERIAL_NO_FIXED)

        if self.access_destination_connected_station:
            network_no_bytes = pack(
                '<B',
                ACCESS_DESTINATION_CONNECTED_STATION_REQUEST_DESTINATION_NETWORK_NO
            )
            station_no_bytes = pack(
                '<B',
                ACCESS_DESTINATION_CONNECTED_STATION_REQUEST_DESTINATION_STATION_NO
            )
        else:
            raise NotImplementedError

        if self.access_destination_own_station:
            access_dest_bytes = pack('<H', ACCESS_DESTINATION_OWN_STATION)
            multidrop_bytes = pack('<B', ACCESS_DESTINATION_OF_EXTERNAL_DEVICE_REQUEST_DESTINATION_MULTIDROP_STATION_NO_STATION_OTHER_THAN_THE_MULTIDROP_CONNECTION_STATION)
        else:
            raise NotImplementedError

        return (subheader_bytes + network_no_bytes + station_no_bytes
                + access_dest_bytes + multidrop_bytes)

    def _build_device_body(
        self,
        command: int,
        register_no: int,
        device_code: Optional[int],
        payload_bytes: bytes,
    ) -> bytes:
        """Build the data section of an SLMP frame.

        Concatenates: monitoring timer + command + subcommand +
        device head address + device code + command-specific payload.

        Args:
            command: SLMP command code (e.g. DEVICE_READ_CMD).
            register_no: Starting register number.
            device_code: Binary device code from DeviceSpec.
            payload_bytes: Command-specific payload (e.g. word count for reads,
                word count + value for writes).

        Returns:
            Packed bytes for the data section (without routing prefix or length field).

        Raises:
            NotImplementedError: For 3-byte addressing or link-direct device access.
            ValueError: If number_of_bytes is not 3 or 4.
        """
        if (self.access_destination_own_station
                and (self.monitoring_timer_in_ms < 250
                     or self.monitoring_timer_in_ms > 10000)):
            pass
        elif ((not self.access_destination_own_station)
              and (self.monitoring_timer_in_ms < 500
                   or self.monitoring_timer_in_ms > 60000)):
            pass
        monitoring_timer_bytes = pack(
            '<H',
            int(self.monitoring_timer_in_ms / MONITORING_TIMER_UNIT_MS),
        )

        command_bytes = pack('<H', command)

        subcommand_correction = (
            0x0080 if self.access_link_direct_device_and_more else 0x0000
        )
        if self.number_of_bytes == 4:
            subcommand_bytes = pack(
                '<H', FOUR_BYTES_SUBCMD + subcommand_correction
            )
            head_device_no_bytes = pack('<I', register_no)
            device_code_bytes = pack('<H', device_code)
        elif self.number_of_bytes == 3:
            subcommand_bytes = pack(
                '<H', THREE_BYTES_SUBCMD + subcommand_correction
            )
            head_device_no_bytes = pack('<I', register_no)[0:3]
            device_code_bytes = pack('<B', device_code)
            raise NotImplementedError
        else:
            raise ValueError(
                f"number_of_bytes must be 3 or 4,"
                f" got {self.number_of_bytes}"
            )

        if self.access_link_direct_device_and_more:
            raise NotImplementedError

        return (monitoring_timer_bytes + command_bytes + subcommand_bytes +
                head_device_no_bytes + device_code_bytes + payload_bytes)

    def _assemble_frame(self, data_bytes: bytes) -> bytes:
        """Wrap data_bytes in a complete SLMP frame.

        Prepends the routing prefix and a 2-byte little-endian data-length field.

        Args:
            data_bytes: The data section built by _build_device_body().

        Returns:
            Complete SLMP frame ready to send over TCP.
        """
        routing = self._build_routing_prefix()
        data_length_bytes = pack('<H', len(data_bytes))
        return routing + data_length_bytes + data_bytes

    def prepare_device_read_n_word_message(
        self, device_str: str, number: int
    ) -> bytes:
        """Build a complete SLMP read request for number words starting at device_str.

        Args:
            device_str: Starting device address (e.g. "D100").
            number: Number of consecutive words to read.

        Returns:
            Complete SLMP frame bytes ready to send.
        """
        parsed = _parse_device_string(device_str)
        payload = pack('<H', number)
        data = self._build_device_body(
            DEVICE_READ_CMD, parsed.register_no, parsed.code, payload
        )
        return self._assemble_frame(data)

    def prepare_device_write_one_word_message(
        self, device_str: str, value: int
    ) -> bytes:
        """Build a complete SLMP write request to set one word register to value.

        Args:
            device_str: Target device address (e.g. "D110").
            value: 16-bit unsigned value to write.

        Returns:
            Complete SLMP frame bytes ready to send.
        """
        parsed = _parse_device_string(device_str)
        payload = pack('<H', 1) + pack('<H', value)
        data = self._build_device_body(
            DEVICE_WRITE_CMD, parsed.register_no, parsed.code, payload
        )
        return self._assemble_frame(data)


def _parse_device_string(device_str: str) -> ParsedDevice:
    """Parse a device address string into a ParsedDevice named tuple.

    Splits the string into a name prefix and a numeric part, then looks up
    the prefix in DEVICES to resolve its code, data type, and addressing base.

    Args:
        device_str: Device address string (e.g. "D100", "X1F", "M0").

    Returns:
        ParsedDevice with name, code, register_no, data_type, and is_hex.

    Raises:
        ValueError: If device_str does not match the expected pattern.
        KeyError: If the device name prefix is not found in DEVICES.
    """
    parts = re.findall(
        r'(s?[a-z]+?)([a-f\d]+)', device_str, flags=re.IGNORECASE
    )
    if len(parts) == 0:
        raise ValueError(f"Invalid device string: {device_str!r}")
    parts = parts[0]
    spec = DEVICES[parts[0]]
    return ParsedDevice(
        name=parts[0],
        code=spec.code,
        register_no=int(parts[1], 16) if spec.is_hex else int(parts[1]),
        data_type=spec.data_type,
        is_hex=spec.is_hex,
    )
