"""In-container forwarder: a loopback TCP port to the run's egress socket.

Chromium speaks to an HTTP proxy over TCP only. The container has no network
interface (OD-AF-12) — only gVisor's loopback — so this relays
`127.0.0.1:<port>` to `/run/jarvis/egress.sock`, JARVIS's CONNECT proxy. It
adds nothing and decides nothing: every decision is the proxy's, on the other
side of the socket. Without the socket, nothing gets out at all.
"""

from __future__ import annotations

import asyncio
import contextlib


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def serve(socket_path: str, port: int) -> asyncio.base_events.Server:
    async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.open_unix_connection(socket_path)
        except OSError:
            client_writer.close()
            return
        await asyncio.gather(_pipe(client_reader, up_writer), _pipe(up_reader, client_writer))

    return await asyncio.start_server(handle, "127.0.0.1", port)


__all__ = ["serve"]
