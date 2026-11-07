"""Zendure Satellite CT – Sensor Platform."""
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

DEFAULT_NAME = "Satellite CT Stromverbrauch"
DEFAULT_ZONE = "Europe/Berlin"
# Standard-Abfrageintervall. Das Feld totalPower wird serverseitig etwa alle
# 3 s fortgeschrieben, deshalb fragen wir ebenso oft ab. Per `poll_interval` in
# der configuration.yaml überschreibbar (z. B. höher, um die Server zu schonen).
DEFAULT_POLL_INTERVAL = 3
MIN_POLL_INTERVAL = 1

BASE_URL = "https://app.zendure.tech/eu/"
DEVICE_LIST_URL = BASE_URL + "productModule/device/queryDeviceListByConsumerId"
DEVICE_LIST_PATH = "productModule/device/queryDeviceListByConsumerId"
LOGIN_URL = BASE_URL + "auth/app/token"
LOGIN_PATH = "auth/app/token"  # Pfad-Teil für die sign-Signatur

# appVersion, die in der sign-Signatur mitverschlüsselt wird (= versionName der
# Zendure-Android-App). Der Server akzeptiert derzeit jeden nicht-leeren Wert,
# der Default muss also nicht gepflegt werden. Falls Zendure künftig eine
# bestimmte Mindestversion erzwingt, lässt sich der Wert per `app_version` in
# der configuration.yaml überschreiben, ohne diese Datei anzufassen.
DEFAULT_APP_VERSION = "5.8.3"

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
            vol.Optional(CONF_APP_VERSION, default=DEFAULT_APP_VERSION): cv.string,
            vol.Optional(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL)
            ),
        }
    ),
    cv.has_at_least_one_key(CONF_BEARER_TOKEN, CONF_USERNAME),
)


def _as_number(raw):
    """Gib totalPower nur als echten Zahlenwert zurück, sonst None.

    Härtung: Der State wird direkt aus der API-Antwort gespeist. Ein nicht
    numerischer Wert (z. B. durch eine veränderte/kompromittierte API) wird
    verworfen, statt ungeprüft als Sensorzustand übernommen zu werden. bool
    wird bewusst abgelehnt (in Python ist bool eine int-Unterklasse).
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
        _LOGGER.warning("Zendure: unerwarteter totalPower-Wert %r – verworfen", raw)
    return None


def _build_sign(app_version: str, path: str, timestamp: int) -> str:
    """Erzeuge den sign-Header: Base64(RSA/PKCS1v15(pubkey, 'appVer./path/ts')).

    Läuft synchron (RSA-1024-Verschlüsselung, wenige Mikrosekunden) und wird vom
    Aufrufer im Executor ausgeführt, um den Event-Loop nicht zu blockieren.
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
    """Coordinator für Zendure Satellite CT API-Polling mit Auto-Login."""

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

    async def _fetch_power(self):
        """Hole die aktuelle Gesamtleistung (totalPower) über die Geräteliste.

        Gibt (expired, value) zurück. expired=True signalisiert einen
        abgelaufenen/ungültigen Token (HTTP 401 oder leere Geräteliste nach
        einem Fremd-Login), der ein Neu-Einloggen auslöst.
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
            raise UpdateFailed(f"Netzwerkfehler: {err}") from err

        if not data.get("success"):
            code = data.get("code")
            if code in (401, 402, 403):
                return True, None
            raise UpdateFailed(f"Zendure API Fehler: {data.get('msg')}")

        devices = data.get("data") or []
        # Leere Liste trotz success=True: der Token wurde durch einen anderen
        # Login (Handy-App o. ä.) entwertet → wie abgelaufen behandeln.
        if not devices:
            return True, None

        for dev in devices:
            if dev.get("id") == self._device_id:
                return False, _as_number(dev.get("totalPower"))
        raise UpdateFailed(
            f"Gerät {self._device_id} nicht in der Zendure-Geräteliste gefunden"
        )

    async def _async_update_data(self):
        if not self._token:
            await self._login()

        expired, value = await self._fetch_power()
        if expired:
            # Token abgelaufen/entwertet → einmal neu einloggen und erneut versuchen.
            _LOGGER.info("Zendure: Token ungültig, logge neu ein")
            await self._login()
            expired, value = await self._fetch_power()
            if expired:
                raise UpdateFailed(
                    "Zendure API lehnt Token auch nach Neu-Login ab"
                )

        # Die API liefert gelegentlich 0 (oder null) als Platzhalter für die
        # gerade laufende Messung. Bei einem Haushalts-CT ist echte 0-Leistung
        # praktisch nie real, daher behandeln wir sie als "noch kein frischer
        # Wert" und behalten den letzten echten Messwert, um kurze Einbrüche im
        # Graphen zu vermeiden.
        if value in (None, 0, 0.0):
            return self.data  # beim Start None -> Sensor bleibt kurz unbekannt

        return value


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
