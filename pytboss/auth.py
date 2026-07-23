"""Authentication routines."""

from aiohttp import ClientSession

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
    params = {"email": email, "password": password}
    async with session.post(f"{API_URL}/login/app", params=params) as response:
        response_json = await response.json()
        if response_json["status"] == "error":
            raise Unauthorized(response_json["error"]["message"])
        token = (await response.json())["data"]["token"]
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
