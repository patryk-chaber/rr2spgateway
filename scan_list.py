#!/usr/bin/python3
"""Register scanner for SLMP-based PLC communication.

Provides three main classes:
    RegisterType      -- supported PLC register data types
    RegisterScanEntry -- a single register range to poll with timing and type conversion
    RegisterScanner   -- manages a dynamic list of entries loaded from a CSV scan list

The module-level `configure(cfg)` function applies runtime config (buffer sizes,
timeouts, stats window) from a Config dataclass before scanning begins.
"""

import csv
import datetime
import json
import logging
import random
import statistics
import traceback
from enum import Enum
from struct import pack, unpack

import slmp as SLMP

_protocol = SLMP.SlmpProtocol(number_of_bytes=4)

logger = logging.getLogger(__name__)


def configure(cfg):
    """Apply config values to module-level constants."""
    global _protocol
    _protocol = SLMP.SlmpProtocol(number_of_bytes=cfg.input.slmp.number_of_bytes)
    RegisterStats.MAX_LIST_LENGTH = cfg.gateway.register_stats_window
    RegisterScanEntry.BUFFER_SIZE = cfg.gateway.buffer_size
    RegisterScanEntry.MAX_TRIES = cfg.gateway.max_tries
    RegisterScanEntry.SOCKET_TIMEOUT_S = cfg.gateway.socket_timeout_s


class RegisterType(Enum):
    """Supported PLC register data types for SLMP reads."""
    BIT = 0
    WORD = 1
    UWORD = 2
    DWORD = 3
    FLOAT = 4
    BIT_R = 9  # R-type relay registers -- each word is expanded into 16 individual bits


class RegisterStats:
    """Per-register communication metrics stored as rolling timestamped lists.

    Each list holds up to MAX_LIST_LENGTH entries so that recent stats can be
    aggregated over a configurable time window without unbounded memory growth.
    """

    MAX_LIST_LENGTH = 10

    def __init__(self):
        """Initialise empty metric lists for all tracked quantities."""
        self.bytes_sent = []
        self.bytes_received = []
        self.attempt_number = []
        self.response_time = []
        self.value_changed = []

    def new_bytes_sent(self, byte_count):
        """Record a transmission event and return its timestamp.

        Args:
            byte_count: Number of bytes sent in this request.

        Returns:
            datetime of the transmission (used later to compute response time).
        """
        timestamp = datetime.datetime.now()
        self.bytes_sent.append((timestamp, byte_count))
        if len(self.bytes_sent) > RegisterStats.MAX_LIST_LENGTH:
            self.bytes_sent.pop(0)
        return timestamp

    def new_bytes_received(self, byte_count, retries, sent_timestamp):
        """Record a received response with its retry count and round-trip time.

        Args:
            byte_count: Number of bytes in the response.
            retries: Attempt number on which the response arrived (1 = first try).
            sent_timestamp: Timestamp returned by new_bytes_sent for this request.
        """
        timestamp = datetime.datetime.now()
        self.bytes_received.append((timestamp, byte_count))
        self.attempt_number.append((timestamp, retries))
        self.response_time.append((timestamp, (timestamp-sent_timestamp).total_seconds()))
        if len(self.bytes_received) > RegisterStats.MAX_LIST_LENGTH:
            self.bytes_received.pop(0)
        if len(self.attempt_number) > RegisterStats.MAX_LIST_LENGTH:
            self.attempt_number.pop(0)
        if len(self.response_time) > RegisterStats.MAX_LIST_LENGTH:
            self.response_time.pop(0)

    def new_values(self, changed):
        """Record whether the register value changed on this scan.

        Args:
            changed: True if the value differed from the previous scan.
        """
        timestamp = datetime.datetime.now()
        self.value_changed.append((timestamp, int(changed)))
        if len(self.value_changed) > RegisterStats.MAX_LIST_LENGTH:
            self.value_changed.pop(0)

    def get_raw_stats_for_period(self, timestart, timeend):
        """Return metric lists filtered to the given time window.

        Args:
            timestart: Start of the window (inclusive).
            timeend: End of the window (inclusive).

        Returns:
            Dict with keys bytes_sent, bytes_received, attempt_number,
            response_time, value_changed -- each a list of raw values.
        """
        return {
            "bytes_sent": [x[1] for x in self.bytes_sent if (x[0] >= timestart and x[0] <= timeend)],
            "bytes_received": [x[1] for x in self.bytes_received if (x[0] >= timestart and x[0] <= timeend)],
            "attempt_number": [x[1] for x in self.attempt_number if (x[0] >= timestart and x[0] <= timeend)],
            "response_time": [x[1] for x in self.response_time if (x[0] >= timestart and x[0] <= timeend)],
            "value_changed": [x[1] for x in self.value_changed if (x[0] >= timestart and x[0] <= timeend)],
        }


