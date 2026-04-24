#!/bin/bash
# ============================================================
# ai-run.sh — submit an orchestration job and poll until done
# ============================================================
#
# Usage:
#   ./ai-run.sh <project_name> <prompt> [deploy_target]
#
# Examples:
#   ./ai-run.sh my_project "Write a python script that prints hello world"
#   ./ai-run.sh sensor_read "Read CPU temp and log to file" pi-2
#   ./ai-run.sh web_test "Fetch example.com and print status code" Rak
#
# Environment variables (override defaults):
#   ORCH_HOST          orchestrator address   (default: 192.168.2.218:8000)
#   ORCH_TARGET        deploy target          (default: pi-1)
#   ORCH_PLANNER       planner model          (default: qwen2.5:72b)
#   ORCH_GENERATORS    generator models       (default: qwen2.5-coder:32b,deepseek-coder:33b)
#   ORCH_JUDGE         judge model            (default: qwen2.5:72b)
#   ORCH_OPTIMIZER     optimizer model         (default: qwen2.5-coder:32b)
#   ORCH_TROUBLESHOOT  troubleshooter model   (default: qwen2.5-coder:32b)
#   ORCH_POLL_INTERVAL seconds between polls  (default: 10)
#

set -euo pipefail

# ---- defaults ----
HOST="${ORCH_HOST:-192.168.2.218:8000}"
TARGET="${ORCH_TARGET:-pi-1}"
PLANNER="${ORCH_PLANNER:-qwen2.5:72b}"
GENERATORS="${ORCH_GENERATORS:-qwen2.5-coder:32b,deepseek-coder:33b}"
JUDGE="${ORCH_JUDGE:-qwen2.5:72b}"
OPTIMIZER="${ORCH_OPTIMIZER:-qwen2.5-coder:32b}"
TROUBLESHOOTER="${ORCH_TROUBLESHOOT:-qwen2.5-coder:32b}"
POLL_INTERVAL="${ORCH_POLL_INTERVAL:-10}"

# ---- args ----
if [ $# -lt 2 ]; then
    echo "Usage: $0 <project_name> <prompt> [deploy_target]"
    exit 1
fi

PROJECT="$1"
PROMPT="$2"
TARGET="${3:-$TARGET}"

# ---- build generator models JSON array ----
GEN_ARRAY=$(echo "$GENERATORS" | tr ',' '\n' | sed 's/.*/"&"/' | paste -sd ',' -)

# ---- colors ----
BOLD="\033[1m"
DIM="\033[2m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
CYAN="\033[36m"
RESET="\033[0m"

echo -e "${BOLD}${CYAN}AI Orchestrator${RESET}"
echo -e "${DIM}───────────────────────────────────────${RESET}"
echo -e "  Project:  ${BOLD}${PROJECT}${RESET}"
echo -e "  Target:   ${BOLD}${TARGET}${RESET}"
echo -e "  Prompt:   ${PROMPT}"
echo -e "${DIM}───────────────────────────────────────${RESET}"
echo ""

# ---- submit ----
echo -e "${YELLOW}Submitting job...${RESET}"

RESPONSE=$(curl -s -X POST "http://${HOST}/orchestrate" \
    -H "Content-Type: application/json" \
    -d "{
        \"project_name\": \"${PROJECT}\",
        \"prompt\": \"${PROMPT}\",
        \"planner_model\": \"${PLANNER}\",
        \"generator_models\": [${GEN_ARRAY}],
        \"judge_model\": \"${JUDGE}\",
        \"optimizer_model\": \"${OPTIMIZER}\",
        \"troubleshooter_model\": \"${TROUBLESHOOTER}\",
        \"deploy_target\": \"${TARGET}\"
    }")

# check for immediate error
if echo "$RESPONSE" | grep -q '"detail"'; then
    echo -e "${RED}Error: $(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['detail'])")${RESET}"
    exit 1
fi

RUN_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['run_id'])")

echo -e "${GREEN}Run started:${RESET} ${BOLD}${RUN_ID}${RESET}"
echo ""

# ---- poll ----
LAST_PHASE=""
SPINNER=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
SPIN_IDX=0
START_TIME=$(date +%s)

