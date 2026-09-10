"""
Backup — Routes
==================
Thin handlers only — all real logic lives in services.py. This
module has no pages of its own: every action here is a form target
POSTed from the Backup & Restore tab inside My Account
(templates/preferences.html, #sec-backup), and every action redirects
straight back to that tab (url_for('preferences') + '#backup') so
the user never leaves My Account.
"""
import os
import tempfile

from flask import redirect, url_for, request, flash
from flask_login import login_required, current_user

from backup import backup_bp
from backup import services


def _back():
    return redirect(url_for("preferences") + "#backup")


@backup_bp.route("/setup", methods=["POST"])
@login_required
def setup():
    folder_path = (request.form.get("folder_path") or "").strip()
    passphrase = request.form.get("passphrase") or ""
    confirm_passphrase = request.form.get("confirm_passphrase") or ""

    if not folder_path:
        flash("A backup folder path is required.", "error")
        return _back()
    if not os.path.isdir(folder_path):
        flash(f'"{folder_path}" doesn\'t exist or isn\'t a folder. '
              f'Double-check the path (e.g. your Google Drive synced folder).', "error")
        return _back()
    if len(passphrase) < 8:
        flash("Passphrase must be at least 8 characters.", "error")
        return _back()
    if passphrase != confirm_passphrase:
        flash("Passphrases don't match.", "error")
        return _back()

    services.set_passphrase(passphrase)
    settings = services.save_settings(current_user.id, folder_path=folder_path, enabled=True)

    result = services.create_backup(current_user.id, settings, force=True)
    if result["status"] == "success":
        flash("Backup set up — first backup completed. It'll now keep itself "
              "up to date automatically, no need to do anything further.", "success")
    else:
        flash(f"Settings saved, but the first backup didn't run: {result.get('reason')}", "error")

    return _back()


@backup_bp.route("/disable", methods=["POST"])
@login_required
def disable():
    services.save_settings(current_user.id, enabled=False)
    flash("Automatic backups turned off. Your existing backup file is untouched.", "success")
    return _back()


@backup_bp.route("/enable", methods=["POST"])
@login_required
def enable():
    settings = services.get_settings(current_user.id)
    if not settings.get("folder_path") or not services.has_passphrase():
        flash("Set up your backup folder and passphrase first.", "error")
        return _back()
    services.save_settings(current_user.id, enabled=True)
    flash("Automatic backups turned back on.", "success")
    return _back()


@backup_bp.route("/backup-now", methods=["POST"])
@login_required
def backup_now():
    settings = services.get_settings(current_user.id)
    if not settings.get("enabled"):
        flash("Set up backup first.", "error")
        return _back()
    result = services.create_backup(current_user.id, settings, force=True)
    if result["status"] == "success":
        flash(f"Backup completed ({result['size_bytes'] / 1024 / 1024:.1f} MB).", "success")
    else:
        flash(f"Backup didn't run: {result.get('reason')}", "error")
    return _back()


@backup_bp.route("/restore", methods=["POST"])
@login_required
def restore():
    uploaded = request.files.get("backup_file")
    passphrase = request.form.get("passphrase") or ""
    confirmed = request.form.get("confirm_restore") == "yes"

    if not confirmed:
        flash("You must check the confirmation box to restore — this replaces all current data.", "error")
        return _back()
    if not uploaded or uploaded.filename == "":
        flash("Choose a backup file to restore from.", "error")
        return _back()
    if not passphrase:
        flash("Enter the backup passphrase.", "error")
        return _back()

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        uploaded.save(tmp.name)
        tmp_path = tmp.name

    try:
        result = services.restore_backup(tmp_path, passphrase)
    finally:
        os.unlink(tmp_path)

    if result["status"] == "success":
        flash("Restore complete. Restart the Flask server now for the restored "
              "data to take effect.", "success")
    else:
        flash(f"Restore failed: {result.get('reason')}", "error")

    return _back()
