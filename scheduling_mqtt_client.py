#!/usr/bin/python3
"""MQTT-based register scheduler that triggers PLC commands on threshold transitions.

Monitors one or more PLC registers published over MQTT and dispatches SLMP
commands back to the gateway when all monitored registers reach the same
threshold band simultaneously.

Usage:
    python scheduling_mqtt_client.py <mqttip> [--mqttport PORT] [--mqttuser USER]
                                              [--mqttpass PASS] [--mqttname NAME]
                                              [--mqttprefix PREFIX]
"""

import argparse
import json
import logging
import time

import paho.mqtt.client as mqtt

mqtt_transport = "tcp"


class RegisterMonitor:
    """Monitors a single PLC register published over MQTT and classifies its value.

    Compares incoming values against an ordered list of thresholds and maps
    each range to a label (e.g. "LL", "L", "N", "H", "HH"). Fires registered
    callbacks whenever the label changes.
    """

    def __init__(self, register_name, prefix, labels, thresholds):
        """Initialise the monitor for one register.

        Args:
            register_name: MQTT sub-topic identifying the register (e.g. "D100").
            prefix: Topic prefix up to and including the trailing slash before
                "separate/" (e.g. "gateway/").
            labels: Ordered list of state labels, one more entry than thresholds
                (e.g. ["LL", "L", "N", "H", "HH"] for 4 thresholds).
            thresholds: Ascending list of boundary values. A value below
                thresholds[i] maps to labels[i]; a value above all thresholds
                maps to labels[-1].
        """
        self.prefix = prefix + "separate/"
        self.register_name = register_name
        self.labels = labels
        self.thresholds = thresholds
        self.state = "N"
        self.state_changed_callbacks = []

    def add_state_changed_callback(self, callback):
        """Register a callable to be invoked whenever the state label changes.

        Args:
            callback: Zero-argument callable called after each state transition.
        """
        self.state_changed_callbacks.append(callback)

    def check_value(self, value):
        """Classify value against thresholds and fire callbacks on state change.

        Args:
            value: Numeric register value to classify.
        """
        previous_state = self.state
        for i in range(len(self.thresholds)):
            if value < self.thresholds[i]:
                self.state = self.labels[i]
                break
        else:
            self.state = self.labels[-1]

        if self.state != previous_state:
            logging.info(f"State of register {self.register_name} changed to {self.state}")
            for callback in self.state_changed_callbacks:
                callback()

    def get_state(self):
        """Return the current state label."""
        return self.state

    def __str__(self):
        """Return a human-readable string representation."""
        return (
            f"RegisterMonitor({self.register_name}, {self.thresholds},"
            f" {self.labels}, {self.state})"
        )

    def subscribe_to_mqtt(self, mqtt_client):
        """Subscribe this monitor to its MQTT topic and attach a message handler.

        Args:
            mqtt_client: Connected paho MQTT client instance.
        """
        def on_message(_client, _userdata, msg):
            if mqtt.topic_matches_sub(self.prefix + self.register_name, msg.topic):
                try:
                    dic = json.loads(msg.payload)
                    if "value" in dic:
                        value = dic["value"]
                        logging.info(f"Received value {value} for register {self.register_name}")
                        self.check_value(value)
                except Exception as e:
                    logging.error(f"Exception: {e}")
            else:
                logging.warning(f"Topic {msg.topic} did not match any subscribed topics")

        mqtt_client.subscribe(self.prefix + self.register_name)
        mqtt_client.message_callback_add(self.prefix + self.register_name, on_message)


class Command:
    """A labelled MQTT command payload to be published to the gateway command topic."""

    def __init__(self, label, command):
        """Initialise a command with a state label and a JSON payload string.

        Args:
            label: State label that triggers this command (e.g. "H", "N").
            command: JSON string to publish to the gateway command topic.
        """
        self.label = label
        self.command = command


