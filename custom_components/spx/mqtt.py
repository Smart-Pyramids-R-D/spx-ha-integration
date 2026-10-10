"""SPX MQTT connection and protocol messages."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from pathlib import Path
import random
import ssl
import time

import paho.mqtt.client as mqtt

from .api import SPXApi, SPXApiError, SPXAuthError
from .const import MQTT_HOST, MQTT_PORT

_LOGGER = logging.getLogger(__name__)


def command(device_topic: str, relay: int, on: bool) -> tuple[str, bytes]:
    if relay < 1:
        raise ValueError("Relay index must be positive")
    return f"devices/{device_topic}/r{relay}/cmd", b"ON" if on else b"OFF"


def parse_state(payload: str) -> bool | None:
    import json

    value = payload.strip().upper()
    if value in ("ON", "1", "TRUE"):
        return True
    if value in ("OFF", "0", "FALSE"):
        return False
    try:
        decoded = json.loads(payload)
    except ValueError:
        return None
    if isinstance(decoded, dict):
        decoded = next((decoded[k] for k in ("state", "status", "value") if k in decoded), None)
    if isinstance(decoded, (str, int, bool)):
        return parse_state(str(decoded)) if str(decoded) != payload else None
    return None


def parse_status(payload: str) -> bool | None:
    import json

    value = payload.strip().upper()
    if value == "ONLINE":
        return True
    if value == "OFFLINE":
        return False
    try:
        decoded = json.loads(payload)
    except ValueError:
        return None
    if isinstance(decoded, dict):
        decoded = next((decoded[k] for k in ("status", "state", "value") if k in decoded), None)
    if isinstance(decoded, str):
        return parse_status(decoded) if decoded != payload else None
    return None


class SPXMqtt:
    """One account's broker connection with token renewal and reconnect."""

    def __init__(
        self, api: SPXApi, topics: Callable[[], set[str]],
        on_message: Callable[[str, str], None], on_connection: Callable[[bool], None],
    ) -> None:
        self.api = api
        self._topics = topics
        self._on_message = on_message
        self._on_connection = on_connection
        self._client: mqtt.Client | None = None
        self._worker: asyncio.Task[None] | None = None
        self._subscribed: set[str] = set()
        self._connected = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._worker = asyncio.create_task(self._run(), name="spx_mqtt")
        await asyncio.wait_for(self._connected.wait(), timeout=25)

    async def stop(self) -> None:
        self._stopping = True
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        await self._close_client()
        self._connected.clear()
        self._on_connection(False)

    async def _close_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await asyncio.to_thread(client.disconnect)
            await asyncio.to_thread(client.loop_stop)
        self._subscribed.clear()

    def _connected_callback(self, client: mqtt.Client, userdata: object, flags: object, reason_code: object, properties: object) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._handle_connect, client, reason_code == 0)

    def _handle_connect(self, client: mqtt.Client, success: bool) -> None:
        if client is not self._client:
            return
        if success:
            self._subscribed.clear()
            self._connected.set()
            self.resubscribe()
            self._on_connection(True)
        else:
            self._disconnected.set()

    def _disconnected_callback(self, client: mqtt.Client, userdata: object, flags: object, reason_code: object, properties: object) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._handle_disconnect, client)

    def _handle_disconnect(self, client: mqtt.Client) -> None:
        if client is self._client:
            self._connected.clear()
            self._on_connection(False)
            self._disconnected.set()

    def _message_callback(self, client: mqtt.Client, userdata: object, message: mqtt.MQTTMessage) -> None:
        if self._loop:
            topic = message.topic
            payload = message.payload.decode("utf-8", errors="replace")
            self._loop.call_soon_threadsafe(self._on_message, topic, payload)

    async def _run(self) -> None:
        delay = 2
        while not self._stopping:
            try:
                policy = await self.api.mqtt_policy(force=True)
                client = mqtt.Client(
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                    client_id=f"ha_spx_{random.getrandbits(64):016x}",
                    clean_session=True,
                )
                client.username_pw_set(policy.username, policy.token)
                await asyncio.to_thread(
                    client.tls_set,
                    ca_certs=str(Path(__file__).with_name("ca.crt")),
                    cert_reqs=ssl.CERT_REQUIRED,
                )
                client.on_connect = self._connected_callback
                client.on_disconnect = self._disconnected_callback
                client.on_message = self._message_callback
                self._client = client
                self._connected.clear()
                self._disconnected.clear()
                await asyncio.to_thread(client.connect, MQTT_HOST, MQTT_PORT, 60)
                client.loop_start()
                await asyncio.wait_for(self._connected.wait(), timeout=15)
                delay = 2
                if policy.expires_at:
                    timeout = max(60, policy.expires_at - time.time() - 60)
                    try:
                        await asyncio.wait_for(self._disconnected.wait(), timeout=timeout)
                    except TimeoutError:
                        pass
                else:
                    await self._disconnected.wait()
            except (OSError, TimeoutError, RuntimeError, SPXApiError, SPXAuthError) as exc:
                _LOGGER.warning("SPX MQTT connection failed (%s); retrying", type(exc).__name__)
            finally:
                self._connected.clear()
                self._on_connection(False)
                await self._close_client()
            if not self._stopping:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    def resubscribe(self) -> None:
        if self.connected and self._client:
            desired = self._topics()
            for topic in self._subscribed - desired:
                self._client.unsubscribe(topic)
            for topic in desired - self._subscribed:
                self._client.subscribe(topic, qos=1)
            self._subscribed = desired

    async def publish(self, topic: str, payload: bytes) -> None:
        if not self.connected or not self._client:
            raise ConnectionError("SPX MQTT is disconnected")
        result = self._client.publish(topic, payload, qos=1, retain=False)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError(f"SPX MQTT publish failed ({result.rc})")
