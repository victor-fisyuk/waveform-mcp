"""MCP server for RTL waveform analysis using WAL (Waveform Analysis Language).

This server provides tools for analyzing waveform files from RTL simulations,
allowing LLMs to inspect signals, detect transitions, and debug hardware designs.

Supported formats: VCD, FST (via WAL)
"""

import asyncio
import io
import logging
import os
from contextlib import redirect_stdout
from typing import Any, Dict, List, Tuple
import re
from difflib import get_close_matches

from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.server.lowlevel import NotificationOptions
from mcp.types import TextContent, Tool

from wal.core import TraceContainer
from wal.eval import SEval
from wal.core import read_wal_sexpr

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = Server("waveform-mcp")

# Cache: {file_path: (modification_time, TraceContainer)}
_waveform_cache: Dict[str, Tuple[float, TraceContainer]] = {}

# Persistent FST callback storage to prevent memory leaks
# These are created once and reused across all calls to _get_fst_signal_transitions_direct
# Each call passes its own transition list via user_data (ffi.new_handle)
_fst_callbacks: Dict[str, Any] = {
    "on_change": None,
    "on_change_var": None,
}

# WAL Documentation and Examples
WAL_DOCUMENTATION = {
    "overview": """
WAL (Waveform Analysis Language) - Quick Reference

WAL is a functional programming language designed for waveform analysis with Lisp-like syntax.
All expressions use parentheses: (function arg1 arg2 ...)

Key Concepts:
• Signals: Access by name (e.g., 'clk', 'tb.counter')
• Time: Navigate with (step N) or use INDEX for current time
• Lists: Most operations return lists of values/times
• Conditions: Use for filtering and searching
""",
    "functions": """
Core WAL Functions for Waveform Analysis:

TIME & NAVIGATION:
• (step N) - Move N steps forward in time
• INDEX - Current time index
• (find condition) - Find all times where condition is true

SIGNAL ACCESS:
• SIGNALS - List of all signal names
• signal_name - Access signal values at current time
• (length signal_or_list) - Get length of signal timeline or list

SEARCH & FILTER:
• (find condition) - Returns list of time indices where condition is true
• (count condition) - Count number of times condition is true
• (= signal value) - Test if signal equals value
• (!= signal value) - Test if signal not equal to value
• (> signal value) - Test if signal greater than value
• (< signal value) - Test if signal less than value

LOGICAL OPERATIONS:
• (&& cond1 cond2 ...) - Logical AND
• (|| cond1 cond2 ...) - Logical OR
• Note: 'and', 'or', 'not' are not available in this WAL implementation

ARITHMETIC:
• (+ arg1 arg2 ...) - Addition
• (- arg1 arg2 ...) - Subtraction
• (* arg1 arg2 ...) - Multiplication
• (/ arg1 arg2 ...) - Division
""",
    "examples": """
WAL Usage Examples:

BASIC SIGNAL ACCESS:
• SIGNALS - List all signals
• clk - Get clock value at current time
• (step 10) - Move 10 time steps forward

TIME & COUNTING:
• (length (find true)) - Total simulation length
• (count (= clk 1)) - Count clock high periods
• (count (= reset 0)) - Count time steps where reset is low

SIGNAL TRANSITIONS:
• (find (= clk 1)) - Find times when clock is high
• (find (&& (= clk 0) (= data 1))) - Find times when clk=0 AND data=1
• (find (|| (= sig1 1) (= sig2 1))) - Find times when either signal is high

COMPLEX CONDITIONS:
• (find (> counter 10)) - Find times when counter > 10
• (find (&& (= clk 1) (> counter 5))) - Find clk high with counter > 5
• (length (find (= state 3))) - How long was state = 3

DEBUGGING PATTERNS:
• (find (= overflow 1)) - Find overflow events
• (find (&& (= valid 1) (= ready 0))) - Find handshake violations
• Note: WAL != operator syntax varies by implementation

MULTI-STEP ANALYSIS:
• (step 0) (find (= reset 1)) - Go to start, find reset assertion times
• (length SIGNALS) - Number of signals in waveform
""",
    "debugging": """
Common WAL Debugging Patterns:

PROTOCOL ANALYSIS:
• Handshake: (find (&& (= valid 1) (= ready 0))) - Stalled transactions
• Bus idle: (find (&& (= valid 0) (= ready 1))) - Ready but no data
• State machines: (find (= state target_state)) - Time in specific state

TIMING ANALYSIS:
• Clock analysis: (length (find (= clk 1))) - Count clock high periods
• Pulse width: Use find with consecutive conditions
• Frequency: (/ (length (find true)) (length (find (= clk 1)))) - Approximate period

SIGNAL VALIDATION:
• Unknown states: (find (= signal 'x')) - Find X states (if supported)
• Range check: (find (> signal max_value)) - Values out of range  
• Constant check: (count (!= signal expected)) - Non-constant periods

MEMORY/COUNTER ANALYSIS:
• Overflow: (find (&& (= counter 15) (= overflow 0))) - Missing overflow flag
• Increment: (find (!= counter (+ (prev counter) 1))) - Non-sequential counts
• Reset behavior: (find (&& (= reset 1) (!= counter 0))) - Reset failures

ERROR DETECTION:
• Glitches: Look for very short pulses
• Race conditions: Multiple signals changing simultaneously
• Protocol violations: Invalid state combinations
""",
    "syntax": """
WAL Syntax Reference:

BASIC SYNTAX:
• Parentheses required: (function arg1 arg2)
• Comments: ; This is a comment
• Numbers: 123, 0xFF (hex), 0b1010 (binary)
• Strings: "text" or text without spaces
• Booleans: #t (true), #f (false)

FUNCTION CALLS:
• (function) - No arguments
• (function arg) - One argument  
• (function arg1 arg2 arg3) - Multiple arguments

OPERATORS:
• Arithmetic: + - * / ** (power)
• Comparison: = != < > <= >=
• Logical: && || (note: 'and', 'or', 'not' are NOT available)
• List: length, nth (if available)

VARIABLES:
• SIGNALS - Built-in list of signal names
• INDEX - Built-in current time index
• signal_name - Direct signal access

CONTROL FLOW:
• (if condition then else) - Conditional
• (let ((var value)) body) - Local variables (if supported)

COMMON PATTERNS:
• (function (condition signal value)) - Nested conditions
• (operation (find condition)) - Apply operation to search results
• (length (find condition)) - Count matching conditions
""",
}


