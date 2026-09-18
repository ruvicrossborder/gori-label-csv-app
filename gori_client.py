import os
import time
import threading
import requests

GORI_BASE_URL = os.environ.get("GORI_BASE_URL", "https://api.goricompany.com/v2").rstrip("/")
GORI_AUTH_URL = os.environ.get("GORI_AUTH_URL", "https://api.goricompany.com/auth/token")
GORI_CLIENT_ID = os.environ.get("GORI_CLIENT_ID", "")
GORI_CLIENT_SECRET = os.environ.get("GORI_CLIENT_SECRET", "")

_lock = threading.Lock()
_token_cache = {"token": None, "expires_at": 0}


def _get_token():
    with _lock:
        if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 60:
            return _token_cache["token"]
        resp = requests.post(
            GORI_AUTH_URL,
            json={
                "client_id": GORI_CLIENT_ID,
                "client_secret": GORI_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token") or data.get("token")
        expires_in = data.get("expires_in", 12 * 3600)
        _token_cache["token"] = token
        _token_cache["expires_at"] = time.time() + int(expires_in)
        return token


def _headers():
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
    }


def _request(method, path, json_body=None, params=None):
    url = f"{GORI_BASE_URL}{path}"
    resp = requests.request(method, url, headers=_headers(), json=json_body, params=params, timeout=60)
    if resp.status_code == 401:
        # token may have been invalidated server-side; force refresh once
        with _lock:
            _token_cache["token"] = None
        resp = requests.request(method, url, headers=_headers(), json=json_body, params=params, timeout=60)
    return resp


def get_rates(to_address, from_address, parcel):
    resp = _request("POST", "/rates", json_body={
        "to_address": to_address,
        "from_address": from_address,
        "parcel": parcel,
    })
    resp.raise_for_status()
    return resp.json()


def create_shipment(service, to_address, from_address, parcel, reference_1=None, reference_2=None):
    body = {
        "service": service,
        "to_address": to_address,
        "from_address": from_address,
        "parcel": parcel,
    }
    if reference_1:
        body["reference_1"] = reference_1
    if reference_2:
        body["reference_2"] = reference_2
    resp = _request("POST", "/shipments", json_body=body)
    resp.raise_for_status()
    return resp.json()
