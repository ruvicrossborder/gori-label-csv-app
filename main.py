import os
import io
import uuid
import secrets
import datetime

from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import gori_client
from csv_fixer import parse_and_fix
from pdf_builder import build_batch_pdf

APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")
APP_USERNAME = os.environ.get("APP_USERNAME", "vikaas")

GOFO_OVERRIDE_PARCEL = {"weight": 4.0, "length": 6.0, "width": 4.0, "height": 4.0}
FALLBACK_SERVICES = ["usps_ground_advantage", "ups_ground_saver", "fedex_home_delivery"]

app = FastAPI(title="Gori Label Batch Uploader")
security = HTTPBasic()

# In-memory stores (single-process app). Keyed by short-lived tokens.
_BATCHES = {}   # token -> {"rows": [...]}
_PDFS = {}      # token -> {"bytes": b"...", "filename": "..."}


def check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = secrets.compare_digest(credentials.username, APP_USERNAME)
    correct_pass = secrets.compare_digest(credentials.password, APP_PASSWORD)
    if not (correct_user and correct_pass):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return True


# ---------------------------------------------------------------------------
# Design: gray/white, minimal, fluid. No external fonts/JS — fast to load.
# ---------------------------------------------------------------------------
PAGE_HEAD = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Gori Label Batch Uploader</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{
  --bg:#f5f5f5; --surface:#ffffff; --border:#e4e4e4; --border-soft:#eeeeee;
  --ink:#1c1c1c; --ink-soft:#6b6b6b; --ink-faint:#9a9a9a;
  --accent:#1c1c1c; --accent-ink:#ffffff;
  --ok-bg:#f0f4f1; --ok-ink:#2f6b3e;
  --err-bg:#fbeeee; --err-ink:#a03d3d;
  --gofo-bg:#eef0f2; --gofo-ink:#3a3f47;
  --radius:14px; --radius-sm:9px;
  --shadow:0 1px 2px rgba(0,0,0,.04), 0 8px 24px rgba(0,0,0,.05);
}
*{box-sizing:border-box}
html{-webkit-font-smoothing:antialiased}
body{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  max-width:960px;margin:0 auto;padding:48px 20px 80px;color:var(--ink);background:var(--bg);
  line-height:1.5;
}
h1{font-size:21px;font-weight:600;letter-spacing:-.01em;margin:0 0 4px}
h2{font-size:14px;font-weight:600;color:var(--ink-soft);text-transform:uppercase;letter-spacing:.04em;margin:0 0 12px}
.subtitle{color:var(--ink-soft);font-size:14px;margin:0 0 28px}
.card{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:28px;margin-bottom:20px;box-shadow:var(--shadow);
  transition:box-shadow .2s ease;
}
.card + .card{margin-top:16px}
.row-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin:18px 0}
.stat{background:var(--bg);border:1px solid var(--border-soft);border-radius:var(--radius-sm);padding:16px}
.stat .label{font-size:12px;color:var(--ink-faint);text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px}
.stat .value{font-size:22px;font-weight:600;letter-spacing:-.01em}
.stat.highlight{background:var(--ink);color:#fff}
.stat.highlight .label{color:#cfcfcf}
input[type=file]{
  width:100%;padding:14px;border:1.5px dashed var(--border);border-radius:var(--radius-sm);
  background:var(--bg);font-size:13px;color:var(--ink-soft);cursor:pointer;
  transition:border-color .15s ease,background .15s ease;
}
input[type=file]:hover{border-color:var(--ink-faint);background:#f0f0f0}
button,.btn{
  appearance:none;background:var(--accent);color:var(--accent-ink);border:none;
  padding:11px 20px;border-radius:999px;font-size:13.5px;font-weight:600;cursor:pointer;
  transition:transform .12s ease,opacity .12s ease,box-shadow .12s ease;
  display:inline-flex;align-items:center;gap:8px;text-decoration:none;
}
button:hover,.btn:hover{opacity:.88;box-shadow:0 4px 14px rgba(0,0,0,.14)}
button:active,.btn:active{transform:scale(.97)}
button:disabled{opacity:.5;cursor:not-allowed}
.btn-secondary{background:var(--surface);color:var(--ink);border:1px solid var(--border)}
.btn-secondary:hover{background:var(--bg);box-shadow:none}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:4px}
th{
  text-align:left;padding:9px 10px;color:var(--ink-faint);font-weight:600;
  font-size:11px;text-transform:uppercase;letter-spacing:.03em;border-bottom:1.5px solid var(--border);
}
td{padding:10px;border-bottom:1px solid var(--border-soft);vertical-align:top}
tr:last-child td{border-bottom:none}
.status-ok{color:var(--ok-ink);font-weight:600}
.status-err{color:var(--err-ink);font-weight:600}
.fix{color:var(--ink-faint);font-size:11.5px;display:block;margin-top:2px}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:11.5px;font-weight:600}
.badge.gofo{background:#eaf1ea;color:#2f6b3e}
.badge.other{background:var(--gofo-bg);color:var(--gofo-ink)}
a{color:var(--ink)}
.muted{color:var(--ink-soft);font-size:13px}
.top-nav{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px}
.top-nav a{font-size:13px;color:var(--ink-soft);text-decoration:none}
.top-nav a:hover{color:var(--ink)}
.spinner{
  width:14px;height:14px;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;
  border-radius:50%;display:inline-block;animation:spin .7s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg)}}
