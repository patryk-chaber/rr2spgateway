#!/usr/bin/python3
"""SLMP-to-MQTT gateway with runtime-configurable register scanning.

Polls a Mitsubishi PLC over SLMP/TCP using a RegisterScanner, publishes each
register value to individual MQTT topics, and accepts JSON commands over MQTT
to add, remove, or modify the scan list at runtime.

MQTT topic layout (all under <prefix>):
    <prefix>/separate/<register>  -- individual register values
    <prefix>/status               -- per-cycle timing statistics
    <prefix>/status/registers     -- current scan list (published on change)
    <prefix>/status/stats         -- register-level statistics
    <prefix>/command              -- inbound command topic

Usage:
    python rr2sp_gateway.py [config.yaml] [--log-level LEVEL]
"""

import argparse
import datetime
import json
import logging
import socket
import statistics
import sys
import traceback
from enum import IntEnum, auto

import paho.mqtt.client as mqtt # pyright: ignore[reportMissingImports]

import config_loader as config
import scan_list as registerscanner
from scan_list import RegisterScanner

logger = logging.getLogger(__name__)


def make_array(rows, cols, value):
    """Return a 2-D list of dimensions rows x cols filled with value."""
    return [[value for _ in range(cols)] for _ in range(rows)]


MQTT_TOPIC_SEPARATE = "/separate"
MQTT_TOPIC_STATUS = "/status"
MQTT_TOPIC_STATUS_REGISTERS = "/status/registers"
MQTT_TOPIC_STATUS_STATS = "/status/stats"
MQTT_TOPIC_COMMAND = "/command"

COMMAND_COMMAND_LABEL = "command"
COMMAND_START_LABEL = "start"
COMMAND_LENGTH_LABEL = "length"
COMMAND_TYPE_LABEL = "type"
COMMAND_PERIOD_LABEL = "period"

COMMAND_COMMAND_ADD = "append"
COMMAND_COMMAND_REM = "remove"
COMMAND_COMMAND_LIST = "list"
COMMAND_COMMAND_MOD = "modify"
COMMAND_COMMAND_POLL = "poll"
COMMAND_COMMAND_STATS = "stats"

COMMAND_DICT = {
    COMMAND_COMMAND_ADD: lambda rs, s, l, t, p: rs.append(s, l, t, p),
    COMMAND_COMMAND_REM: lambda rs, s, l, t, p: rs.remove(s, l, t, p),
    COMMAND_COMMAND_MOD: lambda rs, s, l, t, p: rs.modify(s, l, t, p),
    COMMAND_COMMAND_POLL: lambda rs, s, l, t, p: rs.poll(s, l, t, p),
    COMMAND_COMMAND_LIST: lambda rs, *_: rs.mark_as_changed(),
    COMMAND_COMMAND_STATS: lambda rs, *_: rs.mark_stats_to_publish()
}


def on_connect(client, _userdata, _flags, reason_code, _properties, root):
    """MQTT CONNACK callback. Subscribe to the command topic under root prefix."""
    logger.info(f"Connected with result code {reason_code}")
    client.subscribe(root+MQTT_TOPIC_COMMAND, qos=2)
    logger.info(f"Subscribed to {root+MQTT_TOPIC_COMMAND}")


def on_message(_client, _userdata, msg, rs, root):
    """MQTT PUBLISH callback. Parse a JSON command and dispatch to RegisterScanner.

    Expected payload keys:
        command -- one of append / remove / modify / poll / list / stats
        start -- register address (optional depending on command)
        length -- number of registers (optional)
        type -- register type (optional)
        period -- scan period in ms (optional)
    """
    logger.debug(f"MESSAGE {msg}")
    if mqtt.topic_matches_sub(root+MQTT_TOPIC_COMMAND, msg.topic):
        try:
            logger.debug(f"Got message:\n  topic: {msg.topic}\n  payload: {msg.payload}")
            dic = json.loads(msg.payload)
            logger.debug(dic)
            if COMMAND_COMMAND_LABEL not in dic:
                logger.warning("No command defined in the command message")
                return

            reg_to_read_start = dic[COMMAND_START_LABEL] if COMMAND_START_LABEL in dic else None
            reg_to_read_length = dic[COMMAND_LENGTH_LABEL] if COMMAND_LENGTH_LABEL in dic else None
            reg_to_read_type = dic[COMMAND_TYPE_LABEL] if COMMAND_TYPE_LABEL in dic else None
            reg_to_read_period = dic[COMMAND_PERIOD_LABEL] if COMMAND_PERIOD_LABEL in dic else None
            reg_to_read = (reg_to_read_start, reg_to_read_length, reg_to_read_type, reg_to_read_period)

            if dic[COMMAND_COMMAND_LABEL] in COMMAND_DICT:
                logger.info(f"Running {dic[COMMAND_COMMAND_LABEL]} ::: {reg_to_read}")
                COMMAND_DICT[dic[COMMAND_COMMAND_LABEL].lower()](rs, *reg_to_read)
            else:
                logger.warning(f"Unexpected command: \"{dic[COMMAND_COMMAND_LABEL]}\"")
        except Exception:
            logger.error(f"Unexpected exception in on_message:\n{traceback.format_exc()}")
    else:
        logger.warning(f"Topic {msg.topic} did not match any subscribed topics")


