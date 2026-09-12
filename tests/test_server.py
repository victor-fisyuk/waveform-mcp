import pytest
import os
from unittest.mock import patch

# Make sure the server module is importable
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from waveform_mcp import server
from mcp.types import TextContent

# Paths to the sample waveform files
TRACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "traces"))
VCD_FILE = os.path.join(TRACE_DIR, "counter.vcd")
FST_FILE = os.path.join(TRACE_DIR, "counter.fst")

WAVEFORM_FILES = [VCD_FILE, FST_FILE]


@pytest.fixture(autouse=True)
def clear_waveform_cache():
    """Clear the waveform cache before each test."""
    server._waveform_cache.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_load_waveform(waveform_file):
    """Test the _load_waveform function caches the result."""
    assert len(server._waveform_cache) == 0

    # First load
    container = await server._load_waveform(waveform_file)
    assert container is not None
    assert len(server._waveform_cache) == 1
    assert waveform_file in server._waveform_cache

    # Second load should be from cache
    container2 = await server._load_waveform(waveform_file)
    assert container2 is container  # Should be the same object
    assert len(server._waveform_cache) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_get_signal_list_all(waveform_file):
    """Test get_signal_list without any pattern."""
    args = {"waveform_file": waveform_file}
    result = await server._get_signal_list(args)

    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], TextContent)

    expected = f"""Signals in {waveform_file}:
  tb.overflow [1 bit]
  tb.clk [1 bit]
  tb.reset [1 bit]
  tb.dut.clk [1 bit]
  tb.dut.reset [1 bit]
  tb.dut.overflow [1 bit]
  tb.dut.counter [4 bits]"""
    assert result[0].text == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_get_signal_list_with_pattern(waveform_file):
    """Test get_signal_list with a regex pattern."""
    args = {"waveform_file": waveform_file, "pattern": "tb\\.dut"}
    result = await server._get_signal_list(args)

    expected = f"""Signals in {waveform_file}:
Filter pattern: tb\\.dut
  tb.dut.clk [1 bit]
  tb.dut.reset [1 bit]
  tb.dut.overflow [1 bit]
  tb.dut.counter [4 bits]"""
    assert result[0].text == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_get_signal_list_no_match(waveform_file):
    """Test get_signal_list with a pattern that matches nothing."""
    args = {"waveform_file": waveform_file, "pattern": "nonexistent"}
    result = await server._get_signal_list(args)

    expected = f"""Signals in {waveform_file}:
Filter pattern: nonexistent
  No signals found matching regex pattern."""
    assert result[0].text == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_get_signal_list_invalid_regex(waveform_file):
    """Test get_signal_list with an invalid regex pattern."""
    args = {"waveform_file": waveform_file, "pattern": "["}
    result = await server._get_signal_list(args)

    expected = f"""Signals in {waveform_file}:
Invalid regex pattern '[': unterminated character set at position 0
Please provide a valid regex pattern."""
    assert result[0].text == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_get_waveform_length(waveform_file):
    """Test get_waveform_length."""
    args = {"waveform_file": waveform_file}
    result = await server._get_waveform_length(args)

    # VCD shows "1s", FST shows "s"
    if waveform_file.endswith(".vcd"):
        timescale = "1s"
    else:
        timescale = "s"

    expected = f"""Waveform file: {waveform_file}
Timescale: {timescale}
Time steps: 81
Time step range: 0 to 80
Simulation time: 0 to 800 {timescale}"""
    assert result[0].text == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_get_signal_transitions_exists(waveform_file):
    """Test get_signal_transitions for a signal that exists."""
    args = {"waveform_file": waveform_file, "signal_name": "tb.clk", "limit": 10}
    result = await server._get_signal_transitions(args)

    # VCD shows "1s", FST shows "s"
    u = "1s" if waveform_file.endswith(".vcd") else "s"

    expected = f"""Signal analysis for 'tb.clk':
  Width: 1 bit
  Timescale: 10.0 {u}/step
  Initial value at step 0 (0 {u}): 0

Transitions detected:
  Step 1 (10 {u}): 0 -> 1
  Step 2 (20 {u}): 1 -> 0
  Step 3 (30 {u}): 0 -> 1
  Step 4 (40 {u}): 1 -> 0
  Step 5 (50 {u}): 0 -> 1
  Step 6 (60 {u}): 1 -> 0
  Step 7 (70 {u}): 0 -> 1
  Step 8 (80 {u}): 1 -> 0
  Step 9 (90 {u}): 0 -> 1
  Step 10 (100 {u}): 1 -> 0
  ... (limited to 10 transitions)

Step range analyzed: 0 to 80
Simulation time range: 0 to 800 {u}
Transition limit: 10"""
    assert result[0].text == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_get_signal_transitions_not_exists(waveform_file):
    """Test get_signal_transitions for a signal that does not exist."""
    args = {"waveform_file": waveform_file, "signal_name": "nonexistent"}
    result = await server._get_signal_transitions(args)

    expected = f"Error: Signal 'nonexistent' not found in {waveform_file}"
    assert result[0].text == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_execute_wal_expression_valid(waveform_file):
    """Test execute_wal_expression with a valid expression."""
    args = {
        "waveform_file": waveform_file,
        "expression": "(length (find (= tb.clk 1)))",
    }
    result = await server._execute_wal_expression(args)

    # Clock is high for 40 out of 81 steps (odd steps 1,3,5,...,79)
    expected = f"""WAL Expression: (length (find (= tb.clk 1)))
Waveform file: {waveform_file}

Result: 40
Result type: int"""
    assert result[0].text == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_execute_wal_expression_invalid_syntax(waveform_file):
    """Test execute_wal_expression with invalid syntax."""
    args = {
        "waveform_file": waveform_file,
        "expression": "(count (= tb.clk 1)",  # Missing closing parenthesis
    }
    result = await server._execute_wal_expression(args)

    text = result[0].text
    assert "Execution Error:" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_execute_wal_expression_undefined_signal(waveform_file):
    """Test execute_wal_expression with an undefined signal."""
    args = {
        "waveform_file": waveform_file,
        "expression": "(find (= non_existent_signal 1))",
    }
    result = await server._execute_wal_expression(args)

    text = result[0].text
    assert "Execution Error:" in text


