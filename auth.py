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
    url_for, session, flash, jsonify, make_response
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
    """Create the users and subscription_plans tables if they don't exist."""
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
        plans_sql = """
            CREATE TABLE IF NOT EXISTS subscription_plans (
                key             TEXT PRIMARY KEY,
                label           TEXT NOT NULL,
                display_price   TEXT NOT NULL,
                stripe_price_id TEXT,
                billing_interval TEXT NOT NULL,
                interval_count  INTEGER NOT NULL,
                sort_order      INTEGER NOT NULL,
                active          BOOLEAN DEFAULT TRUE
            )
        """
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(sql)
            # Add plan_key column if missing
            cur.execute("""
                ALTER TABLE users ADD COLUMN plan_key TEXT DEFAULT 'plan_monthly'
            """)
        except Exception:
            conn.rollback()
        try:
            cur = conn.cursor()
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name TEXT")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT")
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cur = conn.cursor()
            cur.execute(plans_sql)
            # Add stripe_price_id column if missing (for tables created before it existed)
            try:
                cur.execute("ALTER TABLE subscription_plans ADD COLUMN IF NOT EXISTS stripe_price_id TEXT")
            except Exception:
                conn.rollback()
                cur = conn.cursor()
            # Seed default plans
            cur.execute("""
                INSERT INTO subscription_plans (key, label, display_price, stripe_price_id, billing_interval, interval_count, sort_order, active)
                VALUES
                    ('plan_monthly',  'Monthly',  '$24.95', '', 'month', 1,  1, TRUE),
                    ('plan_3month',   '3-Month',  '$21.95', '', 'month', 3,  2, TRUE),
                    ('plan_6month',   '6-Month',  '$18.95', '', 'month', 6,  3, TRUE),
                    ('plan_12month',  '12-Month', '$16.95', '', 'month', 12, 4, TRUE)
                ON CONFLICT (key) DO NOTHING
            """)
            conn.commit()
        except Exception:
            conn.rollback()
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
        # Add plan_key column if missing
        try:
            conn.execute("ALTER TABLE users ADD COLUMN plan_key TEXT DEFAULT 'plan_monthly'")
        except Exception:
            pass  # column already exists
        # Add name/phone columns if missing
        for col in ("first_name TEXT", "last_name TEXT", "phone TEXT"):
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except Exception:
                pass  # column already exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscription_plans (
                key             TEXT PRIMARY KEY,
                label           TEXT NOT NULL,
                display_price   TEXT NOT NULL,
                stripe_price_id TEXT,
                billing_interval TEXT NOT NULL,
                interval_count  INTEGER NOT NULL,
                sort_order      INTEGER NOT NULL,
                active          BOOLEAN DEFAULT 1
            )
        """)
        # Add stripe_price_id column if missing (for tables created before it existed)
        try:
            conn.execute("ALTER TABLE subscription_plans ADD COLUMN stripe_price_id TEXT")
        except Exception:
            pass  # column already exists
        # Seed default plans
        for row in [
            ('plan_monthly',  'Monthly',  '$24.95', '', 'month', 1,  1, 1),
            ('plan_3month',   '3-Month',  '$21.95', '', 'month', 3,  2, 1),
            ('plan_6month',   '6-Month',  '$18.95', '', 'month', 6,  3, 1),
            ('plan_12month',  '12-Month', '$16.95', '', 'month', 12, 4, 1),
        ]:
            conn.execute(
                "INSERT OR IGNORE INTO subscription_plans (key, label, display_price, stripe_price_id, billing_interval, interval_count, sort_order, active) VALUES (?,?,?,?,?,?,?,?)",
                row,
            )
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
    if status in ("active", "trialing"):
        return True
    if status == "trial":  # legacy no-card trial
        try:
            val = user["trial_ends_at"]
            if val is None:
                return False
            trial_end = val if not isinstance(val, str) else datetime.fromisoformat(val)
            if trial_end.tzinfo is None:
                trial_end = trial_end.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) < trial_end
        except Exception:
            return False
    return False

def days_left_in_trial(user) -> int:
    try:
        val = user["trial_ends_at"]
        if val is None:
            return 0
        # psycopg2 returns datetime objects; SQLite returns strings
        if isinstance(val, str):
            trial_end = datetime.fromisoformat(val)
        else:
            trial_end = val  # already a datetime object from psycopg2
        if trial_end.tzinfo is None:
            trial_end = trial_end.replace(tzinfo=timezone.utc)
        delta = trial_end - datetime.now(timezone.utc)
        total_seconds = delta.total_seconds()
        if total_seconds <= 0:
            return 0
        import math
        return math.ceil(total_seconds / 86400)
    except Exception:
        return 0

def is_in_trial(user) -> bool:
    return user["subscription_status"] in ("trial", "trialing")

# ── Plan helpers ──────────────────────────────────────────────────────────────
def get_active_plans():
    """Return all active subscription plans, ordered by sort_order."""
    return db_fetchall("SELECT * FROM subscription_plans WHERE active = " + ("TRUE" if _using_postgres() else "1") + " ORDER BY sort_order")

def get_plan(key):
    """Return a single plan by key."""
    return db_fetchone(_q("SELECT * FROM subscription_plans WHERE key = ?"), (key,))

# ── Blueprint ─────────────────────────────────────────────────────────────────
auth_bp = Blueprint("auth", __name__)

# ── Login ─────────────────────────────────────────────────────────────────────
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        # ── Admin bypass: only active when ADMIN_PASSWORD is set in environment ─
        # Railway does NOT have this variable so bypass is disabled in production.
        admin_password = os.environ.get("ADMIN_PASSWORD")  # None if not set
        admin_email    = os.environ.get("ADMIN_EMAIL", "appraiservt@gmail.com").lower()
        if admin_password and email == admin_email and password == admin_password:
            session["user_id"]    = 0
            session["user_email"] = email
            resp = make_response(redirect(url_for("index")))
            resp.set_cookie("remembered_email", email,
                            max_age=60*60*24*365,
                            httponly=False, samesite="Lax")
            return resp
        # ─────────────────────────────────────────────────────────────────────

        user = db_fetchone(_q("SELECT * FROM users WHERE email = ?"), (email,))
        if not user or not check_password(user["password_hash"], password):
            flash("Invalid email or password.", "error")
            return render_template("login.html", remembered_email=email)
        # Account created but Stripe card step was never completed
        if user["subscription_status"] == "pending_payment":
            flash("Please complete your payment setup to activate your account.", "warning")
            return redirect(url_for("auth.subscribe", email=email))
        if not user_has_access(user):
            flash("Your trial has expired. Please subscribe to continue.", "warning")
            return redirect(url_for("auth.subscribe", email=email))
        session["user_id"]    = user["id"]
        session["user_email"] = user["email"]
        # Set a long-lived cookie to remember the email for next login
        resp = make_response(redirect(url_for("index")))
        resp.set_cookie("remembered_email", email,
                        max_age=60*60*24*365,  # 1 year
                        httponly=False, samesite="Lax")
        return resp
    # Pre-fill email from cookie if available
    remembered_email = request.cookies.get("remembered_email", "")
    return render_template("login.html", remembered_email=remembered_email)

# ── Signup ────────────────────────────────────────────────────────────────────
@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    """
    Step 1 of 2: collect email + password, create account in DB with
    status='pending_payment', then redirect to Stripe Checkout.
    Stripe collects the card with a 14-day free trial — no charge until day 15.
    On success, webhook (or /subscribe/success) sets status to 'trialing'.
    """
    if request.method == "POST":
        email      = request.form.get("email", "").strip().lower()
        password   = request.form.get("password", "")
        confirm    = request.form.get("confirm", "")
        plan_key   = request.form.get("plan_key", "plan_monthly")
        first_name = request.form.get("first_name", "").strip()
        last_name  = request.form.get("last_name", "").strip()
        phone      = request.form.get("phone", "").strip() or None

        plans = get_active_plans()

        if not first_name or not last_name:
            flash("First name and last name are required.", "error")
            return render_template("signup.html", plans=plans)
        if not email or "@" not in email:
            flash("Please enter a valid email address.", "error")
            return render_template("signup.html", plans=plans)
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("signup.html", plans=plans)
        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("signup.html", plans=plans)

        # Check for existing account
        existing = db_fetchone(_q("SELECT * FROM users WHERE email = ?"), (email,))
        if existing:
            flash("An account with that email already exists. Please log in.", "error")
            return render_template("signup.html", plans=plans)

        # Look up the selected plan
        signup_plan = get_plan(plan_key)
        if not signup_plan:
            signup_plan = get_plan("plan_monthly")
            plan_key = "plan_monthly"
        signup_price_id = signup_plan["stripe_price_id"] if signup_plan else ""
        if not signup_price_id:
            # Fall back to env var for backwards compatibility during migration
            signup_price_id = STRIPE_PRICE_ID
        if not signup_price_id:
            flash("This plan is not yet configured. Please try another plan or contact support.", "error")
            return render_template("signup.html", plans=plans)

        # Create account with pending_payment status
        trial_end = datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)
        try:
            db_execute(_q("""
                INSERT INTO users
                (email, password_hash, created_at, trial_ends_at, subscription_status, plan_key, first_name, last_name, phone)
                VALUES (?, ?, ?, ?, 'pending_payment', ?, ?, ?, ?)
            """), (email, hash_password(password),
                   datetime.now(timezone.utc).isoformat(),
                   trial_end.isoformat(),
                   plan_key, first_name, last_name, phone))
        except Exception as e:
            flash(f"Error creating account: {str(e)}", "error")
            return render_template("signup.html", plans=plans)

        # Store email in session for the checkout flow
        session["pending_email"] = email

        # Create Stripe customer and checkout session with 14-day trial
        try:
            customer = stripe.Customer.create(email=email)
            db_execute(_q(
                "UPDATE users SET stripe_customer_id = ? WHERE email = ?"
            ), (customer.id, email))

            charge_date = (datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)).strftime("%B %d, %Y")

            checkout = stripe.checkout.Session.create(
                customer=customer.id,
                payment_method_types=["card"],
                line_items=[{"price": signup_price_id, "quantity": 1}],
                mode="subscription",
                subscription_data={"trial_period_days": TRIAL_DAYS, "metadata": {"plan_key": plan_key}},
                metadata={"plan_key": plan_key},
                success_url=request.host_url + "signup/success?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=request.host_url + "signup?cancelled=1",
            )
            return redirect(checkout.url, code=303)
        except Exception as e:
            # If Stripe fails, delete the pending account and show error
            db_execute(_q("DELETE FROM users WHERE email = ?"), (email,))
            flash(f"Payment setup error: {str(e)}", "error")
            return render_template("signup.html", plans=plans)

    cancelled = request.args.get("cancelled")
    if cancelled:
        # User cancelled Stripe checkout — clean up pending account
        pending = session.pop("pending_email", None)
        if pending:
            db_execute(_q(
                "DELETE FROM users WHERE email = ? AND subscription_status = 'pending_payment'"
            ), (pending,))
        flash("Signup cancelled. No account was created and your card was not charged.", "info")

    plans = get_active_plans()
    return render_template("signup.html", plans=plans)


@auth_bp.route("/signup/success")
def signup_success():
    """Stripe redirects here after card is collected for the free trial."""
    session_id = request.args.get("session_id")
    try:
        checkout        = stripe.checkout.Session.retrieve(session_id)
        customer_id     = checkout.customer
        subscription_id = checkout.subscription
        email           = checkout.customer_details.email
        plan_key        = checkout.metadata["plan_key"] if (checkout.metadata and "plan_key" in checkout.metadata) else "plan_monthly"

        db_execute(_q("""
            UPDATE users SET
                subscription_status    = 'trialing',
                stripe_customer_id     = ?,
                stripe_subscription_id = ?,
                plan_key               = ?
            WHERE email = ?
        """), (customer_id, subscription_id, plan_key, email))

        user = db_fetchone(_q("SELECT * FROM users WHERE email = ?"), (email,))
        if user:
            session.permanent = True
            session["user_id"]    = user["id"]
            session["user_email"] = user["email"]
            resp = make_response(redirect(url_for("index")))
            resp.set_cookie("remembered_email", email,
                            max_age=60*60*24*365, httponly=False, samesite="Lax")
            return resp

        flash("Account created! Welcome to VT Property Sales.", "success")
        return redirect(url_for("auth.login"))
    except Exception as e:
        import traceback, logging
        logging.error(f"signup_success error: {type(e).__name__}: {repr(e)}")
        logging.error(traceback.format_exc())
        flash(f"Could not confirm signup: {type(e).__name__}: {str(e)}", "error")
        return redirect(url_for("auth.signup"))

# ── Logout ────────────────────────────────────────────────────────────────────
@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))

# ── Subscribe ─────────────────────────────────────────────────────────────────
@auth_bp.route("/subscribe")
def subscribe():
    email = request.args.get("email", session.get("user_email", ""))
    plans = get_active_plans()
    return render_template("subscribe.html",
                           email=email,
                           plans=plans,
                           publishable_key=STRIPE_PUBLISHABLE_KEY)

@auth_bp.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    email = request.form.get("email", session.get("user_email", ""))
    plan_key = request.form.get("plan_key", "plan_monthly")

    # Look up plan from DB
    plan = get_plan(plan_key)
    if not plan:
        flash("Invalid plan selected.", "error")
        return redirect(url_for("auth.subscribe"))
    if not plan.get("stripe_price_id"):
        flash("This plan is not yet configured. Please contact support.", "error")
        return redirect(url_for("auth.subscribe"))

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
            line_items=[{"price": plan["stripe_price_id"], "quantity": 1}],
            mode="subscription",
            metadata={"plan_key": plan_key},
            subscription_data={"metadata": {"plan_key": plan_key}},
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
        plan_key        = checkout.metadata["plan_key"] if (checkout.metadata and "plan_key" in checkout.metadata) else "plan_monthly"

        db_execute(_q("""
            UPDATE users SET
                subscription_status    = 'active',
                stripe_customer_id     = ?,
                stripe_subscription_id = ?,
                plan_key               = ?
            WHERE email = ?
        """), (customer_id, subscription_id, plan_key, email))

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
        sub = event["data"]["object"]
        stripe_status = sub["status"]
        # Map Stripe statuses to our DB statuses
        if stripe_status == "active":
            status = "active"
        elif stripe_status == "trialing":
            status = "trialing"
        elif stripe_status in ("canceled", "unpaid", "incomplete_expired"):
            status = "cancelled"
        elif stripe_status == "past_due":
            status = "past_due"
        else:
            status = stripe_status
        # Store plan_key from subscription metadata if available
        plan_key = (sub.get("metadata") or {}).get("plan_key")
        if plan_key:
            db_execute(_q(
                "UPDATE users SET subscription_status = ?, plan_key = ? WHERE stripe_subscription_id = ?"
            ), (status, plan_key, sub["id"]))
        else:
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
    trial_days = days_left_in_trial(user) if user["subscription_status"] in ("trial", "trialing") else 0

    # Fetch auto-renewal state from Stripe for active/trialing subscribers
    cancel_at_period_end = False
    renewal_date = None
    if user.get("stripe_subscription_id") and user["subscription_status"] in ("active", "trialing"):
        try:
            sub = stripe.Subscription.retrieve(user["stripe_subscription_id"])
            cancel_at_period_end = sub.cancel_at_period_end
            renewal_date = datetime.fromtimestamp(
                sub.current_period_end, tz=timezone.utc
            ).strftime("%B %d, %Y")
        except Exception:
            renewal_date = None

    # Look up current plan details and all plans for plan switcher
    plan_key = user.get("plan_key") or "plan_monthly"
    user_plan = get_plan(plan_key)
    plans = get_active_plans()

    # Format trial end date for display when user is in trialing status
    trial_end_date = None
    if user["subscription_status"] == "trialing" and user.get("trial_ends_at"):
        try:
            trial_end_date = datetime.strptime(
                user["trial_ends_at"][:10], "%Y-%m-%d"
            ).strftime("%B %-d, %Y")
        except (ValueError, TypeError):
            trial_end_date = None

    return render_template("account.html", user=user, trial_days=trial_days,
                           cancel_at_period_end=cancel_at_period_end,
                           renewal_date=renewal_date,
                           user_plan=user_plan, plans=plans,
                           plan_key=plan_key, trial_end_date=trial_end_date)

# ── Billing portal ────────────────────────────────────────────────────────────
@auth_bp.route("/billing-portal")
def billing_portal():
    if "user_id" not in session:
        return redirect(url_for("auth.login"))
    user = db_fetchone(_q("SELECT * FROM users WHERE id = ?"), (session["user_id"],))
    if not user or not user.get("stripe_customer_id"):
        flash("No billing information found for your account.", "error")
        return redirect(url_for("auth.account"))
    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=user["stripe_customer_id"],
            return_url=url_for("auth.account", _external=True),
        )
        return redirect(portal_session.url, code=303)
    except Exception as e:
        flash(f"Could not open billing portal: {str(e)}", "error")
        return redirect(url_for("auth.account"))

# ── Auto-renewal toggle ──────────────────────────────────────────────────────
@auth_bp.route("/toggle-autorenewal", methods=["POST"])
def toggle_autorenewal():
    if "user_id" not in session:
        return redirect(url_for("auth.login"))
    user = db_fetchone(_q("SELECT * FROM users WHERE id = ?"), (session["user_id"],))
    if not user or not user.get("stripe_subscription_id"):
        flash("No active subscription found.", "error")
        return redirect(url_for("auth.account"))
    try:
        sub = stripe.Subscription.retrieve(user["stripe_subscription_id"])
        new_cancel = not sub.cancel_at_period_end
        stripe.Subscription.modify(
            user["stripe_subscription_id"], cancel_at_period_end=new_cancel
        )
        if new_cancel:
            flash("Auto-renewal turned off. Your subscription will not renew at the end of the billing period.", "info")
        else:
            flash("Auto-renewal resumed. Your subscription will renew automatically.", "success")
    except Exception as e:
        flash(f"Error updating auto-renewal: {str(e)}", "error")
    return redirect(url_for("auth.account"))

@auth_bp.route("/create-checkout-session-upgrade")
def create_checkout_session_upgrade():
    """Let a trialing user skip the remainder of their trial and go active now."""
    if "user_id" not in session:
        return redirect(url_for("auth.login"))
    user = db_fetchone(_q("SELECT * FROM users WHERE id = ?"), (session["user_id"],))
    if not user:
        return redirect(url_for("auth.login"))
    email = user["email"]
    try:
        customer_id = user["stripe_customer_id"]
        if not customer_id:
            customer = stripe.Customer.create(email=email)
            customer_id = customer.id
            db_execute(_q(
                "UPDATE users SET stripe_customer_id = ? WHERE email = ?"
            ), (customer_id, email))
        # Use the user's current plan, or default to monthly
        upgrade_plan_key = user.get("plan_key", "plan_monthly") or "plan_monthly"
        upgrade_plan = get_plan(upgrade_plan_key)
        upgrade_price_id = upgrade_plan["stripe_price_id"] if upgrade_plan else ""
        if not upgrade_price_id:
            upgrade_price_id = STRIPE_PRICE_ID  # fallback during migration
        if not upgrade_price_id:
            flash("Subscription plans are not yet configured. Please contact support.", "error")
            return redirect(url_for("auth.account"))
        checkout = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{"price": upgrade_price_id, "quantity": 1}],
            mode="subscription",
            metadata={"plan_key": upgrade_plan_key},
            subscription_data={"metadata": {"plan_key": upgrade_plan_key}},
            success_url=request.host_url + "subscribe/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.host_url + "account",
        )
        return redirect(checkout.url, code=303)
    except Exception as e:
        flash(f"Error: {str(e)}", "error")
        return redirect(url_for("auth.account"))

@auth_bp.route("/cancel-subscription", methods=["POST"])
def cancel_subscription():
    if "user_id" not in session:
        return redirect(url_for("auth.login"))
    user = db_fetchone(_q("SELECT * FROM users WHERE id = ?"), (session["user_id"],))
    if user and user["stripe_subscription_id"]:
        try:
            # Trial users: cancel immediately (no charge); active users: cancel at period end
            if user["subscription_status"] == "trialing":
                stripe.Subscription.cancel(user["stripe_subscription_id"])
                db_execute(_q(
                    "UPDATE users SET subscription_status = 'cancelled' WHERE id = ?"
                ), (user["id"],))
                # Send cancellation email via Resend
                _send_cancellation_email(user["email"])
                session.clear()
                return redirect(url_for("auth.cancelled"))
            else:
                stripe.Subscription.modify(
                    user["stripe_subscription_id"], cancel_at_period_end=True
                )
                flash("Your subscription will cancel at the end of the billing period.", "info")
        except Exception as e:
            flash(f"Error cancelling: {str(e)}", "error")
    return redirect(url_for("auth.account"))

@auth_bp.route("/cancelled")
def cancelled():
    return render_template("cancelled.html")

def _send_cancellation_email(email):
    """Send trial cancellation confirmation email via Resend."""
    import requests as http_requests
    resend_key = os.environ.get("RESEND_API_KEY", "")
    from_email = os.environ.get("CONTACT_FROM_EMAIL", "info@vtpropertysales.com")
    if not resend_key:
        return
    try:
        http_requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_email,
                "to": [email],
                "subject": "Your VT Property Sales Trial Has Been Cancelled",
                "text": (
                    "Thank you for trying VTPropertySales.com. Your payment will not be charged.\n"
                    "To reach us please send an email to info@VTPropertySales.com\n\n"
                    "If this was a mistake, you can sign up again at vtpropertysales.com"
                ),
            },
            timeout=10,
        )
    except Exception:
        pass  # best-effort — don't block the cancellation flow

# ── Change plan ──────────────────────────────────────────────────────────────
@auth_bp.route("/change-plan", methods=["POST"])
def change_plan():
    if "user_id" not in session:
        return redirect(url_for("auth.login"))
    user = db_fetchone(_q("SELECT * FROM users WHERE id = ?"), (session["user_id"],))
    if not user:
        return redirect(url_for("auth.login"))

    plan_key = request.form.get("plan_key", "")
    plan = get_plan(plan_key)
    if not plan:
        flash("Invalid plan selected.", "error")
        return redirect(url_for("auth.account"))
    if not plan.get("stripe_price_id"):
        flash("This plan is not yet configured. Please contact support.", "error")
        return redirect(url_for("auth.account"))

    # If already on this plan, no action needed
    if plan_key == (user.get("plan_key") or "plan_monthly"):
        flash("You are already on this plan.", "info")
        return redirect(url_for("auth.account"))

    subscription_id = user.get("stripe_subscription_id")

    # If user has an active Stripe subscription, modify it in place with proration
    if subscription_id and user["subscription_status"] in ("active", "trialing"):
        try:
            sub = stripe.Subscription.retrieve(subscription_id)
            existing_item_id = sub["items"]["data"][0]["id"]
            stripe.Subscription.modify(
                subscription_id,
                items=[{"id": existing_item_id, "price": plan["stripe_price_id"]}],
                proration_behavior="create_prorations",
                metadata={"plan_key": plan_key},
            )
            db_execute(_q(
                "UPDATE users SET plan_key = ? WHERE id = ?"
            ), (plan_key, user["id"]))
            flash(f"Plan changed to {plan['label']}. Proration will be applied to your next invoice.", "success")
            return redirect(url_for("auth.account"))
        except Exception as e:
            flash(f"Error changing plan: {str(e)}", "error")
            return redirect(url_for("auth.account"))

    # Fallback: no active subscription (e.g. trial without card) — create new checkout session
    email = user["email"]
    try:
        customer_id = user.get("stripe_customer_id")
        if not customer_id:
            customer = stripe.Customer.create(email=email)
            customer_id = customer.id
            db_execute(_q(
                "UPDATE users SET stripe_customer_id = ? WHERE email = ?"
            ), (customer_id, email))

        checkout = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{"price": plan["stripe_price_id"], "quantity": 1}],
            mode="subscription",
            metadata={"plan_key": plan_key},
            subscription_data={"metadata": {"plan_key": plan_key}},
            success_url=request.host_url + "subscribe/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.host_url + "account",
        )
        return redirect(checkout.url, code=303)
    except Exception as e:
        flash(f"Error changing plan: {str(e)}", "error")
        return redirect(url_for("auth.account"))

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

@auth_bp.route("/admin/plans")
def plans_admin():
    if session.get("user_email") != ADMIN_EMAIL:
        return redirect(url_for("auth.login"))
    plans = db_fetchall("SELECT * FROM subscription_plans ORDER BY sort_order")
    return render_template("plans_admin.html", plans=plans)

@auth_bp.route("/admin/plans/save", methods=["POST"])
def plans_save():
    if session.get("user_email") != ADMIN_EMAIL:
        return jsonify({"error": "unauthorized"}), 403
    try:
        data = request.get_json(force=True)
        key = data.get("key", "").strip()
        display_price = data.get("display_price", "").strip()
        stripe_price_id = data.get("stripe_price_id", "").strip()
        if not key:
            return jsonify({"error": "key is required"}), 400
        db_execute(_q(
            "UPDATE subscription_plans SET display_price = ?, stripe_price_id = ? WHERE key = ?"
        ), (display_price, stripe_price_id, key))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@auth_bp.route("/admin/users")
def users_admin():
    if session.get("user_email") != ADMIN_EMAIL:
        return redirect(url_for("auth.login"))
    return render_template("users_admin.html")

@auth_bp.route("/admin/users/list")
def users_list():
    if session.get("user_email") != ADMIN_EMAIL:
        return jsonify({"error": "unauthorized"}), 403
    users = db_fetchall(
        "SELECT id, email, subscription_status, plan_key, created_at"
        " FROM users ORDER BY created_at DESC"
    )
    return jsonify(users)

@auth_bp.route("/admin/users/delete", methods=["POST"])
def users_delete():
    if session.get("user_email") != ADMIN_EMAIL:
        return jsonify({"error": "unauthorized"}), 403
    try:
        data = request.get_json(force=True)
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"error": "email is required"}), 400
        db_execute(_q("DELETE FROM users WHERE email = ?"), (email,))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
