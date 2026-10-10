"""Relay switches backed by SPX reported MQTT state."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SPXCoordinator
from .api import relay_unique_id


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SPXCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    def add_devices() -> None:
        entities = []
        for device in coordinator.devices.values():
            for relay in range(1, device.relay_count + 1):
                unique_id = relay_unique_id(device.device_id, relay)
                if unique_id not in known:
                    known.add(unique_id)
                    entities.append(SPXRelaySwitch(coordinator, device.device_id, relay))
        if entities:
            async_add_entities(entities)

    add_devices()
    entry.async_on_unload(coordinator.async_add_listener(add_devices))


class SPXRelaySwitch(SwitchEntity):
    """One relay on a physical SPX device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SPXCoordinator, device_id: str, relay: int) -> None:
        self.coordinator = coordinator
        self.device_id = device_id
        self.relay = relay
        self._device = coordinator.devices[device_id]
        self._attr_unique_id = relay_unique_id(device_id, relay)
        self._attr_name = f"Relay {relay}"

    @property
    def device_info(self) -> DeviceInfo:
        device = self.coordinator.devices.get(self.device_id, self._device)
        return DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            name=device.name,
            manufacturer="SPX",
        )

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.relay_states.get((self.device_id, self.relay))

    @property
    def available(self) -> bool:
        return self.coordinator.available(self.device_id, self.relay)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))

    async def async_turn_on(self, **kwargs: object) -> None:
        await self.coordinator.set_relay(self.device_id, self.relay, True)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self.coordinator.set_relay(self.device_id, self.relay, False)
