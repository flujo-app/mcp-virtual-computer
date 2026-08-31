"""End-to-end tests for the MCP protocol over stdio.

These tests run the kilntainers server as a subprocess and communicate
with it using the actual MCP protocol (JSON-RPC over stdio).

They require Docker to be running.

Run with: pytest -m e2e
Skip with: pytest -m "not e2e"
"""

import asyncio
import json
import sys

import pytest

from kilntainers.backends.test_docker_integration import get_docker_backend


@pytest.fixture
async def docker_available():
    """Skip if Docker is not available.

    This fixture uses the get_docker_backend helper to check availability.
    """
    try:
        await get_docker_backend("docker")
    except Exception:
        # get_docker_backend already calls pytest.skip with appropriate messages
        raise


class TestE2EStdioProtocol:
    """End-to-end tests for MCP stdio protocol communication."""

    @pytest.mark.e2e
    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_full_mcp_initialize_and_tools_list(self, docker_available):
        """Test full MCP protocol: initialize request, tools/list response."""

        async def run_session():
            # Start kilntainers as a subprocess
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "kilntainers",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            assert proc.stdin is not None
            assert proc.stdout is not None
            assert proc.stderr is not None

            # Wait a moment for startup
            await asyncio.sleep(1)

            try:
                # Send initialize request
                initialize_request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "test-client",
                            "version": "1.0.0",
                        },
                    },
                }

                request_json = json.dumps(initialize_request) + "\n"
                proc.stdin.write(request_json.encode())
                await proc.stdin.drain()

                # Read initialize response
                response_line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=30
                )
                assert response_line, "No response from server"

                response = json.loads(response_line.decode())
                assert response.get("jsonrpc") == "2.0"
                assert response.get("id") == 1
                assert "result" in response

                # Send initialized notification
                initialized_notification = {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                }

                notification_json = json.dumps(initialized_notification) + "\n"
                proc.stdin.write(notification_json.encode())
                await proc.stdin.drain()

                # Send tools/list request
                tools_list_request = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                }

                request_json = json.dumps(tools_list_request) + "\n"
                proc.stdin.write(request_json.encode())
                await proc.stdin.drain()

                # Read tools/list response
                response_line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=10
                )
                assert response_line, "No tools/list response"

                response = json.loads(response_line.decode())
                assert response.get("jsonrpc") == "2.0"
                assert response.get("id") == 2
                assert "result" in response

                tools = response["result"].get("tools", [])
                tool_names = {tool["name"] for tool in tools}
                assert tool_names == {"terminal_execute"}
                assert all("description" in tool for tool in tools)

            finally:
                # Close stdin to signal shutdown
                proc.stdin.close()
                await proc.wait()
                # Clean up stderr
                _ = await proc.stderr.read()

        await run_session()

    @pytest.mark.e2e
    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_mcp_tool_call_execution(self, docker_available):
        """Test executing a tool call through the MCP protocol."""

        async def run_session():
            # Start kilntainers as a subprocess
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "kilntainers",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            assert proc.stdin is not None
            assert proc.stdout is not None
            assert proc.stderr is not None

            # Wait for startup
            await asyncio.sleep(1)

            try:
                # Initialize
                init_request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                }
                proc.stdin.write(json.dumps(init_request).encode() + b"\n")
                await proc.stdin.drain()
                _ = await asyncio.wait_for(proc.stdout.readline(), timeout=30)

                # Send initialized
                initialized_notif = {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                }
                proc.stdin.write(json.dumps(initialized_notif).encode() + b"\n")
                await proc.stdin.drain()

                # Execute shell command
                tool_call_request = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "terminal_execute",
                        "arguments": {"command": "echo hello world"},
                    },
                }
                proc.stdin.write(json.dumps(tool_call_request).encode() + b"\n")
                await proc.stdin.drain()

                # Read response
                response_line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=30
                )
                assert response_line, "No tool call response"

                response = json.loads(response_line.decode())
                assert response.get("jsonrpc") == "2.0"
                assert response.get("id") == 2
                assert "result" in response

                # Check response content
                result = response["result"]
                assert "content" in result
                assert len(result["content"]) == 1
                assert result["content"][0]["type"] == "text"

                # Parse the JSON response
                response_data = json.loads(result["content"][0]["text"])
                assert response_data["exit_code"] == 0
                assert "hello world" in response_data["stdout"]

            finally:
                proc.stdin.close()
                await proc.wait()
                _ = await proc.stderr.read()

        await run_session()


class TestE2EErrorHandling:
    """Tests for error handling in E2E scenarios."""

    @pytest.mark.e2e
    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_invalid_tool_call_returns_error(self, docker_available):
        """Invalid tool call parameters return isError=true response."""

        async def run_session():
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "kilntainers",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            assert proc.stdin is not None
            assert proc.stdout is not None
            assert proc.stderr is not None

            await asyncio.sleep(1)

            try:
                # Initialize
                init_request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                }
                proc.stdin.write(json.dumps(init_request).encode() + b"\n")
                await proc.stdin.drain()
                _ = await asyncio.wait_for(proc.stdout.readline(), timeout=30)

                initialized_notif = {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                }
                proc.stdin.write(json.dumps(initialized_notif).encode() + b"\n")
                await proc.stdin.drain()

                # Invalid tool call: both command and args
                tool_call_request = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "terminal_execute",
                        "arguments": {"command": "ls", "args": ["/bin/ls"]},
                    },
                }
                proc.stdin.write(json.dumps(tool_call_request).encode() + b"\n")
                await proc.stdin.drain()

                response_line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=10
                )
                assert response_line

                response = json.loads(response_line.decode())
                assert response.get("jsonrpc") == "2.0"
                assert response.get("id") == 2
                assert "result" in response

                result = response["result"]
                assert result.get("isError") is True
                assert "content" in result
                assert "Cannot provide both" in result["content"][0]["text"]

            finally:
                proc.stdin.close()
                await proc.wait()
                _ = await proc.stderr.read()

        await run_session()
