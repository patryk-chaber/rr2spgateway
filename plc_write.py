#!/usr/bin/python3
"""Write a single word register on a Mitsubishi PLC over SLMP/TCP.

Sends a one-word write request to the given register address and prints both
the raw request and response packets as hex strings.

Usage:
    python plc_write.py <ip> <address> <value> [--port PORT]
"""

import socket
import argparse

import slmp as SLMP

# module-level so it can be reused if this file is imported
_protocol = SLMP.SlmpProtocol(number_of_bytes=4)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("ip", help="address of the PLC", type=str)
    parser.add_argument("address", help="starting register address", type=str)
    parser.add_argument("value", help="value to write", type=int)
    parser.add_argument("--port", help="port for SLMP communication", type=int, default=1280)
    args = parser.parse_args()

    message = _protocol.prepare_device_write_one_word_message(args.address, args.value)

    print("Request packet")
    print(SLMP.SlmpProtocol.bytes_to_hex_string(message))

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((args.ip, args.port))
    s.send(message)

    response = s.recv(100)

    print("Response packet")
    print(SLMP.SlmpProtocol.bytes_to_hex_string(response))

    s.close()
