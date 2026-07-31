"""
models.py — Database Models
============================
This file defines what our database looks like.

Think of it like designing a table in Excel:
- We have a "User" table
- Each row is one person who signed up
- The columns are: id, name, email, password

We use SQLAlchemy which lets us write Python instead of
raw database commands. Flask handles the rest automatically.

Password safety:
- We NEVER store the actual password
- We store a "hash" — a scrambled version using bcrypt
- Even if someone steals the database, they cannot read passwords
"""

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

# db is the database connection object.
# We create it here and import it into app.py
db = SQLAlchemy()


class User(UserMixin, db.Model):
    """
    The User table — one row per registered user.

    UserMixin gives us helper methods that Flask-Login needs,
    like is_authenticated, is_active, get_id etc.
    We get all of these for free just by inheriting UserMixin.
    """

    __tablename__ = "user"

    # Auto-incrementing unique ID for each user (1, 2, 3...)
    id = db.Column(db.Integer, primary_key=True)

    # Full name — max 150 characters
    name = db.Column(db.String(150), nullable=False)

    # Email — must be unique, used for login
    email = db.Column(db.String(150), unique=True, nullable=False)

    # Password hash — we store the scrambled version, never plain text
    # 256 characters is enough for bcrypt hashes
    password = db.Column(db.String(256), nullable=False)

    def __repr__(self):
        return f"<User {self.email}>"
