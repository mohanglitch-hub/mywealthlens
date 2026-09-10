"""
Backup — Services
====================
Core backup/restore logic. No Flask request objects here — pure
Python plus `current_app` for path resolution, matching this
project's services.py convention.

WHY BACKUP SETTINGS LIVE IN A LOCAL JSON FILE, NOT A DATABASE TABLE:
the obvious design puts settings (folder path, last-backup signature)
in a new SQLAlchemy table, like every other module. That's wrong
here specifically: this table would live INSIDE mywealthlens.db —
the very file being backed up. Every time a backup finishes and we
record "last_db_mtime = X" back into that same database, the write
itself changes the database's modification time again, so the very
next check always sees "the database changed since last backup" —
even when nothing the user did actually changed. Backup settings
belong outside the file they're tracking, so they're kept in
instance/backup_state.json instead — a small local file, never
included inside the backup archive itself (see create_backup below).

DESIGN NOTE — single-user today: this backs up the ENTIRE database
file, all users included. That's the right call while MyWealthLens
is genuinely a single-user app (Mohan's own instance). If a future
deployment has several people sharing one running copy, a whole-DB
backup written to one person's personal Drive folder would contain
everyone else's data too — that would need revisiting (likely a
per-user export instead of a raw file copy) before this app is ever
run as a shared multi-user deployment. Flagged here, not solved here.

WHY A FRESH SNAPSHOT EVERY TIME, NEVER AN INCREMENTAL UPDATE:
create_backup() always rebuilds the whole archive from what's
currently on disk — the live database and the live documents folder
— into a brand-new temp staging area, then zips that. It never opens
or modifies a previous backup archive. This guarantees a restored
backup never references a document that was since deleted, or misses
one added after the last backup: every run is a complete, accurate
mirror of the CURRENT state, not an accumulation of past states.

WHY THE PASSPHRASE NEVER ENTERS THE ARCHIVE: the staging directory
that gets zipped is built by copying ONLY the database file and the
documents folder — backup_passphrase.txt (and backup_state.json) are
never copied into it. Even if the encrypted archive itself were ever
exposed, the passphrase needed to open it was never inside it.
"""
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone

import pyzipper

BACKUP_FILENAME = "mywealthlens_backup.zip"
PASSPHRASE_FILENAME = "backup_passphrase.txt"
STATE_FILENAME = "backup_state.json"
MIN_SECONDS_BETWEEN_BACKUPS = 180  # safety floor, independent of how
                                    # often Task Scheduler happens to fire


def _instance_path():
    from flask import current_app
    return current_app.instance_path


def _db_path():
    return os.path.join(_instance_path(), "mywealthlens.db")


def _documents_dir():
    return os.path.join(_instance_path(), "documents")


def _passphrase_path():
    return os.path.join(_instance_path(), PASSPHRASE_FILENAME)


def _state_path():
    return os.path.join(_instance_path(), STATE_FILENAME)


def has_passphrase():
    return os.path.isfile(_passphrase_path())


def set_passphrase(passphrase):
    """Stores the passphrase locally — see module docstring on why
    this lives on disk at all (needed for fully silent, unattended
    backups) and what that does and doesn't protect against."""
    with open(_passphrase_path(), "w", encoding="utf-8") as f:
        f.write(passphrase)


def _read_passphrase():
    with open(_passphrase_path(), "r", encoding="utf-8") as f:
        return f.read()


def _load_all_state():
    path = _state_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_all_state(state):
    path = _state_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, path)


def get_settings(user_id):
    """Returns this user's backup settings as a plain dict, with
    sensible defaults if nothing has been configured yet."""
    state = _load_all_state()
    return state.get(str(user_id), {
        "enabled": False,
        "folder_path": None,
        "last_backup_at": None,
        "last_backup_size": None,
        "last_db_mtime": None,
        "last_doc_file_count": None,
        "last_doc_total_size": None,
        "last_doc_max_mtime": None,
    })


def save_settings(user_id, **updates):
    state = _load_all_state()
    current = state.get(str(user_id), {})
    current.update(updates)
    state[str(user_id)] = current
    _save_all_state(state)
    return current


