"""
International Investing Centre — CLI Commands
=================================================
`flask international snapshot` — records today's USD value for every
active holding (see services.take_daily_snapshot()). Meant to be run
once a day via Windows Task Scheduler (see run_scheduled_jobs.bat),
same pattern as `flask wealth snapshot` / `flask prices refresh`. This
is what makes Schedule FA's opening/peak/closing-value reporting
possible — without a daily value trail, "the peak value this asset
held during the year" can't be reconstructed after the fact.

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
        Record today's USD value for every user's active international
        holdings. Safe to run multiple times a day — re-running today
        just updates today's row rather than duplicating it.
        """
        from . import services

        click.echo("Recording today's international holding value snapshots...")
        count = services.take_daily_snapshot()
        click.echo(f"Snapshots recorded/updated: {count}")
        click.echo("Done.")
