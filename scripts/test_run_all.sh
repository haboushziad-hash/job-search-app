#!/bin/bash
# Sequential test-run orchestrator for v0.1.3 verification.
# Runs Ziad -> Zach -> Ryan against the local backend at localhost:8765.
# Emits milestone-only events for the Monitor tool.

set -u
LOG_DIR="C:/Users/habou/OneDrive/Desktop/Job Search App/scripts/test_runs"
PYTHON="C:/Users/habou/OneDrive/Desktop/Job Search App/backend/venv/Scripts/python.exe"
SCRIPT="C:/Users/habou/OneDrive/Desktop/Job Search App/scripts/test_run_search.py"

mkdir -p "$LOG_DIR"

run_persona() {
  local label="$1"
  local resume="$2"
  local salary="$3"
  local context="$4"
  local logfile="$LOG_DIR/${label}_live.log"
  echo "=== START: $label (salary=\$$salary) ==="
  "$PYTHON" "$SCRIPT" \
    --resume "$resume" \
    --label "$label" \
    --salary "$salary" \
    --context "$context" \
    >"$logfile" 2>&1
  local rc=$?
  if [ $rc -eq 0 ]; then
    echo "=== DONE: $label ==="
    # Tail the summary block
    grep -E "Duration:|Cost:|Roles:|Tier mix:|^================" "$logfile" | tail -10
  else
    echo "=== FAILED: $label (exit $rc) ==="
    tail -30 "$logfile" | sed 's/^/    | /'
  fi
}

run_persona ziad \
  "C:/Users/habou/OneDrive/Desktop/Resumes/Ziad Haboush_Resume_AI_April 2026.pdf" \
  130000 \
  "Senior Consultant at Booz Allen seeking AI strategy, enablement, and governance roles. Federal + commercial. 7yrs experience. Georgetown MPS AI Management. Avoid: pure engineering, sales quota, account executive, customer success."

run_persona zach \
  "C:/Users/habou/Downloads/Zachary_Charles_Resume.pdf" \
  110000 \
  ""

run_persona ryan \
  "C:/Users/habou/Downloads/Ryan Abouzaki's Resume.docx" \
  100000 \
  ""

echo "=== ALL THREE COMPLETE ==="
