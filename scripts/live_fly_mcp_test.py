"""Exercise the Fly desktop through the public MCP client boundary."""

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ImageContent


def _structured(result: Any) -> dict[str, Any]:
    payload = result.structuredContent
    return payload if isinstance(payload, dict) else {}


async def run(computer_id: str, output: Path) -> None:
    """Start the Fly MCP server and capture its live Xfce screen through tools."""
    environment = os.environ.copy()
    environment.update(
        {
            "COMPUTER_ID": computer_id,
            "DESKTOP_ENVIRONMENT": "true",
            "NETWORK_ACCESS": "true",
            "AUTO_INSTALL_FLYCTL": "true",
        }
    )
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "kilntainers", "--backend", "fly"],
        env=environment,
        cwd=Path(__file__).resolve().parents[1],
    )

    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            ui_result = await session.call_tool("computer_ui", {})
            if ui_result.isError:
                raise RuntimeError(f"computer_ui failed: {ui_result.content}")
            ui = _structured(ui_result)

            screen_result = await session.call_tool(
                "look_at_screen",
                {"include_image": True, "include_accessibility": True},
            )
            if screen_result.isError:
                raise RuntimeError(f"look_at_screen failed: {screen_result.content}")
            image = next(
                (
                    item
                    for item in screen_result.content
                    if isinstance(item, ImageContent)
                ),
                None,
            )
            if image is None:
                raise RuntimeError("look_at_screen returned no image content")

            output.parent.mkdir(parents=True, exist_ok=True)
            image_bytes = base64.b64decode(image.data)
            output.write_bytes(image_bytes)
            accessibility = _structured(screen_result).get("accessibility", {})
            print(
                json.dumps(
                    {
                        "computer_ui_url": ui.get("url"),
                        "desktop_url": ui.get("desktop_url"),
                        "computer_id": ui.get("computer_id"),
                        "desktop_environment": ui.get("desktop_environment"),
                        "screenshot": str(output.resolve()),
                        "screenshot_bytes": len(image_bytes),
                        "accessibility_applications": accessibility.get(
                            "applications", []
                        ),
                    },
                    indent=2,
                )
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--computer-id", default="fly-live-xfce")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/fly-live-xfce-mcp.png"),
    )
    args = parser.parse_args()
    asyncio.run(run(args.computer_id, args.output))


if __name__ == "__main__":
    main()
