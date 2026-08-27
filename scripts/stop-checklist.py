#!/usr/bin/env python3
"""
Stop Checklist — Stop Hook

Prompts agent to complete checklist items (workspace, code review, tests, commits)
before stopping.
"""

import json
import sys
import os
from pathlib import Path

# Add lib to path for imports
sys.path.insert(0, str(Path(__file__).parent / "lib"))
from meridian_config import (
    get_project_config,
    is_headless,
    build_stop_prompt,
    log_hook_output,
    get_action_counter,
    reset_action_counter,
    has_uncommitted_code_changes,
)


def main():
    if is_headless():
        sys.exit(0)

    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    if input_data.get("hook_event_name") != "Stop":
        sys.exit(0)

    claude_project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if not claude_project_dir:
        sys.exit(0)  # Can't operate without project dir
    base_dir = Path(claude_project_dir)

    # If already prompted, allow stop and reset counter for next task
    if input_data.get("stop_hook_active"):
        reset_action_counter(base_dir)
        sys.exit(0)

    config = get_project_config(base_dir)

    # Skip stop hook if too few actions (trivial task)
    min_actions = config.get('stop_hook_min_actions', 15)
    if min_actions > 0:
        action_count = get_action_counter(base_dir)
        if action_count < min_actions:
            sys.exit(0)  # Allow stop — counter keeps accumulating

    # Git-aware gate: when enabled, only block if uncommitted CODE changes
    # exist. Docs/config-only work doesn't warrant the reviewer/tests
    # checklist. Reset the counter so a later docs-only stop doesn't
    # inherit this window's accumulated count.
    if config.get('stop_checklist_git_aware', False):
        if not has_uncommitted_code_changes(base_dir):
            reset_action_counter(base_dir)
            sys.exit(0)

    # Build the stop prompt using shared helper
    reason = build_stop_prompt(base_dir, config)

    # Reset action counter now that stop hook is firing
    reset_action_counter(base_dir)

    output = {
        "decision": "block",
        "reason": reason,
        "systemMessage": "[Meridian] Checklist triggered."
    }

    log_hook_output(base_dir, "stop-checklist", output)
    sys.exit(0)


if __name__ == "__main__":
    main()
