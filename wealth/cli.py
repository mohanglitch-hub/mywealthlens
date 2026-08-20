"""
Wealth — CLI Commands (Phase I)
==================================
The Flask CLI entry point for automatic Wealth snapshots. Registered
onto the app via register_cli(app) — see the two-line addition in
app.py. This project had no pre-existing Flask CLI convention to
follow (confirmed by audit), so this establishes one from scratch
using Flask/Click's standard `app.cli.command()` pattern.

Command: `flask wealth snapshot`
This is deliberately a thin wrapper (Section 38 of the Phase I spec:
"CLI -> Service -> Models", never "CLI -> duplicated calculations ->
database"). ALL of the actual logic — determining today's IST date,
iterating users, calculating Wealth, creating/skipping/failing
snapshots, writing log rows — lives in
wealth.history_service.run_automatic_snapshot_run(). This file only
parses the --dry-run flag, calls that function, prints a plain-text
summary, and sets the process exit code.

Usage (manual testing, Section 85/86 — no need to wait for midnight):
    py -m flask --app app wealth snapshot
    py -m flask --app app wealth snapshot --dry-run

Windows Task Scheduler invokes the exact same command — see the
Phase I final report for full setup instructions.
"""

import sys
import click
from flask.cli import with_appcontext


def register_cli(app):
    @app.cli.group("wealth")
    def wealth_group():
        """Wealth module CLI commands."""
        pass

    @wealth_group.command("snapshot")
    @click.option("--dry-run", is_flag=True, default=False,
                 help="Show what would happen without writing to the database.")
    @with_appcontext
    def snapshot_command(dry_run):
        """
        Create today's automatic Wealth snapshot for every user who
        doesn't already have one for today (IST). Intended to be run
        once daily via Windows Task Scheduler; safe to run manually
        any number of times (Section 14/27/46 — duplicate protection
        means a repeat run just reports SKIPPED, never creates a
        second snapshot or errors out).
        """
        from . import history_service
        from models import db

        label = "DRY RUN — " if dry_run else ""
        click.echo(f"{label}Starting automatic Wealth snapshot run...")

        summary = history_service.run_automatic_snapshot_run(db, dry_run=dry_run)

        click.echo(f"Date: {summary['date'].isoformat()} IST")
        click.echo("")
        click.echo(f"Users processed: {summary['processed']}")
        if dry_run:
            click.echo(f"Would create:    {summary['created']}")
        else:
            click.echo(f"Created:         {summary['created']}")
        click.echo(f"Skipped:         {summary['skipped']}")
        click.echo(f"Failed:          {summary['failed']}")
        click.echo("")

        if dry_run:
            click.echo("Dry run complete. No database changes were made.")
        else:
            click.echo("Run completed.")

        # Section 40: a normal run — including one with SKIPPED or
        # even individual FAILED users — is not a fatal outcome for
        # the process as a whole (Section 58: one user's failure
        # must not be treated as the whole run failing). Exit
        # non-zero is reserved for the run not completing at all,
        # which would already have raised before reaching this line
        # (Flask would print its own traceback and exit non-zero on
        # an uncaught exception) — so success here always means the
        # loop ran to completion, regardless of individual outcomes.
        sys.exit(0)
