"""
Shared scratch-copy test isolation helper (Batch 7 CI setup, Sep 2026).

Every test script in this folder needs to run against a disposable
copy of the project -- never the real instance/mywealthlens.db -- for
the same reason explained in test_cashflow.py's own docstring:
Flask-SQLAlchemy caches its engine the first time `app` is imported,
so overriding the DB URI afterwards doesn't actually redirect it. The
only safe way to get an isolated, disposable database is to import a
completely fresh copy of `app` from a fresh copy of the project.

test_cashflow.py grew this pattern inline before there were other
test scripts to share it with. This module factors it out so the
scripts added in Batch 7 (test_account_deletion.py,
test_2fa_and_sessions.py, test_goal_shapes.py,
test_pdf_glyph_safety.py) don't each duplicate it -- test_cashflow.py
itself is left as-is rather than risk touching a working suite.

Usage, at the top of a test script's `if __name__ == "__main__":`
block:

    from _scratch_env import run_in_scratch

    def main():
        from app import app, db          # import INSIDE main(), after
        ...                              # the scratch copy is already
                                          # on sys.path -- a top-level
                                          # import would grab the real
                                          # project instead.
        return True   # or False, to signal pass/fail

    if __name__ == "__main__":
        run_in_scratch(main)
"""
import os
import sys
import shutil
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_scratch_copy(prefix="mywealthlens_test_"):
    """
    Copies the project into a fresh temp directory, excluding heavy,
    irrelevant, or state-carrying folders. `instance/` is deliberately
    excluded -- app.py recreates it automatically (fresh secret key,
    fresh empty SQLite database, rebuilt via its own Alembic-based
    _bootstrap_schema()) the moment it's imported from the copy, which
    is exactly the isolated blank slate every one of these scripts
    needs.
    """
    scratch = tempfile.mkdtemp(prefix=prefix)
    shutil.copytree(
        REPO_ROOT, scratch, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            ".venv", ".git", "__pycache__", "instance", "*.pyc",
            "node_modules", ".pytest_cache", "tests",
        ),
    )
    return scratch


def run_in_scratch(main_func):
    """
    Runs main_func() inside a fresh scratch copy of the project (cwd
    changed there, that copy put first on sys.path), then cleans up
    and exits: status 0 if main_func() returned a truthy value
    (or raised nothing), 1 otherwise -- including on any uncaught
    AssertionError, so a plain `assert` inside main_func() is enough
    to fail the script the way every script in this folder already
    assumes.
    """
    scratch = make_scratch_copy()
    ok = False
    try:
        sys.path.insert(0, scratch)
        os.chdir(scratch)
        ok = main_func()
        if ok is None:
            ok = True  # a script that never returns False and doesn't raise = pass
    finally:
        os.chdir(REPO_ROOT)
        shutil.rmtree(scratch, ignore_errors=True)
    sys.exit(0 if ok else 1)