def all_enabled_settings():
    """[(user_id, settings_dict), ...] for every user with backup
    turned on — used by the CLI command to back up everyone."""
    state = _load_all_state()
    return [(int(uid), s) for uid, s in state.items() if s.get("enabled")]


def _documents_signature(documents_dir):
    """A cheap fingerprint of the documents folder's current state —
    file count, total size, and the newest modification time. Full
    content hashing would be more precise but means re-reading every
    uploaded PDF/image on every check; this is enough to reliably
    detect "a file was added, removed, or replaced" for a personal
    finance app's realistic document volumes."""
    if not os.path.isdir(documents_dir):
        return (0, 0, 0.0)
    count, total_size, max_mtime = 0, 0, 0.0
    for root, _dirs, files in os.walk(documents_dir):
        for name in files:
            path = os.path.join(root, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            count += 1
            total_size += stat.st_size
            max_mtime = max(max_mtime, stat.st_mtime)
    return (count, total_size, max_mtime)


def has_changed(settings):
    """True if the database file or the documents folder look
    different from what they were at the last successful backup —
    or if there has never been a successful backup yet."""
    db_path = _db_path()
    db_mtime = os.path.getmtime(db_path) if os.path.isfile(db_path) else None
    doc_count, doc_size, doc_max_mtime = _documents_signature(_documents_dir())

    if settings.get("last_backup_at") is None:
        return True
    if settings.get("last_db_mtime") != db_mtime:
        return True
    if settings.get("last_doc_file_count") != doc_count:
        return True
    if settings.get("last_doc_total_size") != doc_size:
        return True
    if settings.get("last_doc_max_mtime") != doc_max_mtime:
        return True
    return False


def _sqlite_online_backup(source_path, dest_path):
    """Safe copy of a live SQLite database using SQLite's own online
    backup API — NOT a plain file copy. A plain copy of a database
    file that's currently open elsewhere (which it always is, while
    the Flask app is running) can grab a page mid-write and produce
    a corrupted copy; the backup API is specifically designed to be
    safe to run against a live, in-use database. It's also read-only
    against the source, so it doesn't itself change the source
    file's modification time."""
    source_conn = sqlite3.connect(source_path)
    dest_conn = sqlite3.connect(dest_path)
    with dest_conn:
        source_conn.backup(dest_conn)
    source_conn.close()
    dest_conn.close()


def create_backup(user_id, settings, force=False):
    """
    Builds a fresh, complete, encrypted backup archive and writes it
    to settings["folder_path"], then updates the local state file
    with the new signature/timestamp. Returns a summary dict.

    Written atomically: zipped to a temp name in the SAME folder,
    then renamed into place — so the synced folder (Drive/OneDrive/
    etc.) never sees, and never starts uploading, a half-written
    file.
    """
    if not settings.get("enabled"):
        return {"status": "skipped", "reason": "backup not enabled"}
    folder_path = settings.get("folder_path")
    if not folder_path or not os.path.isdir(folder_path):
        return {"status": "error", "reason": f"backup folder not found: {folder_path}"}
    if not has_passphrase():
        return {"status": "error", "reason": "no backup passphrase set up yet"}

    if not force:
        if not has_changed(settings):
            return {"status": "skipped", "reason": "nothing changed since last backup"}
        last_at = settings.get("last_backup_at")
        if last_at is not None:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_at)).total_seconds()
            if elapsed < MIN_SECONDS_BETWEEN_BACKUPS:
                return {"status": "skipped", "reason": "backed up too recently"}

    db_path = _db_path()
    documents_dir = _documents_dir()
    passphrase = _read_passphrase()

    with tempfile.TemporaryDirectory(prefix="mwl_backup_") as staging:
        staged_db = os.path.join(staging, "mywealthlens.db")
        _sqlite_online_backup(db_path, staged_db)

        staged_docs = os.path.join(staging, "documents")
        if os.path.isdir(documents_dir):
            shutil.copytree(documents_dir, staged_docs)
        else:
            os.makedirs(staged_docs, exist_ok=True)

        final_path = os.path.join(folder_path, BACKUP_FILENAME)
        temp_zip_path = os.path.join(folder_path, f".{BACKUP_FILENAME}.tmp")

        with pyzipper.AESZipFile(
            temp_zip_path, "w",
            compression=pyzipper.ZIP_DEFLATED,
            encryption=pyzipper.WZ_AES,
        ) as zf:
            zf.setpassword(passphrase.encode("utf-8"))
            zf.write(staged_db, arcname="mywealthlens.db")
            for root, _dirs, files in os.walk(staged_docs):
                for name in files:
                    full = os.path.join(root, name)
                    arcname = os.path.join("documents", os.path.relpath(full, staged_docs))
                    zf.write(full, arcname=arcname)

        os.replace(temp_zip_path, final_path)
        backup_size = os.path.getsize(final_path)

    # Captured AFTER the backup — and since this all lives in a JSON
    # file, not the database, saving it doesn't move the goalposts
    # for the next has_changed() check the way a DB write would.
    doc_count, doc_size, doc_max_mtime = _documents_signature(documents_dir)
    now = datetime.now(timezone.utc)
    save_settings(
        user_id,
        last_backup_at=now.isoformat(),
        last_backup_size=backup_size,
        last_db_mtime=os.path.getmtime(db_path) if os.path.isfile(db_path) else None,
        last_doc_file_count=doc_count,
        last_doc_total_size=doc_size,
        last_doc_max_mtime=doc_max_mtime,
    )

    return {
        "status": "success",
        "path": final_path,
        "size_bytes": backup_size,
        "at": now,
    }


