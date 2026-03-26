"""
auth.py — User authentication, Stripe billing, and session management
for VT Property Sales.

Tables:
  users(id, email, password_hash, created_at,
        trial_ends_at, subscription_status, stripe_customer_id,
        stripe_subscription_id, reset_token, reset_token_expires)
"""

import sqlite3
import hashlib
import hmac
import os
import secrets
import smtplib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email.mime.text import MIMEText

import stripe
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, jsonify, current_app
)

# ── Stripe config ────────────────────────────────────────────────────────────
stripe.api_key          = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY  = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_PRICE_ID         = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET   = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

TRIAL_DAYS = 14

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = Path(__file__).with_name("users.db")

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
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
    """Return True if user is in trial or has active subscription."""
    status = user["subscription_status"]
    if status == "active":
        return True
    if status == "trial":
        trial_end = datetime.fromisoformat(user["trial_ends_at"])
        return datetime.now(timezone.utc) < trial_end
    return False

def days_left_in_trial(user) -> int:
    try:
        trial_end = datetime.fromisoformat(user["trial_ends_at"])
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
        with get_db() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE email = ?", (email,)
            ).fetchone()
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
        email    = request.form.get("email", "").strip().lower()
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
            with get_db() as conn:
                conn.execute(
                    """INSERT INTO users
                       (email, password_hash, created_at, trial_ends_at, subscription_status)
                       VALUES (?, ?, ?, ?, 'trial')""",
                    (email, hash_password(password),
                     datetime.now(timezone.utc).isoformat(),
                     trial_end.isoformat())
                )
                conn.commit()
                user = conn.execute(
                    "SELECT * FROM users WHERE email = ?", (email,)
                ).fetchone()
        except sqlite3.IntegrityError:
            flash("An account with that email already exists.", "error")
            return render_template("signup.html")

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

# ── Subscribe (Stripe Checkout) ───────────────────────────────────────────────
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
        # Find or create Stripe customer
        with get_db() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE email = ?", (email,)
            ).fetchone()

        customer_id = user["stripe_customer_id"] if user else None
        if not customer_id:
            customer = stripe.Customer.create(email=email)
            customer_id = customer.id
            if user:
                with get_db() as conn:
                    conn.execute(
                        "UPDATE users SET stripe_customer_id = ? WHERE email = ?",
                        (customer_id, email)
                    )
                    conn.commit()

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
        checkout = stripe.checkout.Session.retrieve(session_id)
        customer_id   = checkout.customer
        subscription_id = checkout.subscription
        email = checkout.customer_details.email

        with get_db() as conn:
            conn.execute("""
                UPDATE users SET
                    subscription_status    = 'active',
                    stripe_customer_id     = ?,
                    stripe_subscription_id = ?
                WHERE email = ?
            """, (customer_id, subscription_id, email))
            conn.commit()
            user = conn.execute(
                "SELECT * FROM users WHERE email = ?", (email,)
            ).fetchone()

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
        event = stripe.Webhook.construct_event(
            payload, sig, STRIPE_WEBHOOK_SECRET
        )
    except Exception:
        return jsonify({"error": "Invalid signature"}), 400

    if event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET subscription_status = 'cancelled' WHERE stripe_subscription_id = ?",
                (sub["id"],)
            )
            conn.commit()

    elif event["type"] == "customer.subscription.updated":
        sub    = event["data"]["object"]
        status = "active" if sub["status"] == "active" else "cancelled"
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET subscription_status = ? WHERE stripe_subscription_id = ?",
                (status, sub["id"])
            )
            conn.commit()

    elif event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET subscription_status = 'past_due' WHERE stripe_customer_id = ?",
                (invoice["customer"],)
            )
            conn.commit()

    return jsonify({"status": "ok"})

# ── Account page ──────────────────────────────────────────────────────────────
@auth_bp.route("/account")
def account():
    if "user_id" not in session:
        return redirect(url_for("auth.login"))
    with get_db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE id = ?", (session["user_id"],)
        ).fetchone()
    if not user:
        session.clear()
        return redirect(url_for("auth.login"))
    trial_days = days_left_in_trial(user) if user["subscription_status"] == "trial" else 0
    return render_template("account.html", user=user, trial_days=trial_days)

@auth_bp.route("/cancel-subscription", methods=["POST"])
def cancel_subscription():
    if "user_id" not in session:
        return redirect(url_for("auth.login"))
    with get_db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE id = ?", (session["user_id"],)
        ).fetchone()
    if user and user["stripe_subscription_id"]:
        try:
            stripe.Subscription.modify(
                user["stripe_subscription_id"],
                cancel_at_period_end=True
            )
            flash("Your subscription will cancel at the end of the billing period.", "info")
        except Exception as e:
            flash(f"Error cancelling: {str(e)}", "error")
    return redirect(url_for("auth.account"))

# ── Password reset ────────────────────────────────────────────────────────────
@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET reset_token = ?, reset_token_expires = ? WHERE email = ?",
                (token, expires, email)
            )
            conn.commit()
        # In production configure SMTP — for now just show the link
        reset_url = request.host_url + f"reset-password?token={token}"
        flash(f"Password reset link (send this to yourself for now): {reset_url}", "info")
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
        with get_db() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE reset_token = ?", (token,)
            ).fetchone()
            if not user:
                flash("Invalid or expired reset link.", "error")
                return redirect(url_for("auth.login"))
            expires = datetime.fromisoformat(user["reset_token_expires"])
            if datetime.now(timezone.utc) > expires:
                flash("Reset link has expired. Please request a new one.", "error")
                return redirect(url_for("auth.forgot_password"))
            conn.execute(
                "UPDATE users SET password_hash = ?, reset_token = NULL, reset_token_expires = NULL WHERE id = ?",
                (hash_password(password), user["id"])
            )
            conn.commit()
        flash("Password updated. Please log in.", "success")
        return redirect(url_for("auth.login"))
    return render_template("reset_password.html", token=token)

# ── Admin page ────────────────────────────────────────────────────────────────
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "appraiservt@gmail.com")

@auth_bp.route("/admin")
def admin():
    if session.get("user_email") != ADMIN_EMAIL:
        return redirect(url_for("auth.login"))
    with get_db() as conn:
        users = conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC"
        ).fetchall()
    return render_template("admin.html", users=users)
