#!/usr/bin/env bash
# run_many_experiments.sh -- sweep gateway experiments across scan list sizes and topologies
#
# Generates scan lists of varying sizes and topologies, then calls
# run_experiments.sh (1 PLC -> N gateways -> 0 MQTT clients) and
# run_experiments_mqtt.sh (1 PLC -> 1 gateway -> N MQTT clients) for N in 1..MAX_N.
#
# Usage:
#   ./run_many_experiments.sh [plc_ip] [local_ip] [experiment_time_s] [max_n] [start_device]
#
# Example:
#   ./run_many_experiments.sh 192.168.200.99 127.0.0.1 60 6 SD518

set -euo pipefail

PLC_IP=${1:-"192.168.200.99"}
LOCAL_IP=${2:-"127.0.0.1"}
EXPERIMENT_TIME=${3:-60}
MAX_N=${4:-6}
START_DEVICE=${5:-"SD518"}

SCRIPT_DIR="$(dirname "$0")"
PYTHON="${SCRIPT_DIR}/bin/python"
BETWEEN_EXPERIMENTS_DELAY_S=10

[[ -x "$PYTHON" ]] || { echo "Python interpreter not found: $PYTHON"; exit 1; }
[[ -x "${SCRIPT_DIR}/run_experiments.sh" ]] || { echo "Script not found: ${SCRIPT_DIR}/run_experiments.sh"; exit 1; }
[[ -x "${SCRIPT_DIR}/run_experiments_mqtt.sh" ]] || { echo "Script not found: ${SCRIPT_DIR}/run_experiments_mqtt.sh"; exit 1; }

gen=("$PYTHON" "${SCRIPT_DIR}/generate_scan_list.py" "$START_DEVICE")

echo "=== Experiments for 10 WORD registers ===="
"${gen[@]}" 10 --mode block --output scan_list.csv
sleep "$BETWEEN_EXPERIMENTS_DELAY_S"
for ((N = 1; N <= MAX_N; N++)); do
    "${SCRIPT_DIR}/run_experiments.sh" "$N" "$EXPERIMENT_TIME" "$PLC_IP" "$LOCAL_IP" 30000
    sleep "$BETWEEN_EXPERIMENTS_DELAY_S"
done

echo "=== Experiments for 20 WORD registers ===="
"${gen[@]}" 20 --mode block --output scan_list.csv
sleep "$BETWEEN_EXPERIMENTS_DELAY_S"
for ((N = 1; N <= MAX_N; N++)); do
    "${SCRIPT_DIR}/run_experiments.sh" "$N" "$EXPERIMENT_TIME" "$PLC_IP" "$LOCAL_IP" 30000
    sleep "$BETWEEN_EXPERIMENTS_DELAY_S"
done

echo "=== Experiments for 40 WORD registers ===="
"${gen[@]}" 40 --mode block --output scan_list.csv
sleep "$BETWEEN_EXPERIMENTS_DELAY_S"
for ((N = 1; N <= MAX_N; N++)); do
    "${SCRIPT_DIR}/run_experiments.sh" "$N" "$EXPERIMENT_TIME" "$PLC_IP" "$LOCAL_IP" 30000
    sleep "$BETWEEN_EXPERIMENTS_DELAY_S"
done

echo "=== Experiments for N requests for N different registers ===="
for ((N = 1; N <= MAX_N; N++)); do
    "${gen[@]}" "$N" --mode separate --output scan_list.csv
    sleep "$BETWEEN_EXPERIMENTS_DELAY_S"
    "${SCRIPT_DIR}/run_experiments_mqtt.sh" "$N" "$EXPERIMENT_TIME" "$PLC_IP" "localhost" 30000 1
    sleep "$BETWEEN_EXPERIMENTS_DELAY_S"
done

echo "=== Experiments for 1 request for N different registers ===="
for ((N = 1; N <= MAX_N; N++)); do
    "${gen[@]}" "$N" --mode block --output scan_list.csv
    sleep "$BETWEEN_EXPERIMENTS_DELAY_S"
    "${SCRIPT_DIR}/run_experiments_mqtt.sh" "$N" "$EXPERIMENT_TIME" "$PLC_IP" "localhost" 30000 1
    sleep "$BETWEEN_EXPERIMENTS_DELAY_S"
done

echo "=== Experiments for 1 request for the same register ===="
"${gen[@]}" 1 --mode separate --output scan_list.csv
sleep "$BETWEEN_EXPERIMENTS_DELAY_S"
for ((N = 1; N <= MAX_N; N++)); do
    "${SCRIPT_DIR}/run_experiments_mqtt.sh" "$N" "$EXPERIMENT_TIME" "$PLC_IP" "localhost" 30000 0
    sleep "$BETWEEN_EXPERIMENTS_DELAY_S"
done
