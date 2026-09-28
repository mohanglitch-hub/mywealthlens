"""
International Investing Centre — CLI Commands
=================================================
`flask international snapshot` — refreshes every active holding's live
price/FX rate, THEN records today's USD value (see
services.take_daily_snapshot()). Meant to be run once a day via
Windows Task Scheduler (see run_scheduled_jobs.bat), same pattern as
`flask wealth snapshot` / `flask prices refresh`. This is what makes
Schedule FA's opening/peak/closing-value reporting possible — without
a daily value trail, "the peak value this asset held during the year"
can't be reconstructed after the fact.

Batch 9.1 (Sep 2026): this command used to only snapshot, never
refresh — this module had no equivalent of domestic stocks' `flask
prices refresh`, so a holding nobody manually refreshed would have its
stale value recorded day after day. take_daily_snapshot() now calls
refresh_all_holdings() first, so this one command does both jobs a
domestic holding gets from two separate scheduled commands.

Usage:
    py -m flask --app app international snapshot
"""
import click
from flask.cli import with_appcontext


def register_cli(app):
    @app.cli.group("international")
    def international_group():
        """International Investing Centre CLI commands."""
        pass

    @international_group.command("snapshot")
    @with_appcontext
    def snapshot_command():
        """
        Refresh live price/FX for every user's active international
        holdings, then record today's USD value snapshot. Safe to run
        multiple times a day — re-running today just updates today's
        row rather than duplicating it.
        """
        from . import services

        click.echo("Refreshing live price/FX and recording today's international holding value snapshots...")
        count = services.take_daily_snapshot()
        click.echo(f"Snapshots recorded/updated: {count}")
        click.echo("Done.")
