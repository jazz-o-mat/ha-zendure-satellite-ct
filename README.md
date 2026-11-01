# Zendure Satellite CT – Home Assistant Integration

Custom component for Home Assistant that reads current power consumption from a **Zendure Satellite CT** device.

## Features

- Reports current power consumption in Watts (W)
- Polls the API every 30 seconds for fast state updates in HA
- **Effective data resolution: ~60 seconds** — the Zendure cloud API publishes new measurements once per minute, so the displayed value changes at most every 60 seconds regardless of polling interval
- Uses Zendure cloud API (EU region)

## Prerequisites

This integration logs in to the Zendure cloud with account credentials. Because logging in anywhere rotates the account's token (invalidating any previous session), you should **not** use your primary Zendure account — a login from your phone would break Home Assistant and vice versa.

1. **Create a second, dedicated Zendure account** (a separate e-mail) that is used *only* by Home Assistant and nowhere else.
2. **Share the Satellite CT device** from your primary account to this second account (in the Zendure app: device → share). The second account only needs access to the CT, nothing else.
3. Use the second account's e-mail and password in the configuration below.

> Without the device being shared to the second account, the integration will authenticate but find no CT data.

## Installation via HACS

1. Add this repository as a custom repository in HACS:  
   HACS → Integrations → ⋮ → Custom repositories → `https://github.com/jazz-o-mat/ha-zendure-satellite-ct` → Integration
2. Install "Zendure Satellite CT"
3. Restart Home Assistant

## Manual Installation

Copy `custom_components/zendure_satellite_ct/` to your HA `/config/custom_components/` directory and restart Home Assistant.

## Configuration

### Recommended: username + password (auto-login)

The integration logs in to the Zendure cloud itself and refreshes the token automatically when it expires. No more mitmproxy, no emulator, no manual token updates.

Add to `configuration.yaml`:

```yaml
sensor:
  - platform: zendure_satellite_ct
    username: !secret zendure_username
    password: !secret zendure_password
    device_id: YOUR_DEVICE_ID
```

Add to `secrets.yaml`:

```yaml
zendure_username: "second-account@example.com"
zendure_password: "your-password"
```

> **Tip:** Use a dedicated second Zendure account that only shares the CT device. Logging in anywhere else with that account rotates the token — with auto-login the integration simply logs back in on the next poll, but a shared-only account keeps things clean.

### Alternative: static bearer token (legacy)

```yaml
sensor:
  - platform: zendure_satellite_ct
    bearer_token: !secret zendure_bearer_token
    device_id: YOUR_DEVICE_ID
```

```yaml
zendure_bearer_token: "userId:UUID"
```

The token is obtained via mitmproxy from the Zendure Android app (format `userId:UUID`). This mode does **not** refresh automatically — when the token expires the sensor goes `unavailable` until you update it. Prefer the username/password mode above.

### Finding your device ID

The device ID is visible in the API response body, or in mitmproxy traffic when opening the CT view in the Zendure app.

## Options

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `username` | yes* | — | Zendure account e-mail (enables auto-login) |
| `password` | yes* | — | Zendure account password |
| `bearer_token` | yes* | — | Static blade-auth token (`userId:UUID`); alternative to username/password |
| `device_id` | yes | — | Numeric device ID of the Satellite CT |
| `name` | no | `Satellite CT Stromverbrauch` | Entity friendly name |
| `zone` | no | `Europe/Berlin` | Timezone for API queries |
| `app_version` | no | `5.8.3` | App version string embedded in the login signature (see below) |

\* Provide **either** `username` + `password` (recommended) **or** `bearer_token`.

## How the auto-login works

The Zendure login endpoint (`POST /eu/auth/app/token`) requires a `sign` header: `Base64(RSA/PKCS1v15(publicKey, "<appVersion>./auth/app/token/<unix-seconds>"))`, using a 1024-bit RSA public key embedded in the app. The integration reproduces this signature (public key + `appVersion` are constants in `sensor.py`), logs in with the account credentials, and uses the returned `accessToken` as the blade-auth token. On a `401`/expired token it logs in again and retries. The data endpoint itself does not require the signature.

The server currently accepts **any** non-empty `appVersion` value, so the default (`5.8.3`) needs no maintenance even though it is an old app version. Should Zendure ever start enforcing a minimum version, override it without touching code:

```yaml
    app_version: "6.0.0"
```

The only version-dependent part that can actually break is the embedded RSA public key. If a future Zendure app ships a new key, extract it from the new APK (`strings libzen_native.so | grep '^MI[IG]'`, in the `arm64-v8a` split) and update the `ZENDURE_PUBKEY_B64` constant in `sensor.py`.