@pytest.mark.asyncio
async def test_get_wal_help():
    """Test get_wal_help for different topics."""
    # Default topic (overview)
    result_default = await server._get_wal_help({})
    expected_overview = """WAL Help - Overview
==================================================
WAL (Waveform Analysis Language) - Quick Reference

WAL is a functional programming language designed for waveform analysis with Lisp-like syntax.
All expressions use parentheses: (function arg1 arg2 ...)

Key Concepts:
• Signals: Access by name (e.g., 'clk', 'tb.counter')
• Time: Navigate with (step N) or use INDEX for current time
• Lists: Most operations return lists of values/times
• Conditions: Use for filtering and searching

Available topics: overview, functions, examples, debugging, syntax
Use get_wal_help with different topic for more information."""
    assert result_default[0].text == expected_overview

    # Functions topic
    result_functions = await server._get_wal_help({"topic": "functions"})
    expected_functions = """WAL Help - Functions
==================================================
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

Available topics: overview, functions, examples, debugging, syntax
Use get_wal_help with different topic for more information."""
    assert result_functions[0].text == expected_functions

    # Invalid topic
    result_invalid = await server._get_wal_help({"topic": "invalid"})
    expected_invalid = "Unknown topic 'invalid'. Available topics: overview, functions, examples, debugging, syntax"
    assert result_invalid[0].text == expected_invalid


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_get_wal_examples(waveform_file):
    """Test get_wal_examples."""
    args = {"waveform_file": waveform_file}
    result = await server._get_wal_examples(args)

    expected = f"""WAL Examples for {waveform_file}
============================================================
Available signals: 7 total

BASIC SIGNAL ACCESS:
• SIGNALS - List all signals in waveform
• tb.overflow - Get current value of tb.overflow
• INDEX - Current time index
• (length (find true)) - Total simulation length

CLOCK ANALYSIS (using tb.clk):
• (find (= tb.clk 1)) - Find all clock high times
• (length (find (= tb.clk 1))) - Count clock high periods
• (step 0) (find (= tb.clk 1)) - Go to start, find clock highs

RESET ANALYSIS (using tb.reset):
• (find (= tb.reset 1)) - Find reset assertion times
• (find (= tb.reset 0)) - Find reset deassertion times
• (length (find (= tb.reset 1))) - Total reset duration

COUNTER ANALYSIS (using tb.dut.counter):
• (find (= tb.dut.counter 0)) - Find when counter is zero
• (find (> tb.dut.counter 10)) - Find when counter > 10
• (length (find (>= tb.dut.counter 1))) - Non-zero periods

MULTI-SIGNAL PATTERNS:
• (find (&& (= tb.overflow 1) (= tb.clk 0))) - tb.overflow high AND tb.clk low
• (find (|| (= tb.overflow 1) (= tb.clk 1))) - Either signal high
• (find (&& (>= tb.overflow 1) (>= tb.clk 1))) - Both signals non-zero

DEBUGGING PATTERNS:
• (find (= overflow 1)) - Find overflow events (if overflow signal exists)
• (find (&& (= valid 1) (= ready 0))) - Handshake stalls (if protocol signals exist)
• (length (find (> tb.dut.counter 15))) - Values out of range (example: >15)

TIMING ANALYSIS:
• (step 0) INDEX - Go to start and show time
• (step 10) tb.overflow - Advance 10 steps and show signal value
• (find (= tb.overflow target)) - Find specific signal values

For more help: use get_wal_help with topics 'functions', 'debugging', or 'syntax'"""
    assert result[0].text == expected


