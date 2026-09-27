"""
Outbound Email — Password Reset (production-readiness, Sep 2026)
====================================================================
The only place MyWealthLens sends email. Deliberately small: plain
smtplib against Gmail's SMTP server with an "app password" (not
Mohan's real Gmail password — a revocable, scoped credential Google
issues separately for exactly this kind of use), rather than pulling
in Flask-Mail or a transactional-email SDK for a single email type.

Credentials are never hardcoded and never committed:
  1. Environment variables MWL_SMTP_USER / MWL_SMTP_APP_PASSWORD (and
     optionally MWL_SMTP_FROM_NAME) — checked first, since this is the
     standard way secrets get set once real hosting exists.
  2. Falling back to instance/email_config.json — same local-file
     pattern already used for instance/secret_key.txt, for easy local
     setup on Mohan's own machine without having to set Windows
     environment variables. instance/ is already gitignored.

If neither is configured, email sending is simply skipped — a missing
SMTP setup must never break signup/login/forgot-password for
everyone else. The caller (app.py) still shows the same generic "if
an account exists..." message either way, and logs the real reason
server-side only.
"""
import os
import json
import smtplib
import ssl
from email.message import EmailMessage


SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


class EmailNotConfigured(Exception):
    pass


def _load_smtp_config(instance_path):
    user = os.environ.get("MWL_SMTP_USER", "").strip()
    app_password = os.environ.get("MWL_SMTP_APP_PASSWORD", "").strip()
    from_name = os.environ.get("MWL_SMTP_FROM_NAME", "").strip() or "MyWealthLens"
    if user and app_password:
        return {"user": user, "app_password": app_password, "from_name": from_name}

    config_path = os.path.join(instance_path, "email_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
            user = (data.get("smtp_user") or "").strip()
            app_password = (data.get("smtp_app_password") or "").strip()
            from_name = (data.get("from_name") or "").strip() or "MyWealthLens"
            if user and app_password:
                return {"user": user, "app_password": app_password, "from_name": from_name}
        except (json.JSONDecodeError, OSError):
            pass

    return None


def send_password_reset_email(instance_path, to_email, reset_url, ttl_minutes):
    """
    Sends the password reset email. Returns True on success, False on
    any failure (not configured, auth failure, network error, etc.) —
    never raises, so a caller can always fall back to the same
    generic user-facing message regardless of what actually happened.
    The caller should log the return value server-side if it wants to
    know why a send didn't happen.
    """
    config = _load_smtp_config(instance_path)
    if config is None:
        return False, "Email isn't configured yet (no MWL_SMTP_USER/MWL_SMTP_APP_PASSWORD env vars or instance/email_config.json)."

    msg = EmailMessage()
    msg["Subject"] = "Reset your MyWealthLens password"
    msg["From"] = f'{config["from_name"]} <{config["user"]}>'
    msg["To"] = to_email
    msg.set_content(
        f"We received a request to reset your MyWealthLens password.\n\n"
        f"Reset it here (link expires in {ttl_minutes} minutes):\n{reset_url}\n\n"
        f"If you didn't request this, you can safely ignore this email — "
        f"your password won't change unless you click the link above and "
        f"choose a new one."
    )
    msg.add_alternative(
        f"""\
<html><body style="font-family:sans-serif; color:#1a1a2e;">
  <p>We received a request to reset your MyWealthLens password.</p>
  <p><a href="{reset_url}" style="background:#4f46e5; color:#fff; padding:10px 20px;
     border-radius:8px; text-decoration:none; display:inline-block;">Reset Password</a></p>
  <p style="font-size:0.85rem; color:#666;">This link expires in {ttl_minutes} minutes.
     If you didn't request this, you can safely ignore this email — your password
     won't change unless you click the link above and choose a new one.</p>
</body></html>""",
        subtype="html",
    )

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls(context=context)
            server.login(config["user"], config["app_password"])
            server.send_message(msg)
        return True, None
    except Exception as e:
        return False, str(e)
