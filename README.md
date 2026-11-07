# Zendure Satellite CT – Home Assistant Integration

Custom component for Home Assistant that reads current power consumption from a **Zendure Satellite CT** device.

## Features

- Reports current power consumption in Watts (W)
- Reads the live `totalPower` field from the device-list endpoint, which the
  Zendure cloud updates roughly **every 3 seconds** (the same value the app's
  overview shows) — not the minute-resolution energy-history endpoint
- Polls every **3 seconds** by default; configurable via `poll_interval`
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

The integration logs in to the Zendure cloud itself and refreshes the token automatically when it expires — no manual token handling at all.

Add to `configuration.yaml`:

```yaml
sensor:
  - platform: zendure_satellite_ct
    username: !secret zendure_username
    password: !secret zendure_password
    device_id: YOUR_DEVICE_ID
    # poll_interval: 3   # seconds; optional, default 3, minimum 1
```

Add to `secrets.yaml`:

```yaml
zendure_username: "second-account@example.com"
zendure_password: "your-password"
```

> **Tip:** Use a dedicated second Zendure account that only shares the CT device. Logging in anywhere else with that account rotates the token — with auto-login the integration simply logs back in on the next poll, but a shared-only account keeps things clean. This matters more now that polling runs every few seconds: if the phone app uses the same account, each app login invalidates HA's token and forces a re-login on the very next poll, so keep the HA account separate.

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

The token (format `userId:UUID`) is the `accessToken` returned by the Zendure login. This mode does **not** refresh automatically — when the token expires the sensor goes `unavailable` until you update it. Prefer the username/password mode above, which obtains and renews this token for you.

### Finding your device ID

Use the helper script with the same credentials you put in `secrets.yaml`. It logs in and lists every device on the account with its numeric ID:

```bash
pip install cryptography
python3 tools/get_device_id.py your-account@example.com
```

You are prompted for the password (not stored). Example output:

```
Found 1 device(s):

  device_id: 12345
      name:  Monitor CT  (Satellite Monitor CT)
      serial:XXXXXXXXXXXXXXX
```

Put that `device_id` value into your configuration. If the list is empty, the Satellite CT has not been shared to this account yet (see [Prerequisites](#prerequisites)).

## Options

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `username` | yes* | — | Zendure account e-mail (enables auto-login) |
| `password` | yes* | — | Zendure account password |
| `bearer_token` | yes* | — | Static blade-auth token (`userId:UUID`); alternative to username/password |
| `device_id` | yes | — | Numeric device ID of the Satellite CT |
| `name` | no | `Satellite CT Power` | Entity friendly name |
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

## Troubleshooting

Check **Settings → System → Logs** in Home Assistant (filter for `zendure_satellite_ct`) — the integration logs the concrete reason there.

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Sensor `unavailable` right after setup | Wrong credentials, or `device_id` not on this account | Run `tools/get_device_id.py` to confirm both the login and the ID. |
| Log: `Zendure-Login fehlgeschlagen: Account does not exist or the password is incorrect` | Wrong `username`/`password` | Check `secrets.yaml`. Note the login uses the Zendure **account** credentials, not an app token. |
| Log: `Zendure API still rejects the token after re-login` | Token valid but device not visible to this account | Share the Satellite CT to this account (see [Prerequisites](#prerequisites)). |
| `get_device_id.py` prints *No devices found* | CT not shared to this account | Share the device from your primary account in the Zendure app. |
| Sensor worked, then went `unavailable` and recovered | Normal — you logged in elsewhere with this account, the token rotated | The integration re-logs in automatically on the next poll. Keep this account HA-only. |
| Sensor shows a stale value | Expected — the cloud API only publishes a new measurement once per minute | Nothing to fix; see *Features*. |
| Log mentions `cryptography` import error | Very unusual (it ships with Home Assistant) | Restart HA; if it persists, report an issue. |
| Login fails only after a Zendure app update, for everyone | Zendure rotated the signing key or enforces a newer version | Update `app_version`, or re-extract the RSA public key (see above). |

When reporting an issue, include the log line (it never contains your password) and your Home Assistant version.

## Disclaimer

This is an **unofficial**, community-built integration. It is **not affiliated with, endorsed by, or supported by Zendure**. "Zendure" and "Satellite CT" are trademarks of their respective owners and are used here only to describe compatibility.

The integration authenticates against Zendure's app API using credentials **you** own, to read data from **your own** device — i.e. for interoperability with your own hardware. Accessing the API this way may nevertheless be contrary to Zendure's terms of service; the most likely consequence of any objection would be the affected account being limited, which is why a dedicated, HA-only second account is recommended.

Official developer-API support for the Satellite CT was requested in Zendure's repository ([Zendure-HA#1447](https://github.com/Zendure/Zendure-HA/issues/1447)); the request remained unanswered and the issue was closed. As the Satellite CT is not covered by Zendure's official developer API, this interoperability integration is currently the only way to read its data in Home Assistant.

Use at your own risk. The authors provide this software "as is", without warranty, and accept no liability for account restrictions, data issues, or any other consequences of its use (see [LICENSE](LICENSE)).
