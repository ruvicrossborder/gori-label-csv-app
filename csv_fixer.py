"""Parses the uploaded shipment CSV, auto-fixes common mistakes, and returns
normalized rows ready for rate/label calls, along with a per-row list of the
fixes that were applied (so we can show the user what changed)."""

import csv
import io
import re

STATE_NAME_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}

VALID_STATES = set(STATE_NAME_TO_ABBR.values())

KG_TO_OZ = 35.27396
LB_TO_OZ = 16.0
G_TO_OZ = 0.03527396
CM_TO_IN = 0.393701
MM_TO_IN = 0.0393701


def _clean(v):
    return (v or "").strip()


def _fix_state(raw, fixes):
    s = _clean(raw)
    if not s:
        return s
    up = s.upper()
    if up in VALID_STATES:
        if up != s:
            fixes.append(f"State '{s}' -> '{up}'")
        return up
    mapped = STATE_NAME_TO_ABBR.get(s.lower())
    if mapped:
        fixes.append(f"State '{s}' -> '{mapped}'")
        return mapped
    fixes.append(f"State '{s}' left as-is (unrecognized)")
    return up


def _fix_zip(raw, fixes):
    s = _clean(raw)
    digits = re.sub(r"[^0-9]", "", s)
    if len(digits) >= 9:
        fixed = f"{digits[:5]}-{digits[5:9]}"
    elif len(digits) == 5:
        fixed = digits
    elif 0 < len(digits) < 5:
        fixed = digits.zfill(5)
        fixes.append(f"Zip '{s}' padded to '{fixed}'")
        return fixed
    else:
        return s
    if fixed != s:
        fixes.append(f"Zip '{s}' -> '{fixed}'")
    return fixed


def _fix_phone(raw, fixes):
    s = _clean(raw)
    if not s:
        return s
    digits = re.sub(r"[^0-9]", "", s)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        fixes.append(f"Phone '{s}' looks invalid, left as-is")
        return s
    fixed = digits
    if fixed != s:
        fixes.append(f"Phone '{s}' normalized")
    return fixed


def _to_oz(value, unit, fixes, label):
    unit = (unit or "").strip().lower()
    try:
        val = float(value)
    except (TypeError, ValueError):
        fixes.append(f"{label} weight '{value}' invalid, defaulted to 1")
        return 1.0
    if unit in ("oz", "ounce", "ounces", ""):
        return val
    if unit in ("kg", "kgs", "kilogram", "kilograms"):
        oz = val * KG_TO_OZ
        fixes.append(f"{label} weight {val}kg -> {round(oz, 2)}oz")
        return oz
    if unit in ("lb", "lbs", "pound", "pounds"):
        oz = val * LB_TO_OZ
        fixes.append(f"{label} weight {val}lb -> {round(oz, 2)}oz")
        return oz
    if unit in ("g", "gram", "grams"):
        oz = val * G_TO_OZ
        fixes.append(f"{label} weight {val}g -> {round(oz, 2)}oz")
        return oz
    fixes.append(f"Unknown weight unit '{unit}', assumed oz")
    return val


def _to_in(value, unit, fixes, label):
    unit = (unit or "").strip().lower()
    try:
        val = float(value)
    except (TypeError, ValueError):
        fixes.append(f"{label} dimension '{value}' invalid, defaulted to 6")
        return 6.0
    if unit in ("in", "inch", "inches", ""):
        return val
    if unit in ("cm", "centimeter", "centimeters"):
        inch = val * CM_TO_IN
        fixes.append(f"{label} {val}cm -> {round(inch, 2)}in")
        return inch
    if unit in ("mm", "millimeter", "millimeters"):
        inch = val * MM_TO_IN
        fixes.append(f"{label} {val}mm -> {round(inch, 2)}in")
        return inch
    fixes.append(f"Unknown dimension unit '{unit}', assumed inches")
    return val


def parse_and_fix(file_bytes):
    """Returns a list of dicts: {row_number, fixes, to_address, from_address,
    parcel_oz_in, reference_1, error}"""
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for i, raw_row in enumerate(reader, start=2):  # header is row 1
        fixes = []
        error = None
        row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in raw_row.items() if k}

        weight_unit = row.get("Weight Unit*", "oz")
        dim_unit = row.get("Dim Unit*", "in")

        weight_oz = _to_oz(row.get("Weight*"), weight_unit, fixes, "Package")
        length_in = _to_in(row.get("Length*"), dim_unit, fixes, "Length")
        width_in = _to_in(row.get("Width*"), dim_unit, fixes, "Width")
        height_in = _to_in(row.get("Height*"), dim_unit, fixes, "Height")

        to_company = _clean(row.get("To Company*"))
        to_first = _clean(row.get("To First Name*"))
        to_last = _clean(row.get("To Last Name"))
        if not to_company and not to_first and not to_last:
            error = "Missing recipient name/company entirely"

        to_state = _fix_state(row.get("To State*"), fixes)
        to_zip = _fix_zip(row.get("To Zip*"), fixes)
        to_country = _clean(row.get("To Country*")) or "US"
        if to_country != (row.get("To Country*") or ""):
            if not row.get("To Country*"):
                fixes.append("To Country defaulted to US")

        to_residential_raw = _clean(row.get("Residential"))
        to_residential = True
        if to_residential_raw.lower() in ("no", "n", "false", "0", "commercial"):
            to_residential = False

        from_company = _clean(row.get("From Company*")) or "SHIPPING DEPT"
        from_first = _clean(row.get("From First Name*"))
        from_last = _clean(row.get("From Last Name"))
        from_state = _fix_state(row.get("From State*"), fixes)
        from_zip = _fix_zip(row.get("From Zip*"), fixes)
        from_country = _clean(row.get("From Country*")) or "US"

        to_address = {
            "company": to_company,
            "first_name": to_first,
            "last_name": to_last,
            "phone": _fix_phone(row.get("To Phone Number"), fixes),
            "email": _clean(row.get("To Email Address")),
            "street1": _clean(row.get("To Street 1*")),
            "street2": _clean(row.get("To Street 2")),
            "city": _clean(row.get("To City*")),
            "state": to_state,
            "zip": to_zip,
            "country": to_country,
            "is_residential": to_residential,
        }
        from_address = {
            "company": from_company,
            "first_name": from_first,
            "last_name": from_last,
            "phone": _fix_phone(row.get("From Phone Number"), fixes),
            "email": _clean(row.get("From Email Address")),
            "street1": _clean(row.get("From Street 1*")),
            "street2": _clean(row.get("From Street 2")),
            "city": _clean(row.get("From City*")),
            "state": from_state,
            "zip": from_zip,
            "country": from_country,
        }

        if not to_address["street1"] or not to_address["city"] or not to_state or not to_zip:
            error = error or "Missing required To address field (street/city/state/zip)"

        rows.append({
            "row_number": i,
            "fixes": fixes,
            "error": error,
            "to_address": {k: v for k, v in to_address.items() if v not in (None, "")},
            "from_address": {k: v for k, v in from_address.items() if v not in (None, "")},
            "sheet_parcel_oz_in": {
                "weight": round(weight_oz, 3),
                "length": round(length_in, 3),
                "width": round(width_in, 3),
                "height": round(height_in, 3),
            },
            "reference_1": _clean(row.get("Reference 1")),
            "reference_2": _clean(row.get("Reference 2")),
        })
    return rows
