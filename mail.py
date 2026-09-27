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


def _send(instance_path, to_email, subject, text_body, html_body):
    """
    Shared send path for the notification emails below — same
    config-loading and SMTP mechanics as send_password_reset_email()
    above, just parameterized on subject/body instead of hardcoding
    the reset-password content. Never raises; returns (ok, error).
    """
    config = _load_smtp_config(instance_path)
    if config is None:
        return False, "Email isn't configured yet (no MWL_SMTP_USER/MWL_SMTP_APP_PASSWORD env vars or instance/email_config.json)."

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f'{config["from_name"]} <{config["user"]}>'
    msg["To"] = to_email
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls(context=context)
            server.login(config["user"], config["app_password"])
            server.send_message(msg)
        return True, None
    except Exception as e:
        return False, str(e)


def send_monthly_summary_email(instance_path, to_email, ctx):
    """
    Notifications (Sep 2026) — the opt-in monthly wealth summary email
    (My Account > Notifications). `ctx` is a plain dict built by
    notifications_service.py: {
        month_label, net_worth, net_worth_change_display, total_income,
        total_expense, net_cashflow, savings_rate, top_category,
        top_category_amount,
    } — all money figures pre-formatted strings (already converted to
    the user's own display currency), so this function only lays them
    out, never does currency math itself.
    """
    subject = f"Your MyWealthLens summary — {ctx['month_label']}"
    text_body = (
        f"Your MyWealthLens summary for {ctx['month_label']}\n\n"
        f"Net worth: {ctx['net_worth']} ({ctx['net_worth_change_display']} since last month)\n\n"
        f"Cashflow for {ctx['month_label']}:\n"
        f"  Income:  {ctx['total_income']}\n"
        f"  Expense: {ctx['total_expense']}\n"
        f"  Net:     {ctx['net_cashflow']} ({ctx['savings_rate']}% savings rate)\n"
    )
    if ctx.get("top_category"):
        text_body += f"  Biggest expense category: {ctx['top_category']} ({ctx['top_category_amount']})\n"
    text_body += (
        "\nOpen MyWealthLens to see the full picture.\n\n"
        "You're getting this because you turned on the monthly summary "
        "email under My Account > Notifications — turn it off there any time."
    )

    top_row = ""
    if ctx.get("top_category"):
        top_row = (
            f'<tr><td style="padding:6px 0; color:#666;">Biggest expense category</td>'
            f'<td style="padding:6px 0; text-align:right;">{ctx["top_category"]} '
            f'({ctx["top_category_amount"]})</td></tr>'
        )
    html_body = f"""\
<html><body style="font-family:sans-serif; color:#1a1a2e;">
  <h2 style="margin-bottom:4px;">Your MyWealthLens summary</h2>
  <p style="color:#666; margin-top:0;">{ctx['month_label']}</p>

  <table style="width:100%; max-width:420px; border-collapse:collapse; margin:16px 0;">
    <tr><td style="padding:6px 0; color:#666;">Net worth</td>
        <td style="padding:6px 0; text-align:right; font-weight:600;">{ctx['net_worth']}</td></tr>
    <tr><td style="padding:6px 0; color:#666;">Change since last month</td>
        <td style="padding:6px 0; text-align:right;">{ctx['net_worth_change_display']}</td></tr>
  </table>

  <h3 style="margin-bottom:4px;">Cashflow</h3>
  <table style="width:100%; max-width:420px; border-collapse:collapse; margin:8px 0 16px;">
    <tr><td style="padding:6px 0; color:#666;">Income</td>
        <td style="padding:6px 0; text-align:right;">{ctx['total_income']}</td></tr>
    <tr><td style="padding:6px 0; color:#666;">Expense</td>
        <td style="padding:6px 0; text-align:right;">{ctx['total_expense']}</td></tr>
    <tr><td style="padding:6px 0; color:#666;">Net ({ctx['savings_rate']}% savings rate)</td>
        <td style="padding:6px 0; text-align:right; font-weight:600;">{ctx['net_cashflow']}</td></tr>
    {top_row}
  </table>

  <p style="font-size:0.85rem; color:#666;">
    You're getting this because you turned on the monthly summary email
    under My Account &gt; Notifications — turn it off there any time.
  </p>
</body></html>"""

    return _send(instance_path, to_email, subject, text_body, html_body)


def send_renewal_sip_reminder_email(instance_path, to_email, items):
    """
    Notifications (Sep 2026) — the opt-in renewal/SIP due-date reminder
    email. `items` is a list of dicts (built by notifications_service.py),
    each already formatted for display: {
        label, detail, due_date_display, days_away,
    } — e.g. {"label": "HDFC Life Term Plan renewal",
              "detail": "Insurance", "due_date_display": "3 Oct 2026",
              "days_away": 6}
    One digest email per run per user, not one email per item.
    """
    count = len(items)
    subject = (
        f"1 item due soon — MyWealthLens" if count == 1
        else f"{count} items due soon — MyWealthLens"
    )

    lines = []
    for it in items:
        when = "today" if it["days_away"] == 0 else (
            "tomorrow" if it["days_away"] == 1 else f"in {it['days_away']} days"
        )
        lines.append(f"  - {it['label']} ({it['detail']}) — due {it['due_date_display']}, {when}")
    text_body = (
        "The following are coming due soon:\n\n" + "\n".join(lines) +
        "\n\nOpen MyWealthLens for details.\n\n"
        "You're getting this because you turned on renewal/SIP reminders "
        "under My Account > Notifications — turn it off there any time."
    )

    rows = ""
    for it in items:
        when = "today" if it["days_away"] == 0 else (
            "tomorrow" if it["days_away"] == 1 else f"in {it['days_away']} days"
        )
        rows += (
            f'<tr><td style="padding:8px 0; border-bottom:1px solid #eee;">'
            f'<div style="font-weight:600;">{it["label"]}</div>'
            f'<div style="font-size:0.82rem; color:#666;">{it["detail"]}</div></td>'
            f'<td style="padding:8px 0; border-bottom:1px solid #eee; text-align:right; white-space:nowrap;">'
            f'{it["due_date_display"]}<br><span style="font-size:0.82rem; color:#666;">{when}</span></td></tr>'
        )
    html_body = f"""\
<html><body style="font-family:sans-serif; color:#1a1a2e;">
  <h2 style="margin-bottom:4px;">Coming due soon</h2>
  <table style="width:100%; max-width:480px; border-collapse:collapse; margin:16px 0;">
    {rows}
  </table>
  <p style="font-size:0.85rem; color:#666;">
    You're getting this because you turned on renewal/SIP reminders under
    My Account &gt; Notifications — turn it off there any time.
  </p>
</body></html>"""

    return _send(instance_path, to_email, subject, text_body, html_body)
