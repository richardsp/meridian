#!/usr/bin/env python3
"""
Plan Approval Reminder Hook - PostToolUse ExitPlanMode

Reminds Claude to archive the plan and create Pebble issues after plan is approved.
"""

import json
import sys
import os
from pathlib import Path

# Add lib to path for imports
sys.path.insert(0, str(Path(__file__).parent / "lib"))
from meridian_config import is_headless, log_hook_output, state_path, ACTIVE_PLAN_FILE


def main():
    if is_headless():
        sys.exit(0)

    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    claude_project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")

    if tool_name != "ExitPlanMode":
        sys.exit(0)

    if not claude_project_dir:
        sys.exit(0)

    base_dir = Path(claude_project_dir)


    active_plan_path = str(state_path(base_dir, ACTIVE_PLAN_FILE))

    plan_instructions = (
        f"Plan approved. Archive it: `mkdir -p .meridian/plans && cp ~/.claude/plans/[slug].md .meridian/plans/descriptive-name.md`\n"
        f"Write the absolute path of the archived plan to `{active_plan_path}`."
    )

    reason = plan_instructions

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": reason
        }
    }
    log_hook_output(Path(claude_project_dir), "plan-approval-reminder", output)
    sys.exit(0)


if __name__ == "__main__":
    main()
