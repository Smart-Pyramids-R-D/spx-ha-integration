"""SPX account devices and authoritative relay states."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send

from .api import SPXApi, SPXAuthError, SPXDevice
from .mqtt import SPXMqtt, command, parse_state, parse_status

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


class SPXCoordinator:
    """Keep cloud metadata separate from the MQTT transport."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, api: SPXApi, key: str
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.api = api
        self.key = key
        self.devices: dict[str, SPXDevice] = {}
        self.relay_states: dict[tuple[str, int], bool] = {}
        self.online: dict[str, bool] = {}
        self._topic_devices: dict[str, str] = {}
        self._mqtt = SPXMqtt(
            hass, entry, api, self._topics, self._message, self._connection
        )
        self._refresh_task: asyncio.Task[None] | None = None
        self._broker_connected = False

    def async_add_listener(self, listener):
        return async_dispatcher_connect(self.hass, self.key, listener)

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, self.key)

    def _topics(self) -> set[str]:
        result: set[str] = set()
        for device in self.devices.values():
            base = f"devices/{device.topic}"
            result.add(f"{base}/status")
            result.update(f"{base}/r{i}/state" for i in range(1, device.relay_count + 1))
        return result

    async def refresh_devices(self) -> None:
        devices = await self.api.devices()
        changed = devices != self.devices
        self.devices = devices
        self._topic_devices = {device.topic: device.device_id for device in devices.values()}
        self.relay_states = {key: value for key, value in self.relay_states.items() if key[0] in devices}
        self.online = {key: value for key, value in self.online.items() if key in devices}
        if changed:
            self._mqtt.resubscribe()
            self._notify()

    async def start(self) -> None:
        await self.refresh_devices()
        await self.api.mqtt_policy()
        await self._mqtt.start()
        self._refresh_task = asyncio.create_task(self._periodic_refresh())

    async def stop(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
        await self._mqtt.stop()

    async def _periodic_refresh(self) -> None:
        import logging
        log = logging.getLogger(__name__)
        while True:
            await asyncio.sleep(300)
            try:
                await self.refresh_devices()
            except SPXAuthError:
                self.entry.async_start_reauth(self.hass)
                return
            except Exception as exc:
                log.warning("SPX device refresh failed: %s", type(exc).__name__)

    @callback
    def _connection(self, connected: bool) -> None:
        self._broker_connected = connected
        if not connected:
            self.online.clear()
            self.relay_states.clear()
        self._notify()

    @callback
    def _message(self, topic: str, payload: str) -> None:
        if not self._broker_connected or not topic.startswith("devices/"):
            return
        path = topic[len("devices/"):]
        if path.endswith("/status"):
            device_id = self._topic_devices.get(path[:-7])
            status = parse_status(payload)
            if device_id and status is not None:
                self.online[device_id] = status
                if not status:
                    for key in list(self.relay_states):
                        if key[0] == device_id:
                            del self.relay_states[key]
                self._notify()
            return
        base, sep, segment = path.rpartition("/")
        if segment != "state":
            return
        device_topic, sep, relay_segment = base.rpartition("/")
        device_id = self._topic_devices.get(device_topic)
        if not device_id or not relay_segment.startswith("r") or not relay_segment[1:].isdigit():
            return
        relay = int(relay_segment[1:])
        state = parse_state(payload)
        if state is None or relay < 1 or relay > self.devices[device_id].relay_count:
            return
        self.relay_states[(device_id, relay)] = state
        self.online[device_id] = True
        self._notify()

    def available(self, device_id: str, relay: int) -> bool:
        device = self.devices.get(device_id)
        return (
            self._broker_connected
            and self.online.get(device_id) is True
            and device is not None
            and 1 <= relay <= device.relay_count
        )

    async def set_relay(self, device_id: str, relay: int, on: bool) -> None:
        device = self.devices[device_id]
        if not self.available(device_id, relay):
            raise ConnectionError("SPX device is offline")
        topic, payload = command(device.topic, relay, on)
        await self._mqtt.publish(topic, payload)
