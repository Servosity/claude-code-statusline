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

# --- Tunables -----------------------------------------------------------------
BAR_WIDTH = 26
DEFAULT_CONTEXT_WINDOW = 200_000

# Context bar = a bracketed gradient scaled to the real window. Filled cells use the
# bright ramp; the empty track shows a dim "heat-ahead" version of the same ramp, so the
# whole bar reads as ONE window (no tick dividers). The gradient is anchored to the
# 120k/180k/300k/500k thresholds: green@0 -> yellow@120k -> orange@180k -> red@300k ->
# deep red@500k+. Retune GRAD_ANCHORS to move the color stops.
GRAD_RAMP  = [46, 82, 118, 154, 190, 226, 220, 214, 208, 202, 196, 160]
GRAD_MUTED = [22, 28, 64, 100, 100, 58, 94, 94, 130, 88, 88, 52]
GRAD_ANCHORS = [(0, 0.0), (120_000, 5/11), (180_000, 8/11), (300_000, 10/11), (500_000, 1.0)]
# -----------------------------------------------------------------------------

def read_json_stdin():
    try:
        return json.load(sys.stdin)
    except:
        return {}

def format_tokens(n):
    """Format a token count: M for >=1M (1M, 4.2M), else k (45.2k, 145k, 200k)."""
    n = max(0, n)
    if n >= 1_000_000:
        m = n / 1_000_000
        return f"{m:.0f}M" if abs(m - round(m)) < 0.05 else f"{m:.1f}M"
    k = n / 1000
    if k >= 100:
        return f"{k:.0f}k"  # No decimal for 100k+
    return f"{k:.1f}k"

def fmt_duration(ms):
    """Format milliseconds as 'Xh Ym' / 'Ym Zs' / 'Zs'. None on bad input."""
    try:
        total = int(ms / 1000)
    except (TypeError, ValueError):
        return None
    if total < 0:
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def _grad_frac(tokens):
    """Map a token position to a 0..1 fraction along the gradient, piecewise-linear
    between GRAD_ANCHORS (so the spectrum hits its stops at the 120/180/300/500k marks)."""
    if tokens <= 0:
        return 0.0
    if tokens >= GRAD_ANCHORS[-1][0]:
        return 1.0
    for i in range(1, len(GRAD_ANCHORS)):
        t0, f0 = GRAD_ANCHORS[i - 1]
        t1, f1 = GRAD_ANCHORS[i]
        if tokens <= t1:
            r = (tokens - t0) / (t1 - t0) if t1 > t0 else 0
            return f0 + r * (f1 - f0)
    return 1.0

def grad_color(tokens):
    """Bright gradient escape for a token position (filled cells + the percentage)."""
    return f"\033[38;5;{GRAD_RAMP[round(_grad_frac(tokens) * (len(GRAD_RAMP) - 1))]}m"

def muted_color(tokens):
    """Dim 'heat-ahead' gradient escape for the empty portion of the track."""
    return f"\033[38;5;{GRAD_MUTED[round(_grad_frac(tokens) * (len(GRAD_MUTED) - 1))]}m"

def detect_context_window(input_data):
    """Real context window: prefer the JSON field, fall back to a 1M id marker, else 200k."""
    cw = input_data.get('context_window') or {}
    size = cw.get('context_window_size')
    if isinstance(size, int) and size > 0:
        return size
    model_id = (input_data.get('model') or {}).get('id', '') or ''
    if '1m' in model_id.lower():
        return 1_000_000
    return DEFAULT_CONTEXT_WINDOW

