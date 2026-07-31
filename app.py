"""
app.py — MyWealthLens Main Server
===================================
This is the brain of the application.

What it does:
1. Creates the Flask app
2. Connects to the SQLite database
3. Sets up Flask-Login (session management)
4. Defines all the routes (pages):
   - GET  /           → redirects to dashboard or login
   - GET  /signup     → shows signup form
   - POST /signup     → processes signup form
   - GET  /login      → shows login form
   - POST /login      → processes login form
   - GET  /dashboard  → the main page (only if logged in)
   - GET  /logout     → logs the user out

Security measures in this file:
- Passwords are hashed using bcrypt before saving
- Sessions are protected with a SECRET_KEY
- @login_required decorator blocks non-logged-in users
- Email is checked for duplicates before creating account
"""

from flask import Flask, render_template, redirect, url_for, request, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
import bcrypt

from models import db, User

# ── App setup ────────────────────────────────────────────────────────────────

app = Flask(__name__)

# SECRET_KEY encrypts the session cookie stored in the browser.
# In production this should be a long random string stored in an
# environment variable — never hardcoded. For development this is fine.
app.config["SECRET_KEY"] = "mywealthlens-dev-secret-change-in-production"

# SQLite database — stored as a single file called mywealthlens.db
# in the same folder as app.py. No separate database software needed.
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///mywealthlens.db"

# Disable a Flask-SQLAlchemy feature we don't need (saves memory)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Connect the database to our app
db.init_app(app)

# ── Login manager setup ───────────────────────────────────────────────────────

# LoginManager handles sessions — it knows who is logged in
login_manager = LoginManager()
login_manager.init_app(app)

# If someone tries to visit a protected page without logging in,
# send them to the login page instead
login_manager.login_view = "login"

# The message shown when redirected to login
login_manager.login_message = "Please log in to access MyWealthLens."


@login_manager.user_loader
def load_user(user_id):
    """
    Flask-Login calls this function on every request to check
    who is currently logged in. We look up the user by their ID
    from the session cookie and return the User object.
    """
    return User.query.get(int(user_id))


# ── Create database tables ────────────────────────────────────────────────────

# This runs once when the app starts.
# It creates the database file and tables if they don't exist yet.
# If they already exist, it does nothing — safe to run every time.
with app.app_context():
    db.create_all()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """
    Root route — the homepage.
    If already logged in → go to dashboard.
    If not logged in → go to login page.
    """
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """
    GET  → show the signup form
    POST → process the form data

    On POST we:
    1. Read name, email, password from the form
    2. Check if email is already registered
    3. Hash the password with bcrypt
    4. Save the new user to the database
    5. Log them in automatically
    6. Redirect to dashboard
    """
    # If already logged in, no need to sign up again
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name     = request.form.get("name", "").strip()
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")

        # ── Validation ──────────────────────────────────────────────────────

        if not name or not email or not password:
            flash("All fields are required.", "error")
            return render_template("signup.html")

        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("signup.html")

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("signup.html")

        # Check if email already exists in database
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash("An account with this email already exists.", "error")
            return render_template("signup.html")

        # ── Create user ─────────────────────────────────────────────────────

        # Hash the password — bcrypt turns "mypassword123" into a long
        # scrambled string like "$2b$12$eKTx..." that cannot be reversed.
        # encode() converts the string to bytes (bcrypt requires bytes)
        hashed_password = bcrypt.hashpw(
            password.encode("utf-8"),
            bcrypt.gensalt()          # gensalt() adds randomness
        ).decode("utf-8")             # decode() converts back to string for storage

        new_user = User(
            name=name,
            email=email,
            password=hashed_password
        )

        db.session.add(new_user)      # stage the new user
        db.session.commit()           # save to database

        # Log them in immediately after signup
        login_user(new_user)
        flash(f"Welcome to MyWealthLens, {name}!", "success")
        return redirect(url_for("dashboard"))

    # GET request — just show the form
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    """
    GET  → show the login form
    POST → check email + password, log in if correct

    Security note: we give the same vague error message whether
    the email doesn't exist OR the password is wrong. This prevents
    attackers from knowing which emails are registered.
    """
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        # Look up user by email
        user = User.query.filter_by(email=email).first()

        # Check password using bcrypt.checkpw()
        # This hashes the input and compares to stored hash
        if user and bcrypt.checkpw(password.encode("utf-8"), user.password.encode("utf-8")):
            login_user(user)
            flash(f"Welcome back, {user.name}!", "success")
            return redirect(url_for("dashboard"))
        else:
            # Same message for wrong email OR wrong password — intentional
            flash("Invalid email or password.", "error")

    return render_template("login.html")


@app.route("/dashboard")
@login_required   # blocks access if not logged in
def dashboard():
    """
    The main dashboard — protected page.
    @login_required means Flask-Login checks the session first.
    If not logged in, the user is redirected to /login automatically.
    """
    return render_template("dashboard.html", user=current_user)


@app.route("/logout")
@login_required
def logout():
    """Log the user out and clear their session."""
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, port=5000)


@app.route("/account")
@login_required
def account():
    """Account page — shows the user's profile details."""
    return render_template("account.html", user=current_user)


@app.route("/settings")
@login_required
def settings():
    """Settings page — app preferences."""
    return render_template("settings.html", user=current_user)