@pytest.mark.asyncio
async def test_list_tools_return_format():
    """Test that list_tools returns proper List[Tool] format."""
    tools = await server.list_tools()

    # Should return a list of Tool objects
    assert isinstance(tools, list)
    assert len(tools) == 10  # We have 10 tools defined

    # Check that all items are Tool objects with required fields
    for tool in tools:
        assert hasattr(tool, "name")
        assert hasattr(tool, "description")
        assert hasattr(tool, "inputSchema")
        assert isinstance(tool.name, str)
        assert isinstance(tool.description, str)
        assert isinstance(tool.inputSchema, dict)

    # Verify specific tool names exist
    tool_names = [tool.name for tool in tools]
    expected_tools = [
        "get_signal_list",
        "get_signal_transitions",
        "get_waveform_length",
        "get_wal_help",
        "get_wal_examples",
        "get_signal_overview",
        "get_signal_values",
        "get_signal_statistics",
        "extract_bit_field",
        "find_signal_value",
    ]
    for expected_tool in expected_tools:
        assert expected_tool in tool_names, f"Missing tool: {expected_tool}"


@pytest.mark.asyncio
async def test_invalid_waveform_file_paths():
    """Test behavior with invalid waveform file paths."""
    invalid_paths = [
        "/nonexistent/path/file.vcd",
        "not_a_real_file.fst",
        "",
        "/tmp/corrupted.vcd",
    ]

    for invalid_path in invalid_paths:
        # Test get_signal_list with invalid path
        result = await server._get_signal_list({"waveform_file": invalid_path})
        assert isinstance(result, list)
        assert len(result) == 1
        assert "Error:" in result[0].text or "error" in result[0].text.lower()

        # Test get_waveform_length with invalid path
        result = await server._get_waveform_length({"waveform_file": invalid_path})
        assert isinstance(result, list)
        assert len(result) == 1
        assert "Error:" in result[0].text or "error" in result[0].text.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_signal_transitions_time_range_parameters(waveform_file):
    """Test get_signal_transitions with different time range parameters."""
    signal_name = "tb.clk"
    u = "1s" if waveform_file.endswith(".vcd") else "s"

    # Test with start_time and end_time
    args = {
        "waveform_file": waveform_file,
        "signal_name": signal_name,
        "start_time": 10,
        "end_time": 20,
        "limit": 10,
    }
    result = await server._get_signal_transitions(args)

    # VCD and FST have slightly different transition detection at boundaries
    if waveform_file.endswith(".vcd"):
        expected = f"""Signal analysis for 'tb.clk':
  Width: 1 bit
  Timescale: 10.0 {u}/step
  Initial value at step 10 (100 {u}): 0

Transitions detected:
  Step 11 (110 {u}): 0 -> 1
  Step 12 (120 {u}): 1 -> 0
  Step 13 (130 {u}): 0 -> 1
  Step 14 (140 {u}): 1 -> 0
  Step 15 (150 {u}): 0 -> 1
  Step 16 (160 {u}): 1 -> 0
  Step 17 (170 {u}): 0 -> 1
  Step 18 (180 {u}): 1 -> 0
  Step 19 (190 {u}): 0 -> 1
  Step 20 (200 {u}): 1 -> 0
  ... (limited to 10 transitions)

Step range analyzed: 10 to 20
Simulation time range: 100 to 200 {u}
Transition limit: 10"""
    else:
        expected = f"""Signal analysis for 'tb.clk':
  Width: 1 bit
  Timescale: 10.0 {u}/step
  Initial value at step 10 (100 {u}): 0

Transitions detected:
  Step 10 (100 {u}): 1 -> 0
  Step 11 (110 {u}): 0 -> 1
  Step 12 (120 {u}): 1 -> 0
  Step 13 (130 {u}): 0 -> 1
  Step 14 (140 {u}): 1 -> 0
  Step 15 (150 {u}): 0 -> 1
  Step 16 (160 {u}): 1 -> 0
  Step 17 (170 {u}): 0 -> 1
  Step 18 (180 {u}): 1 -> 0
  Step 19 (190 {u}): 0 -> 1
  ... (limited to 10 transitions)

Step range analyzed: 10 to 20
Simulation time range: 100 to 200 {u}
Transition limit: 10"""
    assert result[0].text == expected

    # Test with invalid time range (start > end) - no transitions
    args = {
        "waveform_file": waveform_file,
        "signal_name": signal_name,
        "start_time": 50,
        "end_time": 30,
        "limit": 10,
    }
    result = await server._get_signal_transitions(args)
    expected = f"""Signal analysis for 'tb.clk':
  Width: 1 bit
  Timescale: 10.0 {u}/step
  Initial value at step 50 (500 {u}): 0

No transitions detected in time range.

Step range analyzed: 50 to 30
Simulation time range: 500 to 300 {u}
Transition limit: 10"""
    assert result[0].text == expected


@pytest.mark.asyncio
@patch("waveform_mcp.server._get_signal_list")
async def test_call_tool_routing(mock_get_signals):
    """Test that call_tool routes to the correct function."""
    mock_get_signals.return_value = [TextContent(type="text", text="mocked")]

    await server.call_tool("get_signal_list", {})
    mock_get_signals.assert_called_once()

    result = await server.call_tool("unknown_tool", {})
    assert "Unknown tool: unknown_tool" in result[0].text


