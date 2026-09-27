# Forgot Password — one-time email setup

This is what makes the "Forgot Password" flow actually send a reset
email — until you do this, the app still works exactly as before
(the generic "if an account exists, a reset link has been sent"
message still shows, nothing crashes), it just quietly doesn't send
anything, and logs why in the console. There's no rush to set this up
before other work.

Recommended for now: **Gmail SMTP with an "app password"** — free,
works in a few minutes, and doesn't require signing up for a separate
service. It sends from your own Gmail address. If MyWealthLens later
has real signed-up users, a proper transactional email service
(Resend, SendGrid, etc.) would give better deliverability and let
email come from a `no-reply@mywealthlens...` address instead of your
personal Gmail — worth revisiting then, not needed now.

## 1. Generate a Gmail "app password"

An app password is a 16-character code Google generates specifically
for one app to use — separate from your real Gmail password, and you
can revoke it any time without changing your actual password.

1. Your Google Account needs **2-Step Verification turned on** first
   (Google requires this before it'll offer app passwords) — if you
   don't already have it on: https://myaccount.google.com/security
2. Go to https://myaccount.google.com/apppasswords
3. Give it any name (e.g. "MyWealthLens") and click **Create**.
4. Google shows you a 16-character password (with spaces, e.g.
   `abcd efgh ijkl mnop`) — copy it now, you won't be able to see it
   again (though you can always generate a new one if you lose it).

## 2. Tell MyWealthLens about it

Create a new file at `instance/email_config.json` (same folder as
your `mywealthlens.db` and `secret_key.txt` — this folder is already
excluded from git, so this file is never committed or pushed):

```json
{
  "smtp_user": "youraddress@gmail.com",
  "smtp_app_password": "abcdefghijklmnop",
  "from_name": "MyWealthLens"
}
```

- `smtp_user` — your full Gmail address (the one you generated the app
  password for).
- `smtp_app_password` — the 16-character code from step 1, with or
  without the spaces (both work).
- `from_name` — optional, defaults to "MyWealthLens" if you leave it out.

## 3. Restart the app

```powershell
py app.py
```

Nothing else changes — the "Forgot password?" link on the login page
was already there. Once this file exists, clicking it and submitting
your email will actually send a real reset link, valid for 60 minutes
and usable once.

## Testing it

1. Go to `/forgot-password`, enter an account's real email.
2. Check that inbox — should arrive within a few seconds.
3. Click the link, set a new password, log in with it.

If nothing arrives, check the console MyWealthLens is running in —
any send failure (wrong app password, 2-Step Verification not
actually on, etc.) is logged there as a warning, without breaking
anything else.

## Alternative: environment variables instead of a file

If you'd rather not keep credentials in a file (e.g. once you're
hosting this somewhere that has its own secrets manager), set these
environment variables instead — they're checked first, before the
`instance/email_config.json` file:

```powershell
$env:MWL_SMTP_USER = "youraddress@gmail.com"
$env:MWL_SMTP_APP_PASSWORD = "abcdefghijklmnop"
$env:MWL_SMTP_FROM_NAME = "MyWealthLens"   # optional
```

(Only lasts for that PowerShell window, same as `DATABASE_URL` in
`README_POSTGRES_MIGRATION.md` — see that file if you want to make an
environment variable permanent.)
