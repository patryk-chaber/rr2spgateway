#!/usr/bin/python3
"""Generate a scan_list.csv for use with scan_list.py.

Modes
-----
block    : one row, reads COUNT registers starting from START in a single request.
separate : COUNT rows, each reading 1 register (START, START+1, ..., START+COUNT-1).

Examples
--------
  # One request for 10 registers starting at SD518, polled every 100 ms
  ./generate_scan_list.py SD518 10 --mode block --period 100

  # Ten separate 1-register requests for SD518..SD527, polled every 1 ms
  ./generate_scan_list.py SD518 10 --mode separate --period 1

  # Write to a custom file
  ./generate_scan_list.py D100 5 --output my_scan_list.csv
"""

import argparse
import csv
import sys

from slmp import BIT, DEVICES, DWORD, SlmpProtocol, WORD, _parse_device_string


VALID_TYPES = {"BIT", "WORD", "UWORD", "DWORD", "FLOAT"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a scan_list.csv for scan_list.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("start", metavar="START", help="Starting register address, e.g. SD518 or D100")
    parser.add_argument("count", metavar="COUNT", type=int, help="Number of registers to include")
    parser.add_argument("--mode", choices=["block", "separate"], default="block", help="block: one multi-register request; separate: one request per register (default: block)")
    parser.add_argument("--type", dest="reg_type", default=None, help="Register type: BIT, WORD, UWORD, DWORD, FLOAT (default: inferred from device)")
    parser.add_argument("--period", type=int, default=1, help="Polling period in milliseconds (default: 1)")
    parser.add_argument("--output", default="scan_list.csv", help="Output CSV file path (default: scan_list.csv)")
    return parser.parse_args()


def infer_type(device_name: str) -> str:
    """Return a default TYPE string based on the device's data_type in SLMP.DEVICES."""
    spec = DEVICES.get(device_name)
    if spec is None:
        return "WORD"
    if spec.data_type == BIT:
        return "BIT"
    if spec.data_type == DWORD:
        return "DWORD"
    return "WORD"


def build_rows(start: str, count: int, mode: str, reg_type: str, period: int) -> list[list]:
    """Return a list of data rows (not including the header)."""
    rows = []

    if mode == "block":
        rows.append([start, count, reg_type, period])
    else:  # separate
        for i in range(count):
            addr = SlmpProtocol.device_address_add(start, i)
            rows.append([addr, 1, reg_type, period])

    return rows


def main():
    args = parse_args()

    if args.count < 1:
        print(f"error: COUNT must be at least 1, got {args.count}", file=sys.stderr)
        sys.exit(1)

    if args.period < 0:
        print(f"error: --period must be non-negative, got {args.period}", file=sys.stderr)
        sys.exit(1)

    # Validate and resolve start device
    try:
        parsed = _parse_device_string(args.start)
    except (ValueError, KeyError) as e:
        print(f"error: invalid START register {args.start!r}: {e}", file=sys.stderr)
        sys.exit(1)

    # Resolve register type
    if args.reg_type is None:
        reg_type = infer_type(parsed.name)
    else:
        reg_type = args.reg_type.upper()
        if reg_type not in VALID_TYPES:
            print(f"error: unknown type {args.reg_type!r}, must be one of {', '.join(sorted(VALID_TYPES))}",
                  file=sys.stderr)
            sys.exit(1)

    rows = build_rows(args.start, args.count, args.mode, reg_type, args.period)

    with open(args.output, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["START", "LEN", "TYPE", "PERIOD_MS"])
        writer.writerows(rows)

    print(f"Wrote {len(rows)} row(s) to {args.output}")
    for row in rows:
        print(f"  {row}")


if __name__ == "__main__":
    main()