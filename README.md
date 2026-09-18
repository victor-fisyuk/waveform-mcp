# Waveform MCP Server

MCP (Model Context Protocol) server for RTL waveform analysis using WAL (Waveform Analysis Language).

> **Note on this fork**
>
> This is a fork created for the [Voodoo FPGA project](https://github.com/victor-fisyuk/voodoo-fpga-public).
> It improves the existing tools and adds new ones. I use it heavily and it works well for me, but
> **the changes are draft quality**: they are not completely tested, and the source code needs
> refactoring before it could be called production quality. Expect rough edges, and treat the
> behavior described below as what works in my own use rather than as a guarantee.
>
> Published **as is**, with no support, no warranty and no promise of maintenance or stable
> interfaces. Use it at your own risk.

## Time arguments

All time arguments (`start_time`, `end_time`, `start_step`, `end_step`, `step`) are **time step
indices**, not simulation time units. Step 0 is the first recorded time step. An `end_time` or
`end_step` of `0` means "end of simulation". Use `get_waveform_length` to translate steps to
simulation time.

## Tools

### get_signal_list
Get hierarchical list of signals from waveform file with optional regex filtering.
- `waveform_file` (required): Path to waveform file
- `pattern` (optional): Regex pattern to filter signals
- `limit` (optional): Maximum number of signals to return, default 100

**Example:**
```json
{"tool": "get_signal_list", "arguments": {"waveform_file": "sim.vcd", "pattern": "cpu.*", "limit": 50}}
```

### get_signal_transitions
Extract signal transitions within a step range.
- `waveform_file` (required): Path to waveform file
- `signal_name` (required): Full signal name
- `start_time` (optional): Start step index, default 0
- `end_time` (optional): End step index, default 0 (end of simulation)
- `limit` (optional): Maximum number of transitions to return, default 10

**Example:**
```json
{"tool": "get_signal_transitions", "arguments": {"waveform_file": "sim.vcd", "signal_name": "clk", "start_time": 0, "end_time": 100, "limit": 20}}
```

### get_waveform_length
Get the simulation length in time steps, along with the timescale and the first and last
timestamps in simulation time.
- `waveform_file` (required): Path to waveform file

**Example:**
```json
{"tool": "get_waveform_length", "arguments": {"waveform_file": "sim.vcd"}}
```

### get_signal_overview
Summarize a signal's behavior as constant-value intervals and high-activity bursts. Supports
conditional analysis, where the signal is only examined while a qualifying signal equals a given
value. Pass a `condition_signal` in most cases, since data signals are usually meaningful only
while their valid or enable signal is asserted. **FST files only.**
- `waveform_file` (required): Path to FST waveform file
- `signal_name` (required): Signal to analyze
- `start_step` (optional): Start step index, default 0
- `end_step` (optional): End step index, default 0 (end of simulation)
- `limit` (optional): Maximum number of intervals to return, default 20
- `condition_signal` (optional): Qualifying signal name, such as `valid`, `enable` or `de`
- `condition_value` (optional): Value the qualifying signal must have, default 1

**Example:**
```json
{"tool": "get_signal_overview", "arguments": {"waveform_file": "sim.fst", "signal_name": "top.pixel_data", "condition_signal": "top.de", "condition_value": 1}}
```

### get_signal_values
Sample several signals at one time step for correlation analysis.
- `waveform_file` (required): Path to waveform file
- `step` (required): Step index to sample at
- `signal_names` (required): Array of signal names

**Example:**
```json
{"tool": "get_signal_values", "arguments": {"waveform_file": "sim.fst", "step": 4200, "signal_names": ["top.pc", "top.valid"]}}
```

### get_signal_statistics
Compute min, max and average for a signal over a step range.
- `waveform_file` (required): Path to waveform file
- `signal_name` (required): Signal to analyze
- `start_time` (optional): Start step index, default 0
- `end_time` (optional): End step index, default 0 (end of simulation)

**Example:**
```json
{"tool": "get_signal_statistics", "arguments": {"waveform_file": "sim.fst", "signal_name": "top.counter"}}
```

### extract_bit_field
Extract a bit slice from a signal, either at one step or tracked across a range.
- `waveform_file` (required): Path to waveform file
- `signal_name` (required): Signal to extract from
- `high_bit` (required): High bit index, inclusive, 0-based
- `low_bit` (required): Low bit index, inclusive, 0-based
- `step` (optional): Single step to extract at; when given, returns one value instead of transitions
- `start_time` (optional): Start step index for transition tracking, default 0
- `end_time` (optional): End step index for transition tracking, default 0 (end of simulation)
- `limit` (optional): Maximum number of transitions to return, default 10

**Example:**
```json
{"tool": "extract_bit_field", "arguments": {"waveform_file": "sim.fst", "signal_name": "top.cmd", "high_bit": 7, "low_bit": 4}}
```

### find_signal_value
Find the steps at which a signal holds a given value.
- `waveform_file` (required): Path to waveform file
- `signal_name` (required): Signal to search
- `value` (required): Value to search for
- `start_time` (optional): Start step index, default 0
- `end_time` (optional): End step index, default 0 (end of simulation)
- `limit` (optional): Maximum number of matches to return, default 10

**Example:**
```json
{"tool": "find_signal_value", "arguments": {"waveform_file": "sim.fst", "signal_name": "top.state", "value": 3, "limit": 20}}
```

### get_wal_help
Get WAL documentation and syntax reference.
- `topic` (optional): Help topic (`overview`, `functions`, `examples`, `debugging`, `syntax`)

**Example:**
```json
{"tool": "get_wal_help", "arguments": {"topic": "examples"}}
```

### get_wal_examples
Generate WAL examples for your waveform. Signal names are matched against `clk`, `reset`/`rst` and
`counter`/`count`; each group that matches adds a section of examples built from the first signal in
it. The remaining sections use the first and last signals in the file.
- `waveform_file` (required): Path to waveform file

**Example:**
```json
{"tool": "get_wal_examples", "arguments": {"waveform_file": "sim.vcd"}}
```

### execute_wal_expression (not advertised)
Execute a raw WAL expression. The handler is implemented and answers calls, but the tool is
deliberately left out of the advertised tool list, so most MCP clients will not offer it. Errors
come back with suggestions derived from the signals in the file.
- `waveform_file` (required): Path to waveform file
- `expression` (required): WAL expression to execute
- `limit` (optional): Maximum number of result items to return, default 10

**Example:**
```json
{"tool": "execute_wal_expression", "arguments": {"waveform_file": "sim.vcd", "expression": "(find (= clk 1))", "limit": 20}}
```

## Supported Formats

- VCD (Value Change Dump)
- FST (Fast Signal Trace)
- Other formats supported by WAL

FST files get a fast path that iterates a single signal's own transitions through pylibfst instead
of walking every global timestamp, which matters a lot on large traces. `get_signal_overview`
requires FST. The other tools fall back to WAL queries for non-FST files.

## Credits

Built on [WAL (Waveform Analysis Language)](https://github.com/ics-jku/wal), a domain-specific language for hardware waveform analysis. See the [WAL website](https://wal-lang.org/) for more information.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Development

To set up a development environment, install the `dev` dependencies:

```bash
pip install -e .[dev]
```

## Testing

To run the test suite:

```bash
pytest
```

## Usage

Add to your MCP client configuration:

```json
{
  "mcpServers": {
    "waveform": {
      "type": "stdio",
      "command": "waveform-mcp",
      "args": []
    }
  }
}
```

For WAL expression syntax and advanced examples, see the [WAL documentation](https://wal-lang.org/documentation/usage).

## Requirements

- Python 3.10+
- `cmake` (for FST support)
- [WAL (Waveform Analysis Language)](https://github.com/ics-jku/wal) >= 0.8.0
- MCP Python SDK >= 1.0.0
- `pylibfst` (FST reading and the FST fast path)

## License

BSD 3-Clause License. See [LICENSE](LICENSE) for details.
