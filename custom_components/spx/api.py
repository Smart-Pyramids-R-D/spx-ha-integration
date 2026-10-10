"""Authenticated SPX cloud API client."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import json
import re
import time
from typing import Any

import aiohttp

from .const import (
    API_BASE_URL, COGNITO_CLIENT_ID, COGNITO_URL,
    MQTT_JWT_URL,
)


class SPXAuthError(Exception):
    """Cognito or SPX rejected authentication."""


class SPXApiError(Exception):
    """SPX request failed or returned invalid data."""


def jwt_claim(token: str, key: str) -> Any:
    """Read a claim for expiry/identification; this does not verify the JWT."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get(key)
    except (IndexError, ValueError, TypeError):
        return None


def _string(value: Any) -> str | None:
    if isinstance(value, dict):
        return next((text for k in ("S", "s", "value", "stringValue") if (text := _string(value.get(k)))), None)
    return (value.strip() or None) if isinstance(value, str) else None


def _identifier(value: Any) -> str | None:
    if isinstance(value, dict):
        return next((text for k in ("N", "n", "numberValue", "S", "s") if (text := _identifier(value.get(k)))), None)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value)) if int(value) == value else str(value)
    return _string(value)


def _topic(raw: dict[str, Any]) -> str | None:
    keys = ("topic", "mqttTopic", "deviceTopic", "mqtt_topic", "device_topic", "commandTopic", "stateTopic", "statusTopic")
    value = next((_string(raw[k]) for k in keys if _string(raw.get(k))), None)
    if value is None and isinstance(raw.get("mqtt"), dict):
        value = next((_string(raw["mqtt"][k]) for k in ("topic", "commandTopic", "stateTopic", "statusTopic") if _string(raw["mqtt"].get(k))), None)
    if not value or value.lower() == "null":
        return None
    value = value.lstrip("/")
    if value.startswith("devices/"):
        value = value[len("devices/"):]
    parts = value.split("/")
    if len(parts) >= 3 and re.fullmatch(r"(?:r|relay)\d+", parts[-2]) and parts[-1] in ("state", "cmd"):
        value = "/".join(parts[:-2])
    elif value.endswith("/status"):
        value = value[:-7]
    return value or None


@dataclass(frozen=True)
class SPXDevice:
    device_id: str
    name: str
    topic: str
    relay_count: int
    role: str | None = None
    master_device_id: str | None = None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> SPXDevice | None:
        device_id = _identifier(raw.get("deviceId"))
        topic = _topic(raw)
        kind = (_string(raw.get("deviceKind")) or "").lower()
        if not device_id or not topic or kind == "sensor":
            return None
        count = _identifier(raw.get("relayCount"))
        relay_count = int(count) if count and count.isdigit() and int(count) > 0 else 4
        return cls(
            device_id=device_id,
            name=_string(raw.get("deviceName")) or _string(raw.get("name")) or device_id,
            topic=topic,
            relay_count=relay_count,
            role=_string(raw.get("role")) or _string(raw.get("accessRole")),
            master_device_id=_identifier(raw.get("masterDeviceId")),
        )


def relay_unique_id(device_id: str, relay: int) -> str:
    return f"{device_id}_r{relay}"


@dataclass(frozen=True)
class MQTTPolicy:
    username: str
    token: str
    expires_at: int | None

    @property
    def usable(self) -> bool:
        return self.expires_at is None or self.expires_at > time.time() + 60


def parse_mqtt_policy(body: str) -> MQTTPolicy:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = body.strip()
    if isinstance(data, dict) and isinstance(data.get("body"), str):
        return parse_mqtt_policy(data["body"])
    if isinstance(data, str):
        token = data.strip()
        username = jwt_claim(token, "username")
    elif isinstance(data, dict):
        token = next((_string(data.get(k)) for k in ("mqttToken", "jwt", "token", "mqtt_jwt", "mqttJwt", "access_token", "id_token") if _string(data.get(k))), None)
        username = next((_string(data.get(k)) for k in ("mqttUsername", "username", "mqtt_username", "mqttUser") if _string(data.get(k))), None)
        username = username or jwt_claim(token or "", "username")
    else:
        token = None
    if not token:
        raise SPXApiError("MQTT credential response has no token")
    if not username:
        raise SPXApiError("MQTT credential response has no username")
    expiry = jwt_claim(token, "exp")
    return MQTTPolicy(str(username), token, int(expiry) if str(expiry).isdigit() else None)


