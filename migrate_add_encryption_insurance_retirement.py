"""
Migration Script — Document Vault client-side encryption, Insurance &
Retirement Centres (Sep 2026)
=========================================================================
Extends the same encryption feature already live for the Wealth Centre
(see migrate_add_encryption.py) to Insurance Centre and Retirement
Centre documents.

New columns:

  insurance_document table:
    - iv            VARCHAR(64)    -- per-file base64 IV, NOT secret
    - is_encrypted  BOOLEAN DEFAULT 0

  retirement_document table:
    - iv            VARCHAR(64)    -- per-file base64 IV, NOT secret
    - is_encrypted  BOOLEAN DEFAULT 0

None of the new columns hold anything secret on their own — the
passphrase and the key derived from it never touch this server at
all (see static/js/mwl-crypto.js). is_encrypted distinguishes
documents uploaded before this feature existed (plain bytes on disk,
is_encrypted=0) from ones uploaded after a user sets up their
passphrase (ciphertext on disk, is_encrypted=1) — both keep working
side by side; nothing forces a re-upload of existing documents.

Safe to run multiple times.

Run from project root: py migrate_add_encryption_insurance_retirement.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db


def _add_columns(conn, table, columns):
    existing_cols = {
        row[1] for row in
        conn.execute(db.text(f"PRAGMA table_info({table})"))
    }
    for col_name, col_type in columns:
        if col_name in existing_cols:
            print(f"  ✓ {table}.{col_name} (already present)")
        else:
            conn.execute(db.text(
                f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"
            ))
            conn.commit()
            print(f"  ✓ {table}.{col_name} (added)")


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Document Vault client-side encryption — Insurance & Retirement")
        print("=" * 60)

        with db.engine.connect() as conn:
            _add_columns(conn, "insurance_document", [
                ("iv",           "VARCHAR(64)"),
                ("is_encrypted", "BOOLEAN DEFAULT 0"),
            ])
            _add_columns(conn, "retirement_document", [
                ("iv",           "VARCHAR(64)"),
                ("is_encrypted", "BOOLEAN DEFAULT 0"),
            ])

        print(f"\n{'=' * 60}")
        print("Migration complete.")
        print("\nExisting documents are unaffected — they keep working")
        print("exactly as before (is_encrypted=0). Only documents")
        print("uploaded AFTER a user sets up their encryption")
        print("passphrase will be encrypted (is_encrypted=1).")
        print("\nNext steps:")
        print("  1. Restart your Flask server")
        print(f"{'=' * 60}")
        print("\n✅ Migration complete")
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
