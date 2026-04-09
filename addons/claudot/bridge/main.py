#!/usr/bin/env python3
"""
Claudot Bridge Daemon - Entry point.

Two operating modes:
1. MCP mode (default): Bridge is MCP server (stdio), accepts TCP from Godot
2. Legacy mode: Bridge spawns Claude subprocess and relays between TCP and subprocess
"""

import argparse
import asyncio
import json
import logging
import platform
import signal
import sys
from typing import Optional

from tcp_server import TCPServer
from claude_subprocess import ClaudeSubprocess

# Conditional import for MCP server (requires fastmcp package)
try:
    from mcp_server import create_mcp_server
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    create_mcp_server = None

logger = logging.getLogger(__name__)


class BridgeDaemon:
    """Main bridge daemon orchestrating TCP server and MCP/Claude subprocess."""

    def __init__(
        self,
        host: str,
        port: int,
        claude_command: str,
        log_level: str,
        mode: str
    ):
        """
        Initialize bridge daemon.

        Args:
            host: TCP server host
            port: TCP server port
            claude_command: Command to invoke Claude CLI (legacy mode only)
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
            mode: Operating mode ("mcp" or "legacy")
        """
        self.host = host
        self.port = port
        self.claude_command = claude_command
        self.log_level = log_level
        self.mode = mode

        self.tcp_server: Optional[TCPServer] = None
        self.claude_subprocess: Optional[ClaudeSubprocess] = None
        self.mcp_server = None
        self.mcp_task: Optional[asyncio.Task] = None
        self.shutdown_event = asyncio.Event()

    def setup_logging(self):
        """Configure logging to stderr."""
        numeric_level = getattr(logging, self.log_level.upper(), logging.INFO)

        logging.basicConfig(
            level=numeric_level,
            format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            stream=sys.stderr
        )

        logger.info(f"Logging configured at {self.log_level.upper()} level")

    async def on_claude_message(self, data: dict):
        """
        Handle message from Claude subprocess stdout.

        Args:
            data: Parsed JSON from Claude
        """
        logger.debug(f"Claude -> Clients: {data}")

        if self.tcp_server:
            await self.tcp_server.broadcast(data)

    async def on_claude_crash(self, exit_code: int, stderr_lines: list[str]):
        """
        Handle Claude subprocess crash.

        Args:
            exit_code: Subprocess exit code
            stderr_lines: Last N lines from stderr
        """
        logger.error(f"Claude subprocess crashed with exit code {exit_code}")

        crash_notification = {
            "type": "bridge/subprocess_crashed",
            "exit_code": exit_code,
            "stderr": stderr_lines
        }

        if self.tcp_server:
            await self.tcp_server.broadcast(crash_notification)

        # Trigger shutdown
        logger.info("Triggering shutdown due to subprocess crash")
        self.shutdown_event.set()

    async def on_client_message(self, data: dict, writer):
        """
        Handle message from TCP client (Godot).

        Args:
            data: Parsed JSON from client
            writer: Client stream writer (for client-specific responses)
        """
        logger.debug(f"Client message: {data}")

        # In MCP mode, check if this is a response to a pending MCP request
        if self.mode == "mcp" and self.mcp_server:
            # Check if message has an "id" field matching a pending request
            if "id" in data and hasattr(self.mcp_server, "_pending_requests"):
                request_id = data["id"]
                pending = self.mcp_server._pending_requests

                # Check if this ID is in pending requests
                async with pending._lock:
                    if request_id in pending._pending:
                        # This is an MCP response, route to pending requests
                        if "error" in data:
                            await pending.reject(request_id, data["error"])
                        else:
                            await pending.resolve(request_id, data)
                        return  # Don't forward to Claude subprocess

            # Check if this is a chat message from Godot
            if data.get("method") == "chat/send":
                logger.info(f"Received chat message from Godot")

                # Extract message content and context
                params = data.get("params", {})
                content = params.get("content", "")
                context = params.get("context", {})

                # Forward to Claude via MCP sampling
                if hasattr(self.mcp_server, "_forward_chat_message"):
                    try:
                        response_text = await self.mcp_server._forward_chat_message(content, context)

                        # Send response back to Godot
                        response_msg = {
                            "jsonrpc": "2.0",
                            "method": "chat/response",
                            "params": {
                                "content": response_text,
                                "timestamp": data.get("id", 0)  # Use original message ID as timestamp
                            }
                        }

                        # Send to the specific client
                        json_str = json.dumps(response_msg) + "\n"
                        writer.write(json_str.encode())
                        await writer.drain()

                        logger.info("Sent response back to Godot")

                    except Exception as e:
                        logger.error(f"Failed to handle chat message: {e}", exc_info=True)
                else:
                    logger.warning("Chat forwarding not available on MCP server")
            else:
                # Not an MCP response or chat message, just log it
                logger.info(f"Godot message (MCP mode): {data.get('method', 'unknown')}")

        # In legacy mode, forward to Claude subprocess
        elif self.mode == "legacy" and self.claude_subprocess:
            success = await self.claude_subprocess.send_message(data)
            if not success:
                logger.warning("Failed to relay client message to Claude")

    async def run(self):
        """Main daemon loop."""
        logger.info(f"Starting Claudot Bridge Daemon in {self.mode.upper()} mode")

        try:
            # Create TCP server (both modes)
            self.tcp_server = TCPServer(
                self.host,
                self.port,
                self.on_client_message
            )

            # Start TCP server as background task
            server_task = asyncio.create_task(self.tcp_server.start())

            if self.mode == "mcp":
                # MCP mode: Start MCP server with stdio transport
                if not MCP_AVAILABLE:
                    logger.error("MCP mode requires fastmcp package: pip install fastmcp")
                    raise RuntimeError("fastmcp package not installed")

                logger.info("Starting MCP server with stdio transport")
                self.mcp_server = create_mcp_server(self.tcp_server)

                # Run MCP server (this blocks until server stops)
                # The MCP server handles stdin/stdout for JSON-RPC
                try:
                    # Import FastMCP's run method - it handles stdio internally
                    # We'll run it as a task so we can also handle shutdown
                    self.mcp_task = asyncio.create_task(self._run_mcp_server())

                    # Wait for shutdown signal
                    await self.shutdown_event.wait()

                except Exception as e:
                    logger.error(f"MCP server error: {e}", exc_info=True)
                    raise

            else:
                # Legacy mode: Start Claude subprocess
                logger.info("Starting Claude subprocess in legacy mode")
                self.claude_subprocess = ClaudeSubprocess(
                    self.on_claude_message,
                    self.on_claude_crash
                )
                await self.claude_subprocess.start(self.claude_command)

                # Wait for shutdown signal
                await self.shutdown_event.wait()

            logger.info("Shutdown signal received")

            # Cancel tasks
            tasks_to_cancel = [server_task]
            server_task.cancel()

            if self.mcp_task:
                self.mcp_task.cancel()
                tasks_to_cancel.append(self.mcp_task)

            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        except Exception as e:
            logger.error(f"Fatal error in daemon: {e}", exc_info=True)
            raise
        finally:
            await self.shutdown()

    async def _run_mcp_server(self):
        """Run the MCP server's main loop."""
        try:
            # The FastMCP.run() method is synchronous and handles stdio transport
            # We need to run it in an executor to avoid blocking
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.mcp_server.run)
        except Exception as e:
            logger.error(f"MCP server run error: {e}", exc_info=True)
            # Trigger shutdown on MCP server failure
            self.shutdown_event.set()

    async def shutdown(self):
        """Clean shutdown of all components."""
        logger.info("Shutting down bridge daemon")

        try:
            # Stop TCP server
            if self.tcp_server:
                await self.tcp_server.stop()

            # Stop Claude subprocess (legacy mode)
            if self.claude_subprocess:
                await self.claude_subprocess.stop()

            # Stop MCP server (MCP mode)
            if self.mcp_task and not self.mcp_task.done():
                self.mcp_task.cancel()
                await asyncio.gather(self.mcp_task, return_exceptions=True)

        except Exception as e:
            logger.error(f"Error during shutdown: {e}")

        logger.info("Bridge daemon stopped")

    def signal_handler(self, signum):
        """Handle shutdown signals (SIGINT, SIGTERM)."""
        logger.info(f"Received signal {signum}, initiating shutdown")
        self.shutdown_event.set()


