# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Installation & Setup
```bash
pip install -e .                    # Install in development mode
pip install -e .[dev]               # Install with dev dependencies
```

### Testing
```bash
pytest                              # Run all tests
pytest tests/test_server.py         # Run specific test file
pytest -v                          # Verbose test output
```

### Code Quality
```bash
black .                             # Format code with Black
ruff .                              # Run linting with Ruff
```

### Running the Server
```bash
waveform-mcp                        # Run MCP server (console script entry point)
```

## Architecture Overview

This is an MCP (Model Context Protocol) server that provides waveform analysis tools for RTL simulations. Everything lives in one module, `src/waveform_mcp/server.py` (~2560 lines).

### Module Layout

- Documentation tables: `WAL_DOCUMENTATION` (server.py:45-185) holds the static help text served by `get_wal_help`.
- Tool declarations: `list_tools` (server.py:188-466) advertises ten tools.
- Dispatch: `call_tool` (server.py:469-499) routes eleven tool names to `_`-prefixed handlers and converts any exception into a text error.
- Shared helpers: `_load_waveform` (server.py:502), `_get_timescale_info` (server.py:553), the FST fast path (server.py:695-855), and `_get_wal_error_suggestions` (server.py:1214).
- Entry points: `_async_main` / `main` / `cli` (server.py:2531-2557), stdio transport.

### Tools

Ten tools are advertised, in three groups.

1. **Discovery and structure**: `get_signal_list`, `get_waveform_length` (returns step count plus timescale and first/last timestamps).
2. **Signal analysis**: `get_signal_transitions`, `get_signal_overview`, `get_signal_values`, `get_signal_statistics`, `extract_bit_field`, `find_signal_value`.
3. **Documentation**: `get_wal_help`, `get_wal_examples`.

`execute_wal_expression` is the eleventh handler. It is still routed by `call_tool` and fully implemented at server.py:1072, but it was removed from `list_tools`, so clients do not see it unless they call it by name. Keep the handler and the routing entry in place when editing that area.

### Key Technical Details

**Step indices, not time units**: every time argument across the tools is a 0-based time step index. An `end_time` or `end_step` of `0` means end of simulation. Keep new parameter descriptions in this vocabulary, since mixing in simulation time units is what the descriptions were rewritten to avoid.

**FST fast path**: `_get_fst_signal_transitions_direct` (server.py:728) walks one signal's own transitions through pylibfst callbacks instead of iterating every global timestamp. Handlers detect FST with `hasattr(trace, "fst")`, try the fast path, and fall back to WAL queries on failure. `get_signal_overview` is FST-only and returns an error for other formats.

**Persistent FST callbacks**: `_fst_callbacks` (server.py:39) holds CFFI callbacks created once by `_init_fst_callbacks`. Per-call state is passed through `ffi.new_handle`. Do not create callbacks per call; that leaks.

**Caching**: `_waveform_cache` maps a path to `(mtime, TraceContainer)`. `_load_waveform` reloads when the modification time changes (server.py:528-540).

**Result limits**: most tools take a `limit` and truncate, so large traces cannot flood the context. Defaults are 100 signals, 20 overview intervals, 10 elsewhere.

**Conditional overview**: `get_signal_overview` accepts `condition_signal` and `condition_value` and only analyzes the main signal while the qualifier holds that value. Its tool description tells callers to supply one, because data signals are meaningful only while their valid or enable signal is asserted. `_find_potential_enable_signals` (server.py:1551) suggests candidates.

**Error handling**: `_get_wal_error_suggestions` (server.py:1214) uses `difflib.get_close_matches` against the file's signals to propose fixes for bad WAL expressions.

**Signal categorization**: `get_wal_examples` buckets signals by substring match on the name — `clk`, `reset`/`rst`, `counter`/`count`, everything else as data (server.py:1440-1450). It emits one section per non-empty clock/reset/counter bucket, using only that bucket's first signal; the data bucket is computed but currently unused, and the multi-signal, debugging and timing sections index `all_signals` directly.

**WAL integration**: `TraceContainer` loads waveforms, `SEval` evaluates expressions. WAL uses Lisp-like syntax, `(find (= signal value))`, with `&&` and `||` for logical operators.

## Development Notes

### Dependencies
- **MCP SDK**: `mcp>=1.0.0` for Model Context Protocol server functionality
- **WAL**: `wal-lang>=0.8.0` for waveform analysis language support
- **pylibfst**: FST (Fast Signal Trace) reading and the FST fast path

### Test Structure
`tests/test_server.py` holds about 60 tests, parametrized over both formats against `tests/traces/counter.vcd` and `tests/traces/counter.fst`. Format-specific expectations are branched on the file suffix, for example the VCD timescale reads `1s` where FST reads `s`.

### Configuration
- Black line length: 88 characters
- Python target: 3.10+
- Pytest runs in async mode (configured in pyproject.toml)
