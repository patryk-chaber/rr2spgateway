#!/usr/bin/env bash
# run_experiments.sh -- 1 PLC -> N gateways -> 0 MQTT clients
#
# Usage:
#   ./run_experiments.sh <N> <duration_seconds> <plc_ip> <mqtt_ip> <base_port> [plc_iface] [mqtt_iface]
#
# Example:
#   ./run_experiments.sh 3 60 192.168.1.10 127.0.0.1 30000 eth1 lo

set -euo pipefail

N=${1:-1}
DURATION=${2:-60}
PLC_IP=${3:-"192.168.1.10"}
MQTT_IP=${4:-"127.0.0.1"}
BASE_PORT=${5:-30000}
PLC_IFACE=${6:-"enxc8a362c01365"}
MQTT_IFACE=${7:-"lo"}

GATEWAY_SCRIPT="rr2sp_gateway.py"
PYTHON="$(dirname "$0")/bin/python"
TCPDUMP_STARTUP_DELAY_S=1

command -v tcpdump >/dev/null 2>&1 || { echo "Required command not found: tcpdump"; exit 1; }
[[ -x "$PYTHON" ]] || { echo "Python interpreter not found: $PYTHON"; exit 1; }

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_DIR="logs/${TIMESTAMP}_N${N}_dur${DURATION}"
mkdir -p "$LOG_DIR"

SLMP_PCAP="${LOG_DIR}/slmp_N${N}.pcap"
MQTT_PCAP="${LOG_DIR}/mqtt_N${N}.pcap"

echo "=================================================="
echo " SLMP Gateway Experiment Runner: 1 PLC-N GTWYs-0 MQTT Clients"
echo "=================================================="
echo " Instances   : $N"
echo " Duration    : ${DURATION}s"
echo " PLC IP      : $PLC_IP"
echo " MQTT IP     : $MQTT_IP"
echo " Base port   : $BASE_PORT"
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
    echo "Terminating gateway instances..."
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" || true
            echo "  Killed gateway PID $pid"
        else
            echo "  Gateway PID $pid already exited"
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

    exit 0
}

trap cleanup SIGINT SIGTERM

# Build port filter for tcpdump as an array to avoid unquoted word-splitting
port_filter=("port" "$BASE_PORT")
for ((i = 2; i <= N; i++)); do
    port_filter+=("or" "port" "$((BASE_PORT + i - 1))")
done

echo "Starting SLMP capture on $PLC_IFACE (filter: ${port_filter[*]})..."
sudo tcpdump -i "$PLC_IFACE" -w "$SLMP_PCAP" "${port_filter[@]}" &
TCPDUMP_PIDS+=($!)
echo "  SLMP tcpdump PID: ${TCPDUMP_PIDS[-1]}"

echo "Starting MQTT capture on $MQTT_IFACE..."
sudo tcpdump -i "$MQTT_IFACE" -w "$MQTT_PCAP" port 1883 &
TCPDUMP_PIDS+=($!)
echo "  MQTT tcpdump PID: ${TCPDUMP_PIDS[-1]}"

sleep "$TCPDUMP_STARTUP_DELAY_S"

for ((i = 1; i <= N; i++)); do
    PORT=$((BASE_PORT + i - 1))
    LOG_FILE="${LOG_DIR}/instance_${i}_port${PORT}.log"

    echo "Starting gateway instance $i on port $PORT..."

    "$PYTHON" "$GATEWAY_SCRIPT" \
        "$PLC_IP" \
        "$MQTT_IP" \
        --plcport "$PORT" \
        --mqttport 1883 \
        --mqttprefix "gateway_${i}" \
        > "$LOG_FILE" 2>&1 &

    PIDS+=($!)
    echo "  Gateway PID: ${PIDS[-1]}"
done

echo "--------------------------------------------------"
echo "All $N gateway instances and tcpdump running."
echo "Duration: ${DURATION}s -- press Ctrl+C to stop early."
echo "--------------------------------------------------"

sleep "$DURATION"
cleanup
