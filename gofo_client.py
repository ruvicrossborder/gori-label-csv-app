import os
import base64
import time
import requests

# Direct integration with Vikaas's own GOFO Express account (NAVI AGI LLC),
# separate from the gori-mcp aggregator. Used specifically for the fast
# coverage-check endpoint (verifyDelivery), which gori-mcp does not expose -
# gori-mcp only wraps full rate-shopping (get_rates) and booking
# (create_shipment). USPS pricing/booking continues to go through gori-mcp
# as before; this module is GOFO-direct only.
GOFO_API_BASE = os.environ.get("GOFO_API_BASE", "https://dmsapi.gofoexpress.com")
GOFO_API_USERNAME = os.environ.get("GOFO_API_USERNAME", "")
GOFO_API_PASSWORD = os.environ.get("GOFO_API_PASSWORD", "")
GOFO_ENTRY_PORT = os.environ.get("GOFO_ENTRY_PORT", "DFW")


def _auth_header():
    raw = f"{GOFO_API_USERNAME}:{GOFO_API_PASSWORD}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def verify_delivery(country, state, city, postal_code, area=None, street=None, timeout=10):
    """Calls GOFO's dedicated /open-api/v2/order/verifyDelivery endpoint - a
    lightweight coverage check, NOT a full rate quote. Returns a dict:
      {"covered": bool, "po_box_excluded": bool, "code": int, "message": str,
       "elapsed_seconds": float, "raw": <parsed response or None>,
       "error": <str or None>}

    Response codes per GOFO's docs:
      200 - Post code covered
      301 - Abnormal parameters
      501 - Post code does not support
      502 - Post code covered, but address is a PO Box - not supported
    """
    payload = {
        "orderConsignee": {
            "consigneeCountry": country,
            "consigneeState": state,
            "consigneeCity": city,
            "consigneeCode": postal_code,
        }
    }
    if area:
        payload["orderConsignee"]["consigneeArea"] = area
    if street:
        payload["orderConsignee"]["consigneeStreet"] = street

    start = time.monotonic()
    try:
        resp = requests.post(
            f"{GOFO_API_BASE}/open-api/v2/order/verifyDelivery",
            json=payload,
            headers={
                "Authorization": _auth_header(),
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        elapsed = time.monotonic() - start
        try:
            data = resp.json()
        except ValueError:
            return {
                "covered": False, "po_box_excluded": False, "code": resp.status_code,
                "message": resp.text[:300], "elapsed_seconds": elapsed, "raw": None,
                "error": f"Non-JSON response (HTTP {resp.status_code})",
            }

        code = data.get("code")
        msg = data.get("msgEn") or data.get("msg") or ""
        return {
            "covered": code == 200,
            "po_box_excluded": code == 502,
            "code": code,
            "message": msg,
            "elapsed_seconds": elapsed,
            "raw": data,
            "error": None,
        }
    except Exception as e:
        elapsed = time.monotonic() - start
        return {
            "covered": False, "po_box_excluded": False, "code": None,
            "message": "", "elapsed_seconds": elapsed, "raw": None, "error": str(e),
        }
