#!/bin/sh
# PostToolUse / UserPromptSubmit hook: increment the session action counter.
#
# Deliberately POSIX sh, not python (grateplan-64jac). PostToolUse BLOCKS the
# agent loop and fires on EVERY tool call; the python version cost a measured
# 62.3ms per fire against 11.5ms here, essentially all of it interpreter startup
# plus the meridian_config import, to add 1 to an integer. Over the 41,444 tool
# calls in one 28-day window that difference is ~35 minutes of blocking wall
# clock.
#
# The state path MUST match meridian_config.get_state_dir():
#   ~/.meridian/state/<md5(realpath(project_dir))[:12]>
# If these ever diverge the counter writes somewhere stop-checklist never reads
# and the checklist silently stops firing -- verify with tests/state_path_parity.

cat > /dev/null   # drain the JSON payload; nothing here needs it

[ "$MERIDIAN_HEADLESS" = "1" ] && exit 0

abs=$(cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null && pwd -P) || exit 0
[ -n "$abs" ] || exit 0

hash=$( { md5 -q -s "$abs" 2>/dev/null || printf '%s' "$abs" | md5sum 2>/dev/null | cut -d' ' -f1; } | cut -c1-12 )
[ -n "$hash" ] || exit 0

dir="$HOME/.meridian/state/$hash"
mkdir -p "$dir" 2>/dev/null || exit 0

f="$dir/action-counter"
n=$(cat "$f" 2>/dev/null || echo 0)
case "$n" in ''|*[!0-9]*) n=0 ;; esac
echo $((n + 1)) > "$f"
exit 0
