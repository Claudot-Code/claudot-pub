"""
Claude Code CLI subprocess manager with crash detection.

Spawns Claude Code CLI as subprocess with asyncio pipes, monitors health,
relays stdout messages, and detects crashes with exit code and stderr capture.
"""

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class ClaudeSubprocess:
    """Manages Claude Code CLI subprocess with bidirectional message relay and crash detection."""

    STDERR_BUFFER_SIZE = 50

    def __init__(
        self,
        on_message_callback: Callable[[dict], Awaitable[None]],
        on_crash_callback: Callable[[int, list[str]], Awaitable[None]]
    ):
        """
        Initialize subprocess manager.

        Args:
            on_message_callback: Async callback when Claude outputs JSON to stdout
            on_crash_callback: Async callback when subprocess exits with (exit_code, stderr_lines)
        """
        self.on_message_callback = on_message_callback
        self.on_crash_callback = on_crash_callback
        self.process: Optional[asyncio.subprocess.Process] = None
        self.running = False
        self.stderr_buffer: list[str] = []
        self._tasks: list[asyncio.Task] = []

    async def start(self, claude_command: str = "claude"):
        """
        Spawn Claude Code CLI subprocess with asyncio pipes.

        Args:
            claude_command: Command to invoke Claude CLI (default: "claude")
        """
        logger.info(f"Starting Claude subprocess: {claude_command}")

        try:
            self.process = await asyncio.create_subprocess_exec(
                claude_command,
                "-p",
                "--output-format", "stream-json",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            self.running = True
            logger.info(f"Claude subprocess started with PID {self.process.pid}")

            # Start background tasks for stdout, stderr, and process monitoring
            self._tasks = [
                asyncio.create_task(self._read_stdout()),
                asyncio.create_task(self._read_stderr()),
                asyncio.create_task(self._monitor_process())
            ]

        except Exception as e:
            logger.error(f"Failed to start Claude subprocess: {e}")
            self.running = False
            raise

    async def _read_stdout(self):
        """Read stdout line-by-line, parse JSON, and invoke message callback."""
        logger.debug("Started stdout reader")

        try:
            while self.running and self.process and self.process.stdout:
                line = await self.process.stdout.readline()

                if not line:  # EOF
                    logger.debug("Claude stdout EOF")
                    break

                try:
                    decoded = line.decode('utf-8').strip()
                    if decoded:
                        parsed = json.loads(decoded)
                        logger.debug(f"Claude message: {parsed}")
                        await self.on_message_callback(parsed)
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse JSON from Claude stdout: {e} | Line: {line[:100]}")
                except Exception as e:
                    logger.error(f"Error processing Claude message: {e}")

        except Exception as e:
            logger.error(f"Fatal error in stdout reader: {e}")
        finally:
            logger.debug("Stdout reader exiting")

    async def _read_stderr(self):
        """Read stderr line-by-line, buffer for crash reports, and log."""
        logger.debug("Started stderr reader")

        try:
            while self.running and self.process and self.process.stderr:
                line = await self.process.stderr.readline()

                if not line:  # EOF
                    logger.debug("Claude stderr EOF")
                    break

                try:
                    decoded = line.decode('utf-8').strip()
                    if decoded:
                        # Add to ring buffer
                        self.stderr_buffer.append(decoded)
                        if len(self.stderr_buffer) > self.STDERR_BUFFER_SIZE:
                            self.stderr_buffer.pop(0)

                        # Log to Python logger
                        logger.debug(f"Claude stderr: {decoded}")
                except Exception as e:
                    logger.error(f"Error processing Claude stderr: {e}")

        except Exception as e:
            logger.error(f"Fatal error in stderr reader: {e}")
        finally:
            logger.debug("Stderr reader exiting")

    async def _monitor_process(self):
        """Monitor subprocess exit and trigger crash callback."""
        logger.debug("Started process monitor")

        try:
            if self.process:
                returncode = await self.process.wait()
                self.running = False

                logger.warning(f"Claude subprocess exited with code {returncode}")

                # Call crash callback with exit code and last 10 stderr lines
                last_stderr = self.stderr_buffer[-10:] if self.stderr_buffer else []
                await self.on_crash_callback(returncode, last_stderr)

        except Exception as e:
            logger.error(f"Fatal error in process monitor: {e}")
        finally:
            logger.debug("Process monitor exiting")

    async def send_message(self, data: dict) -> bool:
        """
        Send JSON message to Claude stdin.

        Args:
            data: Dictionary to serialize and send

        Returns:
            True if sent successfully, False otherwise
        """
        if not self.running or not self.process or not self.process.stdin:
            logger.warning("Cannot send message: subprocess not running or stdin unavailable")
            return False

        try:
            json_line = json.dumps(data) + "\n"
            self.process.stdin.write(json_line.encode('utf-8'))
            await self.process.stdin.drain()
            logger.debug(f"Sent to Claude: {data}")
            return True
        except Exception as e:
            logger.error(f"Failed to send message to Claude: {e}")
            return False

    async def stop(self):
        """Stop subprocess gracefully with timeout, then forcefully if needed."""
        if not self.process:
            logger.debug("No process to stop")
            return

        logger.info("Stopping Claude subprocess")

        try:
            # Close stdin to signal EOF
            if self.process.stdin:
                self.process.stdin.close()
                await self.process.stdin.wait_closed()

            # Wait for graceful exit with timeout
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
                logger.info("Claude subprocess exited gracefully")
            except asyncio.TimeoutError:
                logger.warning("Claude subprocess did not exit gracefully, killing")
                self.process.kill()
                await self.process.wait()
                logger.info("Claude subprocess killed")

        except Exception as e:
            logger.error(f"Error stopping subprocess: {e}")
        finally:
            self.running = False

            # Cancel background tasks
            for task in self._tasks:
                if not task.done():
                    task.cancel()

            # Wait for tasks to finish cancelling
            await asyncio.gather(*self._tasks, return_exceptions=True)
            logger.debug("All subprocess tasks stopped")
