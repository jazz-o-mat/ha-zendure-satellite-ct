"""Zendure Satellite CT – Sensor Platform."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
import logging

import aiohttp
import voluptuous as vol

from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CONF_NAME, UnitOfPower
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

API_URL = "https://app.zendure.tech/eu/tdengine/device/smartct/energy"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_BEARER_TOKEN): cv.string,
        vol.Required(CONF_DEVICE_ID): vol.Coerce(int),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_ZONE, default=DEFAULT_ZONE): cv.string,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Zendure Satellite CT sensor."""
    bearer_token = config[CONF_BEARER_TOKEN]
    device_id = config[CONF_DEVICE_ID]
    name = config[CONF_NAME]
    zone = config[CONF_ZONE]

    session = async_get_clientsession(hass)
    coordinator = ZendureCTCoordinator(hass, session, bearer_token, device_id, zone)
    await coordinator.async_refresh()

    async_add_entities([ZendureCTPowerSensor(coordinator, name, device_id)])


class ZendureCTCoordinator(DataUpdateCoordinator):
    """Coordinator für Zendure Satellite CT API-Polling."""

    def __init__(self, hass, session, bearer_token, device_id, zone):
        super().__init__(
            hass,
            _LOGGER,
            name="Zendure Satellite CT",
            update_interval=SCAN_INTERVAL,
        )
        self._session = session
        self._bearer_token = bearer_token
        self._device_id = device_id
        self._zone = zone

    async def _async_update_data(self):
        today = date.today().isoformat()
        headers = {
            "blade-auth": f"bearer {self._bearer_token}",
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
                    API_URL, json=payload, headers=headers
                ) as resp:
                    if resp.status == 401:
                        raise UpdateFailed(
                            "Bearer token abgelaufen – bitte in secrets.yaml aktualisieren "
                            "(neuen Token via mitmproxy aus der Zendure App auslesen)"
                        )
                    if not resp.ok:
                        raise UpdateFailed(f"Zendure API HTTP {resp.status}")
                    data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Netzwerkfehler: {err}") from err

        if not data.get("success"):
            raise UpdateFailed(f"Zendure API Fehler: {data.get('msg')}")

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
