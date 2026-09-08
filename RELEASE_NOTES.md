# Restore gateway port telemetry compatibility

- Restore gateway's variable-header port push decoding. Port telemetry must not pass through the settings Result parser or a 16/17-byte frame-length filter.
- Retain hardware protocol decoding, existing configuration and entity IDs.
- Add regressions for varying port headers and verify raw port frames reach real HA voltage/current/power entities.

# HA Bluetooth only

- Adopt the HA-routed Bluetooth implementation from gateway/main (`734bbce`).
- Remove MQTT, the standalone BLE Server/Web/Docker stack, Bemfa and custom ESP32 bridge.
- Move the integration to the standard root `custom_components/` layout for HACS.
- Port hardware protocol decoding and PIID 17/18 live updates from main's v3.
- Handle device nonce rollover, corrupt-frame recovery and bounded notification queues.
- Preserve settings/telemetry received during commands and authentication.
- Correct screen-off-time values; reject invalid setting values and unknown control bitmasks.
- Serialize port/protocol updates and disconnects; roll back failed connection toggles.
- Add silent-link/session probes and authentication cooldown without resetting shared adapters.
- Preserve direct-Bluetooth config entries and entity unique IDs. Entries without Bluetooth credentials must be recreated.

See [migration details](docs/direct-bluetooth.md) and [installation](README.md).
