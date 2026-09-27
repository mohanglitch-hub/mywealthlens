"""
Notifications — Service Layer (Sep 2026)
============================================
Two opt-in email notifications, both off by default (My Account >
Notifications):

  1. Monthly wealth summary — net worth + last month's cashflow,
     sent once per calendar month.
  2. Renewal/SIP due-date reminders — a single digest email per run
     listing insurance policy renewals and recurring payments (SIPs/
     EMIs/subscriptions) due within REMINDER_WINDOW_DAYS, sent at
     most once per due-date instance per user.

Both are driven by the `flask notifications ...` CLI commands
(notifications_cli.py), invoked on a schedule via Windows Task
Scheduler — same "CLI -> Service -> Models" pattern as `flask wealth
snapshot` / `flask backup run` / `flask prices refresh`. Actual SMTP
sending lives in mail.py; this module only decides WHO to email and
WHAT the email should contain, then records a NotificationLog row so
a repeat run (or a missed day caught up later) never sends the same
notification twice.

Deliberately thin: every "what's due" query reuses an existing,
already-tested service function (insurance's own days_to_renewal
property, cashflow's get_upcoming_recurring/get_month_summary) rather
than recomputing that logic here.
"""
from datetime import date, timedelta

from flask import current_app

from models import db, User, NotificationLog, NetWorthHistory
import currency_display

# How many days ahead of a renewal/SIP due date the reminder fires.
# (Sep 2026 scoping decision — see My Account > Notifications.)
REMINDER_WINDOW_DAYS = 7


def _instance_path():
    return current_app.instance_path


def _already_sent(user_id, notif_type, ref_key):
    return NotificationLog.query.filter_by(
        user_id=user_id, notif_type=notif_type, ref_key=ref_key).first() is not None


def _mark_sent(user_id, notif_type, ref_key):
    db.session.add(NotificationLog(user_id=user_id, notif_type=notif_type, ref_key=ref_key))
    db.session.commit()


# ── Monthly Wealth Summary ─────────────────────────────────────────────────

def _compute_net_worth(user_id):
    """
    Live net worth for `user_id` right now — same figures the
    dashboard's hero card shows (see dashboard() in app.py): all
    Wealth Centre assets/liabilities plus CAS-imported MF/Stock
    holdings, liabilities already subtracted. Computed fresh rather
    than read from NetWorthHistory, since that table is only written
    on a dashboard page visit and may be stale or missing for a user
    who hasn't opened the app recently.
    """
    from wealth.services import WealthStatisticsService
    from models import MutualFund, Stock

    wstats = WealthStatisticsService(user_id)
    mf_value = sum(m.value for m in MutualFund.query.filter_by(user_id=user_id).all())
    stock_value = sum(s.value for s in Stock.query.filter_by(user_id=user_id).all())
    total_assets = wstats.total_assets() + mf_value + stock_value
    return total_assets - wstats.total_liabilities()


def _net_worth_change_display(user, net_worth_now):
    """
    Compares net_worth_now to the closest NetWorthHistory snapshot
    from ~30 days ago. Returns a display string, or "not enough
    history yet" if no snapshot old enough exists (a new user, or one
    who hasn't opened the dashboard in the last month) — better than
    a misleading 0.00% or a missing line.
    """
    target = date.today() - timedelta(days=30)
    prev = (NetWorthHistory.query.filter_by(user_id=user.id)
            .filter(NetWorthHistory.snapshot_date <= target)
            .order_by(NetWorthHistory.snapshot_date.desc()).first())
    if not prev:
        return "not enough history yet"

    change = net_worth_now - prev.total
    change_str = currency_display.format_money_for_user(abs(change), user)
    if prev.total:
        pct = change / abs(prev.total) * 100
        pct_str = f" ({'+' if pct >= 0 else '-'}{abs(pct):.1f}%)"
    else:
        pct_str = ""
    sign = "+" if change >= 0 else "-"
    return f"{sign}{change_str}{pct_str}"


def _previous_month():
    """(year, month) of the most recently fully-completed calendar
    month, relative to today."""
    today = date.today()
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def send_monthly_summary_for_user(user, dry_run=False):
    """
    Sends (or, if already sent for this month, skips) the monthly
    summary for one user. Returns 'sent', 'skipped_already_sent',
    'skipped_opted_out', or 'failed'.
    """
    if not user.notify_monthly_summary:
        return "skipped_opted_out"

    year, month = _previous_month()
    ref_key = f"{year:04d}-{month:02d}"
    if _already_sent(user.id, "monthly_summary", ref_key):
        return "skipped_already_sent"

    if dry_run:
        return "would_send"

    from cashflow_centre import services as cashflow_services

    net_worth_now = _compute_net_worth(user.id)
    month_summary = cashflow_services.get_month_summary(user.id, year, month)
    savings_rate = cashflow_services.get_savings_rate(
        month_summary["total_income"], month_summary["total_expense"])
    top_category = next(iter(month_summary["by_category"]), None)
    month_label = date(year, month, 1).strftime("%B %Y")

    ctx = {
        "month_label": month_label,
        "net_worth": currency_display.format_money_for_user(net_worth_now, user),
        "net_worth_change_display": _net_worth_change_display(user, net_worth_now),
        "total_income": currency_display.format_money_for_user(month_summary["total_income"], user),
        "total_expense": currency_display.format_money_for_user(month_summary["total_expense"], user),
        "net_cashflow": currency_display.format_money_for_user(month_summary["net"], user),
        "savings_rate": f"{savings_rate:.0f}" if savings_rate is not None else "—",
        "top_category": top_category,
        "top_category_amount": (
            currency_display.format_money_for_user(month_summary["by_category"][top_category], user)
            if top_category else None
        ),
    }

    import mail
    ok, _err = mail.send_monthly_summary_email(_instance_path(), user.email, ctx)
    if not ok:
        return "failed"
    _mark_sent(user.id, "monthly_summary", ref_key)
    return "sent"