@pytest.mark.asyncio
async def test_call_tool_exception_handling():
    """Test that call_tool handles exceptions properly."""
    # Test with invalid arguments that should cause an exception
    result = await server.call_tool("get_signal_list", {"invalid_arg": "value"})
    assert isinstance(result, list)
    assert len(result) == 1
    assert "Error:" in result[0].text


@pytest.mark.asyncio
async def test_corrupted_waveform_handling():
    """Test error handling for corrupted/invalid waveform files."""
    # Create a temporary file with invalid VCD content
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".vcd", delete=False) as f:
        f.write("This is not a valid VCD file content")
        temp_file = f.name

    try:
        # Test various operations with corrupted file
        result = await server._get_signal_list({"waveform_file": temp_file})
        assert isinstance(result, list)
        assert len(result) == 1
        assert "Error:" in result[0].text or "error" in result[0].text.lower()

        result = await server._get_waveform_length({"waveform_file": temp_file})
        assert isinstance(result, list)
        assert len(result) == 1
        assert "Error:" in result[0].text or "error" in result[0].text.lower()

        result = await server._get_signal_transitions(
            {"waveform_file": temp_file, "signal_name": "any_signal"}
        )
        assert isinstance(result, list)
        assert len(result) == 1
        assert "Error:" in result[0].text or "error" in result[0].text.lower()

    finally:
        # Clean up temp file
        import os

        try:
            os.unlink(temp_file)
        except:
            pass


@pytest.mark.asyncio
async def test_waveform_cache_error_handling():
    """Test that waveform cache handles loading errors properly."""
    # Test that failed loads don't pollute the cache
    invalid_file = "/definitely/does/not/exist.vcd"

    # Verify cache is empty initially
    assert len(server._waveform_cache) == 0

    # Try to load invalid file - should raise exception but not cache
    try:
        await server._load_waveform(invalid_file)
    except:
        pass  # Expected to fail

    # Cache should still be empty after failed load
    assert len(server._waveform_cache) == 0


# Tests for get_signal_overview


@pytest.mark.asyncio
async def test_get_signal_overview_single_signal():
    """Test get_signal_overview with a single signal (FST only)."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": FST_FILE,
        "signal_name": "tb.clk",
    }
    result = await server._get_signal_overview(args)

    assert isinstance(result, list)
    assert len(result) == 1

    # Clock toggles every step: 80 transitions (steps 1-80), all active
    expected = f"""=== Signal Overview ===
File: {FST_FILE}
Signal: tb.clk
Time range: steps 0-80 (0-800 s) (full simulation)
Interval limit: 20

WARNING: No condition_signal provided. Data signals are typically only
         valid when their enable/valid signal is active. Consider adding
         condition_signal (e.g., 'valid', 'enable', 'de') for meaningful results.

Summary:
  Total transitions: 80
  Activity rate: 0.9877 transitions/step
  Gap threshold: 4 steps
  Constant periods: 0 (0%)
  Active periods: 1 (100%)

Intervals:
  [0-80] (0-800 s): ACTIVE (80 transitions)"""
    assert result[0].text == expected


@pytest.mark.asyncio
async def test_get_signal_overview_counter_signal():
    """Test get_signal_overview with counter signal (FST only)."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": FST_FILE,
        "signal_name": "tb.dut.counter",
    }
    result = await server._get_signal_overview(args)

    expected = f"""=== Signal Overview ===
File: {FST_FILE}
Signal: tb.dut.counter
Time range: steps 0-80 (0-800 s) (full simulation)
Interval limit: 20

WARNING: No condition_signal provided. Data signals are typically only
         valid when their enable/valid signal is active. Consider adding
         condition_signal (e.g., 'valid', 'enable', 'de') for meaningful results.

Summary:
  Total transitions: 35
  Activity rate: 0.4321 transitions/step
  Gap threshold: 4 steps
  Constant periods: 1 (50%)
  Active periods: 1 (50%)

Intervals:
  [0-10] (0-100 s): CONSTANT (value=0)
  [11-80] (110-800 s): ACTIVE (34 transitions)"""
    assert result[0].text == expected


@pytest.mark.asyncio
async def test_get_signal_overview_with_step_range():
    """Test get_signal_overview with start_step and end_step (FST only)."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": FST_FILE,
        "signal_name": "tb.clk",
        "start_step": 10,
        "end_step": 50,
    }
    result = await server._get_signal_overview(args)

    expected = f"""=== Signal Overview ===
File: {FST_FILE}
Signal: tb.clk
Time range: steps 10-50 (100-500 s) (partial)
Interval limit: 20

WARNING: No condition_signal provided. Data signals are typically only
         valid when their enable/valid signal is active. Consider adding
         condition_signal (e.g., 'valid', 'enable', 'de') for meaningful results.

Summary:
  Total transitions: 41
  Activity rate: 1.0000 transitions/step
  Gap threshold: 2 steps
  Constant periods: 0 (0%)
  Active periods: 1 (100%)

