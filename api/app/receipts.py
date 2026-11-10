from html import escape

from app import files, mail
from app.bookings import states
from app.config import settings
from app.payments import service as payments
from app.timeutil import fmt, rupees

CSS = """
body { font: 11pt/1.5 Helvetica, Arial, sans-serif; color: #1c1917; margin: 2.2cm; }
h1 { font-size: 20pt; margin: 0; } .muted { color: #78716c; }
table { width: 100%; border-collapse: collapse; margin-top: 18pt; }
th, td { text-align: left; padding: 6pt 0; border-bottom: 0.5pt solid #d6d3d1; }
td.num, th.num { text-align: right; } tr.total td { font-weight: bold; border-bottom: none; }
"""


def receipt_html(booking: dict, rows: list[dict]) -> str:
    """The receipt as HTML. WeasyPrint turns it into the PDF; keeping it HTML keeps it testable."""
    paid = sum(p["amount_paise"] - p["refunded_paise"] for p in rows if p["status"] == "succeeded")
    due = sum(p["amount_paise"] for p in rows if p["status"] == "due")
    lines = "".join(
        f"<tr><td>{escape(p['description'] or p['kind'])}</td><td>{escape(p['status'])}</td>"
        f"<td class='num'>{rupees(p['amount_paise'])}</td></tr>"
        + (f"<tr><td>Refund</td><td></td><td class='num'>{rupees(-p['refunded_paise'])}</td></tr>" if p["refunded_paise"] else "")
        for p in sorted(rows, key=lambda p: p["id"])
    )
    late = f"<p>Returned {booking['late_minutes']} minutes late.</p>" if booking["late_minutes"] else ""
    return f"""<html><head><meta charset="utf-8"><style>{CSS}</style></head><body>
<h1>{escape(settings.company_name)}</h1><p class="muted">Receipt for booking {escape(booking['code'])}</p>
<p><b>{escape(booking['customer_name'])}</b><br>{escape(booking['bike_type_name'])} ({escape(booking['bike_code'])})
at {escape(booking['station_name'])}</p>
<p>Booked: {fmt(booking['start_at'])} to {fmt(booking['end_at'])}<br>
Ridden: {fmt(booking['picked_up_at'])} to {fmt(booking['returned_at'])}</p>{late}
<table><tr><th>Item</th><th>Status</th><th class="num">Amount</th></tr>{lines}
<tr class="total"><td>Paid</td><td></td><td class="num">{rupees(paid)}</td></tr>
{f'<tr class="total"><td>Still to pay</td><td></td><td class="num">{rupees(due)}</td></tr>' if due else ''}
</table><p class="muted">Test mode: no real money was charged.</p></body></html>"""


def make_receipt_job(conn, payload: dict) -> None:
    """Outbox consumer for booking.returned: build the PDF, store it, email it."""
    from weasyprint import HTML  # imported here: it needs system libraries that only the containers carry

    booking = states.get_booking(conn, payload["booking_id"])
    rows = payments.list_payments(conn, booking["user_id"], booking_id=booking["id"])
    pdf = HTML(string=receipt_html(booking, rows)).write_pdf()
    file_id = files.put(conn, f"receipts/{booking['code']}.pdf", "application/pdf", pdf)
    conn.execute("UPDATE booking SET receipt_file_id = %s WHERE id = %s", (file_id, booking["id"]))
    due = sum(p["amount_paise"] for p in rows if p["status"] == "due")
    owed = f"\n\nYou have {rupees(due)} to pay: {settings.app_url}/me/dues" if due else ""
    mail.queue_email(
        conn, booking["user_id"], booking["customer_email"], f"Your receipt: {booking['code']}",
        f"Hi {booking['customer_name']},\n\nThanks for riding with us. Your receipt is attached.{owed}",
        kind="receipt", attachment_file_id=file_id, dedupe_key=f"email:{payload['event_id']}",
    )
