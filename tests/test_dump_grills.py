"""Tests for the grill-definition dump script's fetch and retry behaviour.

The vendor API expires or rotates sessions mid-sweep, so these cover what
happens around a 401 in particular: it has to be recoverable by logging in
again, but must never be skipped the way a missing ID is, since that would
commit a catalogue missing whatever the API refused to serve.
"""

from contextlib import AbstractContextManager
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientConnectionError, ClientResponseError

from pytboss.exceptions import Error, Unauthorized
from scripts.dump_grills import API_URL, GrillApi


@pytest.fixture(autouse=True)
def no_sleep():
    """Skips the retry backoff so the tests don't wait it out."""
    with mock.patch("scripts.dump_grills.sleep", AsyncMock()) as sleep:
        yield sleep


def grill_response(name: str = "PB1000SP1") -> MagicMock:
    """A response carrying one grill definition."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json = AsyncMock(
        return_value={"status": "success", "data": {"grill": {"name": name}}}
    )
    return resp


def error_response(status: int) -> MagicMock:
    """A response that raises on `raise_for_status`, as aiohttp would."""
    resp = MagicMock()
    resp.raise_for_status.side_effect = ClientResponseError(
        request_info=MagicMock(), history=(), status=status
    )
    return resp


def make_api(*responses, max_relogins: int = 3) -> tuple[GrillApi, MagicMock]:
    """Returns an API logged in with `token-0`, answering `responses` in order.

    Each later login hands back the next token, so a test can tell which
    headers a given request went out with.
    """
    session = MagicMock()
    session.get = AsyncMock(side_effect=list(responses))
    api = GrillApi(session, "me@example.com", "hunter2", max_relogins=max_relogins)
    return api, session


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def logins(*tokens: str) -> AbstractContextManager[AsyncMock]:
    """Patches `async_login` to hand back `tokens` in order."""
    return mock.patch(
        "scripts.dump_grills.async_login",
        AsyncMock(side_effect=[auth_headers(t) for t in tokens]),
    )


def sent_headers(session: MagicMock) -> list[dict[str, str]]:
    return [call.kwargs["headers"] for call in session.get.call_args_list]


async def test_login_then_fetch():
    api, session = make_api(grill_response())
    with logins("token-0"):
        await api.login()
    assert await api.get_grill_details(1) == {"name": "PB1000SP1"}
    session.get.assert_awaited_once_with(
        f"{API_URL}/grills/1", headers=auth_headers("token-0")
    )


async def test_initial_login_failure_is_fatal():
    # The genuine bad-credentials case: nothing is fetched, and the rejection
    # is reported rather than retried.
    api, session = make_api(grill_response())
    with (
        mock.patch(
            "scripts.dump_grills.async_login",
            AsyncMock(side_effect=Unauthorized("Customer not found")),
        ) as login,
        pytest.raises(Unauthorized, match="Customer not found"),
    ):
        await api.login()
    login.assert_awaited_once()
    session.get.assert_not_awaited()


async def test_401_recovers_after_relogin():
    api, session = make_api(error_response(401), grill_response())
    with logins("token-0", "token-1") as login:
        await api.login()
        assert await api.get_grill_details(1) == {"name": "PB1000SP1"}
    # Re-logged in once, and the retry went out with the fresh headers rather
    # than the stale ones that were just rejected.
    assert login.await_count == 2
    assert sent_headers(session) == [auth_headers("token-0"), auth_headers("token-1")]


async def test_401_relogin_budget_is_bounded():
    api, session = make_api(error_response(401), error_response(401), max_relogins=1)
    with logins("token-0", "token-1") as login:
        await api.login()
        with pytest.raises(ClientResponseError) as ex:
            await api.get_grill_details(1)
    assert ex.value.status == 401
    assert login.await_count == 2  # The initial login, plus the one re-login.
    assert session.get.await_count == 2


async def test_401_budget_spans_the_whole_run():
    # Two IDs, one re-login allowed: the second ID's 401 must not get its own
    # fresh budget.
    api, _ = make_api(
        error_response(401), grill_response(), error_response(401), max_relogins=1
    )
    with logins("token-0", "token-1") as login:
        await api.login()
        assert await api.get_grill_details(1) == {"name": "PB1000SP1"}
        with pytest.raises(ClientResponseError):
            await api.get_grill_details(2)
    assert login.await_count == 2


async def test_persistent_401_fails_the_run():
    api, session = make_api(*[error_response(401)] * 3)
    with logins("token-0", "token-1", "token-2") as login:
        await api.login()
        with pytest.raises(ClientResponseError) as ex:
            await api.get_grill_details(1)
    assert ex.value.status == 401
    # Retried under fresh headers each time, and never returned an empty dict:
    # a 401 is not skippable the way a 404 is.
    assert session.get.await_count == 3
    assert login.await_count == 3


async def test_404_is_skipped():
    api, session = make_api(error_response(404))
    with logins("token-0") as login:
        await api.login()
        assert await api.get_grill_details(1) == {}
    # Skipped immediately: a gap in the ID space is not retried or re-logged.
    session.get.assert_awaited_once()
    login.assert_awaited_once()


async def test_other_client_error_is_raised():
    api, _ = make_api(error_response(403))
    with logins("token-0"):
        await api.login()
        with pytest.raises(ClientResponseError) as ex:
            await api.get_grill_details(1)
    assert ex.value.status == 403


async def test_server_error_is_retried_then_skipped():
    api, session = make_api(*[error_response(500)] * 3)
    with logins("token-0"):
        await api.login()
        assert await api.get_grill_details(1) == {}
    assert session.get.await_count == 3


async def test_server_error_recovers():
    api, session = make_api(error_response(500), grill_response())
    with logins("token-0") as login:
        await api.login()
        assert await api.get_grill_details(1) == {"name": "PB1000SP1"}
    assert session.get.await_count == 2
    login.assert_awaited_once()  # A 5xx does not prompt a re-login.


async def test_connection_drop_is_retried():
    api, session = make_api(ClientConnectionError("closed"), grill_response())
    with logins("token-0"):
        await api.login()
        assert await api.get_grill_details(1) == {"name": "PB1000SP1"}
    assert session.get.await_count == 2


async def test_connection_drop_is_raised_when_persistent():
    api, _ = make_api(*[ClientConnectionError("closed")] * 3)
    with logins("token-0"):
        await api.login()
        with pytest.raises(ClientConnectionError):
            await api.get_grill_details(1)


async def test_unsuccessful_payload_is_raised():
    resp = grill_response()
    resp.json = AsyncMock(return_value={"status": "error", "message": "nope"})
    api, _ = make_api(resp)
    with logins("token-0"):
        await api.login()
        with pytest.raises(Error, match="nope"):
            await api.get_grill_details(1)
