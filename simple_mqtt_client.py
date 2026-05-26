#!/usr/bin/python3
"""Simple MQTT subscriber that logs all received register values.

Connects to an MQTT broker, subscribes to a configurable topic, and logs
each arriving message as a pretty-printed JSON dict.

Usage:
    python simple_mqtt_client.py <mqttip> [--mqttport PORT] [--mqttuser USER]
                                          [--mqttpass PASS] [--mqttname NAME]
                                          [--mqtttopic TOPIC]
"""

import argparse
import json
import logging
import time

import paho.mqtt.client as mqtt

mqtt_transport = "tcp"


def on_connect(client, _userdata, _flags, reason_code, _properties, topic_to_subscribe_to="#"):
    """MQTT CONNACK callback. Subscribe to the configured topic on connect.

    Args:
        client: Connected MQTT client instance.
        _userdata: Unused.
        _flags: Unused connect flags.
        reason_code: Numeric connection result code.
        _properties: Unused MQTT v5 properties.
        topic_to_subscribe_to: MQTT topic filter to subscribe to.
    """
    logging.info(f"Connected with result code {reason_code}")
    client.subscribe(topic_to_subscribe_to)


def on_disconnect(_client, _userdata, _flags, reason_code, _properties):
    """MQTT disconnect callback. Logs the disconnection reason code.

    Args:
        _client: Unused MQTT client instance.
        _userdata: Unused.
        _flags: Unused disconnect flags.
        reason_code: Numeric disconnection reason code.
        _properties: Unused MQTT v5 properties.
    """
    logging.info(f"Disconnected with result code {reason_code}")


def on_message(_client, _userdata, msg, topic_to_subscribe_to="#"):
    """MQTT PUBLISH callback. Log each key-value pair from the JSON payload.

    Args:
        _client: Unused MQTT client instance.
        _userdata: Unused.
        msg: Received MQTT message with .topic and .payload attributes.
        topic_to_subscribe_to: Expected topic filter; unmatched topics are warned.
    """
    if mqtt.topic_matches_sub(topic_to_subscribe_to, msg.topic):
        try:
            logging.info(f"Got message:\n  topic: {msg.topic}\n  payload:")
            dic = json.loads(msg.payload)
            for k in dic:
                logging.info(f"\t{k}: {dic[k]}")
        except Exception as e:
            logging.error(f"Exception: {e}")
    else:
        logging.warning(f"Topic {msg.topic} did not match any subscribed topics")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()

    parser.add_argument("mqttip", help="address of the MQTT broker", type=str)
    parser.add_argument("--mqttport", help="port for MQTT communication", type=int, default=1883)
    parser.add_argument("--mqttuser", help="username for MQTT broker access", type=str, default="basic_user")  # placeholder
    parser.add_argument("--mqttpass", help="password for MQTT broker access", type=str, default="basic")  # placeholder
    parser.add_argument("--mqttname", help="name of the MQTT node", type=str, default="Node")
    parser.add_argument("--mqtttopic", help="topic name to subscribe to", type=str, default="topic")

    args = parser.parse_args()

    MQTT_IP = args.mqttip
    MQTT_PORT = args.mqttport
    MQTT_USERNAME = args.mqttuser
    MQTT_PASSWORD = args.mqttpass
    MQTT_NAME = args.mqttname
    MQTT_TOPIC = args.mqtttopic

    logging.info("╔═════════ CLIENT ═══════════════")
    logging.info(f"║ MQTT_IP       : {MQTT_IP}")
    logging.info(f"║ MQTT_PORT     : {MQTT_PORT}")
    logging.info(f"║ MQTT_USERNAME : {MQTT_USERNAME}")
    logging.info(f"║ MQTT_PASSWORD : {MQTT_PASSWORD}")
    logging.info(f"║ MQTT_NAME     : {MQTT_NAME}")
    logging.info(f"║ MQTT_TOPIC    : {MQTT_TOPIC}")
    logging.info("╚════════════════════════════════")

    mqttc = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=MQTT_NAME,
        transport=mqtt_transport,
        protocol=mqtt.MQTTv311,
        clean_session=True)
    mqttc.username_pw_set(username=MQTT_USERNAME, password=MQTT_PASSWORD)
    mqttc.enable_logger()
    mqttc.on_connect = lambda c, u, f, r, p: on_connect(c, u, f, r, p, MQTT_TOPIC)
    mqttc.on_message = lambda c, u, m: on_message(c, u, m, MQTT_TOPIC)
    mqttc.on_disconnect = on_disconnect

    mqttc.connect(MQTT_IP, MQTT_PORT, 60)

    mqttc.loop_start()

    while True:
        try:
            time.sleep(10)
        except Exception as e:
            logging.error(e)

    mqttc.loop_stop()