while true; do

    STATUS=$(curl -s "http://${HOST}/status/${RUN_ID}")

    PHASE=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('phase','unknown'))")
    SCORE=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('score',0))")
    COMPLETED=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('completed',False))")
    HAS_ERROR=$(echo "$STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d and d['error'] is not None)")

    ELAPSED=$(( $(date +%s) - START_TIME ))
    MINS=$(( ELAPSED / 60 ))
    SECS=$(( ELAPSED % 60 ))
    TIME_STR=$(printf "%02d:%02d" $MINS $SECS)

    # update display
    if [ "$PHASE" != "$LAST_PHASE" ]; then
        # new phase, print on new line
        echo ""
        echo -e "  ${DIM}[${TIME_STR}]${RESET} ${CYAN}${PHASE}${RESET} ${DIM}(score: ${SCORE})${RESET}"
        LAST_PHASE="$PHASE"
    else
        # same phase, show spinner
        printf "\r  ${DIM}[${TIME_STR}]${RESET} ${SPINNER[$SPIN_IDX]} ${DIM}waiting...${RESET}  "
        SPIN_IDX=$(( (SPIN_IDX + 1) % ${#SPINNER[@]} ))
    fi

    if [ "$COMPLETED" = "True" ]; then
        echo ""
        echo -e "${DIM}───────────────────────────────────────${RESET}"

        if [ "$HAS_ERROR" = "True" ]; then
            ERROR=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error','unknown'))")
            echo -e "${RED}${BOLD}FAILED${RESET} after ${TIME_STR}"
            echo -e "${RED}Error: ${ERROR}${RESET}"
            exit 1
        fi

        FINAL_SCORE=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('result',{}).get('score',0))")
        LANG=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('result',{}).get('language','unknown'))" 2>/dev/null)
        PTYPE=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('result',{}).get('project_type','script'))" 2>/dev/null)
        WINNER=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('result',{}).get('winning_model','?'))" 2>/dev/null)
        TS_ATTEMPTS=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('result',{}).get('troubleshoot_attempts',0))" 2>/dev/null)
        STDOUT=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('result',{}).get('execution',{}).get('stdout','').strip())")
        STDERR=$(echo "$STATUS" | python3 -c "import sys,json; r=json.load(sys.stdin).get('result',{}).get('execution',{}).get('stderr','').strip(); print(r[:300] if r else '')")
        RETCODE=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('result',{}).get('execution',{}).get('returncode','?'))")

        echo -e "${GREEN}${BOLD}COMPLETED${RESET} in ${TIME_STR}"
        echo ""
        echo -e "  Score:       ${BOLD}${FINAL_SCORE}${RESET}"
        echo -e "  Language:    ${LANG} (${PTYPE})"
        echo -e "  Model:       ${WINNER}"
        echo -e "  Exit code:   ${RETCODE}"

        if [ "$TS_ATTEMPTS" != "0" ] && [ -n "$TS_ATTEMPTS" ]; then
            echo -e "  Troubleshoot: ${TS_ATTEMPTS} attempt(s)"
        fi

        if [ -n "$STDOUT" ]; then
            echo -e "  ${BOLD}stdout:${RESET}"
            echo "$STDOUT" | sed 's/^/    /'
        fi

        if [ -n "$STDERR" ]; then
            echo -e "  ${DIM}stderr:${RESET}"
            echo "$STDERR" | sed 's/^/    /'
        fi

        echo ""
        echo -e "  Run ID:  ${DIM}${RUN_ID}${RESET}"
        echo -e "  Log:     ${DIM}/opt/ai-orchestrator/logs/${RUN_ID}.log${RESET}"

        DEPLOY_PATH=$(echo "$STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin).get('result',{}).get('deployed_to',''); print(d if d else '')" 2>/dev/null)

        if [ -n "$DEPLOY_PATH" ]; then
            echo -e "  ${GREEN}Deployed: ${TARGET}:${DEPLOY_PATH}${RESET}"
            echo -e "  ${DIM}Run with: ssh ${TARGET} bash ${DEPLOY_PATH}/run.sh${RESET}"
        fi

        echo ""
        exit 0
    fi

    sleep "$POLL_INTERVAL"
done