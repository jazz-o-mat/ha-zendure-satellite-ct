"""Zendure Satellite CT – Sensor Platform."""
from __future__ import annotations

import asyncio
import base64
from datetime import date, timedelta
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

DEFAULT_NAME = "Satellite CT Stromverbrauch"
DEFAULT_ZONE = "Europe/Berlin"
SCAN_INTERVAL = timedelta(seconds=30)

BASE_URL = "https://app.zendure.tech/eu/"
ENERGY_URL = BASE_URL + "tdengine/device/smartct/energy"
LOGIN_URL = BASE_URL + "auth/app/token"
LOGIN_PATH = "auth/app/token"  # Pfad-Teil für die sign-Signatur

# appVersion, die in der sign-Signatur mitverschlüsselt wird. Stammt aus der
# Zendure-Android-App (versionName). Server validiert sie gegen den sign-Header.
APP_VERSION = "5.8.3"

# RSA-Public-Key (1024 bit, SPKI/DER base64) aus libzen_native.so der Zendure-App.
# Wird benutzt, um den sign-Header für den Login zu erzeugen (RSA/ECB/PKCS1Padding).
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
        }
    ),
    cv.has_at_least_one_key(CONF_BEARER_TOKEN, CONF_USERNAME),
)


def _build_sign(path: str, timestamp: int) -> str:
    """Erzeuge den sign-Header: Base64(RSA/PKCS1v15(pubkey, 'appVer./path/ts')).

    Läuft synchron (RSA-1024-Verschlüsselung, wenige Mikrosekunden) und wird vom
    Aufrufer im Executor ausgeführt, um den Event-Loop nicht zu blockieren.
    """
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    pub = load_der_public_key(base64.b64decode(ZENDURE_PUBKEY_B64))
    plain = f"{APP_VERSION}./{path}/{timestamp}".encode()
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
    )
    await coordinator.async_refresh()

    async_add_entities([ZendureCTPowerSensor(coordinator, name, device_id)])


class ZendureCTCoordinator(DataUpdateCoordinator):
    """Coordinator für Zendure Satellite CT API-Polling mit Auto-Login."""

    def __init__(
        self, hass, session, *, device_id, zone, bearer_token, username, password
    ):
        super().__init__(
            hass,
            _LOGGER,
            name="Zendure Satellite CT",
            update_interval=SCAN_INTERVAL,
        )
        self._session = session
        self._device_id = device_id
        self._zone = zone
        self._username = username
        self._password = password
        # Statischer Token aus der Konfiguration (optional). Wird bei
        # username/password-Betrieb durch den Login-Token überschrieben.
        self._token = bearer_token
        self._login_lock = asyncio.Lock()

    @property
    def _can_login(self) -> bool:
        return bool(self._username and self._password)

    async def _login(self) -> None:
        """Logge dich bei Zendure ein und speichere den frischen Token."""
        if not self._can_login:
            raise UpdateFailed(
                "Token abgelaufen und kein username/password zum automatischen "
                "Neu-Einloggen in secrets.yaml hinterlegt."
            )
        async with self._login_lock:
            timestamp = int(time.time())  # Sekunden (App: currentTimeMillis()/1000)
            sign = await self.hass.async_add_executor_job(
                _build_sign, LOGIN_PATH, timestamp
            )
            headers = {
                "Content-Type": "application/json",
                "appVersion": APP_VERSION,
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
                raise UpdateFailed(f"Login-Netzwerkfehler: {err}") from err

            if not data.get("success"):
                raise UpdateFailed(
                    f"Zendure-Login fehlgeschlagen: {data.get('msg')} "
                    f"(code {data.get('code')})"
                )

            token = (data.get("data") or {}).get("accessToken")
            if not token:
                raise UpdateFailed("Zendure-Login lieferte keinen accessToken")

            self._token = token
            _LOGGER.info("Zendure: neuer Token per Auto-Login erhalten")

    async def _fetch_energy(self):
        """Hole die Energie-Daten mit dem aktuellen Token. True-Rückgabe = 401."""
        today = date.today().isoformat()
        headers = {
            "blade-auth": f"bearer {self._token}",
            "Content-Type": "application/json",
        }
        payload = {
            "deviceId": self._device_id,
            "beginDate": today,
            "endDate": today,
            "circuitType": 1,
            "zone": self._zone,
            "type": 0,
        }
        try:
            async with asyncio.timeout(30):
                async with self._session.post(
                    ENERGY_URL, json=payload, headers=headers
                ) as resp:
                    if resp.status == 401:
                        return True, None
                    if not resp.ok:
                        raise UpdateFailed(f"Zendure API HTTP {resp.status}")
                    data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Netzwerkfehler: {err}") from err

        # Abgelaufener Token kann sich auch als success=False im Body zeigen.
        if not data.get("success"):
            code = data.get("code")
            if code in (401, 402, 403):
                return True, None
            raise UpdateFailed(f"Zendure API Fehler: {data.get('msg')}")

        return False, data

    async def _async_update_data(self):
        if not self._token:
            await self._login()

        expired, data = await self._fetch_energy()
        if expired:
            # Token abgelaufen → einmal neu einloggen und erneut versuchen.
            _LOGGER.info("Zendure: Token abgelaufen, logge neu ein")
            await self._login()
            expired, data = await self._fetch_energy()
            if expired:
                raise UpdateFailed(
                    "Zendure API lehnt Token auch nach Neu-Login ab"
                )

        entries = data.get("data", {}).get("data", {}).get("1", [])
        if not entries:
            return None

        # Letzten echten Wert nehmen (API schreibt 0 als Platzhalter für die laufende Minute)
        for entry in reversed(entries):
            if entry.get("data") is not None and entry["data"] > 0:
                return entry["data"]
        return 0


class ZendureCTPowerSensor(CoordinatorEntity, SensorEntity):
    """Aktueller Leistungswert des Satellite CT in Watt."""

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
