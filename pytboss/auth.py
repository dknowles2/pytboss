"""Authentication routines."""

from aiohttp import ClientSession, ContentTypeError

from .exceptions import Unauthorized

API_URL = "https://api-prod.dansonscorp.com/api/v1"


async def async_login(
    session: ClientSession, email: str, password: str
) -> dict[str, str]:
    """Authenticates against the PitBoss cloud API and returns auth headers.

    :param session: An open aiohttp session to issue the request on.
    :param email: The PitBoss account email address.
    :param password: The PitBoss account password.
    :raise pytboss.exceptions.Unauthorized: If the credentials are rejected.
    """
    payload = {"email": email, "password": password}
    async with session.post(f"{API_URL}/login/app", json=payload) as response:
        try:
            response_json = await response.json()
        except ContentTypeError:
            response.raise_for_status()
            raise Unauthorized("Unexpected non-JSON response from login endpoint")
        if response_json.get("status") != "success":
            errors = response_json.get("errors") or {}
            raise Unauthorized(errors.get("message", "Login failed"))
        token = response_json["data"]["token"]
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
