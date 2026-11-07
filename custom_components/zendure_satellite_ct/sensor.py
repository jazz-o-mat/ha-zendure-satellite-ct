"""Zendure Satellite CT – Sensor platform."""
from __future__ import annotations

import asyncio
import base64
from datetime import timedelta
import logging
import time

import aiohttp
import voluptuous as vol

from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CONF_NAME, CONF_PASSWORD, CONF_USERNAME, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

_LOGGER = logging.getLogger(__name__)

CONF_BEARER_TOKEN = "bearer_token"
CONF_DEVICE_ID = "device_id"
CONF_ZONE = "zone"
CONF_APP_VERSION = "app_version"
CONF_POLL_INTERVAL = "poll_interval"

DEFAULT_NAME = "Satellite CT Power"
DEFAULT_ZONE = "Europe/Berlin"
# Default polling interval. The totalPower field is updated server-side roughly
# every 3 s, so we poll at the same rate. Override it via `poll_interval` in
# configuration.yaml (e.g. a higher value to go easier on the servers).
DEFAULT_POLL_INTERVAL = 3
MIN_POLL_INTERVAL = 1

BASE_URL = "https://app.zendure.tech/eu/"
DEVICE_LIST_URL = BASE_URL + "productModule/device/queryDeviceListByConsumerId"
DEVICE_LIST_PATH = "productModule/device/queryDeviceListByConsumerId"
LOGIN_URL = BASE_URL + "auth/app/token"
LOGIN_PATH = "auth/app/token"  # path part used for the sign signature

# appVersion that is encrypted into the sign signature (= versionName of the
# Zendure Android app). The server currently accepts any non-empty value, so the
# default needs no maintenance. Should Zendure ever enforce a minimum version, it
# can be overridden via `app_version` in configuration.yaml without touching this
# file.
DEFAULT_APP_VERSION = "5.8.3"

# RSA public key (1024 bit, SPKI/DER base64) from libzen_native.so of the Zendure
# app. Used to build the sign header for login (RSA/ECB/PKCS1Padding).
ZENDURE_PUBKEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCwoqArnVK5cuSHL3t9uf3d1CGlmcaFE/NT"
    "+t84RuMfLZzfuZbmElP3XmyywvTkNUlBdr6Za2tjVKQqKo5AVV1cxVpa+GLrLpSuH4U/zeLz"
    "eKLpNy9k+B9FoLXBDT4LaJoIcj9VLhlZWMTLxVwXU5NhI/lxWj7cK337dux7NbKltwIDAQAB"
)

PLATFORM_SCHEMA = vol.All(
    PLATFORM_SCHEMA.extend(
        {
            vol.Optional(CONF_BEARER_TOKEN): cv.string,
            vol.Optional(CONF_USERNAME): cv.string,
            vol.Optional(CONF_PASSWORD): cv.string,
            vol.Required(CONF_DEVICE_ID): vol.Coerce(int),
            vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
            vol.Optional(CONF_ZONE, default=DEFAULT_ZONE): cv.string,
            vol.Optional(CONF_APP_VERSION, default=DEFAULT_APP_VERSION): cv.string,
            vol.Optional(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL)
            ),
        }
    ),
    cv.has_at_least_one_key(CONF_BEARER_TOKEN, CONF_USERNAME),
)


def _as_number(raw):
    """Return totalPower only as a real numeric value, otherwise None.

    Hardening: the state is fed directly from the API response. A non-numeric
    value (e.g. from a tampered or compromised API) is rejected instead of being
    passed through unchecked as the sensor state. bool is deliberately rejected
    (in Python, bool is a subclass of int).
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return raw
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            pass
    if raw is not None:
        _LOGGER.warning("Zendure: unexpected totalPower value %r – discarded", raw)
    return None


def _build_sign(app_version: str, path: str, timestamp: int) -> str:
    """Build the sign header: Base64(RSA/PKCS1v15(pubkey, 'appVer./path/ts')).

    Runs synchronously (RSA-1024 encryption, a few microseconds) and is called
    from the executor so it does not block the event loop.
    """
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    pub = load_der_public_key(base64.b64decode(ZENDURE_PUBKEY_B64))
    plain = f"{app_version}./{path}/{timestamp}".encode()
    ciphertext = pub.encrypt(plain, padding.PKCS1v15())
    return base64.b64encode(ciphertext).decode()


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Zendure Satellite CT sensor."""
    device_id = config[CONF_DEVICE_ID]
    name = config[CONF_NAME]
    zone = config[CONF_ZONE]

    session = async_get_clientsession(hass)
    coordinator = ZendureCTCoordinator(
        hass,
        session,
        device_id=device_id,
        zone=zone,
        bearer_token=config.get(CONF_BEARER_TOKEN),
        username=config.get(CONF_USERNAME),
        password=config.get(CONF_PASSWORD),
        app_version=config[CONF_APP_VERSION],
        poll_interval=config[CONF_POLL_INTERVAL],
    )
    await coordinator.async_refresh()

    async_add_entities([ZendureCTPowerSensor(coordinator, name, device_id)])


