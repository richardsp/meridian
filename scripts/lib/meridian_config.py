"""
Shared configuration helpers for Meridian hooks.
"""

import hashlib
import os
import subprocess
from pathlib import Path


def is_headless():
    """Check if running inside a headless session (e.g., session learner subprocess).

    When True, hooks should exit immediately — the headless session shouldn't
    trigger cleanup, context injection, or any other side effects.
    """
    return os.environ.get("MERIDIAN_HEADLESS") == "1"


# =============================================================================
# PATH CONSTANTS
# =============================================================================
MERIDIAN_CONFIG = ".meridian/config.yaml"
WORKSPACE_FILE = ".meridian/WORKSPACE.md"

# Markers that identify system/hook noise rather than real user messages.
# Used by session-transcript to filter injected context.
SYSTEM_NOISE_MARKERS = (
    "<system-reminder>",
    "<injected-project-context>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<task-notification>",
    "Stop hook feedback:",
    "Base directory for this skill:",
    "SessionStart:clear hook",
    "SessionStart hook additional context:",
    "UserPromptSubmit hook",
)


def is_system_noise(text: str) -> bool:
    """Check if a message is system/hook noise rather than real dialogue."""
    for marker in SYSTEM_NOISE_MARKERS:
        if marker in text:
            return True
    return False


# State file names (resolved at runtime via get_state_dir())
# State lives in ~/.meridian/state/<project-hash>/ so .meridian/ can be
# symlinked across worktrees without sharing ephemeral session state.
ACTION_COUNTER_FILE = "action-counter"
PLAN_MODE_STATE = "plan-mode-state"
ACTIVE_PLAN_FILE = "active-plan"
INJECTED_FILES_LOG = "injected-files"
HOOK_LOGS_DIR = "hook_logs"
LAST_SESSION_FILE = "last-session.md"
TRANSCRIPT_PATH_STATE = "transcript-path"


# =============================================================================
# STATE DIRECTORY RESOLUTION
# =============================================================================
_state_dir_cache: dict[str, Path] = {}


def get_state_dir(project_dir: Path) -> Path:
    """Resolve state directory to ~/.meridian/state/<hash>/.

    State is stored per-working-directory in the user's home directory.
    This enables symlinking the entire .meridian/ folder across worktrees
    without sharing ephemeral session state (counters, flags, locks).

    Result is cached per resolved path — safe because hooks are short-lived processes.
    """
    key = str(project_dir.resolve())
    if key in _state_dir_cache:
        return _state_dir_cache[key]
    project_hash = hashlib.md5(key.encode()).hexdigest()[:12]
    state_dir = Path.home() / ".meridian" / "state" / project_hash
    state_dir.mkdir(parents=True, exist_ok=True)
    _state_dir_cache[key] = state_dir
    return state_dir


def state_path(project_dir: Path, filename: str) -> Path:
    """Get full path to a state file."""
    return get_state_dir(project_dir) / filename


# =============================================================================
# HOOK OUTPUT LOGGING
# =============================================================================
def log_hook_output(base_dir: Path, hook_name: str, output: dict) -> None:
    """Write hook output to stdout and save a readable markdown copy to hook_logs/.

    Logs are overwritten each time the hook fires, keeping only the latest output.
    """
    import json
    from datetime import datetime

    output_str = json.dumps(output)

    # Log readable markdown version
    log_dir = get_state_dir(base_dir) / HOOK_LOGS_DIR
    try:
        log_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        hook_specific = output.get("hookSpecificOutput", {})
        event_name = hook_specific.get("hookEventName", output.get("decision", "unknown"))
        decision = hook_specific.get("permissionDecision", output.get("decision", ""))

        lines = [f"# {hook_name}", f"**Time:** {timestamp}  ", f"**Event:** {event_name}  "]
        if decision:
            lines.append(f"**Decision:** {decision}  ")
        lines.append("")
        lines.append("---")
        lines.append("")

        # Extract the human-readable content
        content = (
            hook_specific.get("additionalContext")
            or hook_specific.get("permissionDecisionReason")
            or output.get("reason")
            or ""
        )
        if content:
            lines.append(content)
        else:
            lines.append("*(no content)*")

        (log_dir / f"{hook_name}.md").write_text("\n".join(lines) + "\n")
    except (IOError, OSError):
        pass

    # Print to stdout for Claude Code
    print(output_str)