class Status:
    """Per-cycle timing tracker with a rolling statistics window.

    Records the wall-clock timestamp at each named phase of a scan cycle and
    maintains a fixed-size history buffer so that mean durations and relative
    costs can be computed over the last `window` cycles.
    """

    class TimerType(IntEnum):
        """Ordered phases of a single scan cycle used as timing checkpoints."""
        NOTHING = 0
        LIST = auto()
        STATS = auto()
        SCAN = auto()
        STATUS = auto()
        ALL = auto()

    def __init__(self, window=1000):
        """Initialise timing arrays for all phases.

        Args:
            window: Number of past cycles to retain for statistical analysis.
        """
        self.window = window
        self.number_of_cycles = 0
        self.first_save = True
        self.last_time = None
        self.times = [datetime.datetime.now()]*len(Status.TimerType)
        self.diffs = [datetime.timedelta()]*len(Status.TimerType)
        self.last_times = make_array(len(Status.TimerType), self.window, datetime.datetime.now())
        self.last_diffs = make_array(len(Status.TimerType), self.window, datetime.timedelta())

    def done(self, what):
        """Record completion of phase `what` and compute its elapsed timedelta.

        Args:
            what: Phase name matching a TimerType member (case-insensitive).
        """
        this_time = datetime.datetime.now()
        self.times[Status.TimerType[what.upper()]] = this_time
        self.diffs[Status.TimerType[what.upper()]] = (this_time - self.last_time) if (self.last_time is not None) else datetime.timedelta()
        self.last_time = this_time

    def time(self, what, last=False):
        """Return the timestamp of phase `what`.

        Args:
            what: Phase name (case-insensitive).
            last: If True, return the most recent value from the history window
                instead of the current cycle value.
        """
        if last:
            return self.last_times[Status.TimerType[what.upper()]][-1]
        return self.times[Status.TimerType[what.upper()]]

    def diff(self, what, last=False):
        """Return the timedelta between the preceding phase and phase `what`.

        Args:
            what: Phase name (case-insensitive).
            last: If True, return the value from the history window.
        """
        if last:
            return self.last_diffs[Status.TimerType[what.upper()]][-1]
        return self.diffs[Status.TimerType[what.upper()]]

    def diff_from_to(self, what_from, what_to, last=False):
        """Return the elapsed time between phase `what_from` and phase `what_to`.

        Args:
            what_from: Start phase name (case-insensitive).
            what_to: End phase name (case-insensitive).
            last: If True, use values from the history window.
        """
        return self.time(what_to, last) - self.time(what_from, last)

    def save_last(self):
        """Append current cycle timings to the rolling window and increment the cycle counter.

        On the first call the entire window is filled with the current values
        so that mean calculations are valid immediately.
        """
        if self.first_save:
            for i in range(len(Status.TimerType)):
                self.last_times[i] = [self.times[i]]*self.window
                self.last_diffs[i] = [self.diffs[i]]*self.window
            self.first_save = False
        else:
            for i in range(len(Status.TimerType)):
                self.last_times[i] = self.last_times[i][1:] + [self.times[i]]
                self.last_diffs[i] = self.last_diffs[i][1:] + [self.diffs[i]]
        self.number_of_cycles += 1

    def total(self, last=False):
        """Return the total duration of the cycle from NOTHING to ALL.

        Args:
            last: If True, use values from the history window.
        """
        return self.diff_from_to("NOTHING", "ALL", last)

    def means(self):
        """Compute mean absolute and relative durations for each phase over the window.

        Returns:
            dict with keys:
                mean -- average seconds spent in each phase
                rel_mean -- fraction of total cycle time spent in each phase
                total_mean -- average total cycle duration in seconds
                cycles -- total number of cycles recorded so far
        """
        # Make everything in reference to times["NOTHING"]
        times_from_last = make_array(len(Status.TimerType), self.window, 0.0)
        relative_times = make_array(len(Status.TimerType), self.window, 0.0)
        total_times = [0.0]*self.window
        for i in range(len(total_times)):
            total_times[i] = (self.last_times[Status.TimerType.ALL][i]-self.last_times[Status.TimerType.NOTHING][i]).total_seconds()

        for i in range(1, len(Status.TimerType)):
            for j in range(len(self.last_times[i])):
                times_from_last[i][j] = (self.last_times[i][j]-self.last_times[i-1][j]).total_seconds()
                relative_times[i][j] = times_from_last[i][j] / total_times[j] if total_times[j] > 0 else 9999

        dict_avg = {}
        dict_rel_avg = {}
        for i in range(1, len(Status.TimerType)):
            dict_avg[Status.TimerType(i).name] = statistics.mean(times_from_last[i])
            dict_rel_avg[Status.TimerType(i).name] = statistics.mean(relative_times[i])

        return {"mean": dict_avg, "rel_mean": dict_rel_avg, "total_mean": statistics.mean(total_times), "cycles": self.number_of_cycles}


