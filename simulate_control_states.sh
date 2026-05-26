#!/usr/bin/env bash
# simulate_control_states.sh -- inject PLC register state transitions over time
#
# Writes to PLC registers D110-D115 to simulate operational state changes that
# would normally be driven by the PLC program itself. Cycles D114 through a
# sequence of states (including an overheating spike) a fixed number of times.
# It is assumed that the PLC is connected to the Laboratory Heating and
# Cooling Stand (manufactured by the Warsaw University of Technology, Institute
# of Control and Computation Engineering) and registers D110-D115 are mapped to
# the control signals of this process.
#
# Usage:
#   ./simulate_control_states.sh [plc_ip] [port] [delay_s]
#
# Example (real PLC):
#   ./simulate_control_states.sh 192.168.200.99 30001 1000
#
# Example (mock/local):
#   ./simulate_control_states.sh 127.0.0.1 30001 10

set -euo pipefail

PLC_IP=${1:-"127.0.0.1"}
PORT=${2:-30001}
DELAY_S=${3:-10}

SCRIPT_DIR="$(realpath "$(dirname "$0")")"
PYTHON="${SCRIPT_DIR}/bin/python"
WRITE_SCRIPT="${SCRIPT_DIR}/plc_write.py"

[[ -x "$PYTHON" ]] || { echo "Python interpreter not found: $PYTHON"; exit 1; }
[[ -f "$WRITE_SCRIPT" ]] || { echo "Script not found: $WRITE_SCRIPT"; exit 1; }

echo "=================================================="
echo " Control State Simulator"
echo "=================================================="
echo " PLC IP  : $PLC_IP"
echo " Port    : $PORT"
echo " Delay   : ${DELAY_S}s"
echo "=================================================="

# Set initial register values
"$PYTHON" "$WRITE_SCRIPT" "$PLC_IP" --port "$PORT" D110 400
"$PYTHON" "$WRITE_SCRIPT" "$PLC_IP" --port "$PORT" D111 0
"$PYTHON" "$WRITE_SCRIPT" "$PLC_IP" --port "$PORT" D112 0
"$PYTHON" "$WRITE_SCRIPT" "$PLC_IP" --port "$PORT" D113 0
"$PYTHON" "$WRITE_SCRIPT" "$PLC_IP" --port "$PORT" D114 300
"$PYTHON" "$WRITE_SCRIPT" "$PLC_IP" --port "$PORT" D115 0

for ((i = 1; i <= 10; i++)); do
    echo "--- Cycle $i / 10 ---"

    "$PYTHON" "$WRITE_SCRIPT" "$PLC_IP" --port "$PORT" D114 300
    sleep "$DELAY_S"

    "$PYTHON" "$WRITE_SCRIPT" "$PLC_IP" --port "$PORT" D114 200
    sleep "$DELAY_S"

    "$PYTHON" "$WRITE_SCRIPT" "$PLC_IP" --port "$PORT" D114 400
    sleep "$DELAY_S"

    # Overheating
    "$PYTHON" "$WRITE_SCRIPT" "$PLC_IP" --port "$PORT" D114 1000
    sleep "$DELAY_S"

    "$PYTHON" "$WRITE_SCRIPT" "$PLC_IP" --port "$PORT" D114 500
    sleep "$DELAY_S"
done
