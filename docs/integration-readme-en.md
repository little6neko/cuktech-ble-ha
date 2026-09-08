# CUKTECH Charger — direct Home Assistant Bluetooth

Copy `custom_components/cuktech_charger` into your Home Assistant configuration directory and restart, or install this repository as a custom Integration repository in HACS. Requires Home Assistant 2024.1 or newer.

Enable Home Assistant Bluetooth, then add **CUKTECH Charger** under Settings → Devices & services. Enter the charger's Bluetooth MAC address and its 12-byte token (24 hexadecimal characters). The optional BLE key is validated as 16 bytes; the current login protocol uses the token. Credentials can be obtained with Xiaomi Cloud Tokens Extractor.

A local Bluetooth adapter or an ESPHome Bluetooth Proxy supporting active connections is required. Make sure there is an available connection slot, and close other applications connected to the charger.

Existing gateway entries and entity unique IDs are retained. Legacy entries without Bluetooth credentials must be removed and added again. Stop the old gateway before connecting directly.

This version exposes port telemetry, hardware protocol identification, port/protocol switches, settings, countdowns and connection control. It includes nonce rollover handling, live settings updates and per-device reconnects. It does not include MQTT, the standalone server/Web UI, charge-session storage/events, Bemfa or custom ESP32 bridge firmware. Home Assistant Recorder can retain sensor history.

The connection switch controls only this device, never the shared adapter. Setup validates credential format; authentication happens in the background. SET operations update the cache after a transport ACK; later device pushes reconcile the state.
