#!/usr/bin/env python3
"""Find your Zendure device ID(s) using only your account credentials.

No mitmproxy, no emulator. Logs in to the Zendure cloud (EU region) exactly
like the Home Assistant integration does and prints every device on the
account, including the numeric `id` you put into `device_id:`.

Usage:
    pip install cryptography
    python3 get_device_id.py your-account@example.com

You will be prompted for the password (it is not echoed and never stored).
Use the SECOND, dedicated account that the Satellite CT is shared to.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import sys
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_der_public_key

BASE = "https://app.zendure.tech/eu/"
APP_VERSION = "5.8.3"
DEVICE_LIST_PATH = "productModule/device/queryDeviceListByConsumerId"
LOGIN_PATH = "auth/app/token"

# Same RSA public key the Zendure app uses to build the `sign` header.
PUBKEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCwoqArnVK5cuSHL3t9uf3d1CGlmcaFE/NT"
    "+t84RuMfLZzfuZbmElP3XmyywvTkNUlBdr6Za2tjVKQqKo5AVV1cxVpa+GLrLpSuH4U/zeLz"
    "eKLpNy9k+B9FoLXBDT4LaJoIcj9VLhlZWMTLxVwXU5NhI/lxWj7cK337dux7NbKltwIDAQAB"
)
_PUB = load_der_public_key(base64.b64decode(PUBKEY_B64))


def _headers(path: str, token: str | None = None) -> dict:
    ts = int(time.time())
    plain = f"{APP_VERSION}./{path}/{ts}".encode()
    sign = base64.b64encode(_PUB.encrypt(plain, padding.PKCS1v15())).decode()
    headers = {
        "Content-Type": "application/json",
        "appVersion": APP_VERSION,
        "timestamps": str(ts),
        "sign": sign,
        "style": "1",
        "Accept-Language": "en",
    }
    if token:
        headers["blade-auth"] = f"bearer {token}"
    return headers


def _post(path: str, body: dict, token: str | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), method="POST"
    )
    for key, value in _headers(path, token).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return json.loads(err.read())


def main() -> int:
    parser = argparse.ArgumentParser(description="List Zendure device IDs.")
    parser.add_argument("account", help="Zendure account e-mail (second account)")
    args = parser.parse_args()
    password = getpass.getpass("Zendure password: ")

    login = _post(
        LOGIN_PATH,
        {
            "grantType": "password",
            "tenantId": "",
            "account": args.account,
            "password": password,
            "appType": "Android",
        },
    )
    if not login.get("success"):
        print(f"Login failed: {login.get('msg')} (code {login.get('code')})")
        return 1
    token = login["data"]["accessToken"]

    result = _post(DEVICE_LIST_PATH, {}, token=token)
    if not result.get("success"):
        print(f"Device list failed: {result.get('msg')} (code {result.get('code')})")
        return 1

    devices = result.get("data") or []
    if not devices:
        print(
            "No devices found. Make sure the Satellite CT is shared to this account."
        )
        return 1

    print(f"\nFound {len(devices)} device(s):\n")
    for dev in devices:
        print(f"  device_id: {dev.get('id')}")
        print(f"      name:  {dev.get('name')}  ({dev.get('productName')})")
        print(f"      serial:{dev.get('snNumber')}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
