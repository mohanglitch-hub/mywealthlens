"""
Backup Blueprint
===================
Local backup/restore of the whole app: the SQLite database plus every
document attachment across Insurance/Retirement/Wealth. Designed to
be dropped into a synced folder (Google Drive for Desktop, OneDrive,
Dropbox — anything that mirrors a local folder to the cloud) so the
actual "upload to the cloud" step is handled entirely by that synced
folder's own software, never by this app.

This app never talks to Google (or any cloud provider) directly — no
API keys, no OAuth. It only ever reads/writes a folder ON THIS
COMPUTER that the user points it at.

No pages of its own — every route here is a form-POST target for the
Backup & Restore tab inside My Account (see templates/preferences.html,
#sec-backup). Deliberately not in the sidebar nav.
"""
from flask import Blueprint

backup_bp = Blueprint(
    "backup",
    __name__,
    url_prefix="/backup",
)

from backup import routes  # noqa
