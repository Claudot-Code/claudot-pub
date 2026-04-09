"""
asyncio TCP server for accepting Godot plugin connections.

Listens on TCP port, accepts multiple clients, relays messages from clients
to callback, and broadcasts messages to all connected clients.
"""

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class TCPServer:
    """TCP server for handling Godot plugin connections with message broadcast."""

    def __init__(
        self,
        host: str,
        port: int,
        on_client_message_callback: Callable[[dict, asyncio.StreamWriter], Awaitable[None]]
    ):
        """
        Initialize TCP server.

        Args:
            host: Host to bind to
            port: Port to bind to
            on_client_message_callback: Async callback when client sends message (data, writer)
        """
        self.host = host
        self.port = port
        self.on_client_message_callback = on_client_message_callback
        self.server: Optional[asyncio.Server] = None
        self.clients: set[asyncio.StreamWriter] = set()
        self._lock = asyncio.Lock()

    async def start(self):
        """Start TCP server and begin accepting connections."""
        logger.info(f"Starting TCP server on {self.host}:{self.port}")

        try:
            self.server = await asyncio.start_server(
                self._handle_client,
                self.host,
                self.port
            )

            addrs = ', '.join(str(sock.getsockname()) for sock in self.server.sockets)
            logger.info(f"TCP server listening on {addrs}")

            # Serve forever
            async with self.server:
                await self.server.serve_forever()

        except Exception as e:
            logger.error(f"TCP server error: {e}")
            raise

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """
        Handle individual client connection.

        Args:
            reader: Client input stream
            writer: Client output stream
        """
        addr = writer.get_extra_info('peername')
        logger.info(f"Client connected: {addr}")

        # Register client
        async with self._lock:
            self.clients.add(writer)

        try:
            while True:
                # Read line from client
                line = await reader.readline()

                if not line:  # EOF - client disconnected
                    logger.info(f"Client disconnected: {addr}")
                    break

                try:
                    decoded = line.decode('utf-8').strip()
                    if decoded:
                        parsed = json.loads(decoded)
                        logger.debug(f"Client message from {addr}: {parsed}")
                        await self.on_client_message_callback(parsed, writer)
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse JSON from client {addr}: {e} | Line: {line[:100]}")
                    # Send error response to client
                    await self.send_to_client(writer, {
                        "type": "bridge/error",
                        "error": "Invalid JSON",
                        "message": str(e)
                    })
                except Exception as e:
                    logger.error(f"Error processing client message from {addr}: {e}")

        except Exception as e:
            logger.error(f"Fatal error handling client {addr}: {e}")
        finally:
            # Unregister client
            async with self._lock:
                self.clients.discard(writer)

            # Close connection
            try:
                writer.close()
                await writer.wait_closed()
            except Exception as e:
                logger.debug(f"Error closing client connection {addr}: {e}")

            logger.debug(f"Client handler exiting for {addr}")

    async def broadcast(self, data: dict):
        """
        Broadcast message to all connected clients.

        Args:
            data: Dictionary to serialize and send to all clients
        """
        async with self._lock:
            # Copy client list to avoid modification during iteration
            clients = list(self.clients)

        if not clients:
            logger.debug("No clients connected, skipping broadcast")
            return

        logger.debug(f"Broadcasting to {len(clients)} client(s): {data}")

        # Send to all clients
        for writer in clients:
            await self.send_to_client(writer, data)

    async def send_to_client(self, writer: asyncio.StreamWriter, data: dict):
        """
        Send JSON message to specific client.

        Args:
            writer: Client stream writer
            data: Dictionary to serialize and send
        """
        try:
            json_line = json.dumps(data) + "\n"
            writer.write(json_line.encode('utf-8'))
            await writer.drain()
        except Exception as e:
            addr = writer.get_extra_info('peername')
            logger.warning(f"Failed to send message to client {addr}: {e}")

            # Remove dead client
            async with self._lock:
                self.clients.discard(writer)

    async def stop(self):
        """Stop TCP server and close all client connections."""
        logger.info("Stopping TCP server")

        # Close all client connections
        async with self._lock:
            clients = list(self.clients)
            self.clients.clear()

        for writer in clients:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception as e:
                logger.debug(f"Error closing client: {e}")

        # Close server
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            logger.info("TCP server stopped")
