#!/usr/bin/env bash
# Collect main.py machine-readable stdout into GITHUB_OUTPUT and surface
# P0/P1 findings. Intended to run under GitHub Actions' `bash -eo pipefail`.
#
# A missing optional key (notably feedback_export=, printed only when that
# input is true) must not abort the step: outputs, the step summary, and the
# report artifact still have to be written before exiting with the QA code
# (0 CLEAR, 1 VETO, 2 BLOCKED_ENV).
set -e

qa_out=${1:?usage: gha_finish.sh QA_OUT.txt EXIT_CODE}
code=${2:-0}

grab() {
  # Never fail the script when the key is absent or the file is empty.
  if [ ! -f "$qa_out" ]; then
    return 0
  fi
  grep -m1 "^${1}=" "$qa_out" 2>/dev/null | cut -d= -f2- || true
}

github_output=${GITHUB_OUTPUT:-}
if [ -z "$github_output" ]; then
  echo "gha_finish.sh: GITHUB_OUTPUT is not set" >&2
  exit 2
fi

verdict=$(grab verdict)
report=$(grab report)
summary_path=$(grab summary)
fix_brief_path=$(grab fix_brief)
pr_comment_path=$(grab pr_comment)
feedback_export_path=$(grab feedback_export)
findings_path=$(grab findings)
sarif_from_out=$(grab sarif)

{
  echo "verdict=$verdict"
  echo "report=$report"
  echo "summary=$summary_path"
  echo "fix_brief=$fix_brief_path"
  echo "pr_comment=$pr_comment_path"
  echo "feedback_export=$feedback_export_path"
  if [ -n "${QA_SARIF_PATH:-}" ] && [ -f "$QA_SARIF_PATH" ]; then
    echo "sarif=$QA_SARIF_PATH"
  elif [ -n "$sarif_from_out" ] && [ -f "$sarif_from_out" ]; then
    echo "sarif=$sarif_from_out"
  else
    echo "sarif="
  fi
} >> "$github_output"

if [ "$verdict" = "BLOCKED_ENV" ]; then
  echo "::warning title=QA environment blocked::The tests could not run because of an environment precondition (exit 2). The report names the exact cause; no defect is claimed against the project."
fi

# Resolve findings.json even when only findings.md was printed.
findings_json=""
if [ -n "$findings_path" ]; then
  case "$findings_path" in
    *.json) findings_json=$findings_path ;;
    *.md)   findings_json=${findings_path%.md}.json ;;
    *)      findings_json=$findings_path ;;
  esac
fi
if [ -z "$findings_json" ] || [ ! -f "$findings_json" ]; then
  if [ -n "$report" ]; then
    report_dir=$(dirname "$report")
    report_stem=$(basename "$report" .md)
    if [ -f "$report_dir/$report_stem/findings.json" ]; then
      findings_json="$report_dir/$report_stem/findings.json"
    fi
  fi
fi

here=$(cd "$(dirname "$0")" && pwd)
printer="$here/gha_print_veto_findings.py"
finding_lines=""
if [ -n "$findings_json" ] && [ -f "$findings_json" ]; then
  if command -v python3 >/dev/null 2>&1; then
    finding_lines=$(python3 "$printer" "$findings_json" || true)
  elif command -v python >/dev/null 2>&1; then
    finding_lines=$(python "$printer" "$findings_json" || true)
  fi
elif [ -n "$findings_path" ] && [ -f "$findings_path" ]; then
  if command -v python3 >/dev/null 2>&1; then
    finding_lines=$(python3 "$printer" "$findings_path" || true)
  elif command -v python >/dev/null 2>&1; then
    finding_lines=$(python "$printer" "$findings_path" || true)
  fi
fi

if [ -n "$finding_lines" ]; then
  echo "========== QA VETO findings (P0/P1) =========="
  printf '%s\n' "$finding_lines"
  echo "=============================================="
fi

# Step summary: buyer-safe summary when asked, otherwise the full report,
# plus P0/P1 findings so a VETO is visible even if the artifact is missed.
if [ "${QA_SUMMARY:-true}" = "true" ] && [ -n "$summary_path" ] && [ -f "$summary_path" ]; then
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    cat "$summary_path" >> "$GITHUB_STEP_SUMMARY"
  fi
elif [ -n "$report" ] && [ -f "$report" ]; then
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    cat "$report" >> "$GITHUB_STEP_SUMMARY"
  fi
fi
if [ -n "$finding_lines" ] && [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo ""
    echo "## QA VETO findings (P0/P1)"
    echo ""
    printf '%s\n' "$finding_lines" | sed 's/^/- /'
  } >> "$GITHUB_STEP_SUMMARY"
fi

if [ "${QA_PR_COMMENT:-}" = "true" ] && [ "${GITHUB_EVENT_NAME:-}" = "pull_request" ] && [ -n "$pr_comment_path" ] && [ -f "$pr_comment_path" ]; then
  if command -v gh >/dev/null 2>&1; then
    gh pr comment --body-file "$pr_comment_path" || true
  fi
fi

exit "$code"
