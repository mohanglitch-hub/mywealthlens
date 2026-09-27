"""
Two-Factor Authentication — TOTP + backup codes (Sep 2026, Batch 3)
====================================================================
Standard app-based 2FA: a secret is generated, shown as a QR code for
an authenticator app (Google Authenticator, Authy, 1Password, etc.)
to scan, and the user proves they scanned it correctly by entering the
6-digit code it now generates. From then on, login requires that code
(or a one-time backup code) in addition to the password.

Uses pyotp (RFC 6238 TOTP) and qrcode+Pillow to render the
provisioning QR code as an inline base64 PNG — no external service
ever sees the secret or the QR code; everything happens on this
server, same "local-first" principle as the rest of MyWealthLens.

Backup codes are hashed with bcrypt, the same library already used
for account passwords, rather than pulling in a second hashing
dependency — hashing 10 short codes at setup time is a one-off cost,
not a per-request one.
"""
import base64
import io
import secrets

import bcrypt
import pyotp
import qrcode

APP_NAME = "MyWealthLens"
BACKUP_CODE_COUNT = 10
# 4 groups of 4 alphanumeric characters (e.g. "A1B2-C3D4-E5F6-G7H8") --
# long enough to resist guessing, short enough to type by hand if the
# user's clipboard/download isn't available when they need one.
_BACKUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def generate_secret():
    """A fresh random base32 TOTP secret — pyotp's own generator,
    which already produces something suitable for an otpauth:// URI."""
    return pyotp.random_base32()


def provisioning_uri(secret, email):
    return pyotp.totp.TOTP(secret).provisioning_uri(name=email, issuer_name=APP_NAME)


def qr_data_uri(uri):
    """Renders the otpauth:// URI as a PNG QR code and returns it as a
    data: URI, ready to drop straight into an <img src="...">  --
    nothing is written to disk and nothing leaves this server."""
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def verify_totp_code(secret, code):
    """valid_window=1 tolerates the code from one 30s step before/after
    now, the usual allowance for a little clock drift between the
    server and the user's phone."""
    if not secret or not code:
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit():
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def generate_backup_codes(n=BACKUP_CODE_COUNT):
    """Returns n plaintext codes like 'A1B2-C3D4-E5F6-G7H8'. Callers
    must hash each with hash_backup_code() before storing — these
    plaintext values are shown to the user exactly once and then
    discarded, never persisted anywhere."""
    codes = []
    for _ in range(n):
        raw = ''.join(secrets.choice(_BACKUP_CODE_ALPHABET) for _ in range(16))
        codes.append('-'.join(raw[i:i + 4] for i in range(0, 16, 4)))
    return codes


def hash_backup_code(code):
    normalized = code.strip().upper()
    return bcrypt.hashpw(normalized.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def check_backup_code(code, code_hash):
    normalized = code.strip().upper()
    try:
        return bcrypt.checkpw(normalized.encode('utf-8'), code_hash.encode('utf-8'))
    except (ValueError, TypeError):
        return False
