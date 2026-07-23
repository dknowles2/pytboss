from unittest.mock import AsyncMock

import pytest

from pytboss.config import Config
from pytboss.transport import Transport


@pytest.fixture
def conn() -> AsyncMock:
    return AsyncMock(spec=Transport)


@pytest.fixture
def config(conn: AsyncMock) -> Config:
    return Config(conn)


async def test_get_info(config: Config, conn: AsyncMock):
    conn.send_command.return_value = {"app": "my-app"}
    assert await config.get_info() == {"app": "my-app"}
    conn.send_command.assert_awaited_once_with("Sys.GetInfo", {})


async def test_get_config_no_key(config: Config, conn: AsyncMock):
    conn.send_command.return_value = {"wifi": {}}
    assert await config.get_config() == {"wifi": {}}
    conn.send_command.assert_awaited_once_with("Config.Get", {})


async def test_get_config_with_key(config: Config, conn: AsyncMock):
    conn.send_command.return_value = {"ssid": "my-ssid"}
    assert await config.get_config("wifi.sta.ssid") == {"ssid": "my-ssid"}
    conn.send_command.assert_awaited_once_with("Config.Get", {"key": "wifi.sta.ssid"})


async def test_save_config_with_reboot(config: Config, conn: AsyncMock):
    await config.save_config()
    conn.send_command_without_answer.assert_awaited_once_with(
        "Config.Save", {"reboot": True}
    )
    conn.send_command.assert_not_awaited()


async def test_save_config_without_reboot(config: Config, conn: AsyncMock):
    conn.send_command.return_value = {}
    await config.save_config(reboot=False)
    conn.send_command.assert_awaited_once_with("Config.Save", {"reboot": False})
    conn.send_command_without_answer.assert_not_awaited()


async def test_set(config: Config, conn: AsyncMock):
    conn.send_command.return_value = {}
    assert await config.set(foo="bar") == {}
    conn.send_command.assert_awaited_once_with("Config.Set", {"config": {"foo": "bar"}})


async def test_set_wifi_credentials(config: Config, conn: AsyncMock):
    conn.send_command.return_value = {}
    assert await config.set_wifi_credentials("my-ssid", "my-pass") == {}
    conn.send_command.assert_awaited_once_with(
        "Config.Set",
        {
            "config": {
                "wifi": {"sta": {"enable": True, "ssid": "my-ssid", "pass": "my-pass"}}
            }
        },
    )


async def test_set_wifi_ssid(config: Config, conn: AsyncMock):
    conn.send_command.return_value = {}
    assert await config.set_wifi_ssid("my-ssid") == {}
    conn.send_command.assert_awaited_once_with(
        "Config.Set",
        {"config": {"wifi": {"sta": {"enable": True, "ssid": "my-ssid"}}}},
    )


async def test_set_wifi_password(config: Config, conn: AsyncMock):
    conn.send_command.return_value = {}
    assert await config.set_wifi_password("my-pass") == {}
    conn.send_command.assert_awaited_once_with(
        "Config.Set",
        {"config": {"wifi": {"sta": {"enable": True, "pass": "my-pass"}}}},
    )