@app.list_tools()
async def list_tools() -> List[Tool]:
    """Return list of available waveform analysis tools."""
    return [
        Tool(
            name="get_signal_list",
            description="Get hierarchical list of signals from waveform file",
            inputSchema={
                "type": "object",
                "properties": {
                    "waveform_file": {
                        "type": "string",
                        "description": "Path to waveform file (.vcd, .fst, etc.)",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Optional regex pattern to filter signals (e.g., 'cpu.*', 'top\\.m1\\.*')",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of signals to return (default: 100)",
                        "default": 100,
                    },
                },
                "required": ["waveform_file"],
            },
        ),
        Tool(
            name="get_signal_transitions",
            description="Get signal transitions within a time range",
            inputSchema={
                "type": "object",
                "properties": {
                    "waveform_file": {
                        "type": "string",
                        "description": "Path to waveform file",
                    },
                    "signal_name": {
                        "type": "string",
                        "description": "Full signal name (e.g., 'cpu.pc')",
                    },
                    "start_time": {
                        "type": "integer",
                        "description": "Start time step index (0-based)",
                        "default": 0,
                    },
                    "end_time": {
                        "type": "integer",
                        "description": "End time step index (0 = end of simulation)",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of transitions to return (default: 10)",
                        "default": 10,
                    },
                },
                "required": ["waveform_file", "signal_name"],
            },
        ),
        Tool(
            name="get_waveform_length",
            description="Get the length of the waveform file",
            inputSchema={
                "type": "object",
                "properties": {
                    "waveform_file": {
                        "type": "string",
                        "description": "Path to waveform file",
                    },
                },
                "required": ["waveform_file"],
            },
        ),
        Tool(
            name="get_wal_help",
            description="Get WAL (Waveform Analysis Language) documentation and examples",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Help topic: 'overview', 'functions', 'examples', 'debugging', 'syntax'",
                        "default": "overview",
                    },
                },
            },
        ),
        Tool(
            name="get_wal_examples",
            description="Get WAL examples customized for specific waveform signals",
            inputSchema={
                "type": "object",
                "properties": {
                    "waveform_file": {
                        "type": "string",
                        "description": "Path to waveform file",
                    },
                },
                "required": ["waveform_file"],
            },
        ),
        Tool(
            name="get_signal_overview",
            description="Get an overview of signal behavior patterns, identifying constant-value intervals and high-activity bursts. Supports conditional analysis where signal behavior is only analyzed when a qualifying signal (e.g., 'valid', 'enable', 'de') equals a specified value. IMPORTANT: In most cases you should provide a condition_signal (e.g., 'valid', 'enable', 'de') to get meaningful results, as data signals are typically only valid when their enable/valid signal is active.",
            inputSchema={
                "type": "object",
                "properties": {
                    "waveform_file": {
                        "type": "string",
                        "description": "Path to waveform file (FST format)",
                    },
                    "signal_name": {
                        "type": "string",
                        "description": "Signal name to analyze",
                    },
                    "start_step": {
                        "type": "integer",
                        "description": "Start step index (0-based)",
                        "default": 0,
                    },
                    "end_step": {
                        "type": "integer",
                        "description": "End step index (0 = end of simulation)",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of intervals to return (default: 20)",
                        "default": 20,
                    },
                    "condition_signal": {
                        "type": "string",
                        "description": "Optional qualifying signal name (e.g., 'valid', 'enable', 'de'). When specified, only analyze the main signal when this condition signal equals condition_value.",
                    },
                    "condition_value": {
                        "type": "integer",
                        "description": "Value the condition signal must have for analysis (default: 1)",
                        "default": 1,
                    },
                },
                "required": ["waveform_file", "signal_name"],
            },
        ),
        Tool(
            name="get_signal_values",
            description="Get multiple signals' values at a specific time step for correlation analysis",
            inputSchema={
                "type": "object",
                "properties": {
                    "waveform_file": {
                        "type": "string",
                        "description": "Path to waveform file (.vcd, .fst, etc.)",
                    },
                    "step": {
                        "type": "integer",
                        "description": "Time step index to sample at (0-based)",
                    },
                    "signal_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Array of signal names to sample",
                    },
                },
                "required": ["waveform_file", "step", "signal_names"],
            },
        ),
        Tool(
            name="get_signal_statistics",
            description="Calculate min/max/average statistics for a signal over a time range",
            inputSchema={
                "type": "object",
                "properties": {
                    "waveform_file": {
                        "type": "string",
                        "description": "Path to waveform file (.vcd, .fst, etc.)",
                    },
                    "signal_name": {
                        "type": "string",
                        "description": "Signal name to analyze",
                    },
                    "start_time": {
                        "type": "integer",
                        "description": "Start time step index (0-based)",
                        "default": 0,
                    },
                    "end_time": {
                        "type": "integer",
                        "description": "End time step index (0 = end of simulation)",
                        "default": 0,
                    },
                },
                "required": ["waveform_file", "signal_name"],
            },
        ),
        Tool(
            name="extract_bit_field",
            description="Extract specific bits from a signal. Supports single-step extraction or transition tracking over a range.",
            inputSchema={
                "type": "object",
                "properties": {
                    "waveform_file": {
                        "type": "string",
                        "description": "Path to waveform file (.vcd, .fst, etc.)",
                    },
                    "signal_name": {
                        "type": "string",
                        "description": "Signal name to extract bits from",
                    },
                    "high_bit": {
                        "type": "integer",
                        "description": "High bit index (inclusive, 0-based)",
                    },
                    "low_bit": {
                        "type": "integer",
                        "description": "Low bit index (inclusive, 0-based)",
                    },
                    "step": {
                        "type": "integer",
                        "description": "Single step to extract at (if provided, returns single value instead of transitions)",
                    },
                    "start_time": {
                        "type": "integer",
                        "description": "Start time step index for transition tracking (0-based)",
                        "default": 0,
                    },
                    "end_time": {
                        "type": "integer",
                        "description": "End time step index for transition tracking (0 = end of simulation)",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of transitions to return (default: 10)",
                        "default": 10,
                    },
                },
                "required": ["waveform_file", "signal_name", "high_bit", "low_bit"],
            },
        ),
        Tool(
            name="find_signal_value",
            description="Find time steps where a signal has a specific value within a time range",
            inputSchema={
                "type": "object",
                "properties": {
                    "waveform_file": {
                        "type": "string",
                        "description": "Path to waveform file (.vcd, .fst, etc.)",
                    },
                    "signal_name": {
                        "type": "string",
                        "description": "Signal name to search",
                    },
                    "value": {
                        "type": "integer",
                        "description": "Value to search for",
                    },
                    "start_time": {
                        "type": "integer",
                        "description": "Start time step index (0-based)",
                        "default": 0,
                    },
                    "end_time": {
                        "type": "integer",
                        "description": "End time step index (0 = end of simulation)",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of matches to return (default: 10)",
                        "default": 10,
                    },
                },
                "required": ["waveform_file", "signal_name", "value"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(tool_name: str, arguments: Dict[str, Any]):
    """Route tool calls to appropriate handlers."""
    try:
        if tool_name == "get_signal_list":
            return await _get_signal_list(arguments)
        elif tool_name == "get_signal_transitions":
            return await _get_signal_transitions(arguments)
        elif tool_name == "get_waveform_length":
            return await _get_waveform_length(arguments)
        elif tool_name == "execute_wal_expression":
            return await _execute_wal_expression(arguments)
        elif tool_name == "get_wal_help":
            return await _get_wal_help(arguments)
        elif tool_name == "get_wal_examples":
            return await _get_wal_examples(arguments)
        elif tool_name == "get_signal_overview":
            return await _get_signal_overview(arguments)
        elif tool_name == "get_signal_values":
            return await _get_signal_values(arguments)
        elif tool_name == "get_signal_statistics":
            return await _get_signal_statistics(arguments)
        elif tool_name == "extract_bit_field":
            return await _extract_bit_field(arguments)
        elif tool_name == "find_signal_value":
            return await _find_signal_value(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {tool_name}")]
    except Exception as e:
        logger.error(f"Error in {tool_name}: {e}")
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def _load_waveform(waveform_file: str) -> TraceContainer:
    """Load waveform file using WAL, with caching that checks file modification time.

    Args:
        waveform_file: Path to waveform file (.vcd, .fst, etc.)

    Returns:
        TraceContainer: WAL container with loaded waveform data

    Raises:
        FileNotFoundError: If the waveform file does not exist.
        ValueError: If the waveform_file path is empty.
        Exception: For other errors during loading.
    """
    if not waveform_file:
        raise ValueError("Waveform file path cannot be empty.")

    try:
        current_mtime = os.path.getmtime(waveform_file)
    except FileNotFoundError:
        logger.error(f"Waveform file not found: {waveform_file}")
        raise
    except OSError as e:
        logger.error(f"Error accessing waveform file {waveform_file}: {e}")
        raise

    # Check if file is cached and still current
    if waveform_file in _waveform_cache:
        cached_mtime, container = _waveform_cache[waveform_file]
        if cached_mtime == current_mtime:
            logger.debug(f"Using cached waveform: {waveform_file}")
            return container
        else:
            logger.info(
                f"Waveform file {waveform_file} changed (mtime: {cached_mtime} -> {current_mtime}), reloading..."
            )

    # Load fresh copy
    logger.info(f"Loading waveform file: {waveform_file}")
    try:
        container = TraceContainer()
        container.load(waveform_file)
        _waveform_cache[waveform_file] = (current_mtime, container)
        logger.debug(f"Cached waveform {waveform_file} with mtime: {current_mtime}")
    except Exception as e:
        logger.error(f"Failed to load waveform file {waveform_file}: {e}")
        raise

    return container


async def _get_timescale_info(container) -> Dict[str, Any]:
    """Extract timescale information from a waveform container.

    Returns:
        Dictionary with:
            - timescale: string like 'ps', 'ns', etc.
            - first_ts: first timestamp in simulation time
            - last_ts: last timestamp in simulation time
            - time_steps: total number of time steps
            - time_per_step: simulation time units per step (float)
    """
    # Get timescale and timestamps from the first trace
    trace_id = list(container.traces.keys())[0]
    trace = container.traces[trace_id]

    # Use trace.max_index directly instead of slow WAL query
    time_steps = trace.max_index + 1

    # Get timescale - VCD has it as attribute, FST needs pylibfst
    timescale = getattr(trace, "timescale", None)
    first_ts = 0
    last_ts = None

    if timescale:
        # VCD format - timestamps is a Python list
        timestamps = getattr(trace, "timestamps", None)
        if timestamps and len(timestamps) > 0:
            first_ts = timestamps[0]
            last_ts = timestamps[-1]
    else:
        # FST format - use pylibfst to get timescale and times
        try:
            import pylibfst

            timescale_exp = pylibfst.lib.fstReaderGetTimescale(trace.fst)
            units = {
                0: "s",
                -1: "100ms",
                -2: "10ms",
                -3: "ms",
                -4: "100us",
                -5: "10us",
                -6: "us",
                -7: "100ns",
                -8: "10ns",
                -9: "ns",
                -10: "100ps",
                -11: "10ps",
                -12: "ps",
                -13: "100fs",
                -14: "10fs",
                -15: "fs",
            }
            timescale = units.get(timescale_exp, f"1e{timescale_exp}s")
            first_ts = pylibfst.lib.fstReaderGetStartTime(trace.fst)
            last_ts = pylibfst.lib.fstReaderGetEndTime(trace.fst)
        except Exception:
            pass

    # Calculate time per step
    time_per_step = 1.0
    if last_ts is not None and time_steps > 1:
        time_per_step = (last_ts - first_ts) / (time_steps - 1)

    return {
        "timescale": timescale,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "time_steps": time_steps,
        "time_per_step": time_per_step,
    }


async def _get_signal_list(args: Dict[str, Any]) -> List[TextContent]:
    """Get hierarchical list of signals from waveform file.

    Args:
        args: Dictionary containing:
            - waveform_file: Path to waveform file
            - pattern: Optional regex pattern to filter signal names
            - limit: Maximum number of signals to return (optional, default: 100)

    Returns:
        List of TextContent with formatted signal list
    """
    waveform_file = args.get("waveform_file")
    pattern = args.get("pattern", "")
    limit = args.get("limit", 100)

    try:
        if not waveform_file:
            raise ValueError("Waveform file path cannot be empty.")

        container = await _load_waveform(waveform_file)
        all_signals = container.signals

        if pattern:
            regex = re.compile(pattern)
            filtered_signals = [s for s in all_signals if regex.search(s)]
        else:
            filtered_signals = all_signals

        total_matching = len(filtered_signals)
        limited_signals = filtered_signals[:limit] if limit > 0 else filtered_signals

        result_lines = [f"Signals in {waveform_file}:"]
        if pattern:
            result_lines.append(f"Filter pattern: {pattern}")

        for signal in limited_signals:
            width = container.signal_width(signal)
            bit_word = "bit" if width == 1 else "bits"
            result_lines.append(f"  {signal} [{width} {bit_word}]")

        if limit > 0 and total_matching > limit:
            result_lines.append(f"  ... (showing {limit} of {total_matching} signals)")

        if not filtered_signals:
            if pattern:
                result_lines.append("  No signals found matching regex pattern.")
            else:
                result_lines.append("  No signals found in waveform file.")

    except (FileNotFoundError, ValueError) as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    except re.error as e:
        result_lines = [
            f"Signals in {waveform_file}:",
            f"Invalid regex pattern '{pattern}': {e}",
            "Please provide a valid regex pattern.",
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=f"Error processing waveform file '{waveform_file}': {e}",
            )
        ]

    return [TextContent(type="text", text="\n".join(result_lines))]


def _init_fst_callbacks():
    """Initialize persistent FST callbacks (called once, lazily).

    This creates callbacks once and reuses them to prevent memory leaks
    from repeatedly creating new CFFI callback objects. Each call passes
    its own transition list via user_data using ffi.new_handle/from_handle.
    """
    import pylibfst

    if _fst_callbacks["on_change"] is not None:
        return  # Already initialized

    @pylibfst.ffi.callback("void(void*, uint64_t, fstHandle, const unsigned char*)")
    def on_change(user_data, time, sig_handle, value_ptr):
        # Get the per-call transition list from user_data handle
        transitions_list = pylibfst.ffi.from_handle(user_data)
        value_str = pylibfst.ffi.string(value_ptr).decode("utf-8")
        transitions_list.append((time, value_str))

    @pylibfst.ffi.callback(
        "void(void*, uint64_t, fstHandle, const unsigned char*, uint32_t)"
    )
    def on_change_var(user_data, time, sig_handle, value_ptr, length):
        # Get the per-call transition list from user_data handle
        transitions_list = pylibfst.ffi.from_handle(user_data)
        value_str = pylibfst.ffi.string(value_ptr, length).decode("utf-8")
        transitions_list.append((time, value_str))

    # Store persistent references to prevent garbage collection
    _fst_callbacks["on_change"] = on_change
    _fst_callbacks["on_change_var"] = on_change_var


def _get_fst_signal_transitions_direct(
    trace, signal_name: str, start_step: int, end_step: int, limit: int
) -> List[Tuple[int, Any, Any]]:
    """Get transitions for a specific signal using direct FST iteration.

    This iterates only the signal's actual transitions within the time range,
    providing massive speedup compared to iterating all global timestamps.

    Args:
        trace: The WAL trace object (must be FST format)
        signal_name: Signal name to analyze
        start_step: Start step index (inclusive)
        end_step: End step index (inclusive)
        limit: Maximum number of transitions to return (0 = unlimited)

    Returns:
        List of (step, old_value, new_value) tuples for each transition
    """
    import pylibfst

    # Get the signal's FST handle from references_to_ids
    sig_obj = trace.references_to_ids.get(signal_name)
    if sig_obj is None:
        return []
    handle = sig_obj.handle

    # Get timestamps array for time-to-index conversion
    timestamps = trace.timestamps
    max_index = trace.max_index

    # Handle invalid range (start > end)
    if start_step > end_step:
        return []

    # Convert step bounds to simulation times and limit FST iteration range
    # This dramatically reduces iterations (e.g., 928K -> 9K for partial ranges)
    start_time_sim = timestamps[start_step] if start_step > 0 else 0
    end_time_sim = timestamps[end_step]

    fst_reader = trace.fst

    # Set time range limit BEFORE iteration
    pylibfst.lib.fstReaderSetLimitTimeRange(fst_reader, start_time_sim, end_time_sim)

    # Set up mask to only process this one signal
    pylibfst.lib.fstReaderClrFacProcessMaskAll(fst_reader)
    pylibfst.lib.fstReaderSetFacProcessMask(fst_reader, handle)

    # Build time-to-index lookup using binary search
    def time_to_step(time):
        """Convert simulation time to step index using binary search."""
        lo, hi = 0, max_index
        while lo <= hi:
            mid = (lo + hi) // 2
            mid_time = timestamps[mid]
            if mid_time == time:
                return mid
            elif mid_time < time:
                lo = mid + 1
            else:
                hi = mid - 1
        return None

    # Initialize persistent callbacks (once)
    _init_fst_callbacks()

    # Create per-call transition list and pass via user_data handle
    # This ensures thread safety - each call has its own list
    raw_transitions = []
    user_data_handle = pylibfst.ffi.new_handle(raw_transitions)

    # Iterate through the FST file (limited to time range, masked to single signal)
    # Uses persistent callbacks to avoid memory leaks from repeated CFFI callback creation
    pylibfst.fstReaderIterBlocks2(
        fst_reader,
        _fst_callbacks["on_change"],
        _fst_callbacks["on_change_var"],
        user_data_handle,
        pylibfst.ffi.NULL,
    )

    # Restore unlimited time range and full signal mask
    pylibfst.lib.fstReaderSetUnlimitedTimeRange(fst_reader)
    pylibfst.lib.fstReaderSetFacProcessMaskAll(fst_reader)

    # Get initial value just before the range for proper transition detection
    initial_value = trace.access_signal_data(signal_name, start_step)

    # Convert raw transitions to (step, old_value, new_value) format
    # Filter strictly to ensure we only include transitions within [start_step, end_step]
    transitions = []
    prev_value = initial_value

    for time, value_str in raw_transitions:
        # Parse value
        try:
            value = (
                int(value_str, 2)
                if value_str and value_str[0] in "01xXzZ"
                else int(value_str)
            )
        except (ValueError, TypeError):
            value = value_str

        # Convert time to step
        step = time_to_step(time)
        if step is None:
            continue

        # Strict range check (fstReaderSetLimitTimeRange may not be exact)
        if step > end_step:
            break
        if step < start_step:
            prev_value = value
            continue

        # Record transition if value changed
        if value != prev_value:
            transitions.append((step, prev_value, value))
            if limit > 0 and len(transitions) >= limit:
                break

        prev_value = value

    return transitions


async def _get_signal_transitions(args: Dict[str, Any]) -> List[TextContent]:
    """Get signal transitions within specified time range.

    Uses per-signal transition iteration for FST files (very fast for sparse signals)
    and direct trace data access for VCD files.

    Args:
        args: Dictionary containing:
            - waveform_file: Path to waveform file
            - signal_name: Full signal name (e.g., 'cpu.pc')
            - start_time: Start time in simulation units (optional, default: 0)
            - end_time: End time in simulation units (optional, default: end)
            - limit: Maximum number of transitions to return (optional, default: 10)

    Returns:
        List of TextContent with signal transition information
    """
    waveform_file = args.get("waveform_file")
    signal_name = args.get("signal_name")
    start_time = args.get("start_time", 0)
    end_time = args.get("end_time", 0)
    limit = args.get("limit", 10)

    try:
        if not waveform_file:
            raise ValueError("Waveform file path cannot be empty.")
        if not signal_name:
            raise ValueError("Signal name cannot be empty.")

        container = await _load_waveform(waveform_file)

        if signal_name not in container.signals:
            return [
                TextContent(
                    type="text",
                    text=f"Error: Signal '{signal_name}' not found in {waveform_file}",
                )
            ]

        # Get timescale info for time conversion
        ts_info = await _get_timescale_info(container)
        time_per_step = ts_info["time_per_step"]
        timescale = ts_info["timescale"] or "units"
        first_ts = ts_info["first_ts"]
        total_steps = ts_info["time_steps"]

        result_lines = [f"Signal analysis for '{signal_name}':"]
        width = container.signal_width(signal_name)
        bit_word = "bit" if width == 1 else "bits"
        result_lines.append(f"  Width: {width} {bit_word}")
        result_lines.append(f"  Timescale: {time_per_step} {timescale}/step")

        actual_end_time = end_time if end_time > 0 else total_steps - 1
        actual_end_time = min(actual_end_time, total_steps - 1)
        start_time = max(0, start_time)

        # Get direct access to trace data
        trace = container.traces[list(container.traces.keys())[0]]

        # Get initial value using direct access
        initial_value = trace.access_signal_data(signal_name, start_time)
        sim_time_start = first_ts + start_time * time_per_step
        result_lines.append(
            f"  Initial value at step {start_time} ({sim_time_start:.0f} {timescale}): {initial_value}"
        )

        # Check if this is an FST file (has fst attribute) and try per-signal iteration
        transitions = []
        is_fst = hasattr(trace, "fst") and trace.fst is not None

        if is_fst:
            try:
                # Use per-signal iteration for FST files - much faster for sparse signals
                raw_transitions = _get_fst_signal_transitions_direct(
                    trace, signal_name, start_time, actual_end_time, limit
                )
                for step, old_val, new_val in raw_transitions:
                    sim_time = first_ts + step * time_per_step
                    transitions.append(
                        f"  Step {step} ({sim_time:.0f} {timescale}): {old_val} -> {new_val}"
                    )
            except Exception as e:
                # Fallback to global iteration if per-signal fails
                logger.debug(f"Per-signal FST iteration failed, falling back: {e}")
                is_fst = False

        if not is_fst:
            # VCD or fallback: iterate through global timestamps
            prev_value = initial_value
            for i in range(start_time + 1, actual_end_time + 1):
                curr_value = trace.access_signal_data(signal_name, i)
                if curr_value != prev_value:
                    sim_time = first_ts + i * time_per_step
                    transitions.append(
                        f"  Step {i} ({sim_time:.0f} {timescale}): {prev_value} -> {curr_value}"
                    )
                    prev_value = curr_value
                    if limit > 0 and len(transitions) >= limit:
                        break

        if transitions:
            result_lines.append("")
            result_lines.append("Transitions detected:")
            result_lines.extend(transitions)
            if limit > 0 and len(transitions) >= limit:
                result_lines.append(f"  ... (limited to {limit} transitions)")
        else:
            result_lines.append("")
            result_lines.append("No transitions detected in time range.")

        step_range_end = actual_end_time
        sim_time_end = first_ts + step_range_end * time_per_step
        result_lines.append(f"")
        result_lines.append(f"Step range analyzed: {start_time} to {step_range_end}")
        result_lines.append(
            f"Simulation time range: {sim_time_start:.0f} to {sim_time_end:.0f} {timescale}"
        )
        if limit > 0:
            result_lines.append(f"Transition limit: {limit}")

    except (FileNotFoundError, ValueError) as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    except Exception as e:
        result_lines = [f"Error during transition detection for '{signal_name}': {e}"]

    return [TextContent(type="text", text="\n".join(result_lines))]


async def _get_waveform_length(args: Dict[str, Any]) -> List[TextContent]:
    """Get the length of the waveform file.

    Args:
        args: Dictionary containing:
            - waveform_file: Path to waveform file

    Returns:
        List of TextContent with waveform length information
    """
    waveform_file = args.get("waveform_file")

    try:
        if not waveform_file:
            raise ValueError("Waveform file path cannot be empty.")

        container = await _load_waveform(waveform_file)

        # Get timescale and timestamps from the first trace
        trace_id = list(container.traces.keys())[0]
        trace = container.traces[trace_id]

        # Use trace.max_index directly instead of slow WAL query
        waveform_length = trace.max_index + 1

        # Get timescale - VCD has it as attribute, FST needs pylibfst
        timescale = getattr(trace, "timescale", None)
        first_ts = 0
        last_ts = None

        if timescale:
            # VCD format - timestamps is a Python list
            timestamps = getattr(trace, "timestamps", None)
            if timestamps and len(timestamps) > 0:
                first_ts = timestamps[0]
                last_ts = timestamps[-1]
        else:
            # FST format - use pylibfst to get timescale and times
            try:
                import pylibfst

                timescale_exp = pylibfst.lib.fstReaderGetTimescale(trace.fst)
                units = {
                    0: "s",
                    -1: "100ms",
                    -2: "10ms",
                    -3: "ms",
                    -4: "100us",
                    -5: "10us",
                    -6: "us",
                    -7: "100ns",
                    -8: "10ns",
                    -9: "ns",
                    -10: "100ps",
                    -11: "10ps",
                    -12: "ps",
                    -13: "100fs",
                    -14: "10fs",
                    -15: "fs",
                }
                timescale = units.get(timescale_exp, f"1e{timescale_exp}s")
                first_ts = pylibfst.lib.fstReaderGetStartTime(trace.fst)
                last_ts = pylibfst.lib.fstReaderGetEndTime(trace.fst)
            except Exception:
                pass

        result_lines = [
            f"Waveform file: {waveform_file}",
        ]
        if timescale:
            result_lines.append(f"Timescale: {timescale}")
        result_lines.append(f"Time steps: {waveform_length}")
        result_lines.append(f"Time step range: 0 to {waveform_length - 1}")
        if last_ts is not None:
            result_lines.append(
                f"Simulation time: {first_ts} to {last_ts} {timescale if timescale else 'units'}"
            )

    except (FileNotFoundError, ValueError) as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    except Exception as e:
        result_lines = [
            f"Waveform file: {waveform_file}",
            f"Error getting waveform length: {str(e)}",
        ]

    return [TextContent(type="text", text="\n".join(result_lines))]


async def _execute_wal_expression(args: Dict[str, Any]) -> List[TextContent]:
    """Execute WAL expression on waveform file.

    Args:
        args: Dictionary containing:
            - waveform_file: Path to waveform file
            - expression: WAL expression to execute
            - limit: Maximum number of result items to return (optional, default: 10)

    Returns:
        List of TextContent with expression execution results
    """
    waveform_file = args.get("waveform_file")
    expression = args.get("expression")
    limit = args.get("limit", 10)

    try:
        if not waveform_file:
            raise ValueError("Waveform file path cannot be empty.")
        if not expression:
            raise ValueError("WAL expression cannot be empty.")

        container = await _load_waveform(waveform_file)
        evaluator = SEval(container)

        # Capture stdout to get WAL's error messages (WAL prints errors there)
        stdout_capture = io.StringIO()
        try:
            with redirect_stdout(stdout_capture):
                parsed_expr = read_wal_sexpr(expression)
                result = evaluator.eval(parsed_expr)
        except Exception as wal_exc:
            # Get the captured stdout for the actual error message
            wal_output = stdout_capture.getvalue().strip()

            # Extract the meaningful error from WAL's output
            # WAL prints: ">>>>> WAL Runtime error! <<<<<\n<actual error message>"
            error_msg = wal_output
            if "WAL Runtime error" in wal_output:
                lines = wal_output.split("\n")
                # Find lines after the header that contain actual error info
                error_lines = [
                    l
                    for l in lines
                    if l and "WAL Runtime error" not in l and ">>>>>" not in l
                ]
                error_msg = (
                    "\n".join(error_lines).strip() if error_lines else str(wal_exc)
                )

            if not error_msg:
                error_msg = str(wal_exc) if str(wal_exc) else "Unknown WAL error"

            # Get signal-specific suggestions
            all_signals = (
                list(_waveform_cache[waveform_file][1].signals)
                if waveform_file in _waveform_cache
                else []
            )
            suggestions = _get_wal_error_suggestions(error_msg, all_signals, expression)

            result_lines = [
                f"WAL Expression: {expression}",
                f"Waveform file: {waveform_file}",
                "",
                f"Execution Error: {error_msg}",
            ]

            result_lines.extend(["", *suggestions, ""])
            return [TextContent(type="text", text="\n".join(result_lines))]

        # Limit result if it's a list and exceeds the limit
        original_length = None
        if isinstance(result, list) and len(result) > limit:
            original_length = len(result)
            result = result[:limit]

        result_lines = [
            f"WAL Expression: {expression}",
            f"Waveform file: {waveform_file}",
            "",
            f"Result: {result}",
            f"Result type: {type(result).__name__}",
        ]

        if isinstance(result, list) and len(result) > 5:
            result_lines.append(f"Result length: {len(result)}")
            result_lines.append("First few elements:")
            for i, item in enumerate(result[:5]):
                result_lines.append(f"  [{i}]: {item}")
            if len(result) > 5:
                result_lines.append(f"  ... and {len(result) - 5} more")

        if original_length is not None:
            result_lines.append(
                f"(Result truncated from {original_length} to {limit} items)"
            )

    except (FileNotFoundError, ValueError) as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]

    return [TextContent(type="text", text="\n".join(result_lines))]


async def _get_wal_help(args: Dict[str, Any]) -> List[TextContent]:
    """Get WAL documentation and examples.

    Args:
        args: Dictionary containing:
            - topic: Help topic (overview, functions, examples, debugging, syntax)

    Returns:
        List of TextContent with WAL documentation
    """
    topic = args.get("topic", "overview")

    if topic not in WAL_DOCUMENTATION:
        available_topics = ", ".join(WAL_DOCUMENTATION.keys())
        return [
            TextContent(
                type="text",
                text=f"Unknown topic '{topic}'. Available topics: {available_topics}",
            )
        ]

    content = WAL_DOCUMENTATION[topic]

    # Add topic header and navigation info
    result_lines = [
        f"WAL Help - {topic.title()}",
        "=" * 50,
        content.strip(),
        "",
        f"Available topics: {', '.join(WAL_DOCUMENTATION.keys())}",
        "Use get_wal_help with different topic for more information.",
    ]

    return [TextContent(type="text", text="\n".join(result_lines))]


def _get_wal_error_suggestions(
    error_msg: str, signals: list, expression: str = ""
) -> List[str]:
    """Generate helpful WAL suggestions based on error message and available signals.

    Args:
        error_msg: The WAL error message (from stderr or exception)
        signals: List of available signal names in the waveform
        expression: The WAL expression that failed (for syntax analysis)

    Returns:
        List of helpful suggestion strings
    """
    suggestions = []
    error_lower = error_msg.lower()

    # 1. Check for syntax errors (unbalanced parentheses)
    if expression:
        open_parens = expression.count("(")
        close_parens = expression.count(")")
        if open_parens != close_parens:
            suggestions.extend(
                [
                    "Syntax Error: Unbalanced parentheses",
                    f"  Found {open_parens} opening '(' and {close_parens} closing ')'",
                    f"  Missing {abs(open_parens - close_parens)} {'closing' if open_parens > close_parens else 'opening'} parenthesis",
                    "",
                ]
            )

    # 2. Check for undefined variables (signal not found)
    if "undefined" in error_lower or "unbound" in error_lower:
        # Try to extract the undefined name from the error message
        # Common patterns: "undefined variable: name", "unbound symbol name"
        undefined_name = None
        for pattern in [
            "undefined variable:",
            "undefined:",
            "unbound symbol",
            "unbound:",
        ]:
            if pattern in error_lower:
                # Extract the name after the pattern
                idx = error_lower.find(pattern) + len(pattern)
                rest = error_msg[idx:].strip()
                # Take the first word/token
                undefined_name = rest.split()[0].strip("'\"") if rest else None
                break

        # Also try to find potential signal names in the expression
        if not undefined_name and expression:
            # Look for words that might be signal names (not WAL keywords)
            wal_keywords = {
                "find",
                "count",
                "length",
                "and",
                "or",
                "not",
                "if",
                "let",
                "true",
                "false",
                "step",
                "INDEX",
                "SIGNALS",
                "map",
                "filter",
                "reduce",
                "lambda",
                "quote",
            }
            tokens = (
                expression.replace("(", " ")
                .replace(")", " ")
                .replace("=", " ")
                .replace("!", " ")
                .replace("<", " ")
                .replace(">", " ")
                .split()
            )
            for token in tokens:
                if token and not token.isdigit() and token not in wal_keywords:
                    # Check if this looks like a signal name but isn't in signals list
                    if signals and token not in signals:
                        undefined_name = token
                        break

        if undefined_name and signals:
            # Find similar signal names
            similar = get_close_matches(undefined_name, signals, n=5, cutoff=0.4)
            if similar:
                suggestions.extend(
                    [
                        f"Signal not found: '{undefined_name}'",
                        "",
                        "Did you mean one of these?",
                    ]
                )
                for s in similar:
                    suggestions.append(f"  • {s}")
                suggestions.append("")
            else:
                suggestions.extend(
                    [
                        f"Signal not found: '{undefined_name}'",
                        "",
                        "No similar signal names found. Check available signals with SIGNALS.",
                        f"First 5 signals: {', '.join(signals[:5])}{'...' if len(signals) > 5 else ''}",
                        "",
                    ]
                )
        else:
            suggestions.extend(
                [
                    "Variable/function not found. Try:",
                    "• Check signal names with SIGNALS",
                    "• Use exact signal names from your waveform",
                    f"• Available signals: {', '.join(signals[:5])}{'...' if len(signals) > 5 else ''}",
                    "",
                ]
            )

    # 3. Check for type mismatches
    if "type" in error_lower or "argument" in error_lower or "expected" in error_lower:
        type_suggestion_added = False

        if "argument must be a list" in error_lower or "expected list" in error_lower:
            suggestions.extend(
                [
                    "Type Error: Function expects a list argument",
                    "  • (find condition) returns a list of time indices",
                    "  • (length list) expects a list argument",
                    "",
                    "Fix: Wrap your condition in (find ...) if you need a list:",
                    f"  • (length (find (= {signals[0] if signals else 'signal'} 1)))",
                    "",
                ]
            )
            type_suggestion_added = True

        if "expected number" in error_lower or "not a number" in error_lower:
            suggestions.extend(
                [
                    "Type Error: Function expects a numeric argument",
                    "  • Signal values are numbers (0, 1, etc.)",
                    "  • Arithmetic operations (+, -, *, /) require numbers",
                    "",
                ]
            )
            type_suggestion_added = True

        if "expected bool" in error_lower or "not a bool" in error_lower:
            suggestions.extend(
                [
                    "Type Error: Function expects a boolean argument",
                    "  • Comparison operators (=, !=, <, >) return booleans",
                    "  • Logical operators (&&, ||) expect booleans",
                    "",
                ]
            )
            type_suggestion_added = True

        if not type_suggestion_added and (
            "type" in error_lower or "expected" in error_lower
        ):
            suggestions.extend(
                [
                    "Type Error: Type mismatch in expression",
                    "  • Check that function arguments are the correct type",
                    "  • Signal names evaluate to their current value (a number)",
                    "  • (find cond) returns a list, (count cond) returns a number",
                    "",
                ]
            )

    # 4. If no specific error matched, provide generic help
    if not suggestions:
        suggestions.extend(
            [
                "Expression Error:",
                "",
                "Common WAL patterns to try:",
                "• SIGNALS - List all signal names",
                "• (find (= signal_name value)) - Find when signal equals value",
                "• (count condition) - Count occurrences",
                "• (length (find true)) - Total simulation length",
                "",
            ]
        )

    # Add signal-specific examples if we have signals
    if signals:
        first_signal = signals[0]
        suggestions.extend(
            [
                f"Examples with your signals (using '{first_signal}'):",
                f"  • (find (= {first_signal} 1))",
                f"  • (count (= {first_signal} 0))",
                f"  • (length (find (!= {first_signal} 0)))",
            ]
        )

    return suggestions


async def _get_wal_examples(args: Dict[str, Any]) -> List[TextContent]:
    """Get WAL examples customized for the specific waveform signals.

    Args:
        args: Dictionary containing:
            - waveform_file: Path to waveform file

    Returns:
        List of TextContent with signal-specific WAL examples
    """
    waveform_file = args["waveform_file"]

    try:
        container = await _load_waveform(waveform_file)
        all_signals = list(container.signals)

        if not all_signals:
            return [TextContent(type="text", text="No signals found in waveform file")]

        # Categorize signals by type for better examples
        clock_signals = [s for s in all_signals if "clk" in s.lower()]
        reset_signals = [
            s for s in all_signals if "reset" in s.lower() or "rst" in s.lower()
        ]
        counter_signals = [
            s for s in all_signals if "counter" in s.lower() or "count" in s.lower()
        ]
        data_signals = [
            s
            for s in all_signals
            if s not in clock_signals + reset_signals + counter_signals
        ]

        result_lines = [
            f"WAL Examples for {waveform_file}",
            "=" * 60,
            f"Available signals: {len(all_signals)} total",
            "",
        ]

        # Basic signal access examples
        result_lines.extend(
            [
                "BASIC SIGNAL ACCESS:",
                "• SIGNALS - List all signals in waveform",
                f"• {all_signals[0]} - Get current value of {all_signals[0]}",
                "• INDEX - Current time index",
                "• (length (find true)) - Total simulation length",
                "",
            ]
        )

        # Clock-specific examples
        if clock_signals:
            clk = clock_signals[0]
            result_lines.extend(
                [
                    f"CLOCK ANALYSIS (using {clk}):",
                    f"• (find (= {clk} 1)) - Find all clock high times",
                    f"• (length (find (= {clk} 1))) - Count clock high periods",
                    f"• (step 0) (find (= {clk} 1)) - Go to start, find clock highs",
                    "",
                ]
            )

        # Reset-specific examples
        if reset_signals:
            rst = reset_signals[0]
            result_lines.extend(
                [
                    f"RESET ANALYSIS (using {rst}):",
                    f"• (find (= {rst} 1)) - Find reset assertion times",
                    f"• (find (= {rst} 0)) - Find reset deassertion times",
                    f"• (length (find (= {rst} 1))) - Total reset duration",
                    "",
                ]
            )

        # Counter-specific examples
        if counter_signals:
            cnt = counter_signals[0]
            result_lines.extend(
                [
                    f"COUNTER ANALYSIS (using {cnt}):",
                    f"• (find (= {cnt} 0)) - Find when counter is zero",
                    f"• (find (> {cnt} 10)) - Find when counter > 10",
                    f"• (length (find (>= {cnt} 1))) - Non-zero periods",
                    "",
                ]
            )

        # Multi-signal analysis examples
        if len(all_signals) >= 2:
            sig1, sig2 = all_signals[0], all_signals[1]
            result_lines.extend(
                [
                    f"MULTI-SIGNAL PATTERNS:",
                    f"• (find (&& (= {sig1} 1) (= {sig2} 0))) - {sig1} high AND {sig2} low",
                    f"• (find (|| (= {sig1} 1) (= {sig2} 1))) - Either signal high",
                    f"• (find (&& (>= {sig1} 1) (>= {sig2} 1))) - Both signals non-zero",
                    "",
                ]
            )

        # Debugging patterns
        result_lines.extend(
            [
                "DEBUGGING PATTERNS:",
                f"• (find (= overflow 1)) - Find overflow events (if overflow signal exists)",
                f"• (find (&& (= valid 1) (= ready 0))) - Handshake stalls (if protocol signals exist)",
                f"• (length (find (> {all_signals[-1]} 15))) - Values out of range (example: >15)",
                "",
                "TIMING ANALYSIS:",
                f"• (step 0) INDEX - Go to start and show time",
                f"• (step 10) {all_signals[0]} - Advance 10 steps and show signal value",
                f"• (find (= {all_signals[0]} target)) - Find specific signal values",
                "",
                "For more help: use get_wal_help with topics 'functions', 'debugging', or 'syntax'",
            ]
        )

    except Exception as e:
        result_lines = [
            f"Error loading waveform {waveform_file}: {str(e)}",
            "",
            "Use get_wal_help for general WAL documentation",
        ]

    return [TextContent(type="text", text="\n".join(result_lines))]


def _find_potential_enable_signals(
    signal_name: str, all_signals: List[str]
) -> List[str]:
    """Find potential enable/valid signals that might qualify the given signal.

    Looks for sibling signals (same parent path) with common enable names like
    'de', 'valid', 'ready', 'enable', 'en', 'vld', 'rdy'.

    Args:
        signal_name: The signal being analyzed
        all_signals: List of all available signals in the waveform

    Returns:
        List of potential enable signal names (empty if none found)
    """
    # Common enable signal suffixes/names
    enable_patterns = ["de", "valid", "ready", "enable", "en", "vld", "rdy", "active"]

    # Get the parent path of the signal
    if "." in signal_name:
        parent_path = signal_name.rsplit(".", 1)[0]
    else:
        parent_path = ""

    candidates = []

    for sig in all_signals:
        if sig == signal_name:
            continue

        # Check if signal is a sibling (same parent path)
        if parent_path:
            if not sig.startswith(parent_path + "."):
                continue
            # Get the signal's leaf name
            leaf = sig[len(parent_path) + 1 :]
            # Skip if it has further hierarchy
            if "." in leaf:
                continue
        else:
            # No parent path, check root-level signals
            if "." in sig:
                continue
            leaf = sig

        # Check if leaf matches any enable pattern
        leaf_lower = leaf.lower()
        for pattern in enable_patterns:
            if leaf_lower == pattern or leaf_lower.endswith("_" + pattern):
                candidates.append(sig)
                break

    return candidates


async def _get_signal_overview(args: Dict[str, Any]) -> List[TextContent]:
    """Get an overview of signal behavior patterns (FST format only).

    Analyzes a signal and returns summary statistics and interval breakdowns
    showing constant-value periods vs. high-activity bursts.

    Supports conditional analysis where signal behavior is only analyzed
    when a qualifying signal (e.g., 'valid', 'enable', 'de') equals a specified value.

    Args:
        args: Dictionary containing:
            - waveform_file: Path to FST waveform file
            - signal_name: Signal name to analyze
            - start_step: Start step index (default: 0)
            - end_step: End step index (default: 0 = end of simulation)
            - limit: Maximum number of intervals to return (default: 20)
            - condition_signal: Optional qualifying signal name
            - condition_value: Value the condition signal must have (default: 1)

    Returns:
        List of TextContent with signal overview information
    """
    waveform_file = args.get("waveform_file")
    signal_name = args.get("signal_name", "")
    start_step = args.get("start_step", 0)
    end_step = args.get("end_step", 0)
    limit = args.get("limit", 20)
    condition_signal = args.get("condition_signal")
    condition_value = args.get("condition_value", 1)

    try:
        # Validate inputs
        if not waveform_file:
            return [TextContent(type="text", text="Error: Waveform file path cannot be empty.")]
        if not signal_name:
            return [TextContent(type="text", text="Error: Signal name cannot be empty.")]

        # Load waveform
        container = await _load_waveform(waveform_file)

        # Validate signal exists
        if signal_name not in container.signals:
            return [TextContent(type="text", text=f"Error: Signal not found: {signal_name}")]

        # Get trace and verify FST format
        trace = container.traces[list(container.traces.keys())[0]]
        is_fst = hasattr(trace, "fst") and trace.fst is not None
        if not is_fst:
            return [TextContent(type="text", text="Error: Only FST format is supported.")]

        # Validate condition signal if specified
        if condition_signal and condition_signal not in container.signals:
            return [TextContent(type="text", text=f"Error: Condition signal not found: {condition_signal}")]

        # Get time range
        ts_info = await _get_timescale_info(container)
        total_steps = ts_info["time_steps"]
        actual_end = end_step if end_step > 0 else total_steps - 1
        actual_end = min(actual_end, total_steps - 1)
        actual_start = max(0, start_step)

        if actual_start > actual_end:
            return [TextContent(type="text", text=f"Error: Invalid time range: {actual_start} to {actual_end}")]

        time_range_label = "full simulation" if actual_start == 0 and actual_end == total_steps - 1 else "partial"

        # Get timescale for time display
        timescale = ts_info.get("timescale", "units")
        time_per_step = ts_info.get("time_per_step", 1.0)

        def step_to_time(step):
            return step * time_per_step

        # Build header
        result_lines = [
            "=== Signal Overview ===",
            f"File: {waveform_file}",
            f"Signal: {signal_name}",
            f"Time range: steps {actual_start}-{actual_end} ({step_to_time(actual_start):.0f}-{step_to_time(actual_end):.0f} {timescale}) ({time_range_label})",
            f"Interval limit: {limit}",
        ]

        if condition_signal:
            result_lines.append(f"Condition: {condition_signal} = {condition_value}")
        else:
            result_lines.append("")
            result_lines.append("WARNING: No condition_signal provided. Data signals are typically only")
            result_lines.append("         valid when their enable/valid signal is active. Consider adding")
            result_lines.append("         condition_signal (e.g., 'valid', 'enable', 'de') for meaningful results.")

        result_lines.append("")

        # Get signal transitions using FST direct access
        transitions = _get_fst_signal_transitions_direct(
            trace, signal_name, actual_start, actual_end, 0
        )

        # Conditional analysis
        if condition_signal:
            # Get condition signal transitions
            cond_transitions = _get_fst_signal_transitions_direct(
                trace, condition_signal, actual_start, actual_end, 0
            )

            # Build condition active intervals
            cond_initial = trace.access_signal_data(condition_signal, actual_start)
            cond_active_intervals = []
            cond_start = actual_start if cond_initial == condition_value else None

            for step, _, new_val in cond_transitions:
                if new_val == condition_value:
                    cond_start = step
                elif cond_start is not None:
                    cond_active_intervals.append((cond_start, step - 1))
                    cond_start = None

            if cond_start is not None:
                cond_active_intervals.append((cond_start, actual_end))

            # Build qualified intervals by sampling signal within condition active periods
            qualified_intervals = []
            qualified_values = set()
            total_cond_steps = 0
            trans_idx = 0

            for cond_start, cond_end in cond_active_intervals:
                total_cond_steps += cond_end - cond_start + 1

                # Get signal value at start of condition interval
                interval_start = cond_start
                prev_val = trace.access_signal_data(signal_name, cond_start)
                qualified_values.add(prev_val)

                # Skip transitions before this condition interval
                while trans_idx < len(transitions) and transitions[trans_idx][0] <= cond_start:
                    trans_idx += 1

                # Process signal transitions within this condition interval
                while trans_idx < len(transitions) and transitions[trans_idx][0] <= cond_end:
                    step, _, new_val = transitions[trans_idx]
                    qualified_intervals.append({
                        "start": interval_start,
                        "end": step - 1,
                        "value": prev_val
                    })
                    interval_start = step
                    prev_val = new_val
                    qualified_values.add(new_val)
                    trans_idx += 1

                # Close final interval in this condition period
                qualified_intervals.append({
                    "start": interval_start,
                    "end": cond_end,
                    "value": prev_val
                })

            # Count qualified transitions
            num_qualified_transitions = max(0, len(qualified_intervals) - len(cond_active_intervals))

            # Output qualified analysis
            result_lines.append("Qualified Analysis:")
            result_lines.append(f"  Qualified transitions: {num_qualified_transitions}")

            if len(qualified_values) == 1:
                result_lines.append(f"  Effective constant value: {list(qualified_values)[0]}")
            else:
                result_lines.append(f"  Unique values when qualified: {len(qualified_values)}")

            pct_active = (total_cond_steps / (actual_end - actual_start + 1)) * 100
            result_lines.append(f"  Condition active periods: {total_cond_steps} steps ({pct_active:.1f}% of simulation)")
            result_lines.append("")

            # Merge adjacent intervals with same value only
            merged_intervals = []
            if qualified_intervals:
                current = qualified_intervals[0].copy()
                for qi in qualified_intervals[1:]:
                    if qi["value"] == current["value"]:
                        current["end"] = qi["end"]
                    else:
                        merged_intervals.append(current)
                        current = qi.copy()
                merged_intervals.append(current)

            # Convert to final interval format
            total_interval_count = len(merged_intervals)
            intervals = []
            for m in merged_intervals[:limit]:
                intervals.append({
                    "start_step": m["start"],
                    "end_step": m["end"],
                    "type": "CONSTANT",
                    "value": m["value"],
                    "transitions": 0
                })

            total_transitions = num_qualified_transitions
            gap_threshold = 0  # No gap-based merging for conditional analysis

        else:
            # Non-conditional analysis
            total_transitions = len(transitions)
            time_span = actual_end - actual_start
            gap_threshold = time_span // limit if limit > 0 else 1
            initial_value = trace.access_signal_data(signal_name, actual_start)

            # Build intervals
            intervals = []
            if not transitions:
                intervals.append({
                    "start_step": actual_start,
                    "end_step": actual_end,
                    "type": "CONSTANT",
                    "value": initial_value,
                    "transitions": 0
                })
            else:
                current_start = actual_start
                current_value = initial_value
                activity_count = 0
                in_activity = False
                activity_start = None

                for step, _, new_val in transitions:
                    gap = step - current_start

                    if gap >= gap_threshold:
                        if in_activity:
                            intervals.append({
                                "start_step": activity_start,
                                "end_step": current_start - 1,
                                "type": "ACTIVE",
                                "value": None,
                                "transitions": activity_count
                            })
                            in_activity = False
                            activity_count = 0

                        intervals.append({
                            "start_step": current_start,
                            "end_step": step - 1,
                            "type": "CONSTANT",
                            "value": current_value,
                            "transitions": 0
                        })
                        current_start = step
                        current_value = new_val
                    else:
                        if not in_activity:
                            activity_start = current_start
                            in_activity = True
                        activity_count += 1
                        current_start = step
                        current_value = new_val

                # Handle final period
                if in_activity:
                    remaining = actual_end - current_start
                    if remaining >= gap_threshold:
                        intervals.append({
                            "start_step": activity_start,
                            "end_step": current_start - 1,
                            "type": "ACTIVE",
                            "value": None,
                            "transitions": activity_count
                        })
                        intervals.append({
                            "start_step": current_start,
                            "end_step": actual_end,
                            "type": "CONSTANT",
                            "value": current_value,
                            "transitions": 0
                        })
                    else:
                        intervals.append({
                            "start_step": activity_start,
                            "end_step": actual_end,
                            "type": "ACTIVE",
                            "value": None,
                            "transitions": activity_count
                        })
                else:
                    intervals.append({
                        "start_step": current_start,
                        "end_step": actual_end,
                        "type": "CONSTANT",
                        "value": current_value,
                        "transitions": 0
                    })

            total_interval_count = len(intervals)

        # Calculate summary stats
        activity_rate = total_transitions / (actual_end - actual_start + 1) if actual_end > actual_start else 0
        constant_count = sum(1 for i in intervals if i["type"] == "CONSTANT")
        active_count = sum(1 for i in intervals if i["type"] == "ACTIVE")
        total_intervals = len(intervals)
        constant_pct = int(constant_count / total_intervals * 100) if total_intervals > 0 else 0
        active_pct = int(active_count / total_intervals * 100) if total_intervals > 0 else 0

        result_lines.append("Summary:")
        result_lines.append(f"  Total transitions: {total_transitions}")
        result_lines.append(f"  Activity rate: {activity_rate:.4f} transitions/step")
        result_lines.append(f"  Gap threshold: {gap_threshold} steps")
        result_lines.append(f"  Constant periods: {constant_count} ({constant_pct}%)")
        result_lines.append(f"  Active periods: {active_count} ({active_pct}%)")
        result_lines.append("")

        # Output intervals
        displayed_intervals = intervals[:limit]
        truncated = total_interval_count > limit

        if condition_signal:
            result_lines.append(f"Intervals (when {condition_signal}={condition_value}):")
        else:
            result_lines.append("Intervals:")

        for interval in displayed_intervals:
            start = interval["start_step"]
            end = interval["end_step"]
            start_time = step_to_time(start)
            end_time = step_to_time(end)
            if interval["type"] == "CONSTANT":
                result_lines.append(f"  [{start}-{end}] ({start_time:.0f}-{end_time:.0f} {timescale}): CONSTANT (value={interval['value']})")
            else:
                result_lines.append(f"  [{start}-{end}] ({start_time:.0f}-{end_time:.0f} {timescale}): ACTIVE ({interval['transitions']} transitions)")

        # Add truncation note if intervals were limited
        if truncated and displayed_intervals:
            last_displayed = displayed_intervals[-1]
            last_step = last_displayed["end_step"]
            last_time = step_to_time(last_step)
            remaining = total_interval_count - limit
            result_lines.append("")
            result_lines.append(f"NOTE: Output truncated due to limit={limit}. Showing {limit} of {total_interval_count} intervals.")
            result_lines.append(f"      Displayed range: steps 0-{last_step} ({last_time:.0f} {timescale})")
            result_lines.append(f"      Not shown: {remaining} intervals (up to step {actual_end}, {step_to_time(actual_end):.0f} {timescale})")
            result_lines.append(f"      To see more: increase limit={total_interval_count} or use start_step={last_step + 1}")

        return [TextContent(type="text", text="\n".join(result_lines))]

    except Exception as e:
        logger.error(f"Error during signal overview: {e}")
        return [TextContent(type="text", text=f"Error during signal overview: {e}")]


async def _get_signal_values(args: Dict[str, Any]) -> List[TextContent]:
    """Get multiple signals' values at a specific time step.

    Args:
        args: Dictionary containing:
            - waveform_file: Path to waveform file
            - step: Time step index to sample at (0-based)
            - signal_names: Array of signal names to sample

    Returns:
        List of TextContent with signal values at the specified step
    """
    waveform_file = args.get("waveform_file")
    step = args.get("step", 0)
    signal_names = args.get("signal_names", [])

    try:
        if not waveform_file:
            raise ValueError("Waveform file path cannot be empty.")
        if not signal_names:
            raise ValueError("Signal names list cannot be empty.")

        container = await _load_waveform(waveform_file)

        # Validate step is within range
        ts_info = await _get_timescale_info(container)
        total_steps = ts_info["time_steps"]
        if step < 0 or step >= total_steps:
            return [
                TextContent(
                    type="text",
                    text=f"Error: Step {step} is out of range. Valid range: 0 to {total_steps - 1}",
                )
            ]

        # Get direct access to trace for absolute step positioning
        trace = container.traces[list(container.traces.keys())[0]]

        result_lines = [
            f"=== Signal Sampling at Step {step} ===",
            f"File: {os.path.basename(waveform_file)}",
            "",
        ]

        for signal in signal_names:
            if signal not in container.signals:
                result_lines.append(f"{signal}: NOT FOUND")
                continue
            # Use direct trace access for absolute step positioning
            value = trace.access_signal_data(signal, step)
            width = container.signal_width(signal)
            bit_word = "bit" if width == 1 else "bits"
            result_lines.append(
                f"{signal} [{width} {bit_word}] = 0x{value:x} ({value})"
            )

        result_lines.append("")
        result_lines.append(f"Total signals sampled: {len(signal_names)}")

    except (FileNotFoundError, ValueError) as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error sampling signals: {e}")]

    return [TextContent(type="text", text="\n".join(result_lines))]


async def _get_signal_statistics(args: Dict[str, Any]) -> List[TextContent]:
    """Calculate min/max/average statistics for a signal over a time range.

    Uses WAL batch queries to find unique values and their durations efficiently,
    instead of iterating through every time step.

    Args:
        args: Dictionary containing:
            - waveform_file: Path to waveform file
            - signal_name: Signal name to analyze
            - start_time: Start time step index (default: 0)
            - end_time: End time step index (default: 0 = end of simulation)

    Returns:
        List of TextContent with signal statistics
    """
    waveform_file = args.get("waveform_file")
    signal_name = args.get("signal_name")
    start_time = args.get("start_time", 0)
    end_time = args.get("end_time", 0)

    try:
        if not waveform_file:
            raise ValueError("Waveform file path cannot be empty.")
        if not signal_name:
            raise ValueError("Signal name cannot be empty.")

        container = await _load_waveform(waveform_file)

        # Validate signal exists
        if signal_name not in container.signals:
            return [
                TextContent(
                    type="text",
                    text=f"Error: Signal '{signal_name}' not found in {waveform_file}",
                )
            ]

        # Get total steps if end_time is 0
        ts_info = await _get_timescale_info(container)
        total_steps = ts_info["time_steps"]
        actual_end_time = end_time if end_time > 0 else total_steps - 1

        # Clamp to valid range
        start_time = max(0, start_time)
        actual_end_time = min(actual_end_time, total_steps - 1)

        if start_time >= actual_end_time:
            return [
                TextContent(
                    type="text",
                    text=f"Error: Invalid time range: start_time ({start_time}) >= end_time ({actual_end_time})",
                )
            ]

        # Get transitions using fast per-signal iteration for FST files
        trace = container.traces[list(container.traces.keys())[0]]
        is_fst = hasattr(trace, "fst") and trace.fst is not None

        # Get initial value
        initial_value = trace.access_signal_data(signal_name, start_time)
        total_steps_in_range = actual_end_time - start_time + 1

        # Track value -> duration mapping
        value_durations = {}  # value -> total steps with this value

        if is_fst:
            try:
                # Use per-signal FST iteration (no limit - we need all transitions)
                transitions = _get_fst_signal_transitions_direct(
                    trace, signal_name, start_time, actual_end_time, 0
                )

                # Calculate durations from transitions
                current_value = initial_value
                current_start = start_time

                for step, old_val, new_val in transitions:
                    # Duration of current_value is from current_start to step-1
                    duration = step - current_start
                    if current_value not in value_durations:
                        value_durations[current_value] = 0
                    value_durations[current_value] += duration
                    current_value = new_val
                    current_start = step

                # Add final segment
                duration = actual_end_time - current_start + 1
                if current_value not in value_durations:
                    value_durations[current_value] = 0
                value_durations[current_value] += duration

            except Exception as e:
                logger.debug(f"Per-signal FST iteration failed, falling back: {e}")
                is_fst = False

        if not is_fst:
            # VCD fallback: iterate through all timestamps
            current_value = initial_value
            current_start = start_time

            for i in range(start_time + 1, actual_end_time + 1):
                curr_value = trace.access_signal_data(signal_name, i)
                if curr_value != current_value:
                    duration = i - current_start
                    if current_value not in value_durations:
                        value_durations[current_value] = 0
                    value_durations[current_value] += duration
                    current_value = curr_value
                    current_start = i

            # Add final segment
            duration = actual_end_time - current_start + 1
            if current_value not in value_durations:
                value_durations[current_value] = 0
            value_durations[current_value] += duration

        # Calculate statistics from unique values and their durations
        if not value_durations:
            # Fallback: entire range is the initial value
            value_durations[initial_value] = total_steps_in_range

        min_val = min(value_durations.keys())
        max_val = max(value_durations.keys())

        # Weighted average
        weighted_sum = sum(val * count for val, count in value_durations.items())
        total_samples = sum(value_durations.values())
        avg_val = weighted_sum / total_samples if total_samples > 0 else 0

        unique_count = len(value_durations)

        # Get signal width
        width = container.signal_width(signal_name)
        bit_word = "bit" if width == 1 else "bits"

        result_lines = [
            "=== Signal Statistics ===",
            f"File: {os.path.basename(waveform_file)}",
            f"Signal: {signal_name} [{width} {bit_word}]",
            f"Range: step {start_time} to {actual_end_time}",
            "",
            f"Min value: {min_val} (0x{min_val:x})",
            f"Max value: {max_val} (0x{max_val:x})",
            f"Average: {avg_val:.2f}",
            f"Unique values: {unique_count}",
            f"Samples analyzed: {total_samples}",
        ]

    except (FileNotFoundError, ValueError) as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error calculating statistics: {e}")]

    return [TextContent(type="text", text="\n".join(result_lines))]


async def _extract_bit_field(args: Dict[str, Any]) -> List[TextContent]:
    """Extract specific bits from a signal.

    Uses WAL batch queries to find signal transitions efficiently, then extracts
    bit fields only at transition points instead of iterating every step.

    Supports two modes:
    - Single step mode: Extract bits at a specific time step
    - Range mode: Track transitions of the extracted bit-field over a time range

    Args:
        args: Dictionary containing:
            - waveform_file: Path to waveform file
            - signal_name: Signal name to extract bits from
            - high_bit: High bit index (inclusive, 0-based)
            - low_bit: Low bit index (inclusive, 0-based)
            - step: Optional single step to extract at (triggers single step mode)
            - start_time: Start time step index for range mode (default: 0)
            - end_time: End time step index for range mode (default: 0 = end)
            - limit: Maximum transitions to return (default: 10)

    Returns:
        List of TextContent with extracted bit-field values
    """
    waveform_file = args.get("waveform_file")
    signal_name = args.get("signal_name")
    high_bit = args.get("high_bit")
    low_bit = args.get("low_bit")
    step = args.get("step")  # Optional - single step mode
    start_time = args.get("start_time", 0)
    end_time = args.get("end_time", 0)
    limit = args.get("limit", 10)

    try:
        if not waveform_file:
            raise ValueError("Waveform file path cannot be empty.")
        if not signal_name:
            raise ValueError("Signal name cannot be empty.")
        if high_bit is None or low_bit is None:
            raise ValueError("Both high_bit and low_bit must be specified.")

        container = await _load_waveform(waveform_file)

        # Validate signal exists
        if signal_name not in container.signals:
            return [
                TextContent(
                    type="text",
                    text=f"Error: Signal '{signal_name}' not found in {waveform_file}",
                )
            ]

        # Validate bit range
        signal_width = container.signal_width(signal_name)
        if high_bit >= signal_width or low_bit < 0 or high_bit < low_bit:
            return [
                TextContent(
                    type="text",
                    text=f"Error: Invalid bit range [{high_bit}:{low_bit}] for {signal_width}-bit signal '{signal_name}'",
                )
            ]

        field_width = high_bit - low_bit + 1
        mask = (1 << field_width) - 1

        def extract_field(value):
            return (value >> low_bit) & mask

        # Single step mode
        if step is not None:
            ts_info = await _get_timescale_info(container)
            total_steps = ts_info["time_steps"]
            if step < 0 or step >= total_steps:
                return [
                    TextContent(
                        type="text",
                        text=f"Error: Step {step} is out of range. Valid range: 0 to {total_steps - 1}",
                    )
                ]

            container.step(step)
            full_value = container.signal_value(signal_name)
            field_value = extract_field(full_value)

            result_lines = [
                "=== Bit Field Extraction ===",
                f"Signal: {signal_name}[{high_bit}:{low_bit}] at step {step}",
                f"Original signal width: {signal_width} bits",
                f"Extracted field width: {field_width} bits",
                "",
                f"Full signal value: 0x{full_value:x} ({full_value})",
                f"Extracted bits [{high_bit}:{low_bit}]: 0x{field_value:x} ({field_value})",
            ]
            return [TextContent(type="text", text="\n".join(result_lines))]

        # Range mode - track transitions using WAL batch queries
        ts_info = await _get_timescale_info(container)
        total_steps = ts_info["time_steps"]
        actual_end_time = end_time if end_time > 0 else total_steps - 1

        # Clamp to valid range
        start_time = max(0, start_time)
        actual_end_time = min(actual_end_time, total_steps - 1)

        if start_time >= actual_end_time:
            return [
                TextContent(
                    type="text",
                    text=f"Error: Invalid time range: start_time ({start_time}) >= end_time ({actual_end_time})",
                )
            ]

        # Get transitions using fast per-signal iteration for FST files
        trace = container.traces[list(container.traces.keys())[0]]
        is_fst = hasattr(trace, "fst") and trace.fst is not None

        # Get initial full value
        initial_full_value = trace.access_signal_data(signal_name, start_time)
        prev_field = extract_field(initial_full_value)

        transitions = [f"Step {start_time}: 0x{prev_field:x} ({prev_field})"]
        transition_count = 0

        if is_fst:
            try:
                # Use per-signal FST iteration (no limit on raw transitions)
                raw_transitions = _get_fst_signal_transitions_direct(
                    trace, signal_name, start_time, actual_end_time, 0
                )

                for step, old_val, new_val in raw_transitions:
                    curr_field = extract_field(new_val)

                    # Only report if the extracted bit field actually changed
                    if curr_field != prev_field:
                        transitions.append(
                            f"Step {step}: 0x{prev_field:x} -> 0x{curr_field:x} ({curr_field})"
                        )
                        transition_count += 1
                        prev_field = curr_field

                        if limit > 0 and transition_count >= limit:
                            transitions.append(
                                f"... (limit of {limit} transitions reached)"
                            )
                            break
            except Exception as e:
                logger.debug(f"Per-signal FST iteration failed, falling back: {e}")
                is_fst = False

        if not is_fst:
            # VCD fallback: iterate through all timestamps
            prev_full_value = initial_full_value
            for i in range(start_time + 1, actual_end_time + 1):
                curr_full_value = trace.access_signal_data(signal_name, i)
                if curr_full_value != prev_full_value:
                    curr_field = extract_field(curr_full_value)
                    if curr_field != prev_field:
                        transitions.append(
                            f"Step {i}: 0x{prev_field:x} -> 0x{curr_field:x} ({curr_field})"
                        )
                        transition_count += 1
                        prev_field = curr_field

                        if limit > 0 and transition_count >= limit:
                            transitions.append(
                                f"... (limit of {limit} transitions reached)"
                            )
                            break
                    prev_full_value = curr_full_value

        result_lines = [
            "=== Bit Field Extraction ===",
            f"Signal: {signal_name}[{high_bit}:{low_bit}]",
            f"Original signal width: {signal_width} bits",
            f"Extracted field width: {field_width} bits",
            f"Range: step {start_time} to {actual_end_time}",
            "",
        ] + transitions

        result_lines.append("")
        result_lines.append(f"Total transitions: {transition_count}")

    except (FileNotFoundError, ValueError) as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error extracting bit field: {e}")]

    return [TextContent(type="text", text="\n".join(result_lines))]


async def _find_signal_value(args: Dict[str, Any]) -> List[TextContent]:
    """Find time steps where a signal has a specific value within a time range.

    Uses WAL's native (find) for efficient batch querying instead of step-by-step iteration.

    Args:
        args: Dictionary containing:
            - waveform_file: Path to waveform file
            - signal_name: Signal name to search
            - value: Value to search for
            - start_time: Start time step index (default: 0)
            - end_time: End time step index (default: 0 = end of simulation)
            - limit: Maximum number of matches to return (default: 10)

    Returns:
        List of TextContent with matching time steps
    """
    waveform_file = args.get("waveform_file")
    signal_name = args.get("signal_name")
    value = args.get("value")
    start_time = args.get("start_time", 0)
    end_time = args.get("end_time", 0)
    limit = args.get("limit", 10)

    try:
        if not waveform_file:
            raise ValueError("Waveform file path cannot be empty.")
        if not signal_name:
            raise ValueError("Signal name cannot be empty.")
        if value is None:
            raise ValueError("Value to search for must be specified.")

        container = await _load_waveform(waveform_file)

        # Validate signal exists
        if signal_name not in container.signals:
            return [
                TextContent(
                    type="text",
                    text=f"Error: Signal '{signal_name}' not found in {waveform_file}",
                )
            ]

        # Get total steps if end_time is 0
        ts_info = await _get_timescale_info(container)
        total_steps = ts_info["time_steps"]
        actual_end_time = end_time if end_time > 0 else total_steps - 1

        # Clamp to valid range
        start_time = max(0, start_time)
        actual_end_time = min(actual_end_time, total_steps - 1)

        if start_time >= actual_end_time:
            return [
                TextContent(
                    type="text",
                    text=f"Error: Invalid time range: start_time ({start_time}) >= end_time ({actual_end_time})",
                )
            ]

        # Get signal width
        width = container.signal_width(signal_name)
        bit_word = "bit" if width == 1 else "bits"

        # Get transitions using fast per-signal iteration for FST files
        trace = container.traces[list(container.traces.keys())[0]]
        is_fst = hasattr(trace, "fst") and trace.fst is not None

        # Get initial value
        initial_value = trace.access_signal_data(signal_name, start_time)

        # Find intervals where signal equals the target value
        matches = []

        if is_fst:
            try:
                # Use per-signal FST iteration
                transitions = _get_fst_signal_transitions_direct(
                    trace, signal_name, start_time, actual_end_time, 0
                )

                # Build intervals from transitions
                current_value = initial_value
                current_start = start_time

                for step, old_val, new_val in transitions:
                    # If current interval has the target value, record it
                    if current_value == value:
                        matches.append((current_start, step - 1))
                        if limit > 0 and len(matches) >= limit:
                            break
                    current_value = new_val
                    current_start = step

                # Check final segment (if not already at limit)
                if (limit == 0 or len(matches) < limit) and current_value == value:
                    matches.append((current_start, actual_end_time))

            except Exception as e:
                logger.debug(f"Per-signal FST iteration failed, falling back: {e}")
                is_fst = False

        if not is_fst:
            # VCD fallback: iterate through timestamps
            current_value = initial_value
            current_start = start_time if current_value == value else None

            for i in range(start_time + 1, actual_end_time + 1):
                curr_value = trace.access_signal_data(signal_name, i)
                if curr_value != current_value:
                    # Value changed
                    if current_value == value and current_start is not None:
                        matches.append((current_start, i - 1))
                        if limit > 0 and len(matches) >= limit:
                            break
                    current_value = curr_value
                    current_start = i if curr_value == value else None

            # Check final segment
            if (
                (limit == 0 or len(matches) < limit)
                and current_value == value
                and current_start is not None
            ):
                matches.append((current_start, actual_end_time))

        result_lines = [
            "=== Find Value in Range ===",
            f"File: {os.path.basename(waveform_file)}",
            f"Signal: {signal_name} [{width} {bit_word}]",
            f"Searching for value: {value} (0x{value:x})",
            f"Range: step {start_time} to {actual_end_time}",
            "",
        ]

        if matches:
            result_lines.append(
                f"Found {len(matches)} interval(s) where signal = {value}:"
            )
            for i, (m_start, m_end) in enumerate(matches):
                duration = m_end - m_start + 1
                if m_start == m_end:
                    result_lines.append(f"  [{i+1}] Step {m_start} (1 step)")
                else:
                    result_lines.append(
                        f"  [{i+1}] Steps {m_start} to {m_end} ({duration} steps)"
                    )

            if limit > 0 and len(matches) >= limit:
                result_lines.append(f"  ... (limited to {limit} intervals)")
        else:
            result_lines.append(
                f"No matches found for value {value} in the specified range."
            )

    except (FileNotFoundError, ValueError) as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error searching for value: {e}")]

    return [TextContent(type="text", text="\n".join(result_lines))]


async def _async_main():
    """Async implementation of the MCP server.

    Starts the server using stdio transport for communication with MCP clients.
    """
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="waveform-mcp",
                server_version="0.1.0",
                capabilities=app.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

def cli():
    """Synchronous entry point for CLI packaging."""
    asyncio.run(main())

def main():
    """Main entry point for the MCP server.

    This synchronous wrapper is needed for the console script entry point.
    """
    asyncio.run(_async_main())


if __name__ == "__main__":
    cli()