def run_monthly_summary_job(dry_run=False, force=False):
    """
    The `flask notifications monthly-summary` entry point. Only
    actually sends on the 1st of the month (covering the month that
    just ended) unless `force` is passed — safe to invoke daily via
    Task Scheduler like the other CLI jobs; the date gate plus the
    NotificationLog dedup together mean a normal daily run is a no-op
    on every day but the 1st, and even a re-run ON the 1st (or a
    late/missed run caught up a few days into the new month) never
    double-sends.
    """
    summary = {"processed": 0, "sent": 0, "skipped": 0, "failed": 0}
    if not force and date.today().day != 1:
        summary["gated"] = True
        return summary

    for user in User.query.filter_by(notify_monthly_summary=True).all():
        summary["processed"] += 1
        try:
            result = send_monthly_summary_for_user(user, dry_run=dry_run)
        except Exception:
            result = "failed"
        if result in ("sent", "would_send"):
            summary["sent"] += 1
        elif result == "failed":
            summary["failed"] += 1
        else:
            summary["skipped"] += 1
    return summary


# ── Renewal / SIP Due-Date Reminders ───────────────────────────────────────

def _due_insurance_items(user_id):
    from insurance_centre.models import InsurancePolicy

    items = []
    policies = (InsurancePolicy.query
                .filter_by(user_id=user_id, is_archived=False)
                .filter(InsurancePolicy.renewal_date.isnot(None)).all())
    for p in policies:
        days = p.days_to_renewal
        if days is None or days < 0 or days > REMINDER_WINDOW_DAYS:
            continue
        items.append({
            "ref_key": f"insurance:{p.id}:{p.renewal_date.isoformat()}",
            "label": f"{p.display_type} — {p.insurer} renewal",
            "detail": "Insurance",
            "due_date": p.renewal_date,
            "days_away": days,
        })
    return items


def _due_recurring_items(user_id):
    from cashflow_centre import services as cashflow_services

    items = []
    for rp in cashflow_services.get_upcoming_recurring(user_id, days=REMINDER_WINDOW_DAYS):
        items.append({
            "ref_key": f"recurring:{rp['id']}:{rp['due_date'].isoformat()}",
            "label": rp["name"],
            "detail": f"{rp['category']} · {rp['type'].title()}",
            "due_date": rp["due_date"],
            "days_away": rp["days_away"],
        })
    return items


def send_reminders_for_user(user, dry_run=False):
    """
    Sends (or skips) one digest reminder email for one user, covering
    every due item that hasn't already been reminded about. Returns
    'sent', 'skipped_nothing_new', 'skipped_opted_out', or 'failed'.
    """
    if not user.notify_renewal_sip_reminders:
        return "skipped_opted_out"

    all_items = _due_insurance_items(user.id) + _due_recurring_items(user.id)
    new_items = [it for it in all_items
                 if not _already_sent(user.id, "renewal_sip_reminder", it["ref_key"])]
    if not new_items:
        return "skipped_nothing_new"

    if dry_run:
        return "would_send"

    new_items.sort(key=lambda it: it["due_date"])
    for it in new_items:
        it["due_date_display"] = it["due_date"].strftime("%d %b %Y")

    import mail
    ok, _err = mail.send_renewal_sip_reminder_email(_instance_path(), user.email, new_items)
    if not ok:
        return "failed"
    for it in new_items:
        _mark_sent(user.id, "renewal_sip_reminder", it["ref_key"])
    return "sent"


def run_reminder_job(dry_run=False):
    """The `flask notifications reminders` entry point — safe to run
    daily; NotificationLog dedup means each due item is only ever
    emailed once, on whichever run first sees it inside the window."""
    summary = {"processed": 0, "sent": 0, "skipped": 0, "failed": 0}
    for user in User.query.filter_by(notify_renewal_sip_reminders=True).all():
        summary["processed"] += 1
        try:
            result = send_reminders_for_user(user, dry_run=dry_run)
        except Exception:
            result = "failed"
        if result in ("sent", "would_send"):
            summary["sent"] += 1
        elif result == "failed":
            summary["failed"] += 1
        else:
            summary["skipped"] += 1
    return summary
