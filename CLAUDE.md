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

This is an MCP (Model Context Protocol) server that provides waveform analysis tools for RTL simulations. The architecture centers around:

### Core Components

**`src/waveform_mcp/server.py`** - Main server implementation with these key elements:
- MCP server setup using stdio transport (server.py:826-838)
- Waveform caching system with file modification time tracking (server.py:30-31, 333-379)
- Six main tools exposed via MCP for waveform analysis
- WAL (Waveform Analysis Language) integration via `wal.core.TraceContainer` and `wal.eval.SEval`

### Tool Architecture

The server exposes 6 MCP tools organized into categories:

1. **Basic Waveform Operations**:
   - `get_signal_list` - Signal discovery with regex filtering
   - `get_signal_transitions` - Signal change detection over time ranges
   - `get_waveform_length` - Simulation duration analysis

2. **Advanced Analysis**:
   - `execute_wal_expression` - Direct WAL query execution with comprehensive error handling

3. **Documentation & Help**:
   - `get_wal_help` - Built-in WAL documentation (server.py:34-178)
   - `get_wal_examples` - Context-aware examples based on actual waveform signals

### Key Technical Details

**Caching Strategy**: File-based caching using modification time validation (server.py:360-367) prevents redundant WAL parsing of large waveform files.

**Error Handling**: Comprehensive error handling with context-sensitive suggestions (server.py:663-704) helps users construct valid WAL expressions.

**WAL Integration**: Uses WAL's `TraceContainer` for waveform loading and `SEval` for expression evaluation. WAL expressions use Lisp-like syntax: `(find (= signal value))`.

**Signal Categorization**: The `get_wal_examples` tool automatically categorizes signals by type (clock, reset, counter, data) to provide relevant examples (server.py:729-733).

## Development Notes

### Dependencies
- **MCP SDK**: `mcp>=1.0.0` for Model Context Protocol server functionality
- **WAL**: `wal-lang>=0.8.0` for waveform analysis language support
- **pylibfst**: For FST (Fast Signal Trace) format support

### Test Structure
Tests use parametrized fixtures to test both VCD and FST formats. Sample waveform files are expected in `tests/traces/` directory.

### Configuration
- Black line length: 88 characters
- Python target: 3.10+
- Pytest runs in async mode (configured in pyproject.toml)