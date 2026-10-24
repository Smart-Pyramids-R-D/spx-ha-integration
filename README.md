# Smart Pyramids for Home Assistant

The Smart Pyramids integration connects Home Assistant to the cloud-connected
Smart Pyramids switches and relay devices associated with your SPX account. It
uses the same cloud API and MQTT service as the Smart Pyramids app.

The integration supports Smart Pyramids devices that expose controllable
relays. Sensors, device provisioning, sharing management, and local/LAN control
are not currently supported.

## Installation

1. Open HACS in Home Assistant and select **Integrations**.
2. Select **Explore & download repositories** and search for
   **Smart Pyramids**.
3. Select the integration, choose **Download**, and restart Home Assistant when
   HACS prompts you to do so.

## Setup

1. In Home Assistant, go to **Settings** > **Devices & services**.
2. Select **Add integration**, search for **Smart Pyramids**, and select it.
3. Sign in with the username and password for your Smart Pyramids account.

Home Assistant stores the resulting refresh token, not your password. An
internet connection is required. If the session expires or is revoked, Home
Assistant creates a repair notification and asks you to sign in again.

## Entities

Each supported physical device is added as a Home Assistant device with one
switch entity for every relay reported by the Smart Pyramids service, named
**Relay 1**, **Relay 2**, and so on. Relay availability and state follow the
device's MQTT status and state reports.