class ZendureCTCoordinator(DataUpdateCoordinator):
    """Coordinator for Zendure Satellite CT API polling with auto-login."""

    def __init__(
        self,
        hass,
        session,
        *,
        device_id,
        zone,
        bearer_token,
        username,
        password,
        app_version,
        poll_interval=DEFAULT_POLL_INTERVAL,
    ):
        super().__init__(
            hass,
            _LOGGER,
            name="Zendure Satellite CT",
            update_interval=timedelta(seconds=poll_interval),
        )
        self._session = session
        self._device_id = device_id
        self._zone = zone
        self._username = username
        self._password = password
        self._app_version = app_version
        # Static token from the configuration (optional). In username/password
        # mode it is overwritten by the login token.
        self._token = bearer_token
        self._login_lock = asyncio.Lock()

    @property
    def _can_login(self) -> bool:
        return bool(self._username and self._password)

    async def _login(self) -> None:
        """Log in to Zendure and store the fresh token."""
        if not self._can_login:
            raise UpdateFailed(
                "Token expired and no username/password configured in "
                "secrets.yaml for automatic re-login."
            )
        async with self._login_lock:
            timestamp = int(time.time())  # seconds (app: currentTimeMillis()/1000)
            sign = await self.hass.async_add_executor_job(
                _build_sign, self._app_version, LOGIN_PATH, timestamp
            )
            headers = {
                "Content-Type": "application/json",
                "appVersion": self._app_version,
                "timestamps": str(timestamp),
                "sign": sign,
                "style": "1",
                "Accept-Language": "en",
            }
            body = {
                "grantType": "password",
                "tenantId": "",
                "account": self._username,
                "password": self._password,
                "appType": "Android",
            }
            try:
                async with asyncio.timeout(30):
                    async with self._session.post(
                        LOGIN_URL, json=body, headers=headers
                    ) as resp:
                        data = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise UpdateFailed(f"Login network error: {err}") from err

            if not data.get("success"):
                raise UpdateFailed(
                    f"Zendure login failed: {data.get('msg')} "
                    f"(code {data.get('code')})"
                )

            token = (data.get("data") or {}).get("accessToken")
            if not token:
                raise UpdateFailed("Zendure login returned no accessToken")

            self._token = token
            _LOGGER.info("Zendure: obtained a new token via auto-login")

    async def _fetch_power(self):
        """Fetch the current total power (totalPower) via the device list.

        Returns (expired, value). expired=True signals an expired/invalid token
        (HTTP 401, or an empty device list after a foreign login) and triggers a
        re-login.
        """
        timestamp = int(time.time())
        sign = await self.hass.async_add_executor_job(
            _build_sign, self._app_version, DEVICE_LIST_PATH, timestamp
        )
        headers = {
            "blade-auth": f"bearer {self._token}",
            "Content-Type": "application/json",
            "appVersion": self._app_version,
            "timestamps": str(timestamp),
            "sign": sign,
            "style": "1",
            "Accept-Language": "en",
        }
        try:
            async with asyncio.timeout(30):
                async with self._session.post(
                    DEVICE_LIST_URL, json={}, headers=headers
                ) as resp:
                    if resp.status == 401:
                        return True, None
                    if not resp.ok:
                        raise UpdateFailed(f"Zendure API HTTP {resp.status}")
                    data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Network error: {err}") from err

        if not data.get("success"):
            code = data.get("code")
            if code in (401, 402, 403):
                return True, None
            raise UpdateFailed(f"Zendure API error: {data.get('msg')}")

        devices = data.get("data") or []
        # Empty list despite success=True: the token was invalidated by another
        # login (phone app etc.) → treat it as expired.
        if not devices:
            return True, None

        for dev in devices:
            if dev.get("id") == self._device_id:
                return False, _as_number(dev.get("totalPower"))
        raise UpdateFailed(
            f"Device {self._device_id} not found in the Zendure device list"
        )

    async def _async_update_data(self):
        if not self._token:
            await self._login()

        expired, value = await self._fetch_power()
        if expired:
            # Token expired/invalidated → log in again once and retry.
            _LOGGER.info("Zendure: token invalid, logging in again")
            await self._login()
            expired, value = await self._fetch_power()
            if expired:
                raise UpdateFailed(
                    "Zendure API still rejects the token after re-login"
                )

        # The API occasionally returns 0 (or null) as a placeholder for the
        # sample currently in progress. For a household CT a true 0 W reading is
        # practically never real, so we treat it as "no fresh value yet" and keep
        # the last real reading to avoid short dips in the graph.
        if value in (None, 0, 0.0):
            return self.data  # None at startup -> sensor stays briefly unknown

        return value


class ZendureCTPowerSensor(CoordinatorEntity, SensorEntity):
    """Current power value of the Satellite CT in watts."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_icon = "mdi:flash"

    def __init__(self, coordinator, name, device_id):
        super().__init__(coordinator)
        self._attr_name = name
        self._attr_unique_id = f"zendure_satellite_ct_{device_id}"

    @property
    def native_value(self):
        return self.coordinator.data

    @property
    def available(self):
        return self.coordinator.last_update_success and self.coordinator.data is not None
