"""
Notifications — CLI Commands (Sep 2026)
===========================================
`flask notifications monthly-summary` and `flask notifications
reminders` — registered the same way as `flask wealth snapshot` /
`flask backup run` / `flask prices refresh` (see register_cli(app) in
app.py). Intended to be run once daily via Windows Task Scheduler;
safe to run any number of times in between — see notifications_service.py
for exactly how each command stays idempotent (a date gate for the
monthly summary, NotificationLog dedup for both).

Usage:
    py -m flask --app app notifications monthly-summary
    py -m flask --app app notifications monthly-summary --dry-run
    py -m flask --app app notifications monthly-summary --force
    py -m flask --app app notifications reminders
    py -m flask --app app notifications reminders --dry-run
"""
import click
from flask.cli import with_appcontext


def register_notifications_cli(app):
    @app.cli.group("notifications")
    def notifications_group():
        """Notification email commands."""
        pass

    @notifications_group.command("monthly-summary")
    @click.option("--dry-run", is_flag=True, default=False,
                 help="Show who would be emailed without sending or logging anything.")
    @click.option("--force", is_flag=True, default=False,
                 help="Send even if today isn't the 1st of the month (manual testing).")
    @with_appcontext
    def monthly_summary_command(dry_run, force):
        """
        Emails the opted-in monthly wealth summary to every user who
        has it turned on (My Account > Notifications), covering the
        most recently completed calendar month. Only actually sends
        on the 1st of the month unless --force is given.
        """
        from notifications_service import run_monthly_summary_job

        label = "DRY RUN — " if dry_run else ""
        click.echo(f"{label}Starting monthly summary run...")
        summary = run_monthly_summary_job(dry_run=dry_run, force=force)

        if summary.get("gated"):
            click.echo("Today isn't the 1st of the month — nothing to do (use --force to send anyway).")
            return

        click.echo("")
        click.echo(f"Users opted in / processed: {summary['processed']}")
        if dry_run:
            click.echo(f"Would send:                  {summary['sent']}")
        else:
            click.echo(f"Sent:                        {summary['sent']}")
        click.echo(f"Skipped (already sent):     {summary['skipped']}")
        click.echo(f"Failed:                      {summary['failed']}")
        click.echo("")
        click.echo("Run completed." if not dry_run else "Dry run complete. No emails sent, no rows written.")

    @notifications_group.command("reminders")
    @click.option("--dry-run", is_flag=True, default=False,
                 help="Show who would be emailed without sending or logging anything.")
    @with_appcontext
    def reminders_command(dry_run):
        """
        Emails a digest of upcoming insurance renewals and recurring
        payment (SIP/EMI) due dates to every user who has reminders
        turned on (My Account > Notifications) — one email per user
        per run, only for items due within the reminder window that
        haven't already been reminded about.
        """
        from notifications_service import run_reminder_job

        label = "DRY RUN — " if dry_run else ""
        click.echo(f"{label}Starting renewal/SIP reminder run...")
        summary = run_reminder_job(dry_run=dry_run)

        click.echo("")
        click.echo(f"Users opted in / processed: {summary['processed']}")
        if dry_run:
            click.echo(f"Would send:                  {summary['sent']}")
        else:
            click.echo(f"Sent:                        {summary['sent']}")
        click.echo(f"Skipped (nothing new/opted out): {summary['skipped']}")
        click.echo(f"Failed:                      {summary['failed']}")
        click.echo("")
        click.echo("Run completed." if not dry_run else "Dry run complete. No emails sent, no rows written.")
