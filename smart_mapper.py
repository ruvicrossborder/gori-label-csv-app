"""When an uploaded CSV/Excel file doesn't use the app's expected column
headers, this asks an LLM (via the user's OpenAI API key) to figure out which
of the file's actual columns correspond to which shipping fields, so odd or
relabeled sheets ("Ship To Name", "Buyer Zip", "Recipient Address Line 1", ...)
still work without the user having to reformat anything by hand."""

import json
import os

import requests

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# The canonical column names parse_and_fix() understands, grouped for the prompt.
CANONICAL_FIELDS = {
    "To Company*": "Recipient company name (optional)",
    "To First Name*": "Recipient first name",
    "To Last Name": "Recipient last name",
    "To Phone Number": "Recipient phone number",
    "To Email Address": "Recipient email address",
    "To Street 1*": "Recipient street address line 1 (required)",
    "To Street 2": "Recipient street address line 2 / apt / suite",
    "To City*": "Recipient city (required)",
    "To State*": "Recipient state (required)",
    "To Zip*": "Recipient ZIP/postal code (required)",
    "To Country*": "Recipient country (default US if absent)",
    "Residential": "Whether the To address is residential (yes/no)",
    "From Company*": "Sender/shipper company name",
    "From First Name*": "Sender first name",
    "From Last Name": "Sender last name",
    "From Phone Number": "Sender phone number",
    "From Email Address": "Sender email address",
    "From Street 1*": "Sender street address line 1",
    "From Street 2": "Sender street address line 2",
    "From City*": "Sender city",
    "From State*": "Sender state",
    "From Zip*": "Sender ZIP/postal code",
    "From Country*": "Sender country",
    "Weight*": "Package weight (numeric)",
    "Weight Unit*": "Unit for the weight (oz/lb/kg/g)",
    "Length*": "Package length (numeric)",
    "Width*": "Package width (numeric)",
    "Height*": "Package height (numeric)",
    "Dim Unit*": "Unit for length/width/height (in/cm/mm)",
    "Reference 1": "Order number / reference / SKU for this shipment",
    "Reference 2": "A second reference field, if present",
}

# A minimal set of headers that, if matched exactly (case/asterisk-insensitive),
# mean the file is already in the app's own template - no AI mapping needed.
_ANCHOR_FIELDS = ["to street 1", "to city", "to state", "to zip"]


def _normalize(h):
    return (h or "").strip().lower().rstrip("*").strip()


def needs_smart_mapping(headers):
    """True when the file's headers don't look like the app's own template,
    so we should try to have an LLM map them instead."""
    normalized = {_normalize(h) for h in headers}
    matches = sum(1 for anchor in _ANCHOR_FIELDS if anchor in normalized)
    return matches < 3  # allow one to be slightly off (e.g. a merged "To Address" column)


def map_headers_with_ai(headers, sample_rows, api_key=None):
    """Asks the LLM which source header (if any) corresponds to each canonical
    field. sample_rows: a few raw row dicts (original headers -> values), used
    as context so the model can see the actual data, not just header names.
    Returns {canonical_field: source_header_or_None}.
    Raises RuntimeError if the API key is missing or the call/parse fails."""
    api_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    field_list = "\n".join(f"- \"{k}\": {v}" for k, v in CANONICAL_FIELDS.items())
    sample_preview = json.dumps(sample_rows[:3], indent=2, default=str)

    prompt = f"""You are mapping a shipping spreadsheet's columns onto a fixed set of fields.

The spreadsheet's actual column headers are:
{json.dumps(headers)}

Here are up to 3 sample rows from the sheet (original headers -> values), for context:
{sample_preview}

Map each of the following canonical fields to the ONE spreadsheet header that holds that \
data, or null if no column in the sheet holds it. A single spreadsheet column may combine \
multiple pieces of data (e.g. one "Ship To" column with name+address+city+state+zip all \
together, or a full name column instead of separate first/last) - in that case map the \
canonical field that best fits to that same header, since it will be parsed further. \
Every mapped value must be an exact header string from the list above (or null).

Canonical fields:
{field_list}

Respond with ONLY a JSON object, no prose, no markdown fences: {{"To Street 1*": "...", ...}} \
covering every canonical field key listed above."""

    resp = requests.post(
        OPENAI_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": "You output only valid JSON, nothing else."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        timeout=45,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    mapping = json.loads(text)

    # Keep only recognized canonical keys, and only values that are real headers.
    header_set = set(headers)
    cleaned = {}
    for k in CANONICAL_FIELDS:
        v = mapping.get(k)
        cleaned[k] = v if (isinstance(v, str) and v in header_set) else None
    return cleaned


def apply_mapping(raw_row, mapping):
    """Rebuilds a row dict keyed by canonical field names, pulling values from
    the raw row via the AI-produced mapping. A canonical field with no mapped
    source header is simply absent (same as a blank cell)."""
    out = {}
    for canonical, source_header in mapping.items():
        if source_header:
            out[canonical] = raw_row.get(source_header, "")
    return out
