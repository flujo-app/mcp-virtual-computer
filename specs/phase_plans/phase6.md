# Phase 6: End-to-End stdio & Integration Testing

## Overview

Verify the full pipeline works end-to-end: CLI → startup → FastMCP → tool call → Docker backend → response. Add integration tests and polish.

**Architecture references:**
- [connection_lifecycle.md](../architecture/connection_lifecycle.md) §2 (stdio lifecycle), §6 (death propagation), §7 (graceful shutdown)
- [error_handling.md](../architecture/error_handling.md) §4 (startup errors), §7 (full propagation summary)

## Implementation Steps

### Step 1: Add Lifecycle Integration Tests

Create `src/kilntainers/test_lifecycle_integration.py` with:

1. **Full stdio lifecycle test**: Start a server programmatically (not via CLI), enter lifespan, verify sandbox created, exec commands, exit lifespan, verify sandbox stopped
2. **Sandbox creation failure test**: Test that backend creation failure during lifespan raises properly
3. **Graceful shutdown test**: Verify that `finally` block runs correctly and stops sandbox even when exception occurs
4. **Death propagation test**: Kill container externally during lifespan, verify SIGTERM is sent (mock os.kill)

**Test patterns from connection_lifecycle.md §11:**
- Mock `os.kill` to avoid actually sending signals
- Use real Docker backend for container creation
- Verify call ordering — death_task cancelled before stop, stop always called in finally

### Step 2: Add CLI Integration Tests

Create `src/kilntainers/test_cli_integration.py` with:

1. **Full CLI startup test**: Run `kilntainers` as subprocess, verify it starts without errors
2. **--help output test**: Verify help text is well-structured and complete
3. **Backend validation failure**: Run with Docker not running, verify proper error message to stderr

### Step 3: Add End-to-End MCP Protocol Tests

Create `src/kilntainers/test_e2e_mcp.py` with:

1. **Full MCP stdio test**: Run the actual MCP server via stdio, send real MCP protocol messages (initialize, tools/list, tools/call), verify responses
2. **Multiple sequential tool calls**: Test state persistence across calls in same session
3. **Sandbox death during session**: Kill container during active session, verify error response
4. **Graceful shutdown test**: Close stdin, verify server exits cleanly and container is removed

This uses the actual MCP protocol via subprocess stdio, not a mock client.

### Step 4: Polish

1. **Review --help output**: Ensure all arguments are well-documented (add automated test)
2. **Verify error messages**: All error paths have clear, actionable messages

## Module Changes

### New Files

- `src/kilntainers/test_lifecycle_integration.py` — Lifecycle integration tests
- `src/kilntainers/test_cli_integration.py` — CLI integration tests
- `src/kilntainers/test_e2e_mcp.py` — End-to-end MCP protocol tests

## Testing

### Unit Tests (already exist)

- `test_server.py` — Lifespan, death propagation (unit level with mocks)
- `test_cli.py` — Parser, config, validation
- `test_docker.py` — Docker backend unit tests
- `test_docker_integration.py` — Backend integration tests

### New Integration Tests

**Lifecycle tests (`test_lifecycle_integration.py`):**
- Full stdio lifecycle with real Docker backend
- Sandbox creation failure handling
- Graceful shutdown with exception
- Death propagation (mock os.kill)
- Cleanup verification

**CLI tests (`test_cli_integration.py`):**
- CLI startup doesn't crash
- Help output structure
- Backend validation produces proper error

## Acceptance Criteria

Phase 6 is complete when:

1. ✅ All lifecycle integration tests pass
2. ✅ All CLI integration tests pass
3. ✅ CI runs unit tests without Docker
4. ✅ CI runs integration tests with Docker (on ubuntu-latest)
5. ✅ Manual E2E test passes (Claude Desktop or similar)
6. ✅ `uv run ./checks.sh` passes all checks
7. ✅ Mark phase 6 checkboxes in implementation_plan.md
