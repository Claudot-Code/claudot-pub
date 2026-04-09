"""
MCP server with tool definitions for Godot scene manipulation.

Exposes MCP tools that relay commands to Godot via TCP, handles request-response
correlation with asyncio Futures, and returns structured JSON responses.
"""

import asyncio
import json
import logging
import time
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


class PendingRequests:
    """Manages request-response correlation for MCP tool calls to Godot."""

    def __init__(self):
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id: int = 0
        self._lock = asyncio.Lock()

    async def create_request(self) -> tuple[int, asyncio.Future]:
        """
        Create a new request and return its ID and a Future for the response.

        Returns:
            Tuple of (request_id, future)
        """
        async with self._lock:
            request_id = self._next_id
            self._next_id += 1
            future = asyncio.Future()
            self._pending[request_id] = future
            logger.debug(f"Created request {request_id}")
            return request_id, future

    async def resolve(self, request_id: int, result: dict):
        """
        Resolve the future for the given request ID with a result.

        Args:
            request_id: Request ID to resolve
            result: Result dictionary from Godot
        """
        async with self._lock:
            future = self._pending.pop(request_id, None)
            if future and not future.done():
                future.set_result(result)
                logger.debug(f"Resolved request {request_id}")
            elif not future:
                logger.warning(f"No pending request for ID {request_id}")
            else:
                logger.warning(f"Request {request_id} already resolved")

    async def reject(self, request_id: int, error: str):
        """
        Reject the future for the given request ID with an error.

        Args:
            request_id: Request ID to reject
            error: Error message
        """
        async with self._lock:
            future = self._pending.pop(request_id, None)
            if future and not future.done():
                future.set_exception(Exception(error))
                logger.debug(f"Rejected request {request_id}: {error}")
            elif not future:
                logger.warning(f"No pending request for ID {request_id}")
            else:
                logger.warning(f"Request {request_id} already resolved")