Intervals:
  [10-50] (100-500 s): ACTIVE (41 transitions)"""
    assert result[0].text == expected


@pytest.mark.asyncio
async def test_get_signal_overview_with_limit():
    """Test get_signal_overview respects the limit parameter (FST only)."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": FST_FILE,
        "signal_name": "tb.clk",
        "limit": 3,
    }
    result = await server._get_signal_overview(args)

    expected = f"""=== Signal Overview ===
File: {FST_FILE}
Signal: tb.clk
Time range: steps 0-80 (0-800 s) (full simulation)
Interval limit: 3

WARNING: No condition_signal provided. Data signals are typically only
         valid when their enable/valid signal is active. Consider adding
         condition_signal (e.g., 'valid', 'enable', 'de') for meaningful results.

Summary:
  Total transitions: 80
  Activity rate: 0.9877 transitions/step
  Gap threshold: 26 steps
  Constant periods: 0 (0%)
  Active periods: 1 (100%)

Intervals:
  [0-80] (0-800 s): ACTIVE (80 transitions)"""
    assert result[0].text == expected


@pytest.mark.asyncio
async def test_get_signal_overview_gap_threshold():
    """Test get_signal_overview shows gap threshold in output (FST only)."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": FST_FILE,
        "signal_name": "tb.clk",
        "limit": 20,
    }
    result = await server._get_signal_overview(args)

    expected = f"""=== Signal Overview ===
File: {FST_FILE}
Signal: tb.clk
Time range: steps 0-80 (0-800 s) (full simulation)
Interval limit: 20

WARNING: No condition_signal provided. Data signals are typically only
         valid when their enable/valid signal is active. Consider adding
         condition_signal (e.g., 'valid', 'enable', 'de') for meaningful results.

Summary:
  Total transitions: 80
  Activity rate: 0.9877 transitions/step
  Gap threshold: 4 steps
  Constant periods: 0 (0%)
  Active periods: 1 (100%)

Intervals:
  [0-80] (0-800 s): ACTIVE (80 transitions)"""
    assert result[0].text == expected


@pytest.mark.asyncio
async def test_get_signal_overview_invalid_signal():
    """Test get_signal_overview with invalid signal name (FST only)."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": FST_FILE,
        "signal_name": "nonexistent_signal",
    }
    result = await server._get_signal_overview(args)

    expected = "Error: Signal not found: nonexistent_signal"
    assert result[0].text == expected


@pytest.mark.asyncio
async def test_get_signal_overview_empty_signal_name():
    """Test get_signal_overview with empty signal name (FST only)."""
    args = {
        "waveform_file": FST_FILE,
        "signal_name": "",
    }
    result = await server._get_signal_overview(args)

    text = result[0].text

    assert "Error:" in text


@pytest.mark.asyncio
async def test_get_signal_overview_missing_waveform_file():
    """Test get_signal_overview with missing waveform file."""
    args = {
        "waveform_file": "",
        "signal_name": "tb.clk",
    }
    result = await server._get_signal_overview(args)

    text = result[0].text

    assert "Error:" in text


# Tests for conditional signal analysis in get_signal_overview


@pytest.mark.asyncio
async def test_get_signal_overview_with_condition_signal():
    """Test get_signal_overview with condition_signal parameter (FST only).

    Uses reset as condition signal - counter should show no qualified transitions
    when reset=1 (counter is held at 0 during reset).
    """
    server._waveform_cache.clear()

    args = {
        "waveform_file": FST_FILE,
        "signal_name": "tb.dut.counter",
        "condition_signal": "tb.reset",
        "condition_value": 1,
    }
    result = await server._get_signal_overview(args)

    assert isinstance(result, list)
    assert len(result) == 1

    text = result[0].text

    # Check condition is shown in header
    assert "Condition: tb.reset = 1" in text

    # Check qualified analysis section exists
    assert "Qualified Analysis:" in text
    assert "Qualified transitions:" in text

    # During reset (steps 0-10), counter is 0 and doesn't change
    # So qualified transitions should be 0
    assert "Qualified transitions: 0" in text

    # Counter is constant at 0 during reset
    assert "Effective constant value: 0" in text


@pytest.mark.asyncio
async def test_get_signal_overview_condition_signal_not_found():
    """Test get_signal_overview with invalid condition signal (FST only)."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": FST_FILE,
        "signal_name": "tb.clk",
        "condition_signal": "nonexistent_enable",
        "condition_value": 1,
    }
    result = await server._get_signal_overview(args)

    text = result[0].text

    assert "Error:" in text
    assert "Condition signal not found" in text


@pytest.mark.asyncio
async def test_get_signal_overview_condition_with_custom_value():
    """Test get_signal_overview with condition_value != 1 (FST only)."""
    server._waveform_cache.clear()

    # Use reset=0 as condition (most of the simulation)
    args = {
        "waveform_file": FST_FILE,
        "signal_name": "tb.dut.counter",
        "condition_signal": "tb.reset",
        "condition_value": 0,  # When reset is low
    }
    result = await server._get_signal_overview(args)

    text = result[0].text

    # Check condition is shown with correct value
    assert "Condition: tb.reset = 0" in text
    assert "Qualified Analysis:" in text

    # Counter has transitions when reset=0
    assert "Qualified transitions:" in text


@pytest.mark.asyncio
async def test_get_signal_overview_clock_conditioned_on_reset():
    """Test clock behavior when conditioned on reset being low (FST only)."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": FST_FILE,
        "signal_name": "tb.clk",
        "condition_signal": "tb.reset",
        "condition_value": 0,
    }
    result = await server._get_signal_overview(args)

    text = result[0].text

    # Should show qualified transitions (clock still toggles when reset=0)
    assert "Qualified transitions:" in text

    # Should show unique values (0 and 1)
    assert "Unique values when qualified: 2" in text


