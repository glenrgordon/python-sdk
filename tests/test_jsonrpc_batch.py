"""Wire tests for JSON-RPC 2.0 batch requests over the newline-delimited transport.

`Connection` exchanges decoded messages with its transport, so batch handling lives in
`Connection._process_batch`: every request entry is dispatched like a single request, and the
replies are collected into one array. A batch that carries no request must stay silent.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from acp._jsonrpc import decode_message
from acp._transport import memory_transport_pair
from acp.connection import Connection
from acp.exceptions import RequestError
from acp.meta import PROTOCOL_METHODS


def _conn(server, handler) -> Connection:
    return Connection(handler, server.server_writer, server.server_reader)


async def _write(server, payload: Any) -> None:
    server.client_writer.write((json.dumps(payload) + "\n").encode())
    await server.client_writer.drain()


async def _read(server) -> Any:
    line = await asyncio.wait_for(server.client_reader.readline(), timeout=1)
    return json.loads(line)


@pytest.mark.asyncio
async def test_batch_requests_return_one_ordered_array(server):
    async def handler(method: str, params: Any, is_notification: bool) -> Any:
        if params == {"slow": True}:
            await asyncio.sleep(0.05)
        return {"echo": params}

    conn = _conn(server, handler)
    try:
        await _write(
            server,
            [
                {"jsonrpc": "2.0", "id": 1, "method": "probe", "params": {"slow": True}},
                {"jsonrpc": "2.0", "id": "two", "method": "probe", "params": {"n": 2}},
            ],
        )

        replies = await _read(server)

        assert isinstance(replies, list), "batch replies must be a single array frame"
        assert [reply["id"] for reply in replies] == [1, "two"]
        assert [reply["result"] for reply in replies] == [{"echo": {"slow": True}}, {"echo": {"n": 2}}]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_notification_only_batch_is_silent(server):
    seen: list[Any] = []

    async def handler(method: str, params: Any, is_notification: bool) -> None:
        if is_notification:
            seen.append(params)

    conn = _conn(server, handler)
    try:
        await _write(
            server,
            [
                {"jsonrpc": "2.0", "method": "note", "params": {"n": 1}},
                {"jsonrpc": "2.0", "method": "note", "params": {"n": 2}},
            ],
        )
        # A follow-up request proves the batch produced no frame at all, rather than a slow one.
        await _write(server, {"jsonrpc": "2.0", "id": 7, "method": "after"})

        reply = await _read(server)

        assert reply == {"jsonrpc": "2.0", "id": 7, "result": None}
        assert seen == [{"n": 1}, {"n": 2}]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_mixed_batch_replies_only_for_requests(server):
    seen: list[Any] = []

    async def handler(method: str, params: Any, is_notification: bool) -> Any:
        if is_notification:
            seen.append(params)
            return None
        return {"method": method}

    conn = _conn(server, handler)
    try:
        await _write(
            server,
            [
                {"jsonrpc": "2.0", "method": "note", "params": {"n": 1}},
                {"jsonrpc": "2.0", "id": 1, "method": "probe"},
                {"jsonrpc": "2.0", "method": "note", "params": {"n": 2}},
            ],
        )

        replies = await _read(server)

        assert isinstance(replies, list)
        assert replies == [{"jsonrpc": "2.0", "id": 1, "result": {"method": "probe"}}]
        assert seen == [{"n": 1}, {"n": 2}]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_empty_batch_is_a_single_invalid_request(server):
    conn = _conn(server, lambda method, params, is_notification: None)
    try:
        await _write(server, [])

        reply = await _read(server)

        assert isinstance(reply, dict)
        assert reply["error"]["code"] == -32600
        assert reply["id"] is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_batch_entry_that_is_not_an_object_is_reported_in_place(server):
    async def handler(method: str, params: Any, is_notification: bool) -> Any:
        return {"ok": True}

    conn = _conn(server, handler)
    try:
        await _write(server, [1, {"jsonrpc": "2.0", "id": 2, "method": "probe"}])

        replies = await _read(server)

        assert [reply.get("id") for reply in replies] == [None, 2]
        assert replies[0]["error"]["code"] == -32600
        assert replies[1]["result"] == {"ok": True}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_batch_carries_responses_without_echoing_them():
    async def handler(method: str, params: Any, is_notification: bool) -> Any:
        return None

    peer, transport = memory_transport_pair()
    conn = Connection(handler, transport)
    try:
        pending = asyncio.create_task(conn.send_request("probe"))
        request_id = (await asyncio.wait_for(peer.receive(), timeout=1))["id"]

        await peer.send([
            {"jsonrpc": "2.0", "id": request_id, "result": {"answered": True}},
            {"jsonrpc": "2.0", "id": 9, "method": "probe"},
        ])

        assert await asyncio.wait_for(pending, timeout=1) == {"answered": True}
        assert await asyncio.wait_for(peer.receive(), timeout=1) == [{"jsonrpc": "2.0", "id": 9, "result": None}]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_batch_handler_can_request_the_peer_while_the_batch_is_pending():
    async def handler(method: str, params: Any, is_notification: bool) -> Any:
        # Runs inside ``_execute_request``'s own task, so the receive loop stays free to route the
        # peer's answer back into this coroutine while the batch itself is still unanswered.
        answer = await conn.send_request("peer/question", {"n": params["n"]})
        return {"n": params["n"], "peer": answer}

    peer, transport = memory_transport_pair()
    conn = Connection(handler, transport)
    try:
        await peer.send([
            {"jsonrpc": "2.0", "id": 1, "method": "probe", "params": {"n": 1}},
            {"jsonrpc": "2.0", "id": 2, "method": "probe", "params": {"n": 2}},
        ])

        for _ in range(2):
            nested = await asyncio.wait_for(peer.receive(), timeout=1)
            assert nested["method"] == "peer/question"
            await peer.send({"jsonrpc": "2.0", "id": nested["id"], "result": {"answered": nested["params"]["n"]}})

        replies = await asyncio.wait_for(peer.receive(), timeout=1)

        assert isinstance(replies, list), "both entries must travel back in one array frame"
        assert [reply["id"] for reply in replies] == [1, 2]
        assert [reply["result"] for reply in replies] == [
            {"n": 1, "peer": {"answered": 1}},
            {"n": 2, "peer": {"answered": 2}},
        ]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_cancelled_batch_entry_answers_once_and_keeps_its_sibling_result():
    started = asyncio.Event()

    async def handler(method: str, params: Any, is_notification: bool) -> Any:
        if params == {"slow": True}:
            started.set()
            await asyncio.sleep(5)
        return {"echo": params}

    peer, transport = memory_transport_pair()
    conn = Connection(handler, transport)
    try:
        await peer.send([
            {"jsonrpc": "2.0", "id": "slow", "method": "probe", "params": {"slow": True}},
            {"jsonrpc": "2.0", "id": "fast", "method": "probe", "params": {"slow": False}},
        ])
        await asyncio.wait_for(started.wait(), timeout=1)
        await peer.send([
            {
                "jsonrpc": "2.0",
                "method": PROTOCOL_METHODS["cancel_request"],
                "params": {"requestId": "slow"},
            }
        ])

        replies = await asyncio.wait_for(peer.receive(), timeout=1)

        assert [reply["id"] for reply in replies] == ["slow", "fast"]
        assert "result" not in replies[0]
        assert replies[0]["error"]["code"] == -32800  # Request cancelled
        assert replies[0]["error"]["message"] == "Request cancelled"
        assert replies[1] == {"jsonrpc": "2.0", "id": "fast", "result": {"echo": {"slow": False}}}
        # A single array frame, so the cancelled id is answered exactly once.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(peer.receive(), timeout=0.1)
    finally:
        await conn.close()


def test_decode_message_exposes_batches_but_rejects_an_empty_one():
    assert decode_message('[{"jsonrpc":"2.0","id":1,"method":"probe"}]') == [
        {"jsonrpc": "2.0", "id": 1, "method": "probe"}
    ]
    with pytest.raises(RequestError) as exc_info:
        decode_message("[]")
    assert isinstance(exc_info.value, RequestError)
    assert exc_info.value.code == -32600
