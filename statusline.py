#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# ///
import sys
import json
import os
import subprocess
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CONTEXT_WINDOW = 200_000

def read_json_stdin():
    try:
        return json.load(sys.stdin)
    except:
        return {}

def format_k(n):
    """Format number in .1k units (e.g., 24.9k, 200k)"""
    n = max(0, n)
    k = n / 1000
    if k >= 100:
        return f"{k:.0f}k"  # No decimal for 100k+
    else:
        return f"{k:.1f}k"

def color(pct):
    if pct >= 90:
        return "\033[31m"  # red
    elif pct >= 70:
        return "\033[33m"  # yellow
    else:
        return "\033[32m"  # green

def get_git_branch(cwd_path):
    """Get current git branch if in a git repo"""
    try:
        result = subprocess.run(
            ['git', '-C', cwd_path, 'branch', '--show-current'],
            capture_output=True,
            text=True,
            timeout=1
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            return f"\033[92m{branch}\033[0m" if branch else None
    except:
        pass
    return None

def used_total(usage):
    if not usage:
        return 0
    # Total input context = non-cached input + cached input (both read and created)
    # With prompt caching, input_tokens only shows NON-cached portion (often near 0)
    # cache_read_input_tokens = tokens served from cache
    # cache_creation_input_tokens = tokens being added to cache
    # Do NOT include output_tokens - those are generated, not context
    return (
        usage.get('input_tokens', 0) +
        usage.get('cache_read_input_tokens', 0) +
        usage.get('cache_creation_input_tokens', 0)
    )

def is_synthetic_model(j):
    model = j.get('message', {}).get('model', '').lower()
    return model == '<synthetic>' or 'synthetic' in model

def is_assistant_message(j):
    return j.get('message', {}).get('role') == 'assistant'

def is_sub_context(j):
    return j.get('isSidechain') == True

def has_no_response_content(j):
    content = j.get('message', {}).get('content', [])
    if isinstance(content, list):
        for item in content:
            if item and item.get('type') == 'text':
                text = str(item.get('text', ''))
                if 'no response requested' in text.lower():
                    return True
    return False

def parse_timestamp(j):
    ts = j.get('timestamp')
    if ts:
        try:
            from datetime import datetime
            return datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp()
        except:
            pass
    return float('-inf')

def newest_main_usage_by_timestamp(transcript_path):
    if not transcript_path or not os.path.exists(transcript_path):
        return None

    latest_ts = float('-inf')
    latest_usage = None

    try:
        with open(transcript_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except:
        return None

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue

        try:
            j = json.loads(line)
        except:
            continue

        usage = j.get('message', {}).get('usage')

        if (is_sub_context(j) or
            is_synthetic_model(j) or
            j.get('isApiErrorMessage') == True or
            used_total(usage) == 0 or
            has_no_response_content(j) or
            not is_assistant_message(j)):
            continue

        ts = parse_timestamp(j)
        if ts > latest_ts:
            latest_ts = ts
            latest_usage = usage
        elif ts == latest_ts and used_total(usage) > used_total(latest_usage):
            latest_usage = usage

    return latest_usage

def main():
    input_data = read_json_stdin()

    # Get model info
    model = input_data.get('model', {})
    model_name = f"\033[95m{model.get('display_name', 'Claude')}\033[0m"
    model_id = model.get('id', '')
    if 'sonnet' in model_id.lower():
        model_icon = "🧠"
    elif 'opus' in model_id.lower():
        model_icon = "🚀"
    elif 'haiku' in model_id.lower():
        model_icon = "⚡"
    else:
        model_icon = "🤖"

    # Get workspace info
    workspace = input_data.get('workspace', {})
    cwd_path = workspace.get('current_dir', os.getcwd())
    cwd = os.path.basename(cwd_path) if cwd_path else ''
    cwd_display = f"📁 \033[36m{cwd}\033[0m" if cwd else ""

    # Get git branch
    git_branch = get_git_branch(cwd_path) if cwd_path else None
    git_display = f"({git_branch})" if git_branch else ""

    # Get timestamp. Build the day separately so we avoid %-d (POSIX) / %#d (Windows).
    _now = datetime.now()
    now = f"{_now.strftime('%b')} {_now.day} {_now.strftime('%H:%M:%S')}"
    time_display = f"\033[90m{now}\033[0m"

    transcript_path = input_data.get('transcript_path')

    # Get usage from transcript
    usage = newest_main_usage_by_timestamp(transcript_path)

    if not usage:
        # Initial status line before first response
        parts = [
            f"{model_icon} {model_name}",
            cwd_display,
            git_display,
            f"⏱️  {time_display}"
        ]
        status_line = " | ".join(p for p in parts if p)
        print(f"{status_line}")
        print(f"\033[36mcontext usage starts after first response\033[0m")
        return

    # Calculate usage
    used = used_total(usage)
    pct = round((used * 1000) / CONTEXT_WINDOW) / 10 if CONTEXT_WINDOW > 0 else 0

    # Usage display with progress bar
    bar_width = 20
    filled = int((pct / 100) * bar_width)
    bar = "█" * filled + "░" * (bar_width - filled)
    usage_display = f"{color(pct)}{pct:.1f}%\033[0m {bar} \033[33m{format_k(used)}/{format_k(CONTEXT_WINDOW)}\033[0m"

    # Build status line
    parts = [
        f"{model_icon} {model_name}",
        cwd_display,
        git_display,
        f"⏱️  {time_display}"
    ]
    status_line = " | ".join(p for p in parts if p)

    print(f"{status_line}")
    print(f"context: {usage_display}")

if __name__ == "__main__":
    main()