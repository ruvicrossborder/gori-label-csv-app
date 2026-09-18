import os
import io
import secrets
import traceback

import requests
from fastapi import FastAPI, UploadFile, File, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pypdf import PdfWriter, PdfReader

import gori_client
from csv_fixer import parse_and_fix

APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")
APP_USERNAME = os.environ.get("APP_USERNAME", "vikaas")

GOFO_OVERRIDE_PARCEL = {"weight": 4.0, "length": 6.0, "width": 4.0, "height": 4.0}
FALLBACK_SERVICES = ["usps_ground_advantage", "ups_ground_saver", "fedex_home_delivery"]

app = FastAPI(title="Gori Label Batch Uploader")
security = HTTPBasic()


def check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = secrets.compare_digest(credentials.username, APP_USERNAME)
    correct_pass = secrets.compare_digest(credentials.password, APP_PASSWORD)
    if not (correct_user and correct_pass):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return True


PAGE_HEAD = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Gori Label Batch Uploader</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:900px;margin:40px auto;padding:0 20px;color:#1a1a1a;background:#fafafa}
h1{font-size:22px} h2{font-size:16px;margin-top:28px}
.card{background:#fff;border:1px solid #e5e5e5;border-radius:10px;padding:24px;margin-bottom:20px}
input[type=file]{margin:12px 0}
button{background:#111;color:#fff;border:none;padding:10px 18px;border-radius:6px;font-size:14px;cursor:pointer}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:10px}
th,td{border-bottom:1px solid #eee;padding:6px 8px;text-align:left;vertical-align:top}
.ok{color:#0a7a2f} .err{color:#b3261e} .fix{color:#946b00;font-size:12px}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px}
.badge.gofo{background:#e8f0fe;color:#1a56db}
.badge.other{background:#fdf2e0;color:#946b00}
</style></head><body>
"""

UPLOAD_FORM = PAGE_HEAD + """
<h1>Gori Label Batch Uploader</h1>
<div class="card">
<p>Upload a shipment CSV in the standard format. Rows are auto-corrected where possible
(units, state codes, zip formatting, phone formatting), routed to GOFO Ground first
(fixed 4oz / 6x4x4in), and fall back to the cheapest of USPS Ground Advantage,
UPS Ground Saver, or FedEx Home Delivery using the weight/dimensions from the sheet.</p>
<form action="/upload" method="post" enctype="multipart/form-data">
<input type="file" name="file" accept=".csv" required>
<br><button type="submit">Process &amp; create labels</button>
</form>
</div>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
def index(auth: bool = Depends(check_auth)):
    return UPLOAD_FORM


def pick_service_and_parcel(to_addr, from_addr, sheet_parcel):
    """Returns (service, parcel_dict, rate_amount, notes)."""
    notes = []
    try:
        gofo_rates = gori_client.get_rates(to_addr, from_addr, GOFO_OVERRIDE_PARCEL)
        gofo = next((r for r in gofo_rates if r.get("carrier") == "gofo" and r.get("service") == "gofo_ground" and "fees" in r), None)
    except Exception as e:
        gofo = None
        notes.append(f"GOFO rate check failed: {e}")

    if gofo:
        return "gofo_ground", GOFO_OVERRIDE_PARCEL, gofo["fees"]["amount"], notes

    notes.append("GOFO Ground unavailable, falling back to cheapest of USPS/UPS Ground Saver/FedEx")
    try:
        rates = gori_client.get_rates(to_addr, from_addr, sheet_parcel)
    except Exception as e:
        return None, sheet_parcel, None, notes + [f"Rate lookup failed: {e}"]

    candidates = [r for r in rates if r.get("service") in FALLBACK_SERVICES and "fees" in r]
    if not candidates:
        return None, sheet_parcel, None, notes + ["No fallback carrier available for this address/parcel"]
    best = min(candidates, key=lambda r: r["fees"]["amount"])
    return best["service"], sheet_parcel, best["fees"]["amount"], notes


@app.post("/upload", response_class=HTMLResponse)
async def upload(auth: bool = Depends(check_auth), file: UploadFile = File(...)):
    content = await file.read()
    try:
        rows = parse_and_fix(content)
    except Exception as e:
        return HTMLResponse(PAGE_HEAD + f"<div class='card'><p class='err'>Could not read CSV: {e}</p>"
                             f"<p><a href='/'>Back</a></p></div></body></html>", status_code=400)

    results = []
    label_urls = []
    for row in rows:
        r = {"row_number": row["row_number"], "fixes": row["fixes"], "reference": row["reference_1"]}
        if row["error"]:
            r["status"] = "error"
            r["message"] = row["error"]
            results.append(r)
            continue
        try:
            service, parcel, amount, notes = pick_service_and_parcel(
                row["to_address"], row["from_address"], row["sheet_parcel_oz_in"]
            )
            r["fixes"] = r["fixes"] + notes
            if not service:
                r["status"] = "error"
                r["message"] = "No carrier available (GOFO and all fallbacks failed)"
                results.append(r)
                continue
            shipment = gori_client.create_shipment(
                service=service,
                to_address=row["to_address"],
                from_address=row["from_address"],
                parcel=parcel,
                reference_1=row["reference_1"],
                reference_2=row["reference_2"],
            )
            label_url = shipment.get("label_url") or shipment.get("label_pdf_url") or shipment.get("label")
            tracking = shipment.get("tracking_code") or shipment.get("tracking_number")
            r["status"] = "ok"
            r["service"] = service
            r["cost"] = amount
            r["tracking"] = tracking
            r["label_url"] = label_url
            if label_url:
                label_urls.append(label_url)
            results.append(r)
        except Exception as e:
            r["status"] = "error"
            r["message"] = f"{e}"
            results.append(r)

    # Build the results HTML
    rows_html = []
    for r in results:
        fixes_html = "<br>".join(f"<span class='fix'>{f}</span>" for f in r.get("fixes", []))
        if r["status"] == "ok":
            badge = "gofo" if r.get("service") == "gofo_ground" else "other"
            rows_html.append(
                f"<tr><td>{r['row_number']}</td><td>{r.get('reference','')}</td>"
                f"<td class='ok'>OK</td><td><span class='badge {badge}'>{r.get('service')}</span></td>"
                f"<td>${r.get('cost')}</td><td>{r.get('tracking','')}</td><td>{fixes_html}</td></tr>"
            )
        else:
            rows_html.append(
                f"<tr><td>{r['row_number']}</td><td>{r.get('reference','')}</td>"
                f"<td class='err'>FAILED</td><td colspan='3'>{r.get('message','')}</td><td>{fixes_html}</td></tr>"
            )

    download_link = ""
    if label_urls:
        download_link = "<p><a href='/download-pdf' target='_blank'><button type='button'>Download combined PDF (opens after this page loads)</button></a></p>"

    app.state.last_label_urls = label_urls

    html = PAGE_HEAD + f"""
    <h1>Results</h1>
    <div class="card">
    <p>{len(label_urls)} of {len(results)} rows produced a label.</p>
    {download_link}
    <table>
    <tr><th>Row</th><th>Ref</th><th>Status</th><th>Carrier</th><th>Cost</th><th>Tracking</th><th>Notes / auto-fixes</th></tr>
    {''.join(rows_html)}
    </table>
    </div>
    <p><a href="/">Upload another file</a></p>
    </body></html>
    """
    return HTMLResponse(html)


@app.get("/download-pdf")
def download_pdf(auth: bool = Depends(check_auth)):
    label_urls = getattr(app.state, "last_label_urls", [])
    if not label_urls:
        raise HTTPException(status_code=404, detail="No labels to download yet")
    writer = PdfWriter()
    for url in label_urls:
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            reader = PdfReader(io.BytesIO(resp.content))
            for page in reader.pages:
                writer.add_page(page)
        except Exception:
            traceback.print_exc()
            continue
    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/pdf", headers={
        "Content-Disposition": "attachment; filename=labels.pdf"
    })


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/debug-rates")
def debug_rates(auth: bool = Depends(check_auth)):
    """Temporary diagnostic: confirm gori_client.get_rates (via the gori-mcp
    JSON-RPC proxy) returns real rates end-to-end."""
    to_address = {"street1": "316 Embrey Mill Rd", "city": "Stafford", "state": "VA", "zip": "22554-2577", "country": "US", "first_name": "Test", "last_name": "Test"}
    from_address = {"street1": "2550 Southwell Rd", "city": "Dallas", "state": "TX", "zip": "75229", "country": "US", "company": "SHIPPING DEPT"}
    parcel = {"length": 6, "width": 4, "height": 4, "weight": 4}
    try:
        rates = gori_client.get_rates(to_address, from_address, parcel)
        gofo = next((r for r in rates if r.get("carrier") == "gofo" and r.get("service") == "gofo_ground" and "fees" in r), None)
        return {"ok": True, "num_rates": len(rates), "gofo_rate": gofo, "all_rates": rates}
    except Exception as e:
        return {"ok": False, "error": str(e)}
