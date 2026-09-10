"""
Backup — CLI Command
=======================
Mirrors wealth/cli.py's established pattern exactly: a thin Flask
CLI wrapper, all real logic in services.py, meant to be invoked
repeatedly and silently by Windows Task Scheduler.

Command: `flask backup run`

Set up a Task Scheduler entry the same way as the existing Wealth
Snapshot task (see that task's setup for the general pattern), but
running every few minutes instead of once daily — e.g. every 5
minutes. Safe to run as often as you like: services.create_backup()
skips on its own when nothing has changed, so frequent runs just do
nothing most of the time rather than producing repeat backups.

Usage (manual testing):
    py -m flask --app app backup run
"""
import sys
import click
from flask.cli import with_appcontext


def register_cli(app):
    @app.cli.group("backup")
    def backup_group():
        """Backup module CLI commands."""
        pass

    @backup_group.command("run")
    @with_appcontext
    def run_command():
        """
        Checks every user with backup enabled and, if the database or
        documents folder has changed since their last backup, writes
        a fresh encrypted backup archive to their configured folder.
        Intended to run every few minutes via Windows Task Scheduler;
        safe to run any number of times.
        """
        from . import services

        all_settings = services.all_enabled_settings()
        if not all_settings:
            click.echo("No backup configured yet. Nothing to do.")
            sys.exit(0)

        for user_id, settings in all_settings:
            result = services.create_backup(user_id, settings)
            status = result["status"]
            if status == "success":
                click.echo(
                    f"[user {user_id}] backed up "
                    f"({result['size_bytes'] / 1024 / 1024:.1f} MB) -> {result['path']}"
                )
            elif status == "skipped":
                click.echo(f"[user {user_id}] skipped — {result['reason']}")
            else:
                click.echo(f"[user {user_id}] ERROR — {result['reason']}")

        sys.exit(0)
