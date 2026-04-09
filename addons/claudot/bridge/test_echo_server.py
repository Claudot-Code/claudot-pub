#!/usr/bin/env python3
"""
Standalone echo server for testing Godot plugin TCP client.

This server echoes back JSON-RPC requests as JSON-RPC responses without requiring
Claude Code or the full bridge daemon. Useful for integration testing the TCP client
in isolation.

Usage:
    python addons/claudot/bridge/test_echo_server.py [--port 7777]
"""

import asyncio
import json
import sys
import argparse


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handle individual client connection and echo messages back."""
    addr = writer.get_extra_info('peername')
    print(f"[ECHO] Client connected: {addr}", file=sys.stderr)

    try:
        while True:
            # Read newline-delimited JSON from client
            line = await reader.readline()

            if not line:  # EOF - client disconnected
                print(f"[ECHO] Client disconnected: {addr}", file=sys.stderr)
                break

            try:
                decoded = line.decode('utf-8').strip()
                if not decoded:
                    continue

                # Parse incoming JSON
                msg = json.loads(decoded)
                print(f"[ECHO] Received from {addr}: {msg}", file=sys.stderr)

                # Build echo response as JSON-RPC result
                if "method" in msg:
                    # It's a JSON-RPC request - respond with result
                    response = {
                        "jsonrpc": "2.0",
                        "id": msg.get("id"),
                        "result": {
                            "echo": msg,
                            "status": "ok"
                        }
                    }
                else:
                    # Not a JSON-RPC request - just echo it back
                    response = {
                        "echo": msg,
                        "status": "ok"
                    }

                # Send response as newline-delimited JSON
                response_line = json.dumps(response) + "\n"
                writer.write(response_line.encode('utf-8'))
                await writer.drain()

                print(f"[ECHO] Sent to {addr}: {response}", file=sys.stderr)

            except json.JSONDecodeError as e:
                print(f"[ECHO] JSON decode error from {addr}: {e} | Line: {line[:100]}", file=sys.stderr)
                error_msg = json.dumps({"error": "Invalid JSON", "message": str(e)}) + "\n"
                writer.write(error_msg.encode('utf-8'))
                await writer.drain()
            except Exception as e:
                print(f"[ECHO] Error processing message from {addr}: {e}", file=sys.stderr)

    except Exception as e:
        print(f"[ECHO] Fatal error handling client {addr}: {e}", file=sys.stderr)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def run_server(host: str, port: int):
    """Start echo server and serve forever."""
    server = await asyncio.start_server(handle_client, host, port)

    addrs = ', '.join(str(sock.getsockname()) for sock in server.sockets)
    print(f"[ECHO] Echo server listening on {addrs}", file=sys.stderr)

    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description='Test echo server for Claudot TCP client')
    parser.add_argument('--port', type=int, default=7777, help='Port to listen on (default: 7777)')
    parser.add_argument('--host', type=str, default='127.0.0.1', help='Host to bind to (default: 127.0.0.1)')

    args = parser.parse_args()

    try:
        asyncio.run(run_server(args.host, args.port))
    except KeyboardInterrupt:
        print("\n[ECHO] Server stopped by user", file=sys.stderr)
    except Exception as e:
        print(f"[ECHO] Server error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