class SPXApi:
    """Cognito session plus authenticated SPX API requests."""

    def __init__(self, session: aiohttp.ClientSession, refresh_token: str | None = None) -> None:
        self.session = session
        self.refresh_token = refresh_token
        self.access_token: str | None = None
        self.expires_at = 0.0
        self._refresh_lock = asyncio.Lock()
        self._mqtt_policy: MQTTPolicy | None = None

    async def _cognito(self, flow: str, parameters: dict[str, str]) -> dict[str, Any]:
        try:
            async with self.session.post(
                COGNITO_URL,
                headers={"Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth"},
                json={"AuthFlow": flow, "ClientId": COGNITO_CLIENT_ID, "AuthParameters": parameters},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status != 200:
                    raise SPXAuthError(f"Cognito rejected authentication (HTTP {response.status})")
                # Cognito responds with application/x-amz-json-1.1.
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as exc:
            detail = type(exc).__name__
            if isinstance(exc, aiohttp.ClientConnectorError) and exc.os_error:
                detail = f"{detail}: {exc.os_error}"
            raise SPXApiError(f"Cognito connection failed ({detail})") from exc
        result = data.get("AuthenticationResult")
        if not isinstance(result, dict) or not result.get("AccessToken") or not result.get("RefreshToken", self.refresh_token):
            raise SPXAuthError("Cognito did not return a usable session")
        self.access_token = result["AccessToken"]
        self.refresh_token = result.get("RefreshToken") or self.refresh_token
        self.expires_at = time.time() + int(result.get("ExpiresIn", 0))
        self._mqtt_policy = None
        return result

    async def login(self, username: str, password: str) -> None:
        await self._cognito("USER_PASSWORD_AUTH", {"USERNAME": username, "PASSWORD": password})

    async def ensure_token(self) -> str:
        if self.access_token and time.time() < self.expires_at - 30:
            return self.access_token
        async with self._refresh_lock:
            if self.access_token and time.time() < self.expires_at - 30:
                return self.access_token
            if not self.refresh_token:
                raise SPXAuthError("SPX session has no refresh token")
            await self._cognito("REFRESH_TOKEN_AUTH", {"REFRESH_TOKEN": self.refresh_token})
            return self.access_token  # type: ignore[return-value]

    async def _request(self, method: str, url: str) -> str:
        token = await self.ensure_token()
        try:
            async with self.session.request(
                method, url,
                headers={
                    "Authorization": f"Bearer {token}",
                    **({"Content-Type": "application/json"} if method == "POST" else {"Accept": "application/json"}),
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status in (401, 403):
                    raise SPXAuthError(f"SPX rejected authentication (HTTP {response.status})")
                if response.status < 200 or response.status >= 300:
                    raise SPXApiError(f"SPX request failed (HTTP {response.status})")
                return await response.text()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise SPXApiError("SPX cloud is unreachable") from exc

    async def devices(self) -> dict[str, SPXDevice]:
        body = await self._request("GET", f"{API_BASE_URL}/devices/list")
        try:
            raw = json.loads(body)["devices"]
            if not isinstance(raw, list):
                raise ValueError
        except (ValueError, KeyError, TypeError) as exc:
            raise SPXApiError("Invalid device list response") from exc
        devices = (SPXDevice.from_json(item) for item in raw if isinstance(item, dict))
        return {device.device_id: device for device in devices if device is not None}

    async def mqtt_policy(self, force: bool = False) -> MQTTPolicy:
        if not force and self._mqtt_policy and self._mqtt_policy.usable:
            return self._mqtt_policy
        body = await self._request("POST", MQTT_JWT_URL)
        self._mqtt_policy = parse_mqtt_policy(body)
        return self._mqtt_policy
