"""
auth.py — User authentication, Stripe billing, and session management
for VT Property Sales.

Uses PostgreSQL (via DATABASE_URL env var on Railway) so user accounts
survive redeploys. Falls back to SQLite for local development.
"""

import os
import hashlib
import hmac
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path

import stripe
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, jsonify
)

# ── Stripe config ─────────────────────────────────────────────────────────────
stripe.api_key         = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_PRICE_ID        = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

TRIAL_DAYS = 14

# ── Database connection ───────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")

def _using_postgres():
    return DATABASE_URL.startswith("postgres")

def get_db():
    """Return a DB connection — PostgreSQL on Railway, SQLite locally."""
    if _using_postgres():
        import psycopg2
        import psycopg2.extras
        url = DATABASE_URL
        # Railway sometimes uses postgres:// but psycopg2 needs postgresql://
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    else:
        import sqlite3
        db_path = Path(__file__).with_name("users.db")
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

def _ph(s):
    """Placeholder: %s for postgres, ? for sqlite."""
    return "%s" if _using_postgres() else "?"

def init_db():
    """Create the users table if it doesn't exist."""
    if _using_postgres():
        sql = """
            CREATE TABLE IF NOT EXISTS users (
                id                      SERIAL PRIMARY KEY,
                email                   TEXT UNIQUE NOT NULL,
                password_hash           TEXT NOT NULL,
                created_at              TEXT NOT NULL,
                trial_ends_at           TEXT,
                subscription_status     TEXT DEFAULT 'trial',
                stripe_customer_id      TEXT,
                stripe_subscription_id  TEXT,
                reset_token             TEXT,
                reset_token_expires     TEXT
            )
        """
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(sql)
            conn.commit()
        finally:
            conn.close()
    else:
        import sqlite3
        db_path = Path(__file__).with_name("users.db")
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                email                   TEXT UNIQUE NOT NULL,
                password_hash           TEXT NOT NULL,
                created_at              TEXT NOT NULL,
                trial_ends_at           TEXT,
                subscription_status     TEXT DEFAULT 'trial',
                stripe_customer_id      TEXT,
                stripe_subscription_id  TEXT,
                reset_token             TEXT,
                reset_token_expires     TEXT
            )
        """)
        conn.commit()
        conn.close()

# ── DB helpers ────────────────────────────────────────────────────────────────
def db_fetchone(sql, params=()):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def db_execute(sql, params=()):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()

def db_fetchall(sql, params=()):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def _q(sql):
    """Convert SQLite ? placeholders to %s for PostgreSQL."""
    if _using_postgres():
        return sql.replace("?", "%s")
    return sql

# ── Password helpers ──────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{h}"

def check_password(stored: str, provided: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        return hmac.compare_digest(
            h, hashlib.sha256((salt + provided).encode()).hexdigest()
        )
    except Exception:
        return False

# ── Access check ──────────────────────────────────────────────────────────────
def user_has_access(user) -> bool:
    status = user["subscription_status"]
    if status == "active":
        return True
    if status == "trial":
        try:
            trial_end = datetime.fromisoformat(user["trial_ends_at"])
            if trial_end.tzinfo is None:
                trial_end = trial_end.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) < trial_end
        except Exception:
            return False
    return False

def days_left_in_trial(user) -> int:
    try:
        trial_end = datetime.fromisoformat(user["trial_ends_at"])
        if trial_end.tzinfo is None:
            trial_end = trial_end.replace(tzinfo=timezone.utc)
        delta = trial_end - datetime.now(timezone.utc)
        return max(0, delta.days)
    except Exception:
        return 0

# ── Blueprint ─────────────────────────────────────────────────────────────────
auth_bp = Blueprint("auth", __name__)

# ── Login ─────────────────────────────────────────────────────────────────────
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = db_fetchone(_q("SELECT * FROM users WHERE email = ?"), (email,))
        if not user or not check_password(user["password_hash"], password):
            flash("Invalid email or password.", "error")
            return render_template("login.html")
        if not user_has_access(user):
            flash("Your trial has expired. Please subscribe to continue.", "warning")
            return redirect(url_for("auth.subscribe", email=email))
        session["user_id"]    = user["id"]
        session["user_email"] = user["email"]
        return redirect(url_for("index"))
    return render_template("login.html")

# ── Signup ────────────────────────────────────────────────────────────────────
@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email   = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")

        if not email or "@" not in email:
            flash("Please enter a valid email address.", "error")
            return render_template("signup.html")
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("signup.html")
        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("signup.html")

        trial_end = datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)
        try:
            db_execute(_q("""
                INSERT INTO users
                (email, password_hash, created_at, trial_ends_at, subscription_status)
                VALUES (?, ?, ?, ?, 'trial')
            """), (email, hash_password(password),
                   datetime.now(timezone.utc).isoformat(),
                   trial_end.isoformat()))
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                flash("An account with that email already exists.", "error")
            else:
                flash(f"Error creating account: {str(e)}", "error")
            return render_template("signup.html")

        user = db_fetchone(_q("SELECT * FROM users WHERE email = ?"), (email,))
        session["user_id"]    = user["id"]
        session["user_email"] = user["email"]
        flash(f"Welcome! You have a {TRIAL_DAYS}-day free trial.", "success")
        return redirect(url_for("index"))
    return render_template("signup.html")

# ── Logout ────────────────────────────────────────────────────────────────────
@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))

# ── Subscribe ─────────────────────────────────────────────────────────────────
@auth_bp.route("/subscribe")
def subscribe():
    email = request.args.get("email", session.get("user_email", ""))
    return render_template("subscribe.html",
                           email=email,
                           publishable_key=STRIPE_PUBLISHABLE_KEY)

@auth_bp.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    email = request.form.get("email", session.get("user_email", ""))
    try:
        user = db_fetchone(_q("SELECT * FROM users WHERE email = ?"), (email,))
        customer_id = user["stripe_customer_id"] if user else None
        if not customer_id:
            customer = stripe.Customer.create(email=email)
            customer_id = customer.id
            if user:
                db_execute(_q(
                    "UPDATE users SET stripe_customer_id = ? WHERE email = ?"
                ), (customer_id, email))

        checkout = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            mode="subscription",
            success_url=request.host_url + "subscribe/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.host_url + "subscribe",
        )
        return redirect(checkout.url, code=303)
    except Exception as e:
        flash(f"Payment error: {str(e)}", "error")
        return redirect(url_for("auth.subscribe"))

@auth_bp.route("/subscribe/success")
def subscribe_success():
    session_id = request.args.get("session_id")
    try:
        checkout        = stripe.checkout.Session.retrieve(session_id)
        customer_id     = checkout.customer
        subscription_id = checkout.subscription
        email           = checkout.customer_details.email

        db_execute(_q("""
            UPDATE users SET
                subscription_status    = 'active',
                stripe_customer_id     = ?,
                stripe_subscription_id = ?
            WHERE email = ?
        """), (customer_id, subscription_id, email))

        user = db_fetchone(_q("SELECT * FROM users WHERE email = ?"), (email,))
        if user:
            session["user_id"]    = user["id"]
            session["user_email"] = user["email"]

        flash("Subscription activated! Welcome to VT Property Sales.", "success")
        return redirect(url_for("index"))
    except Exception as e:
        flash(f"Could not confirm subscription: {str(e)}", "error")
        return redirect(url_for("auth.subscribe"))

# ── Stripe Webhook ────────────────────────────────────────────────────────────
@auth_bp.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig     = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return jsonify({"error": "Invalid signature"}), 400

    if event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        db_execute(_q(
            "UPDATE users SET subscription_status = 'cancelled' WHERE stripe_subscription_id = ?"
        ), (sub["id"],))

    elif event["type"] == "customer.subscription.updated":
        sub    = event["data"]["object"]
        status = "active" if sub["status"] == "active" else "cancelled"
        db_execute(_q(
            "UPDATE users SET subscription_status = ? WHERE stripe_subscription_id = ?"
        ), (status, sub["id"]))

    elif event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        db_execute(_q(
            "UPDATE users SET subscription_status = 'past_due' WHERE stripe_customer_id = ?"
        ), (invoice["customer"],))

    return jsonify({"status": "ok"})

# ── Account page ──────────────────────────────────────────────────────────────
@auth_bp.route("/account")
def account():
    if "user_id" not in session:
        return redirect(url_for("auth.login"))
    user = db_fetchone(_q("SELECT * FROM users WHERE id = ?"), (session["user_id"],))
    if not user:
        session.clear()
        return redirect(url_for("auth.login"))
    trial_days = days_left_in_trial(user) if user["subscription_status"] == "trial" else 0
    return render_template("account.html", user=user, trial_days=trial_days)

@auth_bp.route("/cancel-subscription", methods=["POST"])
def cancel_subscription():
    if "user_id" not in session:
        return redirect(url_for("auth.login"))
    user = db_fetchone(_q("SELECT * FROM users WHERE id = ?"), (session["user_id"],))
    if user and user["stripe_subscription_id"]:
        try:
            stripe.Subscription.modify(
                user["stripe_subscription_id"], cancel_at_period_end=True
            )
            flash("Your subscription will cancel at the end of the billing period.", "info")
        except Exception as e:
            flash(f"Error cancelling: {str(e)}", "error")
    return redirect(url_for("auth.account"))

# ── Admin actions ────────────────────────────────────────────────────────────
@auth_bp.route("/admin/reset-trial", methods=["POST"])
def admin_reset_trial():
    if session.get("user_email") != ADMIN_EMAIL:
        return redirect(url_for("auth.login"))
    user_id = request.form.get("user_id")
    trial_end = (datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)).isoformat()
    db_execute(_q("""
        UPDATE users SET subscription_status = 'trial', trial_ends_at = ?
        WHERE id = ?
    """), (trial_end, user_id))
    flash("Trial reset successfully.", "success")
    return redirect(url_for("auth.admin"))

@auth_bp.route("/admin/mark-active", methods=["POST"])
def admin_mark_active():
    if session.get("user_email") != ADMIN_EMAIL:
        return redirect(url_for("auth.login"))
    user_id = request.form.get("user_id")
    db_execute(_q("""
        UPDATE users SET subscription_status = 'active'
        WHERE id = ?
    """), (user_id,))
    flash("User marked as active.", "success")
    return redirect(url_for("auth.admin"))

# ── Password reset ────────────────────────────────────────────────────────────
@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email   = request.form.get("email", "").strip().lower()
        token   = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        db_execute(_q(
            "UPDATE users SET reset_token = ?, reset_token_expires = ? WHERE email = ?"
        ), (token, expires, email))
        reset_url = request.host_url + f"reset-password?token={token}"
        flash(f"Password reset link: {reset_url}", "info")
        return redirect(url_for("auth.login"))
    return render_template("forgot_password.html")

@auth_bp.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    token = request.args.get("token") or request.form.get("token")
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")
        if password != confirm or len(password) < 8:
            flash("Passwords must match and be at least 8 characters.", "error")
            return render_template("reset_password.html", token=token)
        user = db_fetchone(_q("SELECT * FROM users WHERE reset_token = ?"), (token,))
        if not user:
            flash("Invalid or expired reset link.", "error")
            return redirect(url_for("auth.login"))
        expires = datetime.fromisoformat(user["reset_token_expires"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires:
            flash("Reset link has expired. Please request a new one.", "error")
            return redirect(url_for("auth.forgot_password"))
        db_execute(_q("""
            UPDATE users SET password_hash = ?,
                             reset_token = NULL,
                             reset_token_expires = NULL
            WHERE id = ?
        """), (hash_password(password), user["id"]))
        flash("Password updated. Please log in.", "success")
        return redirect(url_for("auth.login"))
    return render_template("reset_password.html", token=token)

# ── Admin ─────────────────────────────────────────────────────────────────────
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "appraiservt@gmail.com")

@auth_bp.route("/admin")
def admin():
    if session.get("user_email") != ADMIN_EMAIL:
        return redirect(url_for("auth.login"))
    users = db_fetchall("SELECT * FROM users ORDER BY created_at DESC")
    return render_template("admin.html", users=users)