@pytest.mark.asyncio
async def test_get_signal_overview_intervals_label_with_condition():
    """Test that intervals section shows condition in label (FST only)."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": FST_FILE,
        "signal_name": "tb.dut.counter",
        "condition_signal": "tb.reset",
        "condition_value": 1,
    }
    result = await server._get_signal_overview(args)

    text = result[0].text

    # Intervals should indicate the condition
    assert "Intervals (when tb.reset=1):" in text


# Tests for _find_potential_enable_signals helper


def test_find_potential_enable_signals_basic():
    """Test finding enable signals in same hierarchy."""
    all_signals = ["top.mod.data", "top.mod.valid", "top.mod.ready", "top.other.clk"]
    result = server._find_potential_enable_signals("top.mod.data", all_signals)

    assert "top.mod.valid" in result
    assert "top.mod.ready" in result
    assert "top.other.clk" not in result


def test_find_potential_enable_signals_with_de():
    """Test finding 'de' (display enable) signal."""
    all_signals = ["vga.r", "vga.g", "vga.b", "vga.de", "vga.hsync", "vga.vsync"]
    result = server._find_potential_enable_signals("vga.b", all_signals)

    assert "vga.de" in result


def test_find_potential_enable_signals_with_suffix():
    """Test finding signals with enable suffixes like _valid, _en."""
    all_signals = ["axi.data", "axi.data_valid", "axi.write_en", "axi.clk"]
    result = server._find_potential_enable_signals("axi.data", all_signals)

    assert "axi.data_valid" in result
    assert "axi.write_en" in result


def test_find_potential_enable_signals_no_matches():
    """Test when no enable signals are found."""
    all_signals = ["cpu.pc", "cpu.instruction", "cpu.alu_result"]
    result = server._find_potential_enable_signals("cpu.pc", all_signals)

    assert len(result) == 0


def test_find_potential_enable_signals_excludes_self():
    """Test that the signal itself is not returned as a match."""
    all_signals = ["mod.valid", "mod.data"]
    result = server._find_potential_enable_signals("mod.valid", all_signals)

    assert "mod.valid" not in result


# Tests for get_signal_values


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_get_signal_values(waveform_file):
    """Test get_signal_values with valid signals."""
    server._waveform_cache.clear()

    # Step 39: clock=1 (odd step), reset=0, counter=15 (max value)
    args = {
        "waveform_file": waveform_file,
        "step": 39,
        "signal_names": ["tb.clk", "tb.reset", "tb.dut.counter"],
    }
    result = await server._get_signal_values(args)

    assert isinstance(result, list)
    assert len(result) == 1

    filename = "counter.vcd" if waveform_file.endswith(".vcd") else "counter.fst"
    expected = f"""=== Signal Sampling at Step 39 ===
File: {filename}

tb.clk [1 bit] = 0x1 (1)
tb.reset [1 bit] = 0x0 (0)
tb.dut.counter [4 bits] = 0xf (15)

Total signals sampled: 3"""
    assert result[0].text == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_get_signal_values_clock_values(waveform_file):
    """Test get_signal_values returns correct clock values at different steps."""
    server._waveform_cache.clear()

    # Step 1 should have clock = 1 (odd step)
    args = {"waveform_file": waveform_file, "step": 1, "signal_names": ["tb.clk"]}
    result = await server._get_signal_values(args)
    text = result[0].text
    assert "tb.clk [1 bit] = 0x1 (1)" in text

    # Step 2 should have clock = 0 (even step)
    args["step"] = 2
    result = await server._get_signal_values(args)
    text = result[0].text
    assert "tb.clk [1 bit] = 0x0 (0)" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_get_signal_values_invalid_signal(waveform_file):
    """Test get_signal_values with a mix of valid and invalid signals."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": waveform_file,
        "step": 10,
        "signal_names": ["tb.clk", "nonexistent_signal", "tb.reset"],
    }
    result = await server._get_signal_values(args)

    text = result[0].text

    assert "tb.clk" in text
    assert "NOT FOUND" in text
    assert "tb.reset" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_get_signal_values_out_of_range(waveform_file):
    """Test get_signal_values with step out of range."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": waveform_file,
        "step": 1000,  # Way beyond 81 steps
        "signal_names": ["tb.clk"],
    }
    result = await server._get_signal_values(args)

    text = result[0].text

    assert "Error:" in text
    assert "out of range" in text


@pytest.mark.asyncio
async def test_get_signal_values_empty_list():
    """Test get_signal_values with empty signal list."""
    args = {"waveform_file": WAVEFORM_FILES[0], "step": 10, "signal_names": []}
    result = await server._get_signal_values(args)

    text = result[0].text

    assert "Error:" in text


# Tests for get_signal_statistics


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_get_signal_statistics(waveform_file):
    """Test get_signal_statistics with a counter signal."""
    server._waveform_cache.clear()

    args = {"waveform_file": waveform_file, "signal_name": "tb.dut.counter"}
    result = await server._get_signal_statistics(args)

    assert isinstance(result, list)
    assert len(result) == 1

    filename = "counter.vcd" if waveform_file.endswith(".vcd") else "counter.fst"
    expected = f"""=== Signal Statistics ===