class RegisterScanEntry:
    """A single register range to poll over SLMP with configurable type and period.

    Holds the pre-built SLMP request bytes, tracks when the next scan is due,
    converts raw word values to the declared type, and records per-entry stats.

    Class-level attributes (overridden by configure()):
        BUFFER_SIZE      -- max bytes to read from the socket per response
        MAX_TRIES        -- retry attempts before giving up on a read
        SOCKET_TIMEOUT_S -- per-attempt socket receive timeout in seconds
        INF_TIMEDELTA    -- sentinel period meaning "never re-scan"
    """

    BUFFER_SIZE = 1000000
    MAX_TRIES = 3
    SOCKET_TIMEOUT_S = 1.0
    INF_TIMEDELTA = datetime.timedelta(days=1000*365)  # approx 1000 years

    def __init__(self, start, length=None, rtype=None, period=None, random_period_shift=100):
        """Initialise a scan entry and pre-build its SLMP request packet.

        Args:
            start: Register start address string (e.g. "D100").
            length: Number of registers to read.
            rtype: RegisterType or equivalent string; sets the
                type-conversion path used by scan().
            period: Scan period in milliseconds; 0 means never scan.
            random_period_shift: Upper bound (ms) for a random jitter applied to
                the first scan time to spread out initial load.
        """
        self.start = start
        self.stats = RegisterStats()
        self.last_late = datetime.timedelta()

        self.last_values: list[int] = []
        self.last_timestamp = datetime.datetime.now()
        self.request: bytes = b''
        self.scan = lambda _s, _nr=False: {}
        if length is not None:
            self.length = length
            self.last_values = [0]*length*2
            self.request = _protocol.prepare_device_read_n_word_message(start, length)
        else:
            self.length = None

        if rtype is not None:
            if rtype == RegisterType.BIT and start[0] == "R":
                self.type = RegisterType.BIT_R
            else:
                self.type = rtype
            self.scan = lambda s, nr=False: self.scan_as(self.type, s, nr)
        else:
            self.type = None

        if period is not None:
            if period == 0:
                self.period = RegisterScanEntry.INF_TIMEDELTA
            elif not isinstance(period, int):
                logger.warning(f"Period {period!r} is not an integer; defaulting to infinity.")
                self.period = RegisterScanEntry.INF_TIMEDELTA
            else:
                self.period = datetime.timedelta(microseconds=period*1000)
        else:
            self.period = None

        self.next_scan = datetime.datetime.now()+datetime.timedelta(microseconds=random.randint(0, random_period_shift)*1000)

    def check_if_value_changed(self, new_values):
        """Return True if new_values differs from the last cached values."""
        if len(self.last_values) != len(new_values):
            return True
        for i in range(0, len(self.last_values)):
            if self.last_values[i] != new_values[i]:
                return True
        return False

    def reconfigure(self, length=None, rtype=None, period=None):
        """Update length, type, and/or period in place and trigger an immediate rescan.

        Args:
            length: New register count, or None to leave unchanged.
            rtype: New RegisterType, or None to leave unchanged.
            period: New scan period in milliseconds, or None to leave unchanged.
        """
        if length is not None:
            self.length = length
            self.last_values = [0]*length*2
            self.request = _protocol.prepare_device_read_n_word_message(self.start, length)

        if rtype is not None:
            if rtype == RegisterType.BIT and self.start[0] == "R":
                self.type = RegisterType.BIT_R
            else:
                self.type = rtype
            self.scan = lambda s, nr=False: self.scan_as(self.type, s, nr)

        if period is not None:
            if period == 0:
                self.period = RegisterScanEntry.INF_TIMEDELTA
            elif not isinstance(period, int):
                logger.warning(f"Period {period!r} is not an integer; keeping current value.")
            else:
                self.period = datetime.timedelta(microseconds=period*1000)

        self.next_scan = datetime.datetime.now()

    def __eq__(self, other):
        """Equality check on start, length, type, period. None fields act as wildcards."""
        if self.start != other.start:
            return False
        if self.length is not None and other.length is not None and self.length != other.length:
            return False
        if self.type is not None and other.type is not None and self.type != other.type:
            return False
        if self.period is not None and other.period is not None and self.period != other.period:
            return False
        return True

    def get_start(self):
        """Return the register start address string."""
        return self.start

    def get_length(self):
        """Return the register count, or None if not set."""
        if self.length is None:
            return None
        return self.length

    def get_type(self):
        """Return the register type as a string (e.g. 'WORD'), or None if not set."""
        if self.type is None:
            return None
        if isinstance(self.type, str):
            return self.type
        if isinstance(self.type, int):
            return RegisterType(self.type).name
        return self.type.name

    def get_period(self):
        """Return the scan period in milliseconds, 0 for never-scan, or None if not set."""
        if self.period is None:
            return None
        if self.period >= RegisterScanEntry.INF_TIMEDELTA:
            return 0
        return self.period.total_seconds()*1000

    def get_stats(self, timestart=None, timeend=None):
        """Return entry metadata combined with communication stats for the given window.

        Args:
            timestart: Start of the aggregation window (default: 60 s ago).
            timeend: End of the aggregation window (default: now).

        Returns:
            Dict merging as_dict() with per-metric tuples of
            (sum, mean, Q1, median, Q3, min, max). Returns {} on error.
        """
        if timestart is None:
            timestart = datetime.datetime.now() - datetime.timedelta(seconds=60)
        if timeend is None:
            timeend = datetime.datetime.now()
        stats_for_period = self.stats.get_raw_stats_for_period(timestart, timeend)
        stats_for_period_processed = {}
        try:
            for k in stats_for_period:
                if len(stats_for_period[k]) < 2:
                    continue
                qntls = statistics.quantiles(stats_for_period[k])
                stats_for_period_processed[k] = (
                    sum(stats_for_period[k]),
                    statistics.mean(stats_for_period[k]),
                    qntls[0],
                    qntls[1],
                    qntls[2],
                    min(stats_for_period[k]),
                    max(stats_for_period[k])
                )
        except Exception:
            logger.warning(f"Failed to compute stats, returning empty.\n{traceback.format_exc()}")
            return {}
        return {**self.as_dict(), **stats_for_period_processed}

    def invalidate(self):
        """Schedule an immediate re-scan by setting next_scan to now."""
        self.next_scan = datetime.datetime.now()

    def check_if_invalid(self):
        """Return True if a scan is overdue (next_scan <= now), and record how late."""
        self.last_late = datetime.datetime.now() - self.next_scan
        return self.last_late >= datetime.timedelta()

    def update_next_scan_raw(self):
        """Advance next_scan by exactly one period."""
        if self.period is None:
            return
        self.next_scan = self.next_scan + self.period

    def update_next_scan(self):
        """Advance next_scan by one period if currently overdue."""
        if self.check_if_invalid():
            self.update_next_scan_raw()
        logger.debug(f"{self.start}[{self.length}] next scan at {self.next_scan}")

    def scan_as_bit_r(self, s, no_read):
        """Scan as R-type bit registers, expanding each word into 16 individual bit entries.

        Args:
            s: Connected socket.
            no_read: If True, return last cached values without issuing a new request.

        Returns:
            Dict keyed as "<address>.<bit_index>" → {value, timestamp}.
        """
        dict_to_publish = {}
        if self.check_if_invalid():
            if no_read:
                values = self.last_values
                timestamp = self.last_timestamp
            else:
                values = self.get_raw_value(s)
                if values is None:
                    return dict_to_publish
                timestamp = datetime.datetime.now()
                self.last_timestamp = timestamp
                self.last_values = values
                self.update_next_scan()
            if self.length is None:
                return dict_to_publish
            for n in range(self.length):
                binvalues = [int(x) for x in "{0:016b}".format(values[n])[::-1]]
                rstring = SLMP.SlmpProtocol.device_address_add(self.start, n)
                for i in range(16):
                    dict_to_publish[f"{rstring}.{i}"] = {
                        "value": binvalues[i],
                        "timestamp": f"{timestamp.isoformat()}",
                    }
        return dict_to_publish

    def scan_as_bit(self, s, no_read):
        """Scan as bit registers, one dict entry per register.

        Args:
            s: Connected socket.
            no_read: If True, return last cached values without issuing a new request.

        Returns:
            Dict keyed as "<address>" → {value, timestamp}.
        """
        dict_to_publish = {}
        if self.check_if_invalid():
            if no_read:
                values = self.last_values
                timestamp = self.last_timestamp
            else:
                values = self.get_raw_value(s)
                if values is None:
                    return dict_to_publish
                timestamp = datetime.datetime.now()
                self.last_timestamp = timestamp
                self.last_values = values
                self.update_next_scan()
            for n in range(len(values)):
                dict_to_publish_sep = {}
                rstring = SLMP.SlmpProtocol.device_address_add(self.start, n)
                dict_to_publish_sep["value"] = values[n]
                dict_to_publish_sep["timestamp"] = f"{timestamp.isoformat()}"
                dict_to_publish[f"{rstring}"] = dict_to_publish_sep
        return dict_to_publish

    def scan_as_word(self, s, no_read):
        """Scan as signed 16-bit word registers.

        Args:
            s: Connected socket.
            no_read: If True, return last cached values without issuing a new request.

        Returns:
            Dict keyed as "<address>" → {value, timestamp}.
        """
        dict_to_publish = {}
        if self.check_if_invalid():
            if no_read:
                values = self.last_values
                timestamp = self.last_timestamp
            else:
                values = self.get_raw_value(s)
                if values is None:
                    return dict_to_publish
                timestamp = datetime.datetime.now()
                self.last_timestamp = timestamp
                self.last_values = values
                self.update_next_scan()
            for n in range(len(values)):
                rstring = SLMP.SlmpProtocol.device_address_add(self.start, n)
                dict_to_publish_sep = {}
                dict_to_publish_sep["value"], = unpack("h", pack("<H", values[n]))
                dict_to_publish_sep["timestamp"] = f"{timestamp.isoformat()}"
                dict_to_publish[f"{rstring}"] = dict_to_publish_sep
        return dict_to_publish

    def scan_as_uword(self, s, no_read):
        """Scan as unsigned 16-bit word registers.

        Args:
            s: Connected socket.
            no_read: If True, return last cached values without issuing a new request.

        Returns:
            Dict keyed as "<address>" → {value, timestamp}.
        """
        dict_to_publish = {}
        if self.check_if_invalid():
            if no_read:
                values = self.last_values
                timestamp = self.last_timestamp
            else:
                values = self.get_raw_value(s)
                if values is None:
                    return dict_to_publish
                timestamp = datetime.datetime.now()
                self.last_timestamp = timestamp
                self.last_values = values
                self.update_next_scan()
            for n in range(len(values)):
                dict_to_publish_sep = {}
                rstring = SLMP.SlmpProtocol.device_address_add(self.start, n)
                dict_to_publish_sep["value"] = values[n]
                dict_to_publish_sep["timestamp"] = f"{timestamp.isoformat()}"
                dict_to_publish[f"{rstring}"] = dict_to_publish_sep
        return dict_to_publish

    def scan_as_dword(self, s, no_read):
        """Scan as signed 32-bit double-word registers (pairs of consecutive words).

        Args:
            s: Connected socket.
            no_read: If True, return last cached values without issuing a new request.

        Returns:
            Dict keyed as "<address>" → {value, timestamp}, one entry per pair.
        """
        dict_to_publish = {}
        if self.check_if_invalid():
            if no_read:
                values = self.last_values
                timestamp = self.last_timestamp
            else:
                values = self.get_raw_value(s)
                if values is None:
                    return dict_to_publish
                timestamp = datetime.datetime.now()
                self.last_timestamp = timestamp
                self.last_values = values
                self.update_next_scan()
            for n in range(len(values)//2):
                dict_to_publish_sep = {}
                rstring = SLMP.SlmpProtocol.device_address_add(self.start, n*2)
                dict_to_publish_sep["value"], = unpack("i", pack("<HH", values[n*2], values[n*2+1]))
                dict_to_publish_sep["timestamp"] = f"{timestamp.isoformat()}"
                dict_to_publish[f"{rstring}"] = dict_to_publish_sep
        return dict_to_publish

    def scan_as_float(self, s, no_read):
        """Scan as 32-bit IEEE 754 float registers (pairs of consecutive words).

        Args:
            s: Connected socket.
            no_read: If True, return last cached values without issuing a new request.

        Returns:
            Dict keyed as "<address>" → {value, timestamp}, one entry per pair.
        """
        dict_to_publish = {}
        if self.check_if_invalid():
            if no_read:
                values = self.last_values
                timestamp = self.last_timestamp
            else:
                values = self.get_raw_value(s)
                if values is None:
                    return dict_to_publish
                timestamp = datetime.datetime.now()
                self.last_timestamp = timestamp
                self.last_values = values
                self.update_next_scan()
            for n in range(len(values)//2):
                dict_to_publish_sep = {}
                rstring = SLMP.SlmpProtocol.device_address_add(self.start, n*2)
                dict_to_publish_sep["value"], = unpack("f", pack("<HH", values[n*2], values[n*2+1]))
                dict_to_publish_sep["timestamp"] = f"{timestamp.isoformat()}"
                dict_to_publish[f"{rstring}"] = dict_to_publish_sep
        return dict_to_publish

    def scan_as(self, reg_type, s, no_read=False):
        """Dispatch to the appropriate scan_as_* method based on reg_type.

        Args:
            reg_type: RegisterType member or equivalent string.
            s: Connected socket.
            no_read: Passed through to the selected scan method.

        Returns:
            Dict of register values, or {} for an unknown type.
        """
        if reg_type == RegisterType.BIT or reg_type == "BIT":
            return self.scan_as_bit(s, no_read)
        elif reg_type == RegisterType.BIT_R or reg_type == "BIT_R":
            return self.scan_as_bit_r(s, no_read)
        elif reg_type == RegisterType.WORD or reg_type == "WORD":
            return self.scan_as_word(s, no_read)
        elif reg_type == RegisterType.UWORD or reg_type == "UWORD":
            return self.scan_as_uword(s, no_read)
        elif reg_type == RegisterType.DWORD or reg_type == "DWORD":
            return self.scan_as_dword(s, no_read)
        elif reg_type == RegisterType.FLOAT or reg_type == "FLOAT":
            return self.scan_as_float(s, no_read)
        else:
            logger.error(f"Unknown register type {reg_type!r} for {self.start}; scan skipped.")
            return {}

    def get_raw_value(self, s):
        """Send an SLMP read request and return decoded word values, retrying on failure.

        Retries up to MAX_TRIES times on OSError (e.g. timeout). Returns None if
        all attempts fail or if length is not set.

        Args:
            s: Connected socket.

        Returns:
            List of raw unsigned 16-bit word values, or None on failure.
        """
        if self.length is None:
            return None
        for i in range(1, RegisterScanEntry.MAX_TRIES+1):
            logger.debug(f"TX [{self.start},{self.length}]: {' '.join(f'{b:02x}' for b in self.request)}")
            bytes_sent = s.send(self.request)
            sent_timestamp = self.stats.new_bytes_sent(bytes_sent)

            try:
                s.settimeout(RegisterScanEntry.SOCKET_TIMEOUT_S)
                response = s.recv(RegisterScanEntry.BUFFER_SIZE)
                if len(response) == 1420:
                    response = response + s.recv(RegisterScanEntry.BUFFER_SIZE)
                self.stats.new_bytes_received(len(response), i, sent_timestamp)
                logger.debug(f"RX [{self.start},{self.length}]: {' '.join(f'{b:02x}' for b in response)}")
                values = SLMP.SlmpProtocol.decode_response_device_read_n_word_message(self.start, self.length, response)
                self.stats.new_values(self.check_if_value_changed(values))
                return values
            except OSError:
                logger.warning(f"No response from [{self.start},{self.length}], attempt {i}/{RegisterScanEntry.MAX_TRIES}.")
                continue
            except Exception:
                logger.warning(f"Read failed [{self.start},{self.length}], attempt {i}/{RegisterScanEntry.MAX_TRIES}:\n{traceback.format_exc()}")
                continue
        else:
            logger.error(f"Gave up reading [{self.start},{self.length}] after {RegisterScanEntry.MAX_TRIES} attempts.")

    def as_dict(self):
        """Return a dict representation with start, length, type, and period."""
        return {
            "start": self.get_start(),
            "length": self.get_length(),
            "type": self.get_type(),
            "period": self.get_period()
        }

    def __str__(self):
        """Return a human-readable string representation."""
        return f"RegisterScanEntry(start={self.start},length={self.length},type={self.type},period={self.period})"


class RegisterScanner:
    """Manages a dynamic list of RegisterScanEntry objects.

    Loads the initial scan list from a CSV file. Supports runtime modification
    via append / remove / modify / poll, typically driven by MQTT commands.
    Tracks change and stats-pending flags so the gateway can publish updates.
    """

    def __init__(self, filename, scan_jitter_ms=100):
        """Load scan entries from a CSV file and initialise state flags.

        Args:
            filename: Path to the CSV scan list.
            scan_jitter_ms: Random jitter range (ms) applied to each entry's
                first scan time to spread initial load.
        """
        self.registers_to_scan = []
        self.changed = False
        self.stats_to_publish = False
        self.stats_time_period = datetime.timedelta(seconds=60)
        self.scan_jitter_ms = scan_jitter_ms
        self.read_from_file(filename)

    def mark_as_changed(self):
        """Set the changed flag so the current scan list is published on the next cycle."""
        self.changed = True

    def is_changed(self):
        """Return True if the scan list has changed since it was last published."""
        return self.changed

    def mark_stats_to_publish(self):
        """Flag that per-register statistics should be published on the next cycle."""
        self.stats_to_publish = True

    def is_stats_to_publish(self):
        """Return True if statistics are pending publication."""
        return self.stats_to_publish

    def set_stats_time_period(self, time_period, fallback=100):
        """Set the time window over which register stats are aggregated.

        Args:
            time_period: Window length as an int (milliseconds) or timedelta.
            fallback: Fallback duration in seconds if time_period has an
                unsupported type.
        """
        if isinstance(time_period, int):
            self.stats_time_period = datetime.timedelta(microseconds=time_period*1000)
        elif isinstance(time_period, datetime.timedelta):
            self.stats_time_period = time_period
        else:
            logger.error(f"stats_time_period has unsupported type {type(time_period).__name__}; falling back to {fallback}s.")
            self.stats_time_period = datetime.timedelta(seconds=fallback)

    def read_from_file(self, filename):
        """Parse a CSV scan list and populate registers_to_scan.

        The CSV must have a header row. Column names are matched by their first
        character (case-insensitive): s=start, l=length, t=type, p=period.

        Args:
            filename: Path to the CSV file.
        """
        with open(filename) as csv_file:
            csv_reader = csv.reader(csv_file, delimiter=',', skipinitialspace=True)
            header = True

            header_start_index = -1
            header_length_index = -1
            header_type_index = -1
            header_period_index = -1

            for row in csv_reader:
                if header:
                    for i, column_name in enumerate(row):
                        column_name = column_name.strip()
                        if column_name[0].lower() == "s":
                            header_start_index = i
                        elif column_name[0].lower() == "l":
                            header_length_index = i
                        elif column_name[0].lower() == "t":
                            header_type_index = i
                        elif column_name[0].lower() == "p":
                            header_period_index = i
                        else:
                            logger.warning(f"Unrecognized column '{column_name}' at index {i} in '{filename}'; ignoring.")
                    header = False
                    missing = [name for name, idx in [("START", header_start_index), ("LENGTH", header_length_index), ("TYPE", header_type_index), ("PERIOD", header_period_index)] if idx == -1]
                    if missing:
                        logger.error(f"Missing required columns in '{filename}': {', '.join(missing)}.")
                        return
                else:
                    if len(row) >= max(max(header_type_index, header_start_index), max(header_length_index, header_period_index)):
                        data_type = row[header_type_index].strip()
                        data_start = row[header_start_index].strip()
                        data_length = int(row[header_length_index].strip())
                        data_period = int(row[header_period_index].strip())
                        self.append(data_start, data_length, data_type, data_period)
                    else:
                        logger.debug(f"Skipping malformed row in '{filename}': {row}")
        self.changed = True

    def append(self, data_start, data_length, data_type, data_period):
        """Add a new register entry if an identical one does not already exist.

        Args:
            data_start: Register start address string.
            data_length: Number of registers to read.
            data_type: Register type string (e.g. "WORD").
            data_period: Scan period in milliseconds.
        """
        if data_start is None:
            logger.error("Register start address is required.")
            return
        if data_length is None:
            logger.error("Register length is required.")
            return
        if data_type is None:
            logger.error("Register type is required.")
            return
        if data_period is None:
            logger.error("Register period is required.")
            return

        if self.find(data_start, data_length, data_type, data_period) >= 0:
            logger.info(f"Register {data_start}[{data_length},{data_type}] already exists; ignoring.")
            return

        self.registers_to_scan.append(RegisterScanEntry(data_start, data_length, data_type, data_period, self.scan_jitter_ms))
        self.changed = True
        logger.info(f"Register added: {data_start}[{data_length},{data_type}, {data_period}ms]")

    def find(self, data_start, data_length=None, data_type=None, data_period=None):
        """Return the index of the first matching entry, or -1 if not found.

        None arguments act as wildcards during equality comparison.

        Args:
            data_start: Register start address (required).
            data_length: Register count, or None to match any.
            data_type: Register type, or None to match any.
            data_period: Scan period, or None to match any.
        """
        if data_start is None:
            logger.error("Register start address is required.")
            return -1
        tmp_scan_entry = RegisterScanEntry(data_start, data_length, data_type, data_period)
        comp_list = [x == tmp_scan_entry for x in self.registers_to_scan]
        if True in comp_list:
            return comp_list.index(True)
        else:
            return -1

    def remove(self, data_start, data_length=None, data_type=None, data_period=None):
        """Remove a register entry by address (and optionally length/type/period).

        Args:
            data_start: Register start address (required).
            data_length: Register count filter, or None to match any.
            data_type: Type filter, or None to match any.
            data_period: Period filter, or None to match any.
        """
        if data_start is None:
            logger.error("Register start address is required.")
            return
        index_to_remove = self.find(data_start, data_length, data_type, data_period)
        if index_to_remove < 0:
            logger.warning(f"Register {data_start} not found; cannot remove.")
            return
        removed_entry = self.registers_to_scan.pop(index_to_remove)
        self.changed = True
        logger.info(f"Register removed: {removed_entry}")

    def modify(self, data_start, data_length=None, data_type=None, data_period=None):
        """Update an existing entry's length, type, and/or period.

        Looks up by start address only (length/type/period are the new values).

        Args:
            data_start: Register start address of the entry to modify (required).
            data_length: New register count, or None to leave unchanged.
            data_type: New type, or None to leave unchanged.
            data_period: New period in milliseconds, or None to leave unchanged.
        """
        if data_start is None:
            logger.error("Register start address is required.")
            return
        if data_length is None and data_type is None and data_period is None:
            logger.warning(f"No fields to modify for {data_start}.")
            return
        index_to_modify = self.find(data_start, None, None, None)
        if index_to_modify < 0:
            logger.warning(f"Register {data_start} not found; cannot modify.")
            return
        self.registers_to_scan[index_to_modify].reconfigure(data_length, data_type, data_period)
        self.changed = True
        logger.info(f"Register modified: {self.registers_to_scan[index_to_modify]}")

    def poll(self, data_start, data_length=None, data_type=None, data_period=None):
        """Mark a register for immediate re-scan on the next cycle.

        Args:
            data_start: Register start address (required).
            data_length: Register count filter, or None to match any.
            data_type: Type filter, or None to match any.
            data_period: Period filter, or None to match any.
        """
        if data_start is None:
            logger.error("Register start address is required.")
            return
        index_to_poll = self.find(data_start, data_length, data_type, data_period)
        if index_to_poll < 0:
            logger.warning(f"Register {data_start} not found; cannot poll.")
            return
        self.registers_to_scan[index_to_poll].invalidate()
        logger.debug(f"Register {self.registers_to_scan[index_to_poll]} marked for immediate scan.")

    def list(self):
        """Return a JSON string of all current scan entries and clear the changed flag."""
        registers_list = []
        for r in self.registers_to_scan:
            registers_list.append(r.as_dict())
        self.changed = False
        return json.dumps(registers_list)

    def stats_as_dict(self):
        """Return a JSON string of per-register stats for the configured time window."""
        stats_list = []
        ref_time = datetime.datetime.now()
        for r in self.registers_to_scan:
            stats_list.append(r.get_stats(ref_time - self.stats_time_period, ref_time))
        self.stats_to_publish = False
        return json.dumps(stats_list)

    def scan_all(self, s):
        """Scan all due registers and return a merged dict of results.

        Args:
            s: Connected socket to the PLC.

        Returns:
            Dict keyed by register address string → {value, timestamp}.
        """
        dict_to_publish_all = {}
        for r in self.registers_to_scan:
            tmp_dict = r.scan(s)
            dict_to_publish_all = {**dict_to_publish_all, **tmp_dict}

        return dict_to_publish_all