# =============================================================================
# YAML PARSING (simple, no dependencies)
# =============================================================================
def get_config_value(content: str, key: str, default: str = "") -> str:
    """Get a simple key: value from YAML content."""
    for line in content.split('\n'):
        stripped = line.strip()
        if stripped.startswith(f'{key}:'):
            return stripped.split(':', 1)[1].strip().strip('"\'')
    return default


# =============================================================================
# CONFIG FILE HELPERS
# =============================================================================
_TRUTHY = frozenset({"true", "yes", "1", "on"})
_FALSY = frozenset({"false", "no", "0", "off"})


def parse_bool(value: str, default: bool) -> bool:
    """Parse a YAML-style boolean string. Returns default for unrecognized values."""
    v = value.strip().lower()
    if v in _TRUTHY:
        return True
    if v in _FALSY:
        return False
    return default


# Config key definitions: (yaml_key, config_key, type, default)
_BOOL_KEYS = [
    ('pebble_enabled', 'pebble_enabled', False),
    ('stop_checklist_commit_item', 'stop_checklist_commit_item', True),
    ('stop_checklist_git_aware', 'stop_checklist_git_aware', False),
]
_INT_KEYS = [
    ('stop_hook_min_actions', 'stop_hook_min_actions', 15),
]

# File extensions treated as "code" by the git-aware stop-checklist gate.
CODE_EXTENSIONS = frozenset({
    '.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.vue', '.svelte',
    '.go', '.rs', '.java', '.kt', '.rb', '.php', '.c', '.h', '.cpp', '.hpp',
    '.cc', '.cs', '.swift', '.scala', '.sql', '.sh', '.bash', '.zsh',
})


def _parse_string_list(content: str, key: str) -> list[str]:
    """Parse a simple YAML string list (no PyYAML dependency).

    Expects format:
        key:
          - "First item"
          - Second item
    """
    result = []
    in_section = False

    for line in content.split('\n'):
        stripped = line.strip()

        if stripped == f'{key}:':
            in_section = True
            continue

        if not in_section:
            continue

        # Exit section on non-indented, non-empty line
        if stripped and not line[0].isspace():
            break

        if stripped.startswith('- '):
            item = stripped[2:].strip().strip('"\'')
            if item:
                result.append(item)

    return result