def get_git_branch(cwd_path):
    """Get current git branch if in a git repo"""
    try:
        result = subprocess.run(
            ['git', '-C', cwd_path, 'branch', '--show-current'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
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

def is_tool_result_msg(msg):
    """A user-role message carrying tool_result blocks is a tool RETURN, not a human
    turn — the gap before it was tool execution (a CI watch, an agent run), not you
    being waited on. Used to keep that time in the 'active' total."""
    content = msg.get('content')
    if isinstance(content, list):
        return any(isinstance(i, dict) and i.get('type') == 'tool_result' for i in content)
    return False

def scan_transcript(transcript_path):
    """Single pass over the transcript.

    Returns (current_main_usage, cum_in, cum_out, active_ms):
      current_main_usage = newest real non-sidechain assistant usage -> the bar numerator
                           (current context occupancy).
      cum_in / cum_out   = session tokens IN (input+cache_read+cache_creation) and OUT
                           (output), summed across EVERY message INCLUDING sidechains
                           (subagent/Task turns). Cache reads recur each turn by design,
                           so cum_in is a processed total, not a unique-token count.
                           Deduped by API message id: the transcript logs the same
                           assistant response on multiple lines (streaming partials +
                           final), so a naive sum over-counts several-fold.
      active_ms          = wall-clock minus the gaps where Claude finished and was
                           waiting on YOU. Tool execution, background agents, and CI/CD
                           watches all happen inside a turn, so they ARE counted; only
                           the assistant-done -> your-next-message idle is excluded.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return None, 0, 0, 0

    try:
        with open(transcript_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except:
        return None, 0, 0, 0

    latest_ts = float('-inf')
    latest_usage = None
    cum_in = 0
    cum_out = 0
    seen_ids = set()
    timeline = []  # (ts, role, is_tool_result) for main-thread msgs -> active_ms

    for line in lines:
        line = line.strip()
        if not line:
            continue

        try:
            j = json.loads(line)
        except:
            continue

        msg = j.get('message', {})
        usage = msg.get('usage')

        # Active-time timeline: main-thread (non-sidechain) messages, in time order.
        # Subagent wall-time is already captured as the main-thread gap between the
        # Task tool_use and its tool_result, so sidechain lines are skipped here.
        if not is_sub_context(j):
            t = parse_timestamp(j)
            if t != float('-inf'):
                timeline.append((t, msg.get('role'), is_tool_result_msg(msg)))

        # Cumulative in/out: count EVERY usage (incl. subagent sidechains) ONCE.
        # Dedup by the API message id (fall back to the wrapper uuid) so streaming
        # partials of the same response don't multiply the totals.
        if usage:
            mid = msg.get('id') or j.get('uuid')
            if mid not in seen_ids:
                seen_ids.add(mid)
                cum_in += (
                    usage.get('input_tokens', 0) +
                    usage.get('cache_read_input_tokens', 0) +
                    usage.get('cache_creation_input_tokens', 0)
                )
                cum_out += usage.get('output_tokens', 0)

        # Bar numerator: newest real main-context assistant usage only.
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

    # Active time: sum gaps, excluding "assistant finished -> you send the next
    # message" (a genuine user turn, not a tool_result). That idle is the only thing
    # dropped; API generation + tool/agent/CI time between events all count.
    timeline.sort(key=lambda x: x[0])
    active_ms = 0
    for (ta, ra, _), (tb, rb, tr_b) in zip(timeline, timeline[1:]):
        if ra == 'assistant' and rb == 'user' and not tr_b:
            continue  # waiting on you — don't count it
        active_ms += (tb - ta) * 1000
    active_ms = int(active_ms)

    return latest_usage, cum_in, cum_out, active_ms

def render_bar(used, window):
    """Bracketed gradient bar scaled to the real window. Filled cells use the bright
    gradient; the empty track shows a dim 'heat-ahead' gradient so the full spectrum
    reads as ONE continuous window. No tick glyphs — the gradient itself marks the
    120k/180k/300k/500k zones."""
    if window <= 0:
        window = DEFAULT_CONTEXT_WINDOW
    filled = int((used / window) * BAR_WIDTH)
    filled = max(0, min(BAR_WIDTH, filled))

    reset = "\033[0m"
    edge = "\033[38;5;244m"

    cells = []
    for i in range(BAR_WIDTH):
        tok = (i + 0.5) / BAR_WIDTH * window  # token position at the cell's center
        if i < filled:
            cells.append(f"{grad_color(tok)}█{reset}")
        else:
            cells.append(f"{muted_color(tok)}░{reset}")
    return f"{edge}[{reset}" + "".join(cells) + f"{edge}]{reset}"

def build_stats(input_data, cum_in, cum_out, active_ms):
    """Returns the cost / tokens / active-time segments that have values (else omitted)."""
    cost = input_data.get('cost') or {}
    segs = []

    # Cost: Claude Code's own client-side figure (cost.total_cost_usd). It already
    # accounts for per-model pricing, cache reads/writes, and input vs output — so we
    # display it as-is rather than recomputing from a (rot-prone) local price table.
    c = cost.get('total_cost_usd')
    if isinstance(c, (int, float)):
        segs.append(f"\033[32m${c:.2f}\033[0m")

    # Tokens in / out for the session. cum_in is cache-inclusive input; cum_out is
    # generated output. Both are deduped in scan_transcript (the raw cumulative
    # double-counts streamed partials, which is what made the old "tok" read high).
    if cum_in > 0 or cum_out > 0:
        segs.append(
            f"\033[36m↑{format_tokens(cum_in)} ↓{format_tokens(cum_out)}\033[0m"
        )

    # Time: ACTIVE time (transcript-derived) = wall-clock minus time spent waiting on
    # you. It counts API generation + tool execution + background agents + CI/CD
    # watches, and excludes the idle gap while Claude waits for your next message — so
    # a prompt left open overnight doesn't inflate it. Fall back to the cost block's
    # API time, then wall-clock, when the transcript isn't available.
    t_ms = active_ms if active_ms and active_ms > 0 else None
    if t_ms is None:
        t_ms = cost.get('total_api_duration_ms')
    if t_ms is None:
        t_ms = cost.get('total_duration_ms')
    d = fmt_duration(t_ms) if t_ms is not None else None
    if d:
        segs.append(f"\033[90m{d}\033[0m")

    return segs

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
    window = detect_context_window(input_data)

    # One transcript read -> current context usage + cumulative in/out + active time.
    usage, cum_in, cum_out, active_ms = scan_transcript(transcript_path)

    sep = " \033[90m|\033[0m "
    stats = build_stats(input_data, cum_in, cum_out, active_ms)

    # Line 1 (unchanged): model | folder | git | clock
    parts = [
        f"{model_icon} {model_name}",
        cwd_display,
        git_display,
        f"⏱️  {time_display}"
    ]
    status_line = " | ".join(p for p in parts if p)
    print(f"{status_line}")

    if not usage:
        # No assistant usage yet: still show whatever stats exist alongside the note.
        note = "\033[36mcontext usage starts after first response\033[0m"
        if stats:
            print(note + sep + sep.join(stats))
        else:
            print(note)
        return

    # Bar (scaled to the real window) + percentage colored by band.
    used = used_total(usage)
    pct = (used / window * 100) if window > 0 else 0
    bar = render_bar(used, window)
    usage_display = (
        f"{bar} \033[33m{format_tokens(used)}/{format_tokens(window)}\033[0m "
        f"{grad_color(used)}{pct:.1f}%\033[0m"
    )

    line2 = f"context: {usage_display}"
    if stats:
        line2 += sep + sep.join(stats)
    print(line2)

if __name__ == "__main__":
    main()
