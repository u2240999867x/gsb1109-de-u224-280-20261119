"""Minimal MongoDB wire-protocol responder used by the offline repro.

The responder is intentionally small. It speaks OP_MSG, answers the driver's
``hello`` handshake as a writable primary, and delegates aggregate/getMore
commands to the scripted change stream responder. Keeping the wire format here
means the repro records exactly what the driver sends, including the namespace,
``$changeStream`` options, and the raw resume token.

The test environment does not permit binding TCP or UNIX listening sockets, so
each connection is backed by ``socket.socketpair()``. The server endpoint is
adapted to asyncio streams with ``connect_accepted_socket`` and the client
endpoint is handed to PyMongo's real pool as an ``AsyncNetworkingInterface``.
"""

from __future__ import annotations

import asyncio
import socket
import struct
from dataclasses import dataclass
from typing import Any

import bson
from pymongo.network_layer import AsyncNetworkingInterface, PyMongoProtocol

OP_MSG = 2013
_MESSAGE_HEADER = struct.Struct("<iiii")
_OP_MSG_SECTION = struct.Struct("<B")

HELLO_COMMANDS = frozenset({"hello", "ismaster", "isMaster"})


class WireError(RuntimeError):
    """Raised when the scripted responder receives an unexpected command."""


@dataclass
class Request:
    """A decoded client command."""

    command: dict[str, Any]
    database: str
    connection: ResponseConnection

    @property
    def name(self) -> str:
        return next(iter(self.command))


@dataclass
class _State:
    hello_count: int = 0
    closed: bool = False


class ResponseConnection:
    """One client connection. Tests use this to drop the socket on demand."""

    def __init__(
        self,
        writer: asyncio.StreamWriter,
        next_request_id: Any,
    ) -> None:
        self._writer = writer
        self._next_request_id = next_request_id
        self.closed = False
        self.drop_after_reply = False

    def reply(self, document: dict[str, Any], response_to: int) -> None:
        payload = bson.encode(document)
        # OP_MSG: flagBits, payload kind 0, then the BSON document. The
        # document's own leading length doubles as the section size.
        body = struct.pack("<IB", 0, 0) + payload
        header = _MESSAGE_HEADER.pack(len(body) + 16, self._next_request_id(), response_to, OP_MSG)
        self._writer.write(header + body)

    def drop(self) -> None:
        """Abruptly close the socket, simulating a dropped network."""
        if self.closed:
            return
        self.closed = True
        self._writer.transport.abort()


class ScriptedServer:
    """A MongoDB server with scripted command handling over real driver framing."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self._state = _State()
        self._request_id = 0
        self._connections: list[ResponseConnection] = []
        self._server_tasks: list[asyncio.Task[Any]] = []
        self._sockets: list[socket.socket] = []
        self._patch: Any = None

    def start(self) -> None:
        import pymongo.asynchronous.pool as pool_module

        self._patch = pool_module._configured_protocol_interface
        pool_module._configured_protocol_interface = self._configured_protocol_interface

    async def stop(self) -> None:
        if self._patch is not None:
            import pymongo.asynchronous.pool as pool_module

            pool_module._configured_protocol_interface = self._patch
            self._patch = None
        self._state.closed = True
        for conn in list(self._connections):
            conn.drop()
        for task in self._server_tasks:
            task.cancel()
        if self._server_tasks:
            await asyncio.gather(*self._server_tasks, return_exceptions=True)
        for sock in self._sockets:
            try:
                sock.close()
            except OSError:
                pass
        self._server_tasks = []
        self._sockets = []

    async def __aenter__(self) -> ScriptedServer:
        self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()

    async def _configured_protocol_interface(
        self,
        address: tuple[str, int],
        options: Any,
        ssl_session_cache: Any = None,
    ) -> AsyncNetworkingInterface:
        del address, ssl_session_cache
        client_sock, server_sock = socket.socketpair()
        client_sock.setblocking(False)
        server_sock.setblocking(False)
        self._sockets.extend([client_sock, server_sock])

        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        server_transport, _ = await loop.connect_accepted_socket(lambda: protocol, server_sock)
        writer = asyncio.StreamWriter(server_transport, protocol, reader, loop)
        conn = ResponseConnection(writer, self._next_request_id)
        self._connections.append(conn)
        task = asyncio.create_task(self._serve_connection(conn, reader, writer))
        self._server_tasks.append(task)

        client_transport, client_protocol = await loop.create_connection(
            lambda: PyMongoProtocol(timeout=options.socket_timeout), sock=client_sock
        )
        return AsyncNetworkingInterface((client_transport, client_protocol))

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _serve_connection(
        self,
        conn: ResponseConnection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while not self._state.closed:
                header = await reader.readexactly(16)
                length, request_id, _response_to, op_code = _MESSAGE_HEADER.unpack(header)
                if op_code != OP_MSG:
                    raise WireError(f"unsupported opcode {op_code}")
                body = await reader.readexactly(length - 16)
                command, database = _decode_op_msg(body)
                document = await self._dispatch(conn, command, database)
                if document is not None:
                    conn.reply(document, request_id)
                    await writer.drain()
                    if conn.drop_after_reply:
                        # The empty batch is flushed to the client and the
                        # socket is torn down immediately afterwards. The
                        # driver only notices on its next read.
                        conn.drop()
                        return
        except (asyncio.IncompleteReadError, ConnectionError, OSError, WireError):
            pass
        finally:
            writer.transport.abort()

    async def _dispatch(
        self, conn: ResponseConnection, command: dict[str, Any], database: str
    ) -> dict[str, Any] | None:
        name = next(iter(command))
        if name in HELLO_COMMANDS:
            self._state.hello_count += 1
            return _hello_reply(self._state.hello_count)
        request = Request(command=command, database=database, connection=conn)
        return self._handler(request)


def _decode_op_msg(body: bytes) -> tuple[dict[str, Any], str]:
    flags = struct.unpack_from("<I", body, 0)[0]
    if flags & 1:
        raise WireError("checksumPresent is not supported")
    offset = 4
    sections: list[dict[str, Any]] = []
    while offset < len(body):
        kind = _OP_MSG_SECTION.unpack_from(body, offset)[0]
        offset += 1
        if kind == 0:
            doc, offset = _read_document(body, offset)
            sections.append(doc)
        elif kind == 1:
            size = struct.unpack_from("<i", body, offset)[0]
            offset += size
        else:
            raise WireError(f"unknown OP_MSG section kind {kind}")
    command = sections[0]
    database = command.pop("$db", "admin")
    return command, database


def _read_document(data: bytes, offset: int) -> tuple[dict[str, Any], int]:
    size = struct.unpack_from("<i", data, offset)[0]
    raw = data[offset : offset + size]
    return bson.decode(raw), offset + size


def _hello_reply(hello_count: int) -> dict[str, Any]:
    return {
        "ok": 1,
        "isWritablePrimary": True,
        "ismaster": True,
        "maxWireVersion": 21,
        "minWireVersion": 9,
        "helloOk": True,
        "maxBsonObjectSize": 16 * 1024 * 1024,
        "maxMessageSizeBytes": 48 * 1000 * 1000,
        "maxWriteBatchSize": 100_000,
        "logicalSessionTimeoutMinutes": 30,
        "connectionId": hello_count,
        "readOnly": False,
    }
