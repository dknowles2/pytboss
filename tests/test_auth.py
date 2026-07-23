from unittest.mock import AsyncMock, MagicMock

import pytest

from pytboss.auth import API_URL, async_login
from pytboss.exceptions import Unauthorized


def make_session(response_json: dict) -> MagicMock:
    response = AsyncMock()
    response.json.return_value = response_json
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.post.return_value = cm
    return session


async def test_async_login_success():
    session = make_session({"status": "success", "data": {"token": "abc123"}})
    headers = await async_login(session, "me@example.com", "hunter2")
    assert headers == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer abc123",
    }
    session.post.assert_called_once_with(
        f"{API_URL}/login/app",
        params={"email": "me@example.com", "password": "hunter2"},
    )


async def test_async_login_error():
    session = make_session(
        {"status": "error", "error": {"message": "Invalid credentials"}}
    )
    with pytest.raises(Unauthorized, match="Invalid credentials"):
        await async_login(session, "me@example.com", "wrong")