def create_mcp_server(tcp_server) -> FastMCP:
    """
    Create and configure the FastMCP server with tool definitions.

    Args:
        tcp_server: TCPServer instance for sending commands to Godot

    Returns:
        Configured FastMCP instance
    """
    mcp = FastMCP("Claudot", json_response=True)
    pending_requests = PendingRequests()

    # Timeout for waiting on Godot responses (seconds)
    RESPONSE_TIMEOUT = 10.0

    async def forward_chat_message(content: str, context: dict = None):
        """
        Forward a chat message from Godot to Claude using MCP sampling.

        Args:
            content: The message content from Godot
            context: Optional context (scene path, selected nodes, etc.)

        Returns:
            Claude's response text
        """
        logger.info(f"Forwarding chat message to Claude: {content[:100]}...")

        # Build the message with context if provided
        message_parts = []

        if context:
            context_str = "**Current Context:**\n"
            if "scene_path" in context:
                context_str += f"- Scene: {context['scene_path']}\n"
            if "scene_root_name" in context:
                context_str += f"- Root node: {context['scene_root_name']} ({context.get('scene_root_type', 'Node')})\n"
            if "selected_nodes" in context and context["selected_nodes"]:
                context_str += f"- Selected nodes:\n"
                for node in context["selected_nodes"]:
                    context_str += f"  - {node['path']} ({node['type']})\n"
                    if "script" in node:
                        context_str += f"    Script: {node['script']}\n"

            message_parts.append(context_str)

        message_parts.append(content)
        full_message = "\n".join(message_parts)

        try:
            # Use MCP's create_message to ask Claude to respond
            # This requires the request context which FastMCP provides automatically
            # We'll use the mcp instance's context
            from mcp.server.models import TextContent

            # Request Claude to respond to the user's message
            # The response will include Claude's reply
            response = await mcp.request_context.session.create_message(
                messages=[{
                    "role": "user",
                    "content": TextContent(type="text", text=full_message)
                }],
                max_tokens=4000
            )

            # Extract the response text
            response_text = ""
            if response.content:
                for item in response.content:
                    if hasattr(item, 'text'):
                        response_text += item.text

            logger.info(f"Received response from Claude: {response_text[:100]}...")
            return response_text

        except Exception as e:
            logger.error(f"Failed to forward chat message: {e}", exc_info=True)
            return f"Error communicating with Claude: {str(e)}"

    # Store the chat handler on the mcp instance so main.py can access it
    mcp._forward_chat_message = forward_chat_message
    mcp._pending_requests = pending_requests

    async def send_to_godot(method: str, params: dict) -> dict:
        """
        Send a command to Godot via TCP and wait for the response.

        Args:
            method: MCP method name
            params: Parameters dictionary

        Returns:
            Response dictionary from Godot

        Raises:
            Exception: If request times out or Godot returns an error
        """
        # Create request
        request_id, future = await pending_requests.create_request()

        # Build JSON-RPC request
        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": request_id
        }

        logger.debug(f"Sending to Godot: {request}")

        # Send to Godot via TCP
        await tcp_server.broadcast(request)

        # Wait for response with timeout
        try:
            response = await asyncio.wait_for(future, timeout=RESPONSE_TIMEOUT)
            logger.debug(f"Received from Godot: {response}")
            return response
        except asyncio.TimeoutError:
            logger.error(f"Request {request_id} timed out after {RESPONSE_TIMEOUT}s")
            raise Exception(f"Request timed out - Godot did not respond")
        except Exception as e:
            logger.error(f"Request {request_id} failed: {e}")
            raise

    @mcp.tool()
    async def get_node_property(node_path: str, property_name: str) -> dict:
        """
        Get a property value from a node in the scene tree.

        Args:
            node_path: Absolute or relative path to the node (e.g., "/root/Player" or "Player")
            property_name: Name of the property to read (e.g., "position", "visible")

        Returns:
            Dictionary with success flag, value, and timestamp
        """
        try:
            if not node_path:
                return {
                    "success": False,
                    "error": "node_path cannot be empty",
                    "timestamp": time.time()
                }

            if not property_name:
                return {
                    "success": False,
                    "error": "property_name cannot be empty",
                    "timestamp": time.time()
                }

            response = await send_to_godot("get_node_property", {
                "node_path": node_path,
                "property_name": property_name
            })

            return {
                "success": response.get("success", False),
                "value": response.get("value"),
                "error": response.get("error"),
                "timestamp": time.time()
            }

        except Exception as e:
            logger.exception("Error in get_node_property")
            return {
                "success": False,
                "error": "Internal error",
                "timestamp": time.time()
            }

    @mcp.tool()
    async def set_node_property(node_path: str, property_name: str, value: Any) -> dict:
        """
        Set a property on a node in the scene tree (undo-safe).

        Args:
            node_path: Absolute or relative path to the node
            property_name: Name of the property to set
            value: New value for the property (will be converted to appropriate Godot type)

        Returns:
            Dictionary with success flag, old value, new value, and timestamp
        """
        try:
            if not node_path:
                return {
                    "success": False,
                    "error": "node_path cannot be empty",
                    "timestamp": time.time()
                }

            if not property_name:
                return {
                    "success": False,
                    "error": "property_name cannot be empty",
                    "timestamp": time.time()
                }

            response = await send_to_godot("set_node_property", {
                "node_path": node_path,
                "property_name": property_name,
                "value": value
            })

            return {
                "success": response.get("success", False),
                "old_value": response.get("old_value"),
                "new_value": response.get("new_value"),
                "error": response.get("error"),
                "timestamp": time.time()
            }

        except Exception as e:
            logger.exception("Error in set_node_property")
            return {
                "success": False,
                "error": "Internal error",
                "timestamp": time.time()
            }

    @mcp.tool()
    async def get_scene_state(max_depth: int = 5) -> dict:
        """
        Get a snapshot of the entire edited scene tree.

        Args:
            max_depth: Maximum depth to traverse (default: 5, prevents excessive recursion)

        Returns:
            Dictionary with success flag, scene snapshot (node paths mapped to node data), and timestamp
        """
        try:
            if max_depth < 1:
                return {
                    "success": False,
                    "error": "max_depth must be at least 1",
                    "timestamp": time.time()
                }

            if max_depth > 20:
                return {
                    "success": False,
                    "error": "max_depth cannot exceed 20",
                    "timestamp": time.time()
                }

            response = await send_to_godot("get_scene_state", {
                "max_depth": max_depth
            })

            return {
                "success": response.get("success", False),
                "scene": response.get("scene"),
                "error": response.get("error"),
                "timestamp": time.time()
            }

        except Exception as e:
            logger.exception("Error in get_scene_state")
            return {
                "success": False,
                "error": "Internal error",
                "timestamp": time.time()
            }

    @mcp.tool()
    async def create_node(parent_path: str, node_type: str, node_name: str) -> dict:
        """
        Create a new node in the scene tree (undo-safe).

        Args:
            parent_path: Path to the parent node where the new node will be added
            node_type: Type of the node to create (e.g., "Node2D", "Sprite2D", "RigidBody3D")
            node_name: Name for the new node

        Returns:
            Dictionary with success flag, created node path, and timestamp
        """
        try:
            if not parent_path:
                return {
                    "success": False,
                    "error": "parent_path cannot be empty",
                    "timestamp": time.time()
                }

            if not node_type:
                return {
                    "success": False,
                    "error": "node_type cannot be empty",
                    "timestamp": time.time()
                }

            if not node_name:
                return {
                    "success": False,
                    "error": "node_name cannot be empty",
                    "timestamp": time.time()
                }

            response = await send_to_godot("create_node", {
                "parent_path": parent_path,
                "node_type": node_type,
                "node_name": node_name
            })

            return {
                "success": response.get("success", False),
                "node_path": response.get("node_path"),
                "error": response.get("error"),
                "timestamp": time.time()
            }

        except Exception as e:
            logger.exception("Error in create_node")
            return {
                "success": False,
                "error": "Internal error",
                "timestamp": time.time()
            }

    @mcp.tool()
    async def delete_node(node_path: str) -> dict:
        """
        Delete a node from the scene tree (undo-safe).

        Args:
            node_path: Path to the node to delete

        Returns:
            Dictionary with success flag and timestamp
        """
        try:
            if not node_path:
                return {
                    "success": False,
                    "error": "node_path cannot be empty",
                    "timestamp": time.time()
                }

            response = await send_to_godot("delete_node", {
                "node_path": node_path
            })

            return {
                "success": response.get("success", False),
                "error": response.get("error"),
                "timestamp": time.time()
            }

        except Exception as e:
            logger.exception("Error in delete_node")
            return {
                "success": False,
                "error": "Internal error",
                "timestamp": time.time()
            }

    @mcp.tool()
    async def reparent_node(node_path: str, new_parent_path: str) -> dict:
        """
        Move a node to a different parent in the scene tree (undo-safe).

        Args:
            node_path: Path to the node to move
            new_parent_path: Path to the new parent node

        Returns:
            Dictionary with success flag, new node path, and timestamp
        """
        try:
            if not node_path:
                return {
                    "success": False,
                    "error": "node_path cannot be empty",
                    "timestamp": time.time()
                }

            if not new_parent_path:
                return {
                    "success": False,
                    "error": "new_parent_path cannot be empty",
                    "timestamp": time.time()
                }

            response = await send_to_godot("reparent_node", {
                "node_path": node_path,
                "new_parent_path": new_parent_path
            })

            return {
                "success": response.get("success", False),
                "new_path": response.get("new_path"),
                "error": response.get("error"),
                "timestamp": time.time()
            }

        except Exception as e:
            logger.exception("Error in reparent_node")
            return {
                "success": False,
                "error": "Internal error",
                "timestamp": time.time()
            }

    @mcp.tool()
    async def get_editor_context() -> dict:
        """
        Get current editor state including scene path, selection, and errors.

        Returns:
            Dictionary with success flag, editor context data, and timestamp
        """
        try:
            response = await send_to_godot("get_editor_context", {})

            return {
                "success": response.get("success", False),
                "context": response.get("context"),
                "error": response.get("error"),
                "timestamp": time.time()
            }

        except Exception as e:
            logger.exception("Error in get_editor_context")
            return {
                "success": False,
                "error": "Internal error",
                "timestamp": time.time()
            }

    # Store pending_requests on the mcp instance so it can be accessed from main.py
    mcp._pending_requests = pending_requests

    return mcp