if __name__ == '__main__' and not sys.flags.inspect:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", help="path to YAML config file", type=str, nargs="?", default="config.yaml")
    parser.add_argument("--log-level", help="logging level (default: from config)", type=str, default=None)
    args = parser.parse_args()

    cfg = config.load(args.config)

    log_level = args.log_level or cfg.logging.level
    logging.basicConfig(level=getattr(logging, log_level.upper(), logging.DEBUG))

    registerscanner.configure(cfg)

    PLC_IP = cfg.input.slmp.ip
    PLC_PORT = cfg.input.slmp.port

    MQTT_IP = cfg.output.mqtt.ip
    MQTT_PORT = cfg.output.mqtt.port
    MQTT_USERNAME = cfg.output.mqtt.username
    MQTT_PASSWORD = cfg.output.mqtt.password
    MQTT_PREFIX = cfg.output.mqtt.prefix

    status_refresh_period = cfg.gateway.status_refresh_ms
    status_next_update = datetime.datetime.now()

    stats_refresh_period = cfg.gateway.stats_refresh_ms
    stats_next_update = datetime.datetime.now()

    rs = RegisterScanner(cfg.gateway.scan_list, scan_jitter_ms=cfg.gateway.scan_jitter_ms)
    rs.set_stats_time_period(stats_refresh_period)

    stats = Status(window=cfg.gateway.stats_window)

    logger.info(f"Loaded config from {args.config!r}:\n{config.pretty(cfg)}")

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2) # type: ignore
    mqttc.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    mqttc.on_connect = lambda c, u, f, r, p: on_connect(c, u, f, r, p, MQTT_PREFIX)
    mqttc.on_message = lambda c, u, m: on_message(c, u, m, rs, MQTT_PREFIX)
    mqttc.connect(MQTT_IP, MQTT_PORT, cfg.output.mqtt.keepalive_s)
    mqttc.loop_start()

    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((PLC_IP, PLC_PORT))

            while True:
                # update registers list if changed
                stats.done("nothing")

                if rs.is_changed():
                    x = rs.list()
                    mqttc.publish(MQTT_PREFIX+MQTT_TOPIC_STATUS_REGISTERS, x)
                stats.done("list")

                if rs.is_stats_to_publish():
                    x = rs.stats_as_dict()
                    mqttc.publish(MQTT_PREFIX+MQTT_TOPIC_STATUS_STATS, x)
                stats.done("stats")

                # registers scan and publish as necessary
                dict_to_publish_all = rs.scan_all(s)
                for k in dict_to_publish_all:
                    mqttc.publish(f"{MQTT_PREFIX}{MQTT_TOPIC_SEPARATE}/{k}", json.dumps(dict_to_publish_all[k]))
                stats.done("scan")

                if status_next_update <= datetime.datetime.now():
                    mqttc.publish(MQTT_PREFIX+MQTT_TOPIC_STATUS, json.dumps(stats.means()))
                    status_next_update = status_next_update + datetime.timedelta(microseconds=status_refresh_period*1000)
                stats.done("status")

                if stats_next_update <= datetime.datetime.now():
                    rs.mark_stats_to_publish()
                    stats_next_update = stats_next_update + datetime.timedelta(microseconds=stats_refresh_period*1000)
                stats.done("all")
                stats.save_last()
        except ConnectionResetError as e:
            logger.warning(f"Connection reset: {e}")
        s.close() # type: ignore
