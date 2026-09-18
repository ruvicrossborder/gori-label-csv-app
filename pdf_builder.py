"""Builds the master summary cover page and merges it with the individual
carrier label PDFs into one combined, downloadable file."""

import io
import datetime

import requests
from pypdf import PdfWriter, PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas


def _draw_cover_page(c, results, batch_date):
    width, height = letter
    margin = 0.6 * inch
    y = height - margin

    c.setFont("Helvetica-Bold", 18)
    c.drawString(margin, y, "Shipment Batch Summary")
    y -= 26

    c.setFont("Helvetica", 10)
    c.drawString(margin, y, f"Batch date: {batch_date}")
    y -= 14
    ok_count = sum(1 for r in results if r["status"] == "ok")
    c.drawString(margin, y, f"Labels created: {ok_count} of {len(results)} rows")
    y -= 24

    c.setLineWidth(0.5)
    c.line(margin, y, width - margin, y)
    y -= 18

    # Table header (no pricing on the master/cover page, by design)
    col_x = [margin, margin + 0.45 * inch, margin + 1.8 * inch, margin + 4.3 * inch, margin + 6.4 * inch]
    headers = ["Row", "Ref", "Carrier / Service", "Tracking #", "Status"]
    c.setFont("Helvetica-Bold", 9)
    for x, h in zip(col_x, headers):
        c.drawString(x, y, h)
    y -= 6
    c.line(margin, y, width - margin, y)
    y -= 14

    c.setFont("Helvetica", 8.5)
    row_h = 13
    for r in results:
        if y < margin + 40:
            c.showPage()
            y = height - margin
            c.setFont("Helvetica-Bold", 9)
            for x, h in zip(col_x, headers):
                c.drawString(x, y, h)
            y -= 6
            c.line(margin, y, width - margin, y)
            y -= 14
            c.setFont("Helvetica", 8.5)

        row_num = str(r.get("row_number", ""))
        ref = str(r.get("reference", "") or "")[:16]
        if r["status"] == "ok":
            service = str(r.get("service", ""))[:32]
            tracking = str(r.get("tracking", "") or "")[:26]
            status = "OK"
        else:
            service = str(r.get("message", ""))[:32]
            tracking = ""
            status = "FAILED"

        values = [row_num, ref, service, tracking, status]
        for x, v in zip(col_x, values):
            c.drawString(x, y, v)
        y -= row_h

    c.showPage()


def build_batch_pdf(results, label_urls):
    """results: the per-row result dicts from the upload run.
    label_urls: ordered list of hosted carrier label PDF URLs (successful rows only).
    Returns (pdf_bytes, filename)."""
    batch_date = datetime.date.today().isoformat()

    cover_buf = io.BytesIO()
    c = canvas.Canvas(cover_buf, pagesize=letter)
    _draw_cover_page(c, results, batch_date)
    c.save()
    cover_buf.seek(0)

    writer = PdfWriter()
    cover_reader = PdfReader(cover_buf)
    for page in cover_reader.pages:
        writer.add_page(page)

    for url in label_urls:
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            reader = PdfReader(io.BytesIO(resp.content))
            for page in reader.pages:
                writer.add_page(page)
        except Exception:
            continue

    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)

    label_count = len(label_urls)
    filename = f"labels_{batch_date}_{label_count}-labels.pdf"
    return buf.getvalue(), filename
