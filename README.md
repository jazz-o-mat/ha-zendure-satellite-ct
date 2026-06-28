# Zendure Satellite CT – Home Assistant Integration

Custom component for Home Assistant that reads current power consumption from a **Zendure Satellite CT** device.

## Features

- Reports current power consumption in Watts (W)
- Updates every 30 seconds
- Uses Zendure cloud API (EU region)

## Installation via HACS

1. Add this repository as a custom repository in HACS:  
   HACS → Integrations → ⋮ → Custom repositories → `https://github.com/jazz-o-mat/ha-zendure-satellite-ct` → Integration
2. Install "Zendure Satellite CT"
3. Restart Home Assistant

## Manual Installation

Copy `custom_components/zendure_satellite_ct/` to your HA `/config/custom_components/` directory and restart Home Assistant.

## Configuration

Add to `configuration.yaml`:

```yaml
sensor:
  - platform: zendure_satellite_ct
    bearer_token: !secret zendure_bearer_token
    device_id: YOUR_DEVICE_ID
```

Add to `secrets.yaml`:

```yaml
zendure_bearer_token: "userId:token"
```

### Getting the bearer token

The bearer token is obtained via mitmproxy from the Zendure Android app. It has the format `userId:UUID`.

> **Tip:** Use a dedicated second Zendure account that shares only the CT device — this prevents your main account logins from invalidating the token.

### Finding your device ID

The device ID is visible in mitmproxy traffic when opening the CT view in the Zendure app, or in the API response body.

## Options

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `bearer_token` | yes | — | Zendure blade-auth token (`userId:UUID`) |
| `device_id` | yes | — | Numeric device ID of the Satellite CT |
| `name` | no | `Satellite CT Stromverbrauch` | Entity friendly name |
| `zone` | no | `Europe/Berlin` | Timezone for API queries |