class Scheduler:
    """Coordinates register monitors and publishes SLMP commands on consensus state.

    Listens to state-change callbacks from all RegisterMonitor instances. When
    all monitored registers reach the same label simultaneously, publishes the
    matching commands to the gateway command topic.
    """

    def __init__(self, prefix, command_topic, registers_topic, mqtt_client):
        """Initialise the scheduler and subscribe to the register list topic.

        Args:
            prefix: MQTT topic prefix (e.g. "gateway").
            command_topic: Sub-topic for outgoing SLMP commands (e.g. "command").
            registers_topic: Sub-topic for the register list feed
                (e.g. "status/registers").
            mqtt_client: Connected paho MQTT client instance.
        """
        self.prefix = prefix
        self.command_topic = prefix + "/" + command_topic
        self.registers_topic = prefix + "/" + registers_topic
        self.mqtt_client = mqtt_client
        self.register_monitors = []
        self.commands = []
        mqtt_client.subscribe(registers_topic)

    def run_commands(self, label):
        """Publish all commands whose label matches to the command topic.

        Args:
            label: State label to match (e.g. "H", "N", "LL").
        """
        for command in [c for c in self.commands if c.label == label]:
            logging.debug(f"Publishing command: {command.command}")
            self.mqtt_client.publish(self.command_topic, command.command, qos=2)

    def add_register_to_monitor(self, register_monitor):
        """Add a RegisterMonitor and wire its state-change callback to this scheduler.

        Args:
            register_monitor: RegisterMonitor instance to track.
        """
        self.register_monitors.append(register_monitor)
        register_monitor.add_state_changed_callback(self.register_state_changed)

    def add_command(self, label, command):
        """Register a command payload to be sent when all registers reach label.

        Args:
            label: State label that triggers this command.
            command: JSON string to publish to the gateway command topic.
        """
        self.commands.append(Command(label, command))

    def register_state_changed(self):
        """Check whether all monitors share the same state and run matching commands."""
        all_states = set(monitor.get_state() for monitor in self.register_monitors)
        logging.debug(f"Current states: {all_states}")
        if len(all_states) == 1:
            all_state = list(all_states)[0]
            logging.info(f"All registers are {all_state}, performing action")
            self.run_commands(all_state)

    def subscribe_all_registers_to_mqtt(self):
        """Subscribe every registered monitor to its MQTT topic."""
        for monitor in self.register_monitors:
            monitor.subscribe_to_mqtt(self.mqtt_client)


def on_connect(_client, _userdata, _flags, reason_code, _properties):
    """MQTT CONNACK callback. Logs the connection result.

    Args:
        _client: Unused MQTT client instance.
        _userdata: Unused.
        _flags: Unused connect flags.
        reason_code: Numeric connection result code.
        _properties: Unused MQTT v5 properties.
    """
    logging.info(f"Connected with result code {reason_code}")


def on_disconnect(_client, _userdata, _flags, reason_code, _properties):
    """MQTT disconnect callback. Logs the disconnection reason.

    Args:
        _client: Unused MQTT client instance.
        _userdata: Unused.
        _flags: Unused disconnect flags.
        reason_code: Numeric disconnection reason code.
        _properties: Unused MQTT v5 properties.
    """
    logging.info(f"Disconnected with result code {reason_code}")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()

    parser.add_argument("mqttip", help="address of the MQTT broker", type=str)
    parser.add_argument("--mqttport", help="port for MQTT communication", type=int, default=1883)
    parser.add_argument("--mqttuser", help="username for MQTT broker access", type=str, default="scheduler_user")  # placeholder
    parser.add_argument("--mqttpass", help="password for MQTT broker access", type=str, default="scheduler")  # placeholder
    parser.add_argument("--mqttname", help="name of the MQTT node", type=str, default="Node")
    parser.add_argument("--mqttprefix", help="topic prefix for MQTT communication", type=str, default="gateway")

    args = parser.parse_args()

    MQTT_IP = args.mqttip
    MQTT_PORT = args.mqttport
    MQTT_USERNAME = args.mqttuser
    MQTT_PASSWORD = args.mqttpass
    MQTT_NAME = args.mqttname
    MQTT_PREFIX = args.mqttprefix

    logging.info("╔═════════ SCHEDULER ════════════")
    logging.info(f"║ MQTT_IP       : {MQTT_IP}")
    logging.info(f"║ MQTT_PORT     : {MQTT_PORT}")
    logging.info(f"║ MQTT_USERNAME : {MQTT_USERNAME}")
    logging.info(f"║ MQTT_PASSWORD : {MQTT_PASSWORD}")
    logging.info(f"║ MQTT_NAME     : {MQTT_NAME}")
    logging.info(f"║ MQTT_TOPIC    : {MQTT_PREFIX}")
    logging.info("╚════════════════════════════════")

    mqttc = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=MQTT_NAME,
        transport=mqtt_transport,
        protocol=mqtt.MQTTv311,
        clean_session=True)
    mqttc.username_pw_set(username=MQTT_USERNAME, password=MQTT_PASSWORD)
    mqttc.on_connect = on_connect
    mqttc.on_disconnect = on_disconnect

    mqttc.connect(MQTT_IP, MQTT_PORT, 60)

    mqttc.loop_start()

    scheduler = Scheduler(
        prefix=MQTT_PREFIX,
        command_topic="command",
        registers_topic="status/registers",
        mqtt_client=mqttc)
    scheduler.add_register_to_monitor(
        RegisterMonitor(
            "D100",
            prefix=MQTT_PREFIX + "/",
            labels=["LL", "L", "N", "H", "HH"],
            thresholds=[2400, 2500, 4500, 5500]
        )
    )
    scheduler.add_command("HH", '{"command": "modify", "start": "D100", "period": 1500 }')
    scheduler.add_command("H", '{"command": "modify", "start": "D100", "period": 15000 }')
    scheduler.add_command("N", '{"command": "modify", "start": "D100", "period": 150000 }')
    scheduler.subscribe_all_registers_to_mqtt()
    scheduler.run_commands("N")

    while True:
        try:
            time.sleep(10)
        except Exception as e:
            logging.error(e)

    mqttc.loop_stop()
