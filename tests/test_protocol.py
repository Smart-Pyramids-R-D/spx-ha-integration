"""Hardware-free checks for the protocol copied from the Flutter client."""

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "spx"
package = types.ModuleType("spx_protocol_test")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package

if "aiohttp" not in sys.modules:
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = type("ClientError", (Exception,), {})
    aiohttp.ClientConnectorError = type("ClientConnectorError", (aiohttp.ClientError,), {})
    aiohttp.ClientTimeout = lambda **kwargs: None
    aiohttp.ClientSession = object
    sys.modules["aiohttp"] = aiohttp

if "paho.mqtt.client" not in sys.modules:
    paho = types.ModuleType("paho")
    mqtt_package = types.ModuleType("paho.mqtt")
    mqtt_client = types.ModuleType("paho.mqtt.client")
    mqtt_client.Client = object
    mqtt_client.MQTTMessage = object
    mqtt_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    mqtt_client.MQTT_ERR_SUCCESS = 0
    paho.mqtt = mqtt_package
    mqtt_package.client = mqtt_client
    sys.modules.update({"paho": paho, "paho.mqtt": mqtt_package, "paho.mqtt.client": mqtt_client})


def load(name):
    full_name = f"spx_protocol_test.{name}"
    spec = importlib.util.spec_from_file_location(full_name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


load("const")
api = load("api")
mqtt = load("mqtt")

ha = types.ModuleType("homeassistant")
ha.__path__ = []
core = types.ModuleType("homeassistant.core")
core.HomeAssistant = object
core.callback = lambda func: func
helpers = types.ModuleType("homeassistant.helpers")
helpers.__path__ = []
dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
dispatcher.async_dispatcher_connect = lambda hass, key, callback: lambda: None
dispatcher.async_dispatcher_send = lambda hass, key: None
sys.modules.update({
    "homeassistant": ha, "homeassistant.core": core,
    "homeassistant.helpers": helpers, "homeassistant.helpers.dispatcher": dispatcher,
})
coordinator = load("coordinator")

components = types.ModuleType("homeassistant.components")
components.__path__ = []
switch_component = types.ModuleType("homeassistant.components.switch")
switch_component.SwitchEntity = object
config_entries = types.ModuleType("homeassistant.config_entries")
config_entries.ConfigEntry = object
entity = types.ModuleType("homeassistant.helpers.entity")
entity.DeviceInfo = dict
entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
entity_platform.AddEntitiesCallback = object
sys.modules.update({
    "homeassistant.components": components,
    "homeassistant.components.switch": switch_component,
    "homeassistant.config_entries": config_entries,
    "homeassistant.helpers.entity": entity,
    "homeassistant.helpers.entity_platform": entity_platform,
})
switch = load("switch")


class ProtocolTests(unittest.TestCase):
    def test_device_parsing_and_identity(self):
        device = api.SPXDevice.from_json({
            "deviceId": "device119", "deviceName": "Kitchen", "topic": "devices/kitchen/r1/state",
            "relayCount": {"N": "4"}, "role": "shared", "masterDeviceId": "master1",
        })
        self.assertEqual((device.device_id, device.topic, device.relay_count), ("device119", "kitchen", 4))
        self.assertEqual(device.role, "shared")
        self.assertEqual(device.master_device_id, "master1")
        self.assertEqual(api.relay_unique_id(device.device_id, 1), "device119_r1")
        self.assertIsNone(api.SPXDevice.from_json({"deviceId": "sensor1", "deviceKind": "sensor", "topic": "x"}))

    def test_commands_and_reports(self):
        self.assertEqual(mqtt.command("kitchen", 2, True), ("devices/kitchen/r2/cmd", b"ON"))
        self.assertEqual(mqtt.command("kitchen", 2, False), ("devices/kitchen/r2/cmd", b"OFF"))
        for payload in ("ON", "1", "true", '{"state":"ON"}'):
            self.assertIs(mqtt.parse_state(payload), True)
        for payload in ("OFF", "0", "false", '{"value":0}'):
            self.assertIs(mqtt.parse_state(payload), False)
        self.assertIsNone(mqtt.parse_state("bad"))
        self.assertIs(mqtt.parse_status('{"status":"OFFLINE"}'), False)
        self.assertIs(mqtt.parse_status("ONLINE"), True)

    def test_mqtt_credentials_require_user_identity(self):
        with self.assertRaises(api.SPXApiError):
            api.parse_mqtt_policy('{"token":"opaque"}')
        policy = api.parse_mqtt_policy('{"token":"opaque","mqttUsername":"alice"}')
        self.assertEqual(policy.username, "alice")

    def test_mqtt_disconnect_marks_unavailable(self):
        changes = []
        transport = mqtt.SPXMqtt(
            object(), object(), None,
            lambda: {"devices/kitchen/r1/state"}, lambda *args: None, changes.append,
        )

        class Client:
            def subscribe(self, topic, qos):
                self.topic = topic

        client = Client()
        transport._client = client
        transport._handle_connect(client, True)
        self.assertTrue(transport.connected)
        self.assertEqual(client.topic, "devices/kitchen/r1/state")
        transport._handle_disconnect(client)
        self.assertFalse(transport.connected)
        self.assertEqual(changes, [True, False])

    def test_reported_state_and_offline(self):
        account = coordinator.SPXCoordinator(object(), object(), None, "key")
        account.devices = {"device119": api.SPXDevice("device119", "Kitchen", "kitchen", 2)}
        account._topic_devices = {"kitchen": "device119"}
        account._connection(True)
        self.assertFalse(account.available("device119", 1))
        account._message("devices/kitchen/r1/state", "ON")
        self.assertTrue(account.available("device119", 1))
        self.assertTrue(account.available("device119", 2))
        self.assertNotIn(("device119", 2), account.relay_states)
        self.assertIs(account.relay_states[("device119", 1)], True)
        account._message("devices/kitchen/status", "OFFLINE")
        self.assertFalse(account.available("device119", 1))
        self.assertNotIn(("device119", 1), account.relay_states)

    def test_four_relays_share_one_device(self):
        account = coordinator.SPXCoordinator(object(), object(), None, "key")
        account.devices = {"device119": api.SPXDevice("device119", "Kitchen", "kitchen", 4)}
        entities = [switch.SPXRelaySwitch(account, "device119", relay) for relay in range(1, 5)]
        self.assertEqual([item._attr_unique_id for item in entities], [f"device119_r{i}" for i in range(1, 5)])
        self.assertEqual({frozenset(item.device_info["identifiers"]) for item in entities}, {frozenset({("spx", "device119")})})


class ApiFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_refresh_starts_reauth_and_stops_on_auth_error(self):
        hass = object()

        class Entry:
            def __init__(self):
                self.reauth_calls = []

            def async_start_reauth(self, reauth_hass):
                self.reauth_calls.append(reauth_hass)

        class Api:
            async def devices(self):
                raise api.SPXAuthError("refresh token revoked")

        entry = Entry()
        account = coordinator.SPXCoordinator(hass, entry, Api(), "key")

        with patch.object(coordinator.asyncio, "sleep", new=AsyncMock()):
            await account._periodic_refresh()

        self.assertEqual(entry.reauth_calls, [hass])

    async def test_mqtt_starts_reauth_and_stops_on_auth_error(self):
        hass = object()

        class Entry:
            def __init__(self):
                self.reauth_calls = []

            def async_start_reauth(self, reauth_hass):
                self.reauth_calls.append(reauth_hass)

        class Api:
            def __init__(self):
                self.policy_calls = 0

            async def mqtt_policy(self, force):
                self.policy_calls += 1
                raise api.SPXAuthError("refresh token revoked")

        entry = Entry()
        client = Api()
        transport = mqtt.SPXMqtt(
            hass, entry, client, lambda: set(), lambda *args: None, lambda connected: None
        )

        await transport._run()

        self.assertEqual(entry.reauth_calls, [hass])
        self.assertEqual(client.policy_calls, 1)

    async def test_online_relay_can_command_without_initial_state(self):
        account = coordinator.SPXCoordinator(object(), object(), None, "key")
        account.devices = {"device119": api.SPXDevice("device119", "Kitchen", "kitchen", 2)}
        account._connection(True)
        account.online["device119"] = True
        published = []

        async def publish(topic, payload):
            published.append((topic, payload))

        account._mqtt.publish = publish
        await account.set_relay("device119", 2, True)
        self.assertEqual(published, [("devices/kitchen/r2/cmd", b"ON")])
        self.assertNotIn(("device119", 2), account.relay_states)

    async def test_cognito_amazon_json_content_type(self):
        class Response:
            status = 200
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def json(self, *, content_type):
                self.content_type = content_type
                return {"AuthenticationResult": {
                    "AccessToken": "access", "RefreshToken": "refresh", "ExpiresIn": 3600,
                }}

        response = Response()

        class Session:
            def post(self, *args, **kwargs):
                return response

        client = api.SPXApi(Session())
        await client.login("user", "password")
        self.assertIsNone(response.content_type)
        self.assertEqual(client.refresh_token, "refresh")

    async def test_rejected_login(self):
        class Response:
            status = 401
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False

        class Session:
            def post(self, *args, **kwargs):
                return Response()

        client = api.SPXApi(Session())
        with self.assertRaises(api.SPXAuthError):
            await client.login("user", "password")


if __name__ == "__main__":
    unittest.main()
