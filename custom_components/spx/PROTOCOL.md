# SPX client protocol findings

Source of truth: Flutter project at `/mnt/data/zMyImportantFiles/work/mobileapp`. The repository shows client behavior and the backend list response; it does not prove live device behavior or broker retention settings.

## Authentication

- `lib/config/app_config.dart` names the Cognito region, public app client ID, hosted UI, cloud API, MQTT JWT URL, and broker. The app client secret is empty.
- `lib/services/auth_service.dart` `loginWithCredentials` uses Cognito `InitiateAuth` with `USER_PASSWORD_AUTH`, `ClientId`, `USERNAME`, and `PASSWORD`. It receives `IdToken`, `AccessToken`, `RefreshToken`, and `ExpiresIn`. A challenge fails the current password sign-in path.
- The same class supports Google via Cognito hosted UI authorization code with PKCE. It also supports `REFRESH_TOKEN_AUTH` for credential sessions and OAuth token endpoint refresh for browser sessions. Tokens are held in memory and, with “remember me,” in secure storage. The integration implements the credential path and stores only the refresh token in its config entry.
- `getAuthHeader` uses the **access token** as `Authorization: Bearer ...` for cloud APIs and the MQTT JWT exchange. The ID token is not used for these requests.

## Device list and model

- `lib/services/device/device_api_service.dart` `listDevices` calls `GET https://dmmjit4odj.execute-api.us-east-1.amazonaws.com/devices/list` through `lib/services/backend_api_client.dart`, with `Accept: application/json` and the bearer access token.
- The response contains a `devices` array. `backend/device_share_func/lambda_function.py` `_list_user_devices` emits `{"devices": [...], "count": n}`. Each entry includes device metadata plus `role`, `isPrimaryOwner`, `sharedByUserId`, `ownerGrantedByUserId`, and `addedAt`. Role is `owner` or `shared`; an owner grant can also make a recipient an owner.
- `lib/models/device_model.dart` reads `deviceId`, `deviceName`/`name`, `topic` and its aliases, `relayCount` (default 4), `deviceKind`, `masterDeviceId`, `childId`, `sensorId`, and `sensorType`. It strips an optional `devices/` prefix and relay/status suffix from `topic`. Sensor records are outside this switch milestone.
- Physical device identity is `deviceId`. The MQTT topic base comes from the API `topic` field and can differ from the ID. This integration never derives the topic from `deviceId`.

## MQTT connection and messages

- `lib/services/auth_service.dart` `getMqttBrokerCredentials` sends `POST https://v4ywovptfb.execute-api.us-east-1.amazonaws.com/MQTT_JWT` with `Authorization: Bearer <access token>` and `Content-Type: application/json`, no body. It accepts a bare JWT, JSON string, or JSON object with token keys such as `mqttToken`, `jwt`, or `token`. Username is read from the response, then the JWT `username` claim. The Flutter app has a hardcoded fallback username; this integration requires a username in the response or token. JWT `exp` governs credential cache reuse.
- `lib/services/mqtt_connection.dart` connects to `100.51.201.208:8884` over TLS with `certs/ca.crt`, generated client ID, MQTT username and JWT password, clean session, and 60 second keepalive. The Flutter client also allows bad certificates. The integration uses the same CA with certificate verification. The live broker certificate was checked and includes the broker IP in its SAN.
- `lib/services/mqtt_service.dart` tracks subscriptions, resubscribes after a new connection, publishes QoS 1 without retain, and retries a broker refusal once with fresh credentials. Its app lifecycle also forces reconnection after long background pauses. The integration refreshes the JWT on every new connection and reconnects with backoff.
- `lib/services/device/mqtt_device_transport.dart` sends switch commands to `devices/{device.topic}/rN/cmd` with UTF-8 `ON` or `OFF`. It subscribes to `devices/{device.topic}/rN/state` and `devices/{device.topic}/status` at QoS 1.
- Relay state accepts `ON`/`OFF`, `1`/`0`, `TRUE`/`FALSE`, or a JSON string/object whose `state`, `status`, or `value` contains those values. Status accepts `ONLINE`/`OFFLINE`, directly or in a JSON string/object. A valid relay state also marks the device online. Offline status or a broker disconnect makes the Home Assistant relay unavailable.
- The app stores the latest received message in memory and limits cached status reuse to 30 seconds. The repository does not establish whether the broker retains state messages. The integration waits for a real state report after connection and does not assume command success means state changed.

## LAN and scope

- `lib/services/device/device_control_service.dart` selects cloud MQTT or `LanDeviceTransport`. `lib/services/device/lan_device_transport.dart` implements authenticated local HTTP state polling and command POSTs, using the device or its master device LAN password. `lib/services/mqtt_connection.dart` also defines optional LAN MQTT. This integration keeps cloud transport in `mqtt.py` and entity state in `coordinator.py`, so local transport can be added without changing the switch entity.
- The first Home Assistant version supports account password login and cloud smart switches. Google hosted UI, sensors, provisioning, sharing management, and LAN control remain outside scope.
