from base64 import b64encode
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


async def test_set_file_content(fs: FileSystem, conn: AsyncMock):
    conn.send_command.return_value = {}
    assert await fs.set_file_content("test.txt", "data", True) == {}
    conn.send_command.assert_awaited_once_with(
        "FS.Put", {"filename": "test.txt", "data": "data", "append": True}
    )


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