File: {filename}
Signal: tb.dut.counter [4 bits]
Range: step 0 to 80

Min value: 0 (0x0)
Max value: 15 (0xf)
Average: 6.07
Unique values: 16
Samples analyzed: 81"""
    assert result[0].text == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_get_signal_statistics_with_range(waveform_file):
    """Test get_signal_statistics with time range."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": waveform_file,
        "signal_name": "tb.dut.counter",
        "start_time": 10,
        "end_time": 50,
    }
    result = await server._get_signal_statistics(args)

    text = result[0].text

    assert "=== Signal Statistics ===" in text
    assert "Signal: tb.dut.counter [4 bits]" in text
    assert "Range: step 10 to 50" in text
    # Counter goes from 0 (step 10) to higher values
    assert "Min value: 0 (0x0)" in text
    # Range is 10-50 inclusive = 41 samples
    assert "Samples analyzed: 41" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_get_signal_statistics_clock(waveform_file):
    """Test get_signal_statistics with clock signal (binary)."""
    server._waveform_cache.clear()

    args = {"waveform_file": waveform_file, "signal_name": "tb.clk"}
    result = await server._get_signal_statistics(args)

    # Clock toggles every step: 41 steps at 0, 40 steps at 1
    # Average = 40/81 ≈ 0.49
    filename = "counter.vcd" if waveform_file.endswith(".vcd") else "counter.fst"
    expected = f"""=== Signal Statistics ===
File: {filename}
Signal: tb.clk [1 bit]
Range: step 0 to 80

Min value: 0 (0x0)
Max value: 1 (0x1)
Average: 0.49
Unique values: 2
Samples analyzed: 81"""
    assert result[0].text == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_get_signal_statistics_invalid_signal(waveform_file):
    """Test get_signal_statistics with invalid signal."""
    server._waveform_cache.clear()

    args = {"waveform_file": waveform_file, "signal_name": "nonexistent_signal"}
    result = await server._get_signal_statistics(args)

    text = result[0].text

    assert "Error:" in text
    assert "not found" in text


# Tests for extract_bit_field


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_extract_bit_field_single_step(waveform_file):
    """Test extract_bit_field in single step mode."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": waveform_file,
        "signal_name": "tb.dut.counter",
        "high_bit": 3,
        "low_bit": 2,
        "step": 10,
    }
    result = await server._extract_bit_field(args)

    assert isinstance(result, list)
    assert len(result) == 1
    text = result[0].text

    assert "=== Bit Field Extraction ===" in text
    assert "Signal: tb.dut.counter[3:2] at step 10" in text
    assert "Original signal width: 4 bits" in text
    assert "Extracted field width: 2 bits" in text
    # At step 10, counter is still 0 (reset period ends at step 10)
    assert "Full signal value: 0x0 (0)" in text
    assert "Extracted bits [3:2]: 0x0 (0)" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_extract_bit_field_range_mode(waveform_file):
    """Test extract_bit_field in range mode (tracking transitions)."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": waveform_file,
        "signal_name": "tb.dut.counter",
        "high_bit": 3,
        "low_bit": 0,
        "start_time": 0,
        "end_time": 40,
        "limit": 10,
    }
    result = await server._extract_bit_field(args)

    text = result[0].text

    assert "=== Bit Field Extraction ===" in text
    assert "Signal: tb.dut.counter[3:0]" in text
    assert "Original signal width: 4 bits" in text
    assert "Extracted field width: 4 bits" in text
    assert "Range: step 0 to 40" in text
    # Initial value at step 0
    assert "Step 0: 0x0 (0)" in text
    # Counter increments starting at step 11
    assert "Step 11: 0x0 -> 0x1 (1)" in text
    assert "Step 13: 0x1 -> 0x2 (2)" in text
    assert "Step 15: 0x2 -> 0x3 (3)" in text
    assert "Total transitions:" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_extract_bit_field_with_limit(waveform_file):
    """Test extract_bit_field respects limit parameter."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": waveform_file,
        "signal_name": "tb.dut.counter",
        "high_bit": 3,
        "low_bit": 0,
        "start_time": 0,
        "end_time": 80,
        "limit": 5,
    }
    result = await server._extract_bit_field(args)

    text = result[0].text

    assert "limit of 5 transitions reached" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_extract_bit_field_invalid_bit_range(waveform_file):
    """Test extract_bit_field with invalid bit range."""
    server._waveform_cache.clear()

    # high_bit >= signal_width (counter is 4 bits)
    args = {
        "waveform_file": waveform_file,
        "signal_name": "tb.dut.counter",
        "high_bit": 5,
        "low_bit": 0,
        "step": 10,
    }
    result = await server._extract_bit_field(args)

    text = result[0].text

    assert "Error:" in text
    assert "Invalid bit range" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_extract_bit_field_high_less_than_low(waveform_file):
    """Test extract_bit_field with high_bit < low_bit."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": waveform_file,
        "signal_name": "tb.dut.counter",
        "high_bit": 1,
        "low_bit": 3,  # low > high is invalid
        "step": 10,
    }
    result = await server._extract_bit_field(args)

    text = result[0].text

    assert "Error:" in text
    assert "Invalid bit range" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_extract_bit_field_single_bit(waveform_file):
    """Test extract_bit_field extracting a single bit."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": waveform_file,
        "signal_name": "tb.dut.counter",
        "high_bit": 0,
        "low_bit": 0,
        "step": 10,
    }
    result = await server._extract_bit_field(args)

    text = result[0].text

    assert "=== Bit Field Extraction ===" in text
    assert "Signal: tb.dut.counter[0:0] at step 10" in text
    assert "Extracted field width: 1 bits" in text
    # At step 10, counter is 0, so bit 0 is 0
    assert "Full signal value: 0x0 (0)" in text
    assert "Extracted bits [0:0]: 0x0 (0)" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_extract_bit_field_invalid_signal(waveform_file):
    """Test extract_bit_field with invalid signal."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": waveform_file,
        "signal_name": "nonexistent_signal",
        "high_bit": 3,
        "low_bit": 0,
        "step": 10,
    }
    result = await server._extract_bit_field(args)

    text = result[0].text

    assert "Error:" in text
    assert "not found" in text


