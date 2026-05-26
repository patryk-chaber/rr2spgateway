#!/usr/bin/python3
"""MQTT subscriber that collects PLC register values to a timestamped CSV file.

Subscribes to a fixed list of MQTT topics, parses each JSON payload, and
appends a row of (timestamp, register, label, value) to a CSV file named
after the start time of the collection run.

Monitored topics (gateway prefix, label):
    gateway/separate/D100  T1
    gateway/separate/D101  T2
    gateway/separate/D102  T3
    gateway/separate/D103  T4
    gateway/separate/D104  T5
    gateway/separate/D110  W1
    gateway/separate/D111  W2
    gateway/separate/D112  W3
    gateway/separate/D113  W4
    gateway/separate/D114  G1
    gateway/separate/D115  G2

Usage:
    python collecting_mqtt_client.py <mqttip> [--mqttport PORT] [--mqttuser USER]
                                             [--mqttpass PASS] [--mqttname NAME]
"""

import argparse
import csv
import datetime
import json
import logging
import time

import paho.mqtt.client as mqtt

mqtt_transport = "tcp"

# Each entry: (mqtt_topic, register_name, label)
topics_to_collect = [
    ("gateway/separate/D100", "D100", "T1"),
    ("gateway/separate/D101", "D101", "T2"),
    ("gateway/separate/D102", "D102", "T3"),
    ("gateway/separate/D103", "D103", "T4"),
    ("gateway/separate/D104", "D104", "T5"),
    ("gateway/separate/D110", "D110", "W1"),
    ("gateway/separate/D111", "D111", "W2"),
    ("gateway/separate/D112", "D112", "W3"),
    ("gateway/separate/D113", "D113", "W4"),
    ("gateway/separate/D114", "D114", "G1"),
    ("gateway/separate/D115", "D115", "G2"),
]


def on_connect(client, _userdata, _flags, reason_code, _properties):
    """MQTT CONNACK callback. Subscribe to all configured topics on connect.

    Args:
        client: Connected MQTT client instance.
        _userdata: Unused.
        _flags: Unused connect flags.
        reason_code: Numeric connection result code.
        _properties: Unused MQTT v5 properties.
    """
    client.subscribe([(t[0], 0) for t in topics_to_collect])
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


def on_message(_client, _userdata, msg, csv_filename):
    """MQTT PUBLISH callback. Append matched register values to the CSV file.

    Matches msg.topic against topics_to_collect and writes a row of
    (timestamp, register_name, label, value) for the first match found.

    Args:
        _client: Unused MQTT client instance.
        _userdata: Unused.
        msg: Received MQTT message with .topic and .payload attributes.
        csv_filename: Path to the CSV file to append data rows to.
    """
    for t in topics_to_collect:
        if mqtt.topic_matches_sub(t[0], msg.topic):
            try:
                dic = json.loads(msg.payload)
                row = [dic['timestamp'], t[1], t[2], dic['value']]
                logging.info(f"[DATA]{row}")
                with open(csv_filename, 'a', newline='') as f:
                    csv.writer(f).writerow(row)
            except Exception as e:
                logging.error(f"Exception: {e}")
            break
    else:
        logging.warning(f"Topic {msg.topic} did not match any subscribed topics")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()

    parser.add_argument("mqttip", help="address of the MQTT broker", type=str)
    parser.add_argument("--mqttport", help="port for MQTT communication", type=int, default=1883)
    parser.add_argument("--mqttuser", help="username for MQTT broker access", type=str, default="basic_user")  # placeholder
    parser.add_argument("--mqttpass", help="password for MQTT broker access", type=str, default="basic")  # placeholder
    parser.add_argument("--mqttname", help="name of the MQTT node", type=str, default="Collector")

    args = parser.parse_args()

    MQTT_IP = args.mqttip
    MQTT_PORT = args.mqttport
    MQTT_USERNAME = args.mqttuser
    MQTT_PASSWORD = args.mqttpass
    MQTT_NAME = args.mqttname

    logging.info("╔═════════ COLLECTOR ════════════")
    logging.info(f"║ MQTT_IP       : {MQTT_IP}")
    logging.info(f"║ MQTT_PORT     : {MQTT_PORT}")
    logging.info(f"║ MQTT_USERNAME : {MQTT_USERNAME}")
    logging.info(f"║ MQTT_PASSWORD : {MQTT_PASSWORD}")
    logging.info(f"║ MQTT_NAME     : {MQTT_NAME}")
    logging.info("╚════════════════════════════════")

    data_filename = f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_data.csv"

    mqttc = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=MQTT_NAME,
        transport=mqtt_transport,
        protocol=mqtt.MQTTv311,
        clean_session=True)
    mqttc.username_pw_set(username=MQTT_USERNAME, password=MQTT_PASSWORD)
    mqttc.enable_logger()
    mqttc.on_connect = on_connect
    mqttc.on_message = lambda c, u, m: on_message(c, u, m, data_filename)
    mqttc.on_disconnect = on_disconnect

    mqttc.connect(MQTT_IP, MQTT_PORT, 60)

    mqttc.loop_start()

    while True:
        try:
            time.sleep(10)
        except Exception as e:
            logging.error(e)

    mqttc.loop_stop()