def validate_backup_archive(zip_path, passphrase):
    """Opens the archive and confirms both the passphrase is correct
    AND the database file inside is a genuine, non-corrupt SQLite
    file — checked BEFORE anything on disk is touched, so a bad
    passphrase or a damaged archive never partially overwrites a
    working install."""
    try:
        with pyzipper.AESZipFile(zip_path) as zf:
            zf.setpassword(passphrase.encode("utf-8"))
            names = zf.namelist()
            if "mywealthlens.db" not in names:
                return False, "Archive doesn't contain mywealthlens.db — not a valid backup file."
            with tempfile.TemporaryDirectory(prefix="mwl_verify_") as tmp:
                zf.extract("mywealthlens.db", path=tmp)
                test_conn = sqlite3.connect(os.path.join(tmp, "mywealthlens.db"))
                test_conn.execute("PRAGMA integrity_check")
                test_conn.close()
    except RuntimeError:
        # pyzipper raises RuntimeError on wrong password for AES zips
        return False, "Incorrect passphrase."
    except Exception as e:  # noqa: BLE001 — surfaced to the user as-is
        return False, f"Couldn't read this backup file: {e}"
    return True, None


def restore_backup(zip_path, passphrase):
    """
    Extracts and swaps in a validated backup — REPLACES the current
    database and documents folder entirely. Caller is responsible for
    getting explicit confirmation before calling this; there is no
    further confirmation step here.

    The live app process needs restarting after this returns
    successfully — SQLAlchemy's engine/session inside the currently
    running process still has the OLD database file's connection
    open and won't pick up the swapped-in file on its own.
    """
    ok, error = validate_backup_archive(zip_path, passphrase)
    if not ok:
        return {"status": "error", "reason": error}

    db_path = _db_path()
    documents_dir = _documents_dir()

    with tempfile.TemporaryDirectory(prefix="mwl_restore_") as staging:
        with pyzipper.AESZipFile(zip_path) as zf:
            zf.setpassword(passphrase.encode("utf-8"))
            zf.extractall(path=staging)

        staged_db = os.path.join(staging, "mywealthlens.db")
        staged_docs = os.path.join(staging, "documents")

        shutil.copy2(staged_db, db_path)

        if os.path.isdir(documents_dir):
            shutil.rmtree(documents_dir)
        if os.path.isdir(staged_docs):
            shutil.copytree(staged_docs, documents_dir)
        else:
            os.makedirs(documents_dir, exist_ok=True)

    return {"status": "success"}
