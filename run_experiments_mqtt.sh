#!/usr/bin/env bash
# run_experiments_mqtt.sh -- 1 PLC -> 1 gateway -> N MQTT clients
#
# Usage:
#   ./run_experiments_mqtt.sh <N> <duration_seconds> <plc_ip> <mqtt_ip> <base_port> <register_diff> [plc_iface] [mqtt_iface]
#
# Example:
#   ./run_experiments_mqtt.sh 3 60 192.168.1.10 127.0.0.1 30000 1 eth1 lo

set -euo pipefail

N=${1:-1}
DURATION=${2:-60}
PLC_IP=${3:-"192.168.1.10"}
MQTT_IP=${4:-"127.0.0.1"}
BASE_PORT=${5:-30000}
MQTT_REG_DIFF=${6:-0}
PLC_IFACE=${7:-"enxc8a362c01365"}
MQTT_IFACE=${8:-"lo"}

GATEWAY_SCRIPT="rr2sp_gateway.py"
CLIENT_SCRIPT="simple_mqtt_client.py"
SCRIPT_DIR="$(dirname "$0")"
PYTHON="${SCRIPT_DIR}/bin/python"
TCPDUMP_STARTUP_DELAY_S=1

command -v tcpdump >/dev/null 2>&1 || { echo "Required command not found: tcpdump"; exit 1; }
[[ -x "$PYTHON" ]] || { echo "Python interpreter not found: $PYTHON"; exit 1; }

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_DIR="mqtt_logs/${TIMESTAMP}_N${N}_dur${DURATION}"
mkdir -p "$LOG_DIR"

SLMP_PCAP="${LOG_DIR}/slmp_N${N}.pcap"
MQTT_PCAP="${LOG_DIR}/mqtt_N${N}.pcap"

echo "=================================================="
echo " SLMP Gateway Experiment Runner: 1 PLC-1 GTWYs-N MQTT Clients"
echo "=================================================="
echo " Instances   : $N"
echo " Duration    : ${DURATION}s"
echo " PLC IP      : $PLC_IP"
echo " MQTT IP     : $MQTT_IP"
echo " Base port   : $BASE_PORT"
echo " MQTT reg dif: $MQTT_REG_DIFF"
echo " PLC iface   : $PLC_IFACE"
echo " MQTT iface  : $MQTT_IFACE"
echo " Log dir     : $LOG_DIR"
echo " SLMP pcap   : $SLMP_PCAP"
echo " MQTT pcap   : $MQTT_PCAP"
echo "=================================================="

PIDS=()
TCPDUMP_PIDS=()

cleanup() {
    local pid

    echo ""
    echo "Terminating gateway and client instances..."
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" || true
            echo "  Killed PID $pid"
        else
            echo "  PID $pid already exited"
        fi
    done

    echo "Terminating tcpdump..."
    for pid in "${TCPDUMP_PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" || true
            echo "  Killed tcpdump PID $pid"
        else
            echo "  tcpdump PID $pid already exited"
        fi
    done

    for pid in "${PIDS[@]}" "${TCPDUMP_PIDS[@]}"; do
        wait "$pid" 2>/dev/null || true
    done

    echo ""
    echo "=================================================="
    echo " Capture summary"
    echo "=================================================="
    if [[ -f "$SLMP_PCAP" ]]; then
        SLMP_PACKETS=$(tcpdump -r "$SLMP_PCAP" 2>/dev/null | wc -l) || true
        echo " SLMP packets captured : $SLMP_PACKETS"
        echo " SLMP pcap size        : $(du -h "$SLMP_PCAP" | cut -f1)"
    fi
    if [[ -f "$MQTT_PCAP" ]]; then
        MQTT_PACKETS=$(tcpdump -r "$MQTT_PCAP" 2>/dev/null | wc -l) || true
        echo " MQTT packets captured : $MQTT_PACKETS"
        echo " MQTT pcap size        : $(du -h "$MQTT_PCAP" | cut -f1)"
    fi
    echo " Logs saved to         : $LOG_DIR"
    echo "=================================================="

    echo "=================================================="
    echo " Logs"
    echo "=================================================="
    grep -i -E "╔|║|╚|ERR|WARN" "$LOG_DIR"/*.log || true

    exit 0
}

trap cleanup SIGINT SIGTERM

slmp_port_filter=("port" "$BASE_PORT")
mqtt_port_filter=("port" "1883")

echo "Starting SLMP capture on $PLC_IFACE (filter: ${slmp_port_filter[*]})..."
sudo tcpdump -i "$PLC_IFACE" -w "$SLMP_PCAP" "${slmp_port_filter[@]}" &
TCPDUMP_PIDS+=($!)
echo "  SLMP tcpdump PID: ${TCPDUMP_PIDS[-1]}"

echo "Starting MQTT capture on $MQTT_IFACE (filter: ${mqtt_port_filter[*]})..."
sudo tcpdump -i "$MQTT_IFACE" -w "$MQTT_PCAP" "${mqtt_port_filter[@]}" &
TCPDUMP_PIDS+=($!)
echo "  MQTT tcpdump PID: ${TCPDUMP_PIDS[-1]}"

sleep "$TCPDUMP_STARTUP_DELAY_S"

echo "Starting gateway on port $BASE_PORT..."
"$PYTHON" "$GATEWAY_SCRIPT" \
    "$PLC_IP" \
    "$MQTT_IP" \
    --plcport "$BASE_PORT" \
    --mqttport 1883 \
    --mqttprefix "gateway" \
    > "${LOG_DIR}/gateway_port${BASE_PORT}.log" 2>&1 &

PIDS+=($!)
echo "  Gateway PID: ${PIDS[-1]}"

for ((i = 0; i < N; i++)); do
    REGSTART=$(( 518 + i * MQTT_REG_DIFF ))
    REGISTER="SD${REGSTART}"
    NODENAME="Node${i}"
    LOG_FILE="${LOG_DIR}/client${i}.log"

    echo "Starting client instance $i (register: $REGISTER)..."

    "$PYTHON" "$CLIENT_SCRIPT" \
        "$MQTT_IP" \
        --mqtttopic "gateway/separate/$REGISTER" \
        --mqttname "$NODENAME" \
        > "$LOG_FILE" 2>&1 &

    PIDS+=($!)
    echo "  Client PID: ${PIDS[-1]}"
done

echo "--------------------------------------------------"
echo "1 gateway + $N client instances and tcpdump running."
echo "Duration: ${DURATION}s -- press Ctrl+C to stop early."
echo "--------------------------------------------------"

sleep "$DURATION"
cleanup