.fade-in{animation:fadeIn .25s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
</style></head><body>
"""

UPLOAD_FORM = PAGE_HEAD + """
<div class="fade-in">
<h1>Gori Label Batch Uploader</h1>
<p class="subtitle">Upload a shipment CSV. Rows are auto-corrected (units, state codes, zip/phone
formatting), then priced through GOFO Ground first and the cheapest fallback carrier when needed.</p>
<div class="card">
<form id="uploadForm" action="/preview" method="post" enctype="multipart/form-data">
<input type="file" name="file" accept=".csv" required>
<br><br>
<button type="submit" id="submitBtn">Preview rates &amp; savings</button>
</form>
</div>
</div>
<script>
document.getElementById('uploadForm').addEventListener('submit', function(){
  var btn = document.getElementById('submitBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Calculating rates…';
});
</script>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
def index(auth: bool = Depends(check_auth)):
    return UPLOAD_FORM


# ---------------------------------------------------------------------------
# Rate / carrier selection
# ---------------------------------------------------------------------------
def cheapest_rate(rates):
    candidates = [r for r in rates if "fees" in r]
    if not candidates:
        return None
    return min(candidates, key=lambda r: r["fees"]["amount"])


def pick_fallback(to_addr, from_addr, sheet_parcel, notes):
    """Cheapest of the fallback carriers using the sheet's real weight/dims."""
    try:
        rates = gori_client.get_rates(to_addr, from_addr, sheet_parcel)
    except Exception as e:
        return None, sheet_parcel, None, notes + [f"Rate lookup failed: {e}"]

    candidates = [r for r in rates if r.get("service") in FALLBACK_SERVICES and "fees" in r]
    if not candidates:
        return None, sheet_parcel, None, notes + ["No fallback carrier available for this address/parcel"]
    best = min(candidates, key=lambda r: r["fees"]["amount"])
    return best["service"], sheet_parcel, best["fees"]["amount"], notes


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
    return pick_fallback(to_addr, from_addr, sheet_parcel, notes)


# ---------------------------------------------------------------------------
# Step 1: preview rates & savings before anything is purchased
# ---------------------------------------------------------------------------
@app.post("/preview", response_class=HTMLResponse)
async def preview(auth: bool = Depends(check_auth), file: UploadFile = File(...)):
    content = await file.read()
    try:
        rows = parse_and_fix(content)
    except Exception as e:
        return HTMLResponse(PAGE_HEAD + f"<div class='card fade-in'><p class='status-err'>Could not read CSV: {e}</p>"
                             f"<p><a href='/'>&larr; Back</a></p></div></body></html>", status_code=400)

    token = uuid.uuid4().hex
    _BATCHES[token] = {"rows": rows}

    preview_rows = []
    total_original = 0.0
    total_gofo = 0.0
    priced_count = 0

    for row in rows:
        if row["error"]:
            preview_rows.append({"row_number": row["row_number"], "reference": row["reference_1"],
                                  "error": row["error"]})
            continue
        try:
            orig_rates = gori_client.get_rates(row["to_address"], row["from_address"], row["sheet_parcel_oz_in"])
            orig_best = cheapest_rate(orig_rates)
        except Exception as e:
            orig_best = None

        try:
            gofo_rates = gori_client.get_rates(row["to_address"], row["from_address"], GOFO_OVERRIDE_PARCEL)
            gofo_best = next((r for r in gofo_rates if r.get("carrier") == "gofo" and r.get("service") == "gofo_ground" and "fees" in r), None)
        except Exception:
            gofo_best = None

        orig_amount = orig_best["fees"]["amount"] if orig_best else None
        gofo_amount = gofo_best["fees"]["amount"] if gofo_best else None

        if orig_amount is not None:
            total_original += orig_amount
        if gofo_amount is not None:
            total_gofo += gofo_amount
        elif orig_amount is not None:
            total_gofo += orig_amount  # GOFO unavailable, this row will fall back to the same price

        if orig_amount is not None or gofo_amount is not None:
            priced_count += 1

        preview_rows.append({
            "row_number": row["row_number"],
            "reference": row["reference_1"],
            "orig_service": orig_best.get("service") if orig_best else None,
            "orig_amount": orig_amount,
            "gofo_amount": gofo_amount,
            "error": None,
        })

    savings = total_original - total_gofo

    rows_html = []
    for pr in preview_rows:
        if pr.get("error"):
            rows_html.append(
                f"<tr><td>{pr['row_number']}</td><td>{pr.get('reference','')}</td>"
                f"<td class='status-err' colspan='3'>{pr['error']}</td></tr>"
            )
            continue
        orig_txt = f"${pr['orig_amount']:.2f} ({pr.get('orig_service') or '?'})" if pr['orig_amount'] is not None else "—"
        gofo_txt = f"${pr['gofo_amount']:.2f}" if pr['gofo_amount'] is not None else "unavailable, uses fallback"
        row_savings = (pr['orig_amount'] - pr['gofo_amount']) if (pr['orig_amount'] is not None and pr['gofo_amount'] is not None) else None
        savings_txt = f"${row_savings:.2f}" if row_savings is not None else "—"
        rows_html.append(
            f"<tr><td>{pr['row_number']}</td><td>{pr.get('reference','')}</td>"
            f"<td>{orig_txt}</td><td>{gofo_txt}</td><td>{savings_txt}</td></tr>"
        )

    html = PAGE_HEAD + f"""
    <div class="fade-in">
    <div class="top-nav"><h1>Rate Preview</h1><a href="/">&larr; Upload a different file</a></div>
    <p class="subtitle">Estimated cost across {priced_count} priceable rows, before any label is purchased.</p>

    <div class="card">
    <div class="row-grid">
      <div class="stat"><div class="label">At original weight/dims</div><div class="value">${total_original:.2f}</div></div>
      <div class="stat"><div class="label">Via GOFO Ground (4oz, 6x4x4)</div><div class="value">${total_gofo:.2f}</div></div>
      <div class="stat highlight"><div class="label">Estimated savings</div><div class="value">${savings:.2f}</div></div>
    </div>
    <form action="/process" method="post" id="processForm">
    <input type="hidden" name="token" value="{token}">
    <button type="submit" id="processBtn">Create all labels</button>
    <a class="btn btn-secondary" href="/">Cancel</a>
    </form>
    </div>

    <div class="card">
    <h2>Row-by-row</h2>
    <table>
    <tr><th>Row</th><th>Ref</th><th>Original (cheapest carrier)</th><th>GOFO Ground</th><th>Savings</th></tr>
    {''.join(rows_html)}
    </table>
    </div>
    </div>
    <script>
    document.getElementById('processForm').addEventListener('submit', function(){{
      var btn = document.getElementById('processBtn');
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span> Creating labels…';
    }});
    </script>
    </body></html>
    """
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Step 2: actually create the labels for a previewed batch
# ---------------------------------------------------------------------------
@app.post("/process", response_class=HTMLResponse)
async def process(auth: bool = Depends(check_auth), token: str = Form(...)):
    batch = _BATCHES.get(token)
    if not batch:
        return HTMLResponse(PAGE_HEAD + "<div class='card fade-in'><p class='status-err'>This preview has expired."
                             " Please upload the file again.</p><p><a href='/'>&larr; Back</a></p></div></body></html>",
                             status_code=404)
    rows = batch["rows"]

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

            try:
                shipment = gori_client.create_shipment(
                    service=service,
                    to_address=row["to_address"],
                    from_address=row["from_address"],
                    parcel=parcel,
                    reference_1=row["reference_1"],
                    reference_2=row["reference_2"],
                )
            except Exception as e:
                if service == "gofo_ground":
                    # GOFO sometimes quotes fine but fails at actual booking time
                    # (provider-side issue) - fall back to the next best carrier.
                    r["fixes"].append(f"GOFO Ground booking failed ({e}), falling back to cheapest of USPS/UPS Ground Saver/FedEx")
                    service, parcel, amount, fb_notes = pick_fallback(
                        row["to_address"], row["from_address"], row["sheet_parcel_oz_in"], []
                    )
                    r["fixes"] = r["fixes"] + fb_notes
                    if not service:
                        r["status"] = "error"
                        r["message"] = "GOFO booking failed and no fallback carrier available"
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
                else:
                    raise

            label_info = shipment.get("label") or {}
            label_url = (
                label_info.get("image_url") if isinstance(label_info, dict) else label_info
            ) or shipment.get("label_url") or shipment.get("label_pdf_url")
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

    _BATCHES.pop(token, None)

    pdf_link_html = ""
    if label_urls:
        pdf_bytes, filename = build_batch_pdf(results, label_urls)
        pdf_token = uuid.uuid4().hex
        _PDFS[pdf_token] = {"bytes": pdf_bytes, "filename": filename}
        pdf_link_html = (
            f"<p><a class='btn' href='/pdf/{pdf_token}' target='_blank'>Open combined PDF in a new tab</a>"
            f"<span class='muted' style='margin-left:10px'>{filename} &mdash; copy the tab's URL to share it</span></p>"
        )

    rows_html = []
    for r in results:
        fixes_html = "".join(f"<span class='fix'>{f}</span>" for f in r.get("fixes", []))
        if r["status"] == "ok":
            badge = "gofo" if r.get("service") == "gofo_ground" else "other"
            rows_html.append(
                f"<tr><td>{r['row_number']}</td><td>{r.get('reference','')}</td>"
                f"<td class='status-ok'>OK</td><td><span class='badge {badge}'>{r.get('service')}</span></td>"
                f"<td>${r.get('cost'):.2f}</td><td>{r.get('tracking','')}</td><td>{fixes_html}</td></tr>"
            )
        else:
            rows_html.append(
                f"<tr><td>{r['row_number']}</td><td>{r.get('reference','')}</td>"
                f"<td class='status-err'>FAILED</td><td colspan='3'>{r.get('message','')}</td><td>{fixes_html}</td></tr>"
            )

    html = PAGE_HEAD + f"""
    <div class="fade-in">
    <div class="top-nav"><h1>Results</h1><a href="/">&larr; Upload another file</a></div>
    <div class="card">
    <div class="row-grid">
      <div class="stat"><div class="label">Labels created</div><div class="value">{len(label_urls)} / {len(results)}</div></div>
    </div>
    {pdf_link_html}
    </div>
    <div class="card">
    <h2>Row-by-row</h2>
    <table>
    <tr><th>Row</th><th>Ref</th><th>Status</th><th>Carrier</th><th>Cost</th><th>Tracking</th><th>Notes / auto-fixes</th></tr>
    {''.join(rows_html)}
    </table>
    </div>
    </div>
    </body></html>
    """
    return HTMLResponse(html)


@app.get("/pdf/{pdf_token}")
def get_pdf(pdf_token: str, auth: bool = Depends(check_auth)):
    entry = _PDFS.get(pdf_token)
    if not entry:
        raise HTTPException(status_code=404, detail="This PDF link has expired")
    return StreamingResponse(
        io.BytesIO(entry["bytes"]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{entry["filename"]}"'},
    )


@app.get("/health")
def health():
    return {"status": "ok"}
