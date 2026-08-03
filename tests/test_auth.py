from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientResponseError, ContentTypeError

from pytboss.auth import API_URL, async_login
from pytboss.exceptions import Unauthorized


def make_session(response_json: dict | None = None, json_side_effect=None) -> MagicMock:
    response = MagicMock()
    response.json = AsyncMock(return_value=response_json, side_effect=json_side_effect)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.post.return_value = cm
    return session


async def test_async_login_success():
    session = make_session(
        {
            "status": "success",
            "message": None,
            "data": {"token": "abc123", "token_expiration": "2026-01-01T00:00:00Z"},
            "errors": None,
        }
    )
    headers = await async_login(session, "me@example.com", "hunter2")
    assert headers == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer abc123",
    }
    session.post.assert_called_once_with(
        f"{API_URL}/login/app",
        json={"email": "me@example.com", "password": "hunter2"},
    )


async def test_async_login_bad_credentials():
    # Live API shape: HTTP 404 with an "errors" (plural) object.
    session = make_session(
        {
            "status": "error",
            "error_code": "UNIDENTIFIED_CUSTOMER",
            "message": "Error",
            "errors": {
                "message": (
                    "Customer not found or password entered incorrectly. "
                    "Please try again."
                )
            },
        }
    )
    with pytest.raises(Unauthorized, match="Customer not found"):
        await async_login(session, "me@example.com", "wrong")


async def test_async_login_non_json_response():
    session = make_session(
        json_side_effect=ContentTypeError(request_info=None, history=())
    )
    with pytest.raises(Unauthorized, match="non-JSON"):
        await async_login(session, "me@example.com", "hunter2")


async def test_async_login_non_json_http_error():
    session = make_session(
        json_side_effect=ContentTypeError(request_info=None, history=())
    )
    response = session.post.return_value.__aenter__.return_value
    response.raise_for_status.side_effect = ClientResponseError(
        request_info=None, history=(), status=502
    )
    with pytest.raises(ClientResponseError):
        await async_login(session, "me@example.com", "hunter2")
