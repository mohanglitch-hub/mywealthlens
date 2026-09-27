"""
Prices — CLI Commands (production-readiness, Sep 2026)
==========================================================
`flask prices refresh` — refreshes live stock prices (yfinance) and
mutual fund NAVs (AMFI's daily NAVAll.txt) for every user in one run.
Registered the same way as `flask wealth snapshot` / `flask backup
run` (see register_cli(app) in app.py) — intended to be run once daily
via Windows Task Scheduler for automatic refresh, and safe to run
manually any number of times in between.

Usage:
    py -m flask --app app prices refresh
"""
import click
from flask.cli import with_appcontext


def register_price_refresh_cli(app):
    @app.cli.group("prices")
    def prices_group():
        """Market price / NAV refresh commands."""
        pass

    @prices_group.command("refresh")
    @with_appcontext
    def refresh_command():
        """
        Refresh live stock prices and mutual fund NAVs for every user.
        One AMFI file download for the whole run, regardless of how
        many users/holdings exist. A single holding that can't be
        refreshed (delisted stock, scheme missing from today's AMFI
        file, no amfi_code on record) is skipped and counted, never
        treated as a run failure.
        """
        from models import db
        from price_refresh import refresh_all_prices

        click.echo("Starting price/NAV refresh for all users...")
        summary = refresh_all_prices(db, user_id=None)

        click.echo("")
        click.echo(f"Stocks refreshed:        {summary['stocks_updated']}")
        click.echo(f"Stocks couldn't refresh: {summary['stocks_failed']}")
        click.echo(f"MF NAVs refreshed:       {summary['mfs_updated']}")
        click.echo(f"MFs missing AMFI code:   {summary['mfs_no_code']}")
        click.echo(f"MFs not in AMFI file:    {summary['mfs_failed']}")
        if summary["amfi_error"]:
            click.echo(f"AMFI file error:         {summary['amfi_error']}")
        click.echo("")
        click.echo("Run completed.")
