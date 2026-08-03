from base64 import b64decode, b64encode
from unittest import mock
from unittest.mock import AsyncMock

import pytest

from pytboss.fs import FileSystem
from pytboss.transport import Transport


@pytest.fixture
def conn() -> AsyncMock:
    return AsyncMock(spec=Transport)


@pytest.fixture
def fs(conn: AsyncMock) -> FileSystem:
    return FileSystem(conn)


async def test_get_file_list(fs: FileSystem, conn: AsyncMock):
    conn.send_command.return_value = {"files": ["a.txt"]}
    assert await fs.get_file_list() == {"files": ["a.txt"]}
    conn.send_command.assert_awaited_once_with("FS.List", {})


async def test_get_file_content_single_chunk(fs: FileSystem, conn: AsyncMock):
    conn.send_command.return_value = {
        "data": b64encode(b"hello world").decode(),
        "left": 0,
    }
    assert await fs.get_file_content("test.txt") == "hello world"
    conn.send_command.assert_awaited_once_with(
        "FS.Get", {"filename": "test.txt", "offset": 0, "len": 512}
    )


async def test_get_file_content_multiple_chunks(fs: FileSystem, conn: AsyncMock):
    chunk1 = "a" * 512
    chunk2 = "b" * 10
    conn.send_command.side_effect = [
        {"data": b64encode(chunk1.encode()).decode(), "left": len(chunk2)},
        {"data": b64encode(chunk2.encode()).decode(), "left": 0},
    ]
    assert await fs.get_file_content("test.txt") == chunk1 + chunk2
    conn.send_command.assert_has_awaits(
        [
            mock.call("FS.Get", {"filename": "test.txt", "offset": 0, "len": 512}),
            mock.call("FS.Get", {"filename": "test.txt", "offset": 512, "len": 512}),
        ]
    )


async def test_get_file_content_multibyte_split_across_chunks(
    fs: FileSystem, conn: AsyncMock
):
    """A multi-byte character on the chunk boundary must survive the read."""
    content = ("x" * 511 + "é" + "y").encode("utf-8")  # é spans bytes 511-512
    conn.send_command.side_effect = [
        {"data": b64encode(content[:512]).decode(), "left": len(content) - 512},
        {"data": b64encode(content[512:]).decode(), "left": 0},
    ]
    assert await fs.get_file_content("test.txt") == "x" * 511 + "éy"


async def test_set_file_content(fs: FileSystem, conn: AsyncMock):
    """The RPC takes base64: the device declares `data` as `%V`."""
    conn.send_command.return_value = {}
    assert await fs.set_file_content("test.txt", "data", True) == {}
    conn.send_command.assert_awaited_once_with(
        "FS.Put",
        {
            "filename": "test.txt",
            "data": b64encode(b"data").decode(),
            "append": True,
        },
    )


async def test_set_file_content_round_trips_through_get(
    fs: FileSystem, conn: AsyncMock
):
    """What Put sends must be what Get decodes, or writes land corrupted."""
    content = "héllo wörld"  # multi-byte, so a raw write would not survive
    conn.send_command.return_value = {}
    await fs.set_file_content("test.txt", content, False)
    sent = conn.send_command.await_args.args[1]["data"]

    conn.send_command.reset_mock()
    conn.send_command.return_value = {"data": sent, "left": 0}
    assert await fs.get_file_content("test.txt") == content


async def test_set_file_content_accepts_bytes(fs: FileSystem, conn: AsyncMock):
    conn.send_command.return_value = {}
    await fs.set_file_content("test.bin", b"\x00\xff", False)
    sent = conn.send_command.await_args.args[1]["data"]
    assert b64decode(sent) == b"\x00\xff"


async def test_rename_file(fs: FileSystem, conn: AsyncMock):
    conn.send_command.return_value = {}
    assert await fs.rename_file("a.txt", "b.txt") == {}
    conn.send_command.assert_awaited_once_with(
        "FS.Rename", {"src": "a.txt", "dst": "b.txt"}
    )


async def test_delete_file(fs: FileSystem, conn: AsyncMock):
    conn.send_command.return_value = {}
    assert await fs.delete_file("test.txt") == {}
    conn.send_command.assert_awaited_once_with("FS.Remove", {"filename": "test.txt"})


async def test_get_file_content_follows_what_the_device_actually_sent(
    fs: FileSystem, conn: AsyncMock
):
    """The reply is bounded by the RPC frame size, not by what we asked for.

    Advancing the offset by the requested length instead leaves a hole in the
    middle of the file, and the read reports success.
    """
    raw = "".join(f"line{i:04d}\n" for i in range(140)).encode()

    async def short_chunks(method, params, *, timeout=None):
        offset = params["offset"]
        chunk = raw[offset : offset + 200]
        return {
            "data": b64encode(chunk).decode(),
            "left": max(0, len(raw) - offset - len(chunk)),
        }

    conn.send_command = short_chunks
    assert await fs.get_file_content("test.txt") == raw.decode()


async def test_get_file_content_stops_when_the_device_sends_nothing(
    fs: FileSystem, conn: AsyncMock
):
    """`left` can disagree with what is actually there.

    Without this the same offset is requested forever.
    """
    conn.send_command.return_value = {"data": "", "left": 999}
    assert await fs.get_file_content("test.txt") == ""
    assert conn.send_command.await_count == 1


async def test_get_file_content_tolerates_a_reply_without_data(
    fs: FileSystem, conn: AsyncMock
):
    conn.send_command.return_value = {}
    assert await fs.get_file_content("test.txt") == ""