async def main():
    """Entry point for bridge daemon."""
    parser = argparse.ArgumentParser(
        description="Claudot Bridge Daemon - MCP server and TCP relay to Godot"
    )

    parser.add_argument(
        "--mode",
        default="mcp",
        choices=["mcp", "legacy"],
        help="Operating mode: 'mcp' (MCP server with stdio) or 'legacy' (Claude subprocess) (default: mcp)"
    )

    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="TCP server host (default: 127.0.0.1)"
    )

    parser.add_argument(
        "--port",
        type=int,
        default=7777,
        help="TCP server port (default: 7777)"
    )

    parser.add_argument(
        "--claude-command",
        default="claude",
        help="Command to invoke Claude CLI (legacy mode only, default: claude)"
    )

    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)"
    )

    args = parser.parse_args()

    # Create daemon
    daemon = BridgeDaemon(
        host=args.host,
        port=args.port,
        claude_command=args.claude_command,
        log_level=args.log_level,
        mode=args.mode
    )

    daemon.setup_logging()

    # Setup signal handlers
    # Note: Windows doesn't support loop.add_signal_handler()
    # We rely on KeyboardInterrupt (Ctrl+C) caught in __main__ instead
    if platform.system() != 'Windows':
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda s=sig: daemon.signal_handler(s))
    else:
        logger.debug("Running on Windows, signal handlers disabled (using KeyboardInterrupt)")

    # Run daemon
    await daemon.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl+C pressed - clean exit
        # (shutdown already handled by daemon.run() exception handling)
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
