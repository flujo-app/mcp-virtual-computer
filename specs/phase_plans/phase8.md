# Phase 8: Streamable HTTP Transport

Implementation plan for adding Streamable HTTP support to Kilntainers.

## Status

Most HTTP transport infrastructure is already in place from earlier phases. This phase completes the HTTP support with session timeout integration and integration tests.

## What's Already Done

From earlier phases (1-7), the following HTTP support is already implemented:

- **CLI arguments**: `--transport http`, `--host`, `--port`, `--session-timeout` (cli.py)
- **Validation**: HTTP-only args rejected in stdio mode (cli.py:validate_config)
- **Transport selection**: Maps "http" → "streamable-http" (cli.py)
- **Per-session sandbox creation**: Lifespan creates sandbox per session (server.py:create_lifespan)
- **Death propagation for HTTP**: Request-time detection via SandboxDiedError (server.py)
- **Server config**: Includes session_timeout field (config.py)

## What Needs to Be Done

### 1. Session Timeout Integration

**Architecture reference**: `connection_lifecycle.md` §4.2

FastMCP's `StreamableHTTPSessionManager` needs to be configured with the session timeout. The implementation approach:

1. Check if FastMCP constructor accepts a session timeout parameter
2. If not, implement manual transport setup with direct `StreamableHTTPSessionManager` configuration
3. Update `create_server()` to pass session timeout to the transport layer

**Implementation file**: `src/kilntainers/server.py`

### 2. Integration Tests for HTTP Mode

**Architecture reference**: `connection_lifecycle.md` §11.3

Add comprehensive integration tests for HTTP transport:

1. **HTTP session lifecycle test**: Verify session creates sandbox, exec works, cleanup on disconnect
2. **Concurrent sessions test**: Multiple sessions create independent sandboxes
3. **Session timeout test**: Verify idle sessions are cleaned up after timeout
4. **Server shutdown test**: SIGTERM with active sessions tears down all sandboxes
5. **Session isolation test**: Verify sessions don't share state

**Implementation file**: `src/kilntainers/test_http_integration.py` (new file)

### 3. Verify Death Propagation for HTTP

**Architecture reference**: `connection_lifecycle.md` §6.4

The request-time death detection is already implemented (SandboxDiedError in handler). Add a test to verify:
- Sandbox death is detected on next tool call
- Session returns error response
- No proactive session termination needed for v1 (Option A from architecture doc)

## Implementation Order

### Step 1: Check FastMCP session timeout support

Create a simple test to check if FastMCP's constructor accepts session timeout or related parameters.

### Step 2: Implement session timeout integration

Update `create_server()` to configure session timeout:
- If FastMCP supports it directly: pass parameter to constructor
- Otherwise: implement manual transport setup with `StreamableHTTPSessionManager`

### Step 3: Add HTTP integration tests

Create `test_http_integration.py` with comprehensive tests:
- `test_http_session_lifecycle()`
- `test_http_concurrent_sessions()`
- `test_http_session_timeout()`
- `test_http_server_shutdown_with_active_sessions()`
- `test_http_session_isolation()`

### Step 4: Verify death propagation

Add test for request-time death detection in HTTP mode.

### Step 5: Run all checks

- Run unit tests
- Run integration tests (including docker integration)
- Run format check
- Run lint check
- Run typecheck
- Fix any issues

## Tests to Implement

See `connection_lifecycle.md` §11.3 for detailed test specifications.

Key tests:
1. **Idle Session Timeout**: Create HTTP server with short timeout, verify sandbox cleanup after idle period
2. **Concurrent Sessions**: Multiple clients connect simultaneously, verify independent sandboxes
3. **Server SIGTERM**: With active sessions, verify all sandboxes stopped concurrently
4. **Session Isolation**: State changes in one session don't affect another

## Architecture References

- `connection_lifecycle.md` §3-§5: HTTP lifecycle, session timeout, death propagation
- `connection_lifecycle.md` §7.3: Server shutdown with active sessions
- `connection_lifecycle.md` §11.3: HTTP integration tests
- `mcp_server.md` §5.2: Streamable HTTP wiring
- `cli_and_startup.md` §4.2: HTTP-only arg detection

## Success Criteria

1. Session timeout is properly configured with FastMCP/StreamableHTTPSessionManager
2. Idle sessions are cleaned up after configured timeout
3. Multiple concurrent HTTP sessions work with independent sandboxes
4. Server SIGTERM tears down all active sessions cleanly
5. All integration tests pass
6. All checks pass (format, lint, typecheck, tests)