def has_uncommitted_code_changes(base_dir: Path) -> bool:
    """True if git shows uncommitted changes (staged, unstaged, or untracked)
    to files with a code extension. Used by the git-aware stop-checklist gate
    so docs/config-only turns don't pay a blocked stop.

    Fails open (returns True) if git is unavailable or errors — the checklist
    should degrade to its pre-gate behavior, not silently disable itself.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(base_dir)
        )
        if result.returncode != 0:
            return True
        for line in result.stdout.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            # Renames report "old -> new"; the new path is what matters
            if ' -> ' in path:
                path = path.split(' -> ', 1)[1]
            path = path.strip('"')
            if Path(path).suffix.lower() in CODE_EXTENSIONS:
                return True
        return False
    except Exception:
        return True


def _parse_extra_doc_dirs(content: str) -> list[dict]:
    """Parse extra_doc_dirs list from YAML content (no PyYAML dependency).

    Expects format:
        extra_doc_dirs:
          - path: "knowledge/"
            header: "My docs"
    """
    result = []
    in_section = False
    current: dict = {}

    for line in content.split('\n'):
        stripped = line.strip()

        if stripped == 'extra_doc_dirs:':
            in_section = True
            continue

        if not in_section:
            continue

        # Exit section on non-indented, non-empty line
        if stripped and not line[0].isspace():
            break

        if stripped.startswith('- path:'):
            if current and 'path' in current:
                result.append(current)
            current = {'path': stripped.split(':', 1)[1].strip().strip('"\'') }
        elif stripped.startswith('header:') and current:
            current['header'] = stripped.split(':', 1)[1].strip().strip('"\'')

    if current and 'path' in current:
        result.append(current)

    return result


def get_extra_doc_dirs(project_config: dict) -> list[tuple[str, str]]:
    """Extract (path, header) tuples from extra_doc_dirs config."""
    result = []
    for extra in project_config.get('extra_doc_dirs', []):
        if isinstance(extra, dict) and 'path' in extra:
            result.append((extra['path'], extra.get('header', f"Additional docs from {extra['path']}")))
    return result


def get_project_config(base_dir: Path) -> dict:
    """Read project config and return as dict with defaults."""
    config = {
        'pebble_enabled': False,
        'stop_hook_min_actions': 15,
        'extra_doc_dirs': [],
        'stop_checklist_extra': [],
        'stop_checklist_commit_item': True,
        'stop_checklist_git_aware': False,
    }

    config_path = base_dir / MERIDIAN_CONFIG
    if not config_path.exists():
        return config

    try:
        content = config_path.read_text()

        for yaml_key, config_key, default in _BOOL_KEYS:
            val = get_config_value(content, yaml_key)
            if val:
                config[config_key] = parse_bool(val, default)

        for yaml_key, config_key, default in _INT_KEYS:
            val = get_config_value(content, yaml_key)
            if val:
                try:
                    config[config_key] = int(val)
                except ValueError:
                    pass

        config['extra_doc_dirs'] = _parse_extra_doc_dirs(content)
        config['stop_checklist_extra'] = _parse_string_list(content, 'stop_checklist_extra')

    except IOError:
        pass

    return config


def get_additional_review_files(base_dir: Path, absolute: bool = False) -> list[str]:
    """Get list of additional files for implementation/plan review.

    Args:
        base_dir: Base directory of the project
        absolute: If True, return absolute paths; otherwise relative paths
    """
    files = [".meridian/docs/code-guide.md", ".meridian/WORKSPACE.md"]

    if absolute:
        return [str(base_dir / f) for f in files]
    return files


# =============================================================================
# FLAG FILE HELPERS
# =============================================================================
def cleanup_flag(base_dir: Path, flag_name: str) -> None:
    """Delete a flag file if it exists."""
    try:
        state_path(base_dir, flag_name).unlink(missing_ok=True)
    except Exception:
        pass


def create_flag(base_dir: Path, flag_name: str) -> None:
    """Create a flag file."""
    path = state_path(base_dir, flag_name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except Exception:
        pass


def flag_exists(base_dir: Path, flag_name: str) -> bool:
    """Check if a flag file exists."""
    return state_path(base_dir, flag_name).exists()


# =============================================================================
# ACTION COUNTER HELPERS
# =============================================================================
def get_action_counter(base_dir: Path) -> int:
    """Get current main action counter value."""
    counter_path = state_path(base_dir, ACTION_COUNTER_FILE)
    try:
        if counter_path.exists():
            return int(counter_path.read_text().strip())
    except (ValueError, IOError):
        pass
    return 0


def set_action_counter(base_dir: Path, value: int) -> None:
    """Set the main action counter to a specific value."""
    try:
        state_path(base_dir, ACTION_COUNTER_FILE).write_text(str(value))
    except IOError:
        pass


def reset_action_counter(base_dir: Path) -> None:
    """Reset the main action counter to 0."""
    set_action_counter(base_dir, 0)


# =============================================================================
# PEBBLE INTEGRATION
# =============================================================================


def get_pebble_context(base_dir: Path) -> str:
    """Get Pebble context for injection: in-progress work and ready issues.

    Runs pb commands to get:
    - Currently in-progress issues
    - Ready issues (unblocked, can be picked up)

    Returns formatted string or empty if commands fail.
    """
    parts = []

    try:
        result = subprocess.run(
            ["pb", "list", "--status", "in_progress", "--pretty"],
            capture_output=True, text=True, timeout=10, cwd=str(base_dir)
        )
        output = result.stdout.strip()
        if result.returncode == 0 and output and "No issues found" not in output:
            parts.append("## In Progress")
            parts.append("")
            parts.append(output)
            parts.append("")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    try:
        result = subprocess.run(
            ["pb", "ready", "--pretty"],
            capture_output=True, text=True, timeout=10, cwd=str(base_dir)
        )
        output = result.stdout.strip()
        if result.returncode == 0 and output and "No issues found" not in output and "No ready issues" not in output:
            parts.append("## Ready")
            parts.append("")
            parts.append(output)
            parts.append("")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return "\n".join(parts) if parts else ""


# =============================================================================
# FRONTMATTER-BASED DOC SCANNING
# =============================================================================
def extract_frontmatter(file_path: Path) -> tuple[str, list[str]]:
    """Extract summary and read_when from YAML frontmatter.

    Returns (summary, read_when_list). Both empty if no valid frontmatter.
    Only reads the frontmatter header, not the full file.
    """
    try:
        with file_path.open() as f:
            first_line = f.readline()
            if not first_line.startswith("---"):
                return "", []
            fm_lines = []
            for line in f:
                if line.strip() == "---":
                    break
                fm_lines.append(line)
            else:
                return "", []  # no closing ---
            frontmatter = "\n".join(fm_lines).strip()
    except IOError:
        return "", []
    summary = ""
    read_when: list[str] = []
    collecting_read_when = False

    for line in frontmatter.split("\n"):
        stripped = line.strip()

        if stripped.startswith("summary:"):
            summary = stripped[len("summary:"):].strip().strip("'\"")
            collecting_read_when = False

        elif stripped.startswith("read_when:"):
            collecting_read_when = True
            inline = stripped[len("read_when:"):].strip()
            if inline.startswith("[") and inline.endswith("]"):
                try:
                    import json
                    parsed = json.loads(inline.replace("'", '"'))
                    if isinstance(parsed, list):
                        read_when.extend(str(x).strip() for x in parsed if x)
                except (json.JSONDecodeError, ValueError):
                    pass

        elif collecting_read_when and stripped.startswith("- "):
            hint = stripped[2:].strip()
            if hint:
                read_when.append(hint)
        elif collecting_read_when and stripped:
            collecting_read_when = False

    return summary, read_when


def scan_docs_directory(dir_path: Path, base_dir: Path,
                        budget: int | None = None) -> str:
    """Scan a directory for .md files with frontmatter, return formatted listing.

    Skips INDEX.md and README.md files. Returns empty string if no docs found.

    If BUDGET is given the listing is fitted to it by reducing per-entry DETAIL,
    never by dropping entries. That invariant is the point: the prefix used to be
    tail-truncated mid-list, and a partial index looks complete -- the agent
    cannot tell that six more docs exist, so it never reads them. A terse entry
    still routes; a missing entry silently cannot.
    """
    if not dir_path.exists():
        return ""

    rows = []
    for md_file in sorted(dir_path.rglob("*.md")):
        if md_file.name in SKIP_NAMES:
            continue
        rel_path = str(md_file.relative_to(base_dir))
        summary, read_when = extract_frontmatter(md_file)
        rows.append((rel_path, summary or "", read_when or []))

    if not rows:
        return ""

    sum_budget = DOCS_ENTRY_SUMMARY_BUDGET
    hint_budget = DOCS_ENTRY_READWHEN_BUDGET
    if budget is not None:
        # Room left for prose once every path is spelled out, split per entry.
        fixed = sum(len(r[0]) + 8 for r in rows)
        per_entry = max(0, (budget - fixed) // len(rows))
        # Hints route, summaries describe -- so hints get the larger share.
        sum_budget = min(sum_budget, max(0, per_entry // 3))
        hint_budget = min(hint_budget, max(24, per_entry - sum_budget))

    entries = []
    for rel_path, summary, read_when in rows:
        if summary and sum_budget > 0:
            entry = f"- **{rel_path}** — {_clip(summary, sum_budget)}"
        else:
            entry = f"- **{rel_path}**"
        if read_when and hint_budget > 0:
            entry += f"\n  Read when: {_clip_hints(read_when, hint_budget)}"
        entries.append(entry)

    return "\n".join(entries)


def _clip(text: str, budget: int) -> str:
    """Trim TEXT to BUDGET chars on a word boundary, marking the cut."""
    text = " ".join(text.split())
    if len(text) <= budget:
        return text
    cut = text[:budget].rsplit(" ", 1)[0]
    return f"{cut}…"


def _clip_hints(hints: list, budget: int) -> str:
    """Keep whole read_when hints until BUDGET is spent, then say how many remain.

    Hints are the routing signal, so a PARTIAL hint is worse than a dropped one --
    it can match on a fragment. Always keeps at least the first hint, and always
    reports the remainder, so the listing never understates what a doc covers.
    """
    kept, used = [], 0
    for h in hints:
        h = " ".join(h.split())
        if kept and used + len(h) + 2 > budget:
            break
        kept.append(h)
        used += len(h) + 2
    out = "; ".join(kept)
    remaining = len(hints) - len(kept)
    if remaining > 0:
        out += f" (+{remaining} more)"
    return out


MAX_DOC_DEPTH = 3  # Max directory depth for project-wide frontmatter scanning

SKIP_DIRS = {
    ".git", "node_modules", ".next", "dist", "build", "__pycache__",
    "vendor", ".venv", ".env", ".tox", ".mypy_cache", ".ruff_cache",
}

SKIP_NAMES = {"INDEX.md", "README.md", "CHANGELOG.md"}


def scan_project_frontmatter(project_dir: Path) -> str:
    """Scan the project for .md files with frontmatter, up to MAX_DOC_DEPTH levels deep.

    Returns formatted listing with absolute paths, summaries, and read_when hints.
    Only includes files that have a valid 'summary' field in their frontmatter.
    """
    entries = []

    for md_file in sorted(project_dir.rglob("*.md")):
        # Depth check
        rel = md_file.relative_to(project_dir)
        if len(rel.parts) - 1 > MAX_DOC_DEPTH:
            continue

        # Skip junk directories
        if any(part in SKIP_DIRS for part in rel.parts):
            continue

        # Skip index/readme/changelog
        if md_file.name in SKIP_NAMES:
            continue

        summary, read_when = extract_frontmatter(md_file)
        if not summary:
            continue

        entry = f"- **{md_file}** — {summary}"
        if read_when:
            entry += f"\n  Read when: {'; '.join(read_when)}"
        entries.append(entry)

    return "\n".join(entries)


# =============================================================================
# NESTED GIT REPO SCANNING
# =============================================================================
def scan_nested_git_repos(base_dir: Path, max_depth: int = 3) -> str:
    """Scan for nested git repositories and return their recent commits.

    Finds .git directories up to max_depth levels deep (excluding the root).
    Returns formatted string with recent commits per nested repo.
    """
    nested_repos = []

    for git_dir in sorted(base_dir.rglob(".git")):
        # Skip the root repo's .git
        if git_dir.parent == base_dir:
            continue

        # Skip if too deep
        rel = git_dir.parent.relative_to(base_dir)
        if len(rel.parts) > max_depth:
            continue

        if any(part in SKIP_DIRS for part in rel.parts):
            continue

        # Only include actual directories (not submodule .git files)
        if not git_dir.is_dir():
            continue

        nested_repos.append((str(rel), git_dir.parent))

    if not nested_repos:
        return ""

    parts = ["## Nested Repositories"]
    parts.append("")

    for rel_path, repo_dir in nested_repos:
        try:
            result = subprocess.run(
                ["git", "log", "--format=%h %s (%cr)", "-10", "--all"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(repo_dir)
            )
            if result.returncode == 0 and result.stdout.strip():
                # Get current branch
                branch_result = subprocess.run(
                    ["git", "branch", "--show-current"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=str(repo_dir)
                )
                branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "unknown"

                parts.append(f"### {rel_path}/ (branch: {branch})")
                parts.append("```")
                parts.append(result.stdout.strip())
                parts.append("```")
                parts.append("")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue

    if len(parts) <= 2:  # Only header, no repos had commits
        return ""

    return "\n".join(parts)


# =============================================================================
# CONTEXT INJECTION HELPERS
# =============================================================================

# Hook stdout above roughly 8-10KB is NOT inlined into the conversation: the
# harness keeps a ~2KB preview and persists the remainder to a file the model
# never reads. Measured 2026-08-20 (grateplan-5iifs): 8KB inlined, 10KB
# truncated. Budget below the verified-safe floor. Before this cap the injector
# ran a 22.7KB median and a 343KB max, so most of what it built was discarded
# silently -- the hook reports success either way.
INJECTED_CONTEXT_BUDGET = 8000
LAST_SESSION_BUDGET = 4000

# Per-section budgets (grateplan-7wlmq, measured 2026-08-27).
#
# WHY THESE EXIST. The prefix competes with last-session for one 8000-char
# budget, so the REAL prefix allowance is ~4000. Measured on a live repo the
# injector assembled 23,791 chars: Recent Commits alone took 2,614 (20 commits)
# and the .meridian/docs index took 10,060, so the two cheapest-to-reproduce
# sections consumed the whole allowance between them and everything after
# position 8,000 -- the operating manual, SOUL.md and WORKSPACE.md -- was built
# from disk and silently discarded. Given the pre-cap 22.7KB median noted above,
# those three have plausibly NEVER been delivered.
#
# Truncating the tail also cut the docs index mid-list, which is worse than
# terse: a partial index looks complete, so the agent cannot tell that six more
# docs exist. Hence DOCS_ENTRY_* trims each ENTRY and never drops one.
DOCS_INDEX_BUDGET = 2200        # .meridian/docs — the routing table
API_DOCS_INDEX_BUDGET = 400     # api-docs — mostly bare filenames
DOCS_ENTRY_SUMMARY_BUDGET = 90  # prose; the read_when hints do the routing
DOCS_ENTRY_READWHEN_BUDGET = 150
RECENT_COMMITS_COUNT = 6        # `git log` is one command away


def _tail_within(text: str, budget: int) -> str:
    """Trim TEXT to the last BUDGET chars, marking what was dropped.

    Tail, not head: "pick up where you left off" wants the most recent dialogue.
    The marker matters as much as the trim -- silent truncation is what made the
    original bug invisible.
    """
    if len(text) <= budget:
        return text
    kept = text[-budget:]
    nl = kept.find("\n")
    if nl != -1:
        kept = kept[nl + 1:]
    return f"[... {len(text) - len(kept)} earlier chars omitted ...]\n{kept}"

def build_injected_context(base_dir: Path, source: str = "startup") -> tuple[str, dict]:
    """Build the full injected context string with XML-wrapped file contents.

    Args:
        base_dir: Base directory of the project
        source: SessionStart source (startup|clear|compact|resume). Network
            lookups run only on startup -- SessionStart blocks, and on
            compact/clear the conversation already carries its PR state.

    Returns:
        Tuple of (context_string, metadata_dict) where metadata tracks what was injected.
        Metadata keys: workspace, docs, api_docs, last_session, plan, pebble,
        manual, soul, nested_repos, errors.
    """
    parts = []
    meta: dict = {
        "workspace": False,
        "docs": 0,
        "api_docs": 0,
        "last_session": False,
        "pebble": False,
        "manual": False,
        "soul": False,
        "nested_repos": 0,
        "errors": [],
    }

    # Header
    parts.append("<injected-project-context>")
    parts.append("")

    # Current datetime
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts.append(f"**Current datetime:** {now}")
    parts.append("")

    # Git/PR state is assembled here but EMITTED LATER, after the docs index.
    # Ordering is load-bearing: the prefix shares one budget with last-session,
    # so whatever sits last is what the outer trim sacrifices. Commits and PRs
    # are one `git log` / `gh pr list` away, while the docs index is the only
    # record of WHICH docs exist and when to read them -- so the reproducible
    # sections go last and absorb the trim (grateplan-7wlmq).
    git_parts: list[str] = []

    # Uncommitted changes (git diff --stat)
    try:
        result = subprocess.run(
            ["git", "diff", "--stat"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(base_dir)
        )
        if result.returncode == 0 and result.stdout.strip():
            git_parts.append("## Uncommitted Changes")
            git_parts.append("```")
            git_parts.append(result.stdout.strip())
            git_parts.append("```")
            git_parts.append("")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Recent commits (user's only, all branches, with branch decoration and relative time)
    try:
        # Get current user's email for filtering
        user_email_result = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(base_dir)
        )
        user_email = user_email_result.stdout.strip() if user_email_result.returncode == 0 else None

        cmd = ["git", "log", "--format=%h%d %s (%cr)", f"-{RECENT_COMMITS_COUNT}", "--all"]
        if user_email:
            cmd.append(f"--author={user_email}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(base_dir)
        )
        if result.returncode == 0 and result.stdout.strip():
            git_parts.append("## Recent Commits")
            git_parts.append("```")
            git_parts.append(result.stdout.strip())
            git_parts.append("```")
            git_parts.append("")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Nested git repositories
    nested_context = scan_nested_git_repos(base_dir)
    if nested_context:
        # Count repos by counting "### " headers in the output
        meta["nested_repos"] = nested_context.count("### ")
        git_parts.append(nested_context)
        git_parts.append("")

    # Network lookups: startup only. SessionStart BLOCKS, these are two
    # gh round-trips at timeout=10 each, and SessionStart also fires on every
    # compact -- 200 of 240 fires in the grateplan-8c6jf window. On
    # compact/clear the conversation already knows its PR state.
    if source == "startup":
        # Recent PRs (open, with authors)
        try:
            result = subprocess.run(
                ["gh", "pr", "list", "--state", "open", "--author", "@me", "--limit", "5",
                 "--json", "number,title,author,headRefName",
                 "--template", '{{range .}}#{{.number}} {{.title}} ({{.author.login}}) [{{.headRefName}}]\n{{end}}'],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(base_dir)
            )
            if result.returncode == 0 and result.stdout.strip():
                git_parts.append("## Open PRs")
                git_parts.append("```")
                git_parts.append(result.stdout.strip())
                git_parts.append("```")
                git_parts.append("")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

        # Recent PRs (merged, with authors)
        try:
            result = subprocess.run(
                ["gh", "pr", "list", "--state", "merged", "--author", "@me", "--limit", "5",
                 "--json", "number,title,author,mergedAt",
                 "--template", '{{range .}}#{{.number}} {{.title}} ({{.author.login}}) merged {{timeago .mergedAt}}\n{{end}}'],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(base_dir)
            )
            if result.returncode == 0 and result.stdout.strip():
                git_parts.append("## Recently Merged PRs")
                git_parts.append("```")
                git_parts.append(result.stdout.strip())
                git_parts.append("```")
                git_parts.append("")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    # Get project config for addons and pebble
    project_config = get_project_config(base_dir)

    # Documentation directories — scan for frontmatter summaries
    doc_dirs = [
        (".meridian/api-docs", "External API docs. Read the relevant doc before using any listed API."),
        (".meridian/docs", "Project documentation. Read relevant docs when your task matches a hint below."),
    ]

    # Add extra doc dirs from config
    doc_dirs.extend(get_extra_doc_dirs(project_config))

    any_docs = False
    for dir_rel, header in doc_dirs:
        # api-docs are mostly bare filenames; the project docs carry the routing.
        dir_budget = (API_DOCS_INDEX_BUDGET if dir_rel == ".meridian/api-docs"
                      else DOCS_INDEX_BUDGET)
        listing = scan_docs_directory(base_dir / dir_rel, base_dir, budget=dir_budget)
        if listing:
            any_docs = True
            # Count docs in this listing (each doc starts with "- **")
            doc_count = listing.count("\n- **") + (1 if listing.startswith("- **") else 0)
            if dir_rel == ".meridian/api-docs":
                meta["api_docs"] += doc_count
            else:
                meta["docs"] += doc_count
            parts.append(f"**{header}**")
            parts.append(f"<docs-index dir=\"{dir_rel}\">")
            parts.append(listing)
            parts.append("</docs-index>")
            parts.append("")
    if any_docs:
        parts.append("When your task matches a \"Read when\" hint above, read that doc before coding. When you make changes that affect a documented topic, update the doc. When you discover something worth preserving — a decision, a gotcha, a new integration — create a new doc in `.meridian/docs/` with frontmatter (`summary`, `read_when`). Documentation is part of the work, not an afterthought.")
        parts.append("")

    # Reproducible sections last -- see the git_parts note above.
    parts.extend(git_parts)

    # Pebble live context (if enabled)
    if project_config.get('pebble_enabled', False):
        # Pebble rules (behavioral — must be followed when Pebble is active)
        # Check plugin root first (.meridian/prompts/ relative to repo root)
        pebble_rules_path = Path(__file__).parent.parent.parent / ".meridian" / "prompts" / "pebble-rules.md"
        if not pebble_rules_path.exists():
            # Fallback: check project directory
            pebble_rules_path = base_dir / ".meridian" / "prompts" / "pebble-rules.md"
        if pebble_rules_path.exists():
            try:
                rules_content = pebble_rules_path.read_text()
                parts.append(rules_content.rstrip())
                parts.append("")
            except IOError:
                pass

        # Get live Pebble context (in-progress, ready issues)
        pebble_context = get_pebble_context(base_dir)
        if pebble_context:
            meta["pebble"] = True
            parts.append('<pebble-context>')
            parts.append(pebble_context.rstrip())
            parts.append('</pebble-context>')
            parts.append("")

    # Agent operating manual (authoritative — follow at all times)
    manual_path = base_dir / ".meridian" / "prompts" / "agent-operating-manual.md"
    if manual_path.exists():
        try:
            content = manual_path.read_text()
            meta["manual"] = True
            parts.append("**Agent operating manual. This is authoritative — follow these procedures at all times.**")
            parts.append(f'<file path=".meridian/prompts/agent-operating-manual.md">')
            parts.append(content.rstrip())
            parts.append('</file>')
            parts.append("")
        except IOError as e:
            meta["errors"].append(f"Could not read agent-operating-manual.md: {e}")
            parts.append(f'<file path=".meridian/prompts/agent-operating-manual.md" error="Could not read file" />')
            parts.append("")

    # SOUL.md (agent identity and principles)
    soul_path = base_dir / ".meridian" / "SOUL.md"
    if soul_path.exists():
        try:
            content = soul_path.read_text()
            meta["soul"] = True
            parts.append("**Agent identity and principles. This defines who you are and how you work.**")
            parts.append(f'<file path=".meridian/SOUL.md">')
            parts.append(content.rstrip())
            parts.append('</file>')
            parts.append("")
        except IOError as e:
            meta["errors"].append(f"Could not read SOUL.md: {e}")
            pass

    # Workspace (slim current-state notepad — last for highest attention)
    workspace_path = base_dir / WORKSPACE_FILE
    if workspace_path.exists():
        try:
            content = workspace_path.read_text()
            meta["workspace"] = True
            parts.append("**Your current-state notepad. What's in progress, key decisions, and next steps. Not documentation — keep it slim.**")
            parts.append(f'<file path="{WORKSPACE_FILE}">')
            parts.append(content.rstrip())
            parts.append('</file>')
            parts.append("")
        except IOError as e:
            meta["errors"].append(f"Could not read WORKSPACE.md: {e}")
            pass

    # Last session transcript (dialogue from previous session).
    # Index recorded so the budget backstop trims the bulk ABOVE this point
    # rather than eating the most recent dialogue (grateplan-5iifs).
    protected_from = len(parts)
    last_session_path = state_path(base_dir, LAST_SESSION_FILE)
    if last_session_path.exists():
        try:
            content = last_session_path.read_text()
            if content.strip():
                meta["last_session"] = True
                meta["last_session_chars"] = len(content)
                content = _tail_within(content, LAST_SESSION_BUDGET)
                parts.append("**Previous session dialogue. Use this to understand what happened last session and pick up where you left off.**")
                parts.append('<last-session>')
                parts.append(content.rstrip())
                parts.append('</last-session>')
                parts.append("")
        except IOError as e:
            meta["errors"].append(f"Could not read last-session.md: {e}")
            pass

    # Footer
    parts.append("</injected-project-context>")

    result = "\n".join(parts)

    # Backstop. last-session is capped individually above, but any section could
    # grow; going over the budget means the harness swaps the whole payload for a
    # ~2KB preview, so an over-budget build delivers LESS than a trimmed one.
    if len(result) > INJECTED_CONTEXT_BUDGET:
        # Trim the bulk that sits ABOVE the last-session block (docs indexes, the
        # inlined operating manual, soul). Trimming the tail instead would delete
        # the previous session's dialogue first -- the single thing this injection
        # exists to carry across a compact.
        protected = "\n".join(parts[protected_from:])
        prefix = "\n".join(parts[:protected_from])
        room = INJECTED_CONTEXT_BUDGET - len(protected) - 1
        over = len(result) - INJECTED_CONTEXT_BUDGET
        if room > 200:
            marker = f"\n[... {over} chars of project context omitted: over the {INJECTED_CONTEXT_BUDGET}-char inline budget ...]"
            result = prefix[: max(0, room - len(marker))] + marker + "\n" + protected
        else:
            # last-session alone fills the budget; fall back to trimming it too
            result = _tail_within(result, INJECTED_CONTEXT_BUDGET)
        meta["truncated_chars"] = over

    return result, meta


# =============================================================================
# STOP PROMPT BUILDER
# =============================================================================

def build_stop_prompt(base_dir: Path, config: dict) -> str:
    """
    Build a checklist of tasks to complete. No mention of stopping — the agent
    should treat these as work items, not a wind-down signal.

    Args:
        base_dir: Project root directory
        config: Project config from get_project_config()

    Returns:
        The checklist prompt string
    """
    pebble_enabled = config.get('pebble_enabled', False)
    extra_items = config.get('stop_checklist_extra', [])

    parts = ["**Complete these tasks:**\n"]

    parts.append("- Run **code-health-reviewer** if you made significant code changes")

    if pebble_enabled:
        parts.append("- Close/update Pebble issues for completed work")

    parts.append("- Run tests/lint/build if you made code changes")
    parts.append("- Update relevant documentation (CLAUDE.md, docs, workspace) if you made significant changes")

    # User-configured extra items
    if isinstance(extra_items, list):
        for item in extra_items:
            if isinstance(item, str) and item.strip():
                parts.append(f"- {item.strip()}")

    # Check for uncommitted changes (suppressible for PR-based workflows
    # where the agent must never commit directly — use stop_checklist_extra
    # to phrase the project's own delivery step instead)
    if config.get('stop_checklist_commit_item', True):
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(base_dir)
            )
            if result.returncode == 0 and result.stdout.strip():
                changed_files = len([l for l in result.stdout.strip().split('\n') if l])
                parts.append(f"- Commit {changed_files} uncommitted file{'s' if changed_files != 1 else ''}")
        except Exception:
            pass

    parts.append("")
    parts.append("Skip items you already completed. Do the rest now.")

    return "\n".join(parts)
