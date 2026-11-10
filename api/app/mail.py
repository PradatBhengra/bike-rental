import smtplib
from email.message import EmailMessage

from app.config import settings
from app.jobs import enqueue


def queue_email(conn, user_id: int | None, to: str, subject: str, text: str, kind: str,
                attachment_file_id: int | None = None, dedupe_key: str | None = None) -> None:
    """Emails are jobs: they go out after the transaction that caused them commits."""
    payload = {"user_id": user_id, "to": to, "subject": subject, "text": text, "kind": kind,
               "attachment_file_id": attachment_file_id}
    enqueue(conn, "send_email", payload, dedupe_key=dedupe_key)


def send_email_job(conn, p: dict) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.mail_from, p["to"], p["subject"]
    msg.set_content(p["text"] + f"\n\n— {settings.company_name}, Ranchi\n")
    if p.get("attachment_file_id"):
        from app import files

        name, content_type, data = files.get(conn, p["attachment_file_id"])
        maintype, subtype = content_type.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        smtp.send_message(msg)
    if p.get("user_id"):
        conn.execute(
            "INSERT INTO notification (user_id, kind, channel, payload, sent_at) VALUES (%s, %s, 'email', %s, now())",
            (p["user_id"], p["kind"], {"subject": p["subject"]}),
        )