# Tests for find_signal_value


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_find_signal_value_basic(waveform_file):
    """Test find_signal_value with a basic search."""
    server._waveform_cache.clear()

    args = {"waveform_file": waveform_file, "signal_name": "tb.dut.counter", "value": 5}
    result = await server._find_signal_value(args)

    assert isinstance(result, list)
    assert len(result) == 1

    filename = "counter.vcd" if waveform_file.endswith(".vcd") else "counter.fst"
    expected = f"""=== Find Value in Range ===
File: {filename}
Signal: tb.dut.counter [4 bits]
Searching for value: 5 (0x5)
Range: step 0 to 80

Found 2 interval(s) where signal = 5:
  [1] Steps 19 to 20 (2 steps)
  [2] Steps 51 to 52 (2 steps)"""
    assert result[0].text == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_find_signal_value_with_time_range(waveform_file):
    """Test find_signal_value with a specified time range."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": waveform_file,
        "signal_name": "tb.clk",
        "value": 1,
        "start_time": 0,
        "end_time": 20,
    }
    result = await server._find_signal_value(args)

    # Clock is high at odd steps (1,3,5,...,19) = 10 intervals of 1 step each
    filename = "counter.vcd" if waveform_file.endswith(".vcd") else "counter.fst"
    expected = f"""=== Find Value in Range ===
File: {filename}
Signal: tb.clk [1 bit]
Searching for value: 1 (0x1)
Range: step 0 to 20

Found 10 interval(s) where signal = 1:
  [1] Step 1 (1 step)
  [2] Step 3 (1 step)
  [3] Step 5 (1 step)
  [4] Step 7 (1 step)
  [5] Step 9 (1 step)
  [6] Step 11 (1 step)
  [7] Step 13 (1 step)
  [8] Step 15 (1 step)
  [9] Step 17 (1 step)
  [10] Step 19 (1 step)
  ... (limited to 10 intervals)"""
    assert result[0].text == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_find_signal_value_no_match(waveform_file):
    """Test find_signal_value when value doesn't exist."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": waveform_file,
        "signal_name": "tb.dut.counter",
        "value": 100,  # Counter only goes 0-15
        "start_time": 0,
        "end_time": 40,
    }
    result = await server._find_signal_value(args)

    text = result[0].text

    assert "No matches found" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_find_signal_value_with_limit(waveform_file):
    """Test find_signal_value with a limit on results."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": waveform_file,
        "signal_name": "tb.clk",
        "value": 1,
        "limit": 3,
    }
    result = await server._find_signal_value(args)

    text = result[0].text

    assert "=== Find Value in Range ===" in text
    # Should find exactly 3 intervals due to limit
    assert "Found 3 interval(s)" in text
    # Should indicate more exist
    assert "limited to 3 intervals" in text
    # Should show [1], [2], [3] but not [4]
    assert "[1]" in text
    assert "[2]" in text
    assert "[3]" in text
    assert "[4]" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("waveform_file", WAVEFORM_FILES)
async def test_find_signal_value_invalid_signal(waveform_file):
    """Test find_signal_value with an invalid signal."""
    server._waveform_cache.clear()

    args = {
        "waveform_file": waveform_file,
        "signal_name": "nonexistent_signal",
        "value": 1,
    }
    result = await server._find_signal_value(args)

    text = result[0].text

    assert "Error:" in text
    assert "not found" in text
