"""
application.py — Zero-SSH Flask App
  - All config (DB credentials + SendGrid API key) loaded from SSM Parameter Store
  - Writes user data to RDS PostgreSQL
  - Sends a welcome email via SendGrid API when a new user is created
  - Dashboard page shows all DB records with timestamps

ROUTES:
  GET  /            → app status
  GET  /health      → EB health check
  GET  /ssm-demo    → shows all 7 SSM parameters that were loaded
  GET  /db-check    → confirms DB connection
  GET  /dashboard   → HTML page: add user form + full DB table
  GET  /users       → list all users (JSON)
  POST /users       → create user → save to DB → send welcome email
  GET  /users/<id>  → get one user (JSON)
  DELETE /users/<id>→ delete user (JSON)
"""

import boto3
import logging
import os
import uuid
import urllib.request
import urllib.error
import json as json_module
from botocore.exceptions import ClientError
from flask import Flask, jsonify, request
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 1 — SSM Parameter Store
#
# This is the core of the project requirement:
#   "store all config (database URL, API keys) in SSM Parameter Store"
#
# We read EVERY secret from SSM at startup.
# Nothing sensitive is in EB environment variables or in this code file.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_parameter(name: str, decrypt: bool = True) -> str:
    """
    Reads one parameter from AWS SSM Parameter Store.

    boto3 uses the IAM role attached to the EC2 instance automatically.
    No access keys or passwords are written in this code.

    decrypt=True  → for SecureString parameters (KMS encrypted)
                    used for: db_password, sendgrid_api_key, secret_key
    decrypt=False → for plain String parameters (not sensitive)
                    used for: db_host, db_port, db_name, db_user
    """
    region = os.environ.get("AWS_REGION", "us-east-1")
    client = boto3.client("ssm", region_name=region)
    try:
        logger.info("  Reading SSM: %s", name)
        response = client.get_parameter(Name=name, WithDecryption=decrypt)
        return response["Parameter"]["Value"]
    except ClientError as e:
        code = e.response["Error"]["Code"]
        logger.error("  FAILED SSM read '%s': %s", name, code)
        raise RuntimeError(f"SSM read failed: {name}") from e


def load_config_from_ssm() -> dict:
    """
    Loads ALL application configuration from SSM Parameter Store.

    7 parameters total:
      4 plain String  → DB connection details (not sensitive)
      3 SecureString  → DB password, SendGrid API key, Flask secret (encrypted)

    APP_ENV (from EB env var, default 'prod') tells us which SSM path to use.
    This is the ONLY environment variable we use — it is not a secret.
    """
    env    = os.environ.get("APP_ENV", "prod")
    prefix = f"/myapp/{env}"

    logger.info("━━━ Loading ALL config from SSM Parameter Store ━━━")
    logger.info("    Prefix: %s", prefix)

    # ── 4 plain String parameters (not sensitive, no encryption needed) ──
    db_host = get_parameter(f"{prefix}/db_host", decrypt=False)
    db_port = get_parameter(f"{prefix}/db_port", decrypt=False)
    db_name = get_parameter(f"{prefix}/db_name", decrypt=False)
    db_user = get_parameter(f"{prefix}/db_user", decrypt=False)

    # ── 3 SecureString parameters (sensitive, KMS encrypted at rest) ─────
    db_password      = get_parameter(f"{prefix}/db_password")       # RDS master password
    sendgrid_api_key = get_parameter(f"{prefix}/sendgrid_api_key")  # SendGrid API key
    secret_key       = get_parameter(f"{prefix}/secret_key")        # Flask session signing key

    # Assemble the DB URL from the 5 individual DB parameters.
    # This URL only exists in memory — never stored anywhere.
    db_url = f"postgresql+pg8000://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"

    logger.info("━━━ SSM load complete — 7 parameters loaded ━━━")
    logger.info("  /db_host         = %s  (String)", db_host)
    logger.info("  /db_port         = %s  (String)", db_port)
    logger.info("  /db_name         = %s  (String)", db_name)
    logger.info("  /db_user         = %s  (String)", db_user)
    logger.info("  /db_password     = ***  (SecureString — KMS encrypted)")
    logger.info("  /sendgrid_api_key= ***  (SecureString — KMS encrypted)")
    logger.info("  /secret_key      = ***  (SecureString — KMS encrypted)")

    return {
        "DB_HOST":           db_host,
        "DB_PORT":           db_port,
        "DB_NAME":           db_name,
        "DB_USER":           db_user,
        "DB_URL":            db_url,
        "SENDGRID_API_KEY":  sendgrid_api_key,
        "SECRET_KEY":        secret_key,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 2 — SendGrid Email
#
# WHAT IS SENDGRID?
#   SendGrid is an email delivery service.
#   Instead of setting up your own mail server, you call their API
#   and they send the email on your behalf.
#
# WHAT IS THE API KEY?
#   When you sign up on sendgrid.com, they give you a secret key
#   that looks like: SG.xxxxxxxxxxxxxxxxxxxxxxxx
#   Your app sends this key with every API call to prove it's you.
#
# WHY STORE IT IN SSM?
#   If someone gets your SendGrid API key, they can send emails
#   pretending to be you — spam, phishing, etc.
#   Storing it in SSM (SecureString) keeps it encrypted and safe.
#
# HOW IT WORKS HERE:
#   When a new user is created via POST /users:
#     1. User is saved to RDS
#     2. App calls SendGrid API with the key from SSM
#     3. SendGrid sends a welcome email to the new user's email address
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def send_welcome_email(to_email: str, to_name: str, api_key: str, meeting_link: str, registration_id: str) -> bool:
    """
    Sends a registration confirmation email via SendGrid.
    Includes the meeting link (from EB env var WORKSHOP_MEETING_LINK)
    and the unique registration ID generated in the backend.
    """
    SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"

    payload = {
        "personalizations": [{
            "to": [{"email": to_email, "name": to_name}],
            "subject": f"Registration Confirmed — Micro Degree Workshop"
        }],
        "from": {
            # IMPORTANT: replace this with your verified SendGrid sender email
            "email": "vamshikrishnak2506@gmail.com",
            "name":  "Micro Degree"
        },
        "content": [{
            "type":  "text/plain",
            "value": (
                f"Hi {to_name},\n\n"
                f"Thank you for registering for the Micro Degree workshop on\n"
                f"Elastic Beanstalk and AWS Systems Manager (SSM).\n\n"
                f"Your registration details:\n"
                f"  Registration ID : {registration_id}\n"
                f"  Name            : {to_name}\n"
                f"  Email           : {to_email}\n\n"
                f"Join the workshop using the link below:\n"
                f"  {meeting_link}\n\n"
                f"Please keep your Registration ID handy — you may need it\n"
                f"to verify your seat on the day of the workshop.\n\n"
                f"See you at the workshop!\n"
                f"Team Micro Degree"
            )
        }]
    }

    try:
        data    = json_module.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",   # API key from SSM
            "Content-Type":  "application/json",
        }
        req      = urllib.request.Request(SENDGRID_API_URL, data=data, headers=headers)
        response = urllib.request.urlopen(req, timeout=10)

        # SendGrid returns 202 Accepted for successful sends
        if response.status == 202:
            logger.info("Welcome email sent to %s via SendGrid", to_email)
            return True
        else:
            logger.warning("SendGrid returned unexpected status %s", response.status)
            return False

    except urllib.error.HTTPError as e:
        logger.error("SendGrid API error %s: %s", e.code, e.read().decode())
        return False
    except Exception as e:
        logger.error("Failed to send welcome email: %s", e)
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 3 — Database
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def create_db_engine(db_url: str):
    logger.info("Creating SQLAlchemy engine (connection pool to RDS)...")
    return create_engine(db_url, pool_pre_ping=True, pool_size=5, max_overflow=10)


def init_db(engine):
    """Creates the registrations table if it doesn't exist. Safe to run every startup."""
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS registrations (
                id              SERIAL PRIMARY KEY,
                registration_id VARCHAR(36)  NOT NULL UNIQUE,
                name            VARCHAR(100) NOT NULL,
                phone           VARCHAR(20)  NOT NULL,
                email           VARCHAR(150) NOT NULL UNIQUE,
                registered_at   TIMESTAMP DEFAULT NOW()
            )
        """))
        conn.commit()
    logger.info("DB table 'registrations' is ready.")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 4 — Flask Application
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def create_app() -> Flask:
    app = Flask(__name__)

    # Step 1 — Load ALL secrets from SSM (7 parameters)
    cfg = load_config_from_ssm()
    app.config.update(cfg)
    app.secret_key = cfg["SECRET_KEY"]

    # Step 2 — Connect to RDS using the URL assembled from SSM parameters
    app.db_engine = create_db_engine(cfg["DB_URL"])

    # Step 3 — Create users table if it doesn't exist
    init_db(app.db_engine)

    # ── Health & Status ───────────────────────────────────────────────

    @app.route("/")
    def index():
        return jsonify(
            status="ok",
            message="Zero-SSH app is running",
            config_source="AWS SSM Parameter Store",
            db_host=app.config["DB_HOST"],
            db_name=app.config["DB_NAME"],
        )

    @app.route("/health")
    def health():
        """
        EB calls this every 30 seconds.
        Returns 200 = healthy, 500 = unhealthy (EB may replace the instance).
        """
        try:
            with app.db_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return jsonify(status="healthy", db="connected"), 200
        except OperationalError as e:
            logger.error("Health check failed: %s", e)
            return jsonify(status="unhealthy", db="unreachable"), 500

    @app.route("/ssm-demo")
    def ssm_demo():
        """
        Shows all 7 SSM parameters that were loaded at startup.
        Secrets are masked — proves SSM is the config source, not EB env vars.
        """
        env    = os.environ.get("APP_ENV", "prod")
        prefix = f"/myapp/{env}"
        return jsonify(
            message="All config loaded from SSM Parameter Store at startup",
            total_parameters=7,
            ssm_parameters={
                f"{prefix}/db_host":          {"type": "String",       "value": app.config["DB_HOST"],  "sensitive": False},
                f"{prefix}/db_port":          {"type": "String",       "value": app.config["DB_PORT"],  "sensitive": False},
                f"{prefix}/db_name":          {"type": "String",       "value": app.config["DB_NAME"],  "sensitive": False},
                f"{prefix}/db_user":          {"type": "String",       "value": app.config["DB_USER"],  "sensitive": False},
                f"{prefix}/db_password":      {"type": "SecureString", "value": "*** (KMS encrypted)",  "sensitive": True},
                f"{prefix}/sendgrid_api_key": {"type": "SecureString", "value": "*** (KMS encrypted)",  "sensitive": True},
                f"{prefix}/secret_key":       {"type": "SecureString", "value": "*** (KMS encrypted)",  "sensitive": True},
            },
            note="Zero secrets in EB environment variables. Zero secrets in code.",
        )

    @app.route("/db-check")
    def db_check():
        """Confirms DB connection is alive and shows RDS details."""
        try:
            with app.db_engine.connect() as conn:
                pg_version = conn.execute(text("SELECT version()")).scalar()
                db_now     = conn.execute(text("SELECT NOW()")).scalar()
            return jsonify(
                connection="ok",
                rds_host=app.config["DB_HOST"],
                database=app.config["DB_NAME"],
                pg_version=pg_version,
                db_time=str(db_now),
            )
        except OperationalError as e:
            return jsonify(connection="failed", error=str(e)), 500

    # ── Users API ─────────────────────────────────────────────────────

    @app.route("/users", methods=["GET"])
    def get_users():
        """Read all registrations from RDS."""
        try:
            with app.db_engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT id, registration_id, name, phone, email, registered_at FROM registrations ORDER BY id")
                )
                users = [
                    {
                        "id": r.id,
                        "registration_id": r.registration_id,
                        "name": r.name,
                        "phone": r.phone,
                        "email": r.email,
                        "registered_at": str(r.registered_at)
                    }
                    for r in rows
                ]
            return jsonify(registrations=users, count=len(users)), 200
        except OperationalError:
            return jsonify(error="Failed to fetch registrations"), 500

    @app.route("/users/<int:user_id>", methods=["GET"])
    def get_user(user_id):
        """Read one registration by ID."""
        try:
            with app.db_engine.connect() as conn:
                row = conn.execute(
                    text("SELECT id, registration_id, name, phone, email, registered_at FROM registrations WHERE id = :id"),
                    {"id": user_id}
                ).fetchone()
            if row is None:
                return jsonify(error=f"Registration {user_id} not found"), 404
            return jsonify(
                id=row.id,
                registration_id=row.registration_id,
                name=row.name,
                phone=row.phone,
                email=row.email,
                registered_at=str(row.registered_at)
            ), 200
        except OperationalError:
            return jsonify(error="Failed to fetch registration"), 500

    @app.route("/users", methods=["POST"])
    def create_user():
        """
        Receives name + phone + email
        → generates a unique registration_id (UUID)
        → saves everything to RDS
        → sends confirmation email with registration_id + meeting link via SendGrid.

        MEETING LINK:
          Read from EB environment variable WORKSHOP_MEETING_LINK.
          This is NOT a secret so EB env var is the right place for it.
          You update it in EB console whenever the link changes — no redeployment needed.
        """
        data = request.get_json(silent=True)
        if not data:
            return jsonify(error="Request body must be JSON"), 400

        name  = data.get("name",  "").strip()
        phone = data.get("phone", "").strip()
        email = data.get("email", "").strip()

        # Validate
        errors = []
        if not name:
            errors.append("'name' is required")
        if not phone:
            errors.append("'phone' is required")
        if not email:
            errors.append("'email' is required")
        if email and "@" not in email:
            errors.append("'email' must be a valid email address")
        if errors:
            return jsonify(error="Validation failed", details=errors), 400

        # Generate unique registration ID in the backend
        # UUID4 = random, e.g. "a3f8c2d1-e4b7-4a9f-8c3d-2e1b4a7f8c9d"
        # The user never types this — we generate it and store it in DB
        registration_id = str(uuid.uuid4())

        # Write to RDS — registration_id stored alongside the user data
        try:
            with app.db_engine.connect() as conn:
                result = conn.execute(
                    text("""
                        INSERT INTO registrations (registration_id, name, phone, email)
                        VALUES (:registration_id, :name, :phone, :email)
                        RETURNING id, registration_id, name, phone, email, registered_at
                    """),
                    {
                        "registration_id": registration_id,
                        "name":  name,
                        "phone": phone,
                        "email": email,
                    }
                )
                conn.commit()
                new_reg = result.fetchone()
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                return jsonify(error=f"Email '{email}' is already registered"), 409
            logger.error("POST /users DB error: %s", e)
            return jsonify(error="Failed to save registration"), 500

        logger.info("Registration saved: id=%s reg_id=%s email=%s",
                    new_reg.id, new_reg.registration_id, new_reg.email)

        # Get meeting link from EB environment variable
        # This is NOT a secret — just a URL that changes day to day
        # Update it in EB Console → Configuration → Environment properties
        # whenever the meeting link changes. No redeployment needed.
        meeting_link = os.environ.get("WORKSHOP_MEETING_LINK", "Link will be shared soon")

        # Send confirmation email with registration_id + meeting link
        email_sent = send_welcome_email(
            to_email=new_reg.email,
            to_name=new_reg.name,
            api_key=app.config["SENDGRID_API_KEY"],   # from SSM
            meeting_link=meeting_link,                 # from EB env var
            registration_id=new_reg.registration_id,  # generated in backend
        )

        return jsonify(
            message="Registration successful",
            registration={
                "id":              new_reg.id,
                "registration_id": new_reg.registration_id,
                "name":            new_reg.name,
                "phone":           new_reg.phone,
                "email":           new_reg.email,
                "registered_at":   str(new_reg.registered_at),
            },
            confirmation_email_sent=email_sent,
        ), 201

    @app.route("/users/<int:user_id>", methods=["DELETE"])
    def delete_user(user_id):
        """Delete a registration by ID."""
        try:
            with app.db_engine.connect() as conn:
                result = conn.execute(
                    text("DELETE FROM registrations WHERE id = :id"),
                    {"id": user_id}
                )
                conn.commit()
            if result.rowcount == 0:
                return jsonify(error=f"Registration {user_id} not found"), 404
            return jsonify(message=f"Registration {user_id} deleted successfully"), 200
        except OperationalError as e:
            return jsonify(error="Failed to delete registration"), 500

    # ── Dashboard — Registration page (user-facing) ───────────────────

    @app.route("/dashboard")
    def dashboard():
        """
        Public registration page for the Micro Degree workshop.
        Shows company branding, workshop info, and the registration form.
        No DB records shown here — that's at /dashboard/records.
        """
        db_host = app.config.get("DB_HOST", "unknown")

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Micro Degree — Workshop Registration</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f4f6fb;color:#1a1a2e;min-height:100vh}}

    /* ── Top navbar ── */
    .navbar{{background:#1e3a5f;padding:0 32px;height:60px;display:flex;align-items:center;justify-content:space-between}}
    .navbar .brand{{color:#fff;font-size:1.2rem;font-weight:700;letter-spacing:.5px}}
    .navbar .brand span{{color:#60a5fa}}
    .navbar .nav-tag{{color:#93c5fd;font-size:.8rem;border:1px solid #3b82f6;padding:3px 10px;border-radius:20px}}

    /* ── Hero banner ── */
    .hero{{background:linear-gradient(135deg,#1e3a5f 0%,#1e40af 100%);color:#fff;padding:48px 32px 40px;text-align:center}}
    .hero .tag{{display:inline-block;background:rgba(96,165,250,.2);border:1px solid #60a5fa;color:#93c5fd;font-size:.78rem;font-weight:600;padding:4px 14px;border-radius:20px;margin-bottom:16px;letter-spacing:.5px;text-transform:uppercase}}
    .hero h1{{font-size:2rem;font-weight:800;margin-bottom:10px;line-height:1.2}}
    .hero h1 span{{color:#60a5fa}}
    .hero p{{font-size:1rem;color:#bfdbfe;max-width:560px;margin:0 auto 24px}}
    .topics{{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}}
    .topic-pill{{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);color:#e0f2fe;font-size:.8rem;padding:5px 14px;border-radius:20px}}

    /* ── Main content ── */
    .main{{max-width:520px;margin:0 auto;padding:36px 20px}}

    /* ── Registration card ── */
    .card{{background:#fff;border-radius:14px;box-shadow:0 4px 20px rgba(0,0,0,.08);overflow:hidden}}
    .card-header{{background:#1e3a5f;padding:18px 24px}}
    .card-header h2{{color:#fff;font-size:1rem;font-weight:700;margin-bottom:2px}}
    .card-header p{{color:#93c5fd;font-size:.8rem}}
    .card-body{{padding:24px}}

    .form-group{{margin-bottom:16px}}
    .form-group label{{display:block;font-size:.82rem;font-weight:600;color:#374151;margin-bottom:6px}}
    .form-group label span{{color:#ef4444;margin-left:2px}}
    .form-group input{{width:100%;padding:10px 14px;border:1.5px solid #e5e7eb;border-radius:8px;font-size:.9rem;outline:none;transition:border .2s,box-shadow .2s;color:#1a1a2e}}
    .form-group input:focus{{border-color:#1e40af;box-shadow:0 0 0 3px rgba(30,64,175,.1)}}
    .form-group input::placeholder{{color:#9ca3af}}

    .submit-btn{{width:100%;background:#1e40af;color:#fff;border:none;padding:12px;border-radius:8px;font-size:.95rem;font-weight:600;cursor:pointer;transition:background .2s;margin-top:4px}}
    .submit-btn:hover{{background:#1e3a8a}}
    .submit-btn:active{{transform:scale(.99)}}

    .msg-success{{background:#ecfdf5;color:#065f46;border:1px solid #6ee7b7;border-radius:8px;padding:12px 16px;font-size:.85rem;margin-top:14px;line-height:1.7}}
    .msg-error{{background:#fef2f2;color:#991b1b;border:1px solid #fca5a5;border-radius:8px;padding:12px 16px;font-size:.85rem;margin-top:14px}}

    .records-link{{display:inline-flex;align-items:center;gap:6px;margin-top:10px;font-size:.82rem;color:#1e40af;text-decoration:none;font-weight:500}}
    .records-link:hover{{text-decoration:underline}}

    /* ── Footer note ── */
    .footer-note{{text-align:center;font-size:.75rem;color:#9ca3af;margin-top:24px;line-height:1.6}}
  </style>
</head>
<body>

  <!-- Navbar -->
  <nav class="navbar">
    <div class="brand">Micro<span>Degree</span></div>
    <span class="nav-tag">Workshop 2026</span>
  </nav>

  <!-- Hero -->
  <div class="hero">
    <div class="tag">Live Workshop</div>
    <h1>Elastic Beanstalk &amp;<br><span>AWS SSM</span> Deep Dive</h1>
    <p>Hands-on workshop covering managed application deployment,
       zero-SSH infrastructure, and secrets management on AWS.</p>
    <div class="topics">
      <span class="topic-pill">Elastic Beanstalk</span>
      <span class="topic-pill">SSM Parameter Store</span>
      <span class="topic-pill">Session Manager</span>
      <span class="topic-pill">Run Command</span>
      <span class="topic-pill">Immutable Deployments</span>
    </div>
  </div>

  <!-- Registration Form -->
  <div class="main">
    <div class="card">
      <div class="card-header">
        <h2>Workshop Registration</h2>
        <p>Fill in your details to reserve your seat</p>
      </div>
      <div class="card-body">
        <form id="regForm">

          <div class="form-group">
            <label>Full Name <span>*</span></label>
            <input type="text" id="name" placeholder="Enter your full name" required />
          </div>

          <div class="form-group">
            <label>Phone Number <span>*</span></label>
            <input type="tel" id="phone" placeholder="Enter your phone number" required />
          </div>

          <div class="form-group">
            <label>Email Address <span>*</span></label>
            <input type="email" id="email" placeholder="Enter your email address" required />
          </div>

          <button type="submit" class="submit-btn">Register Now</button>
          <div id="formMsg"></div>

        </form>
      </div>
    </div>

    <p class="footer-note">
      Your details are stored securely in AWS RDS PostgreSQL.<br>
      Credentials managed via AWS SSM Parameter Store — not environment variables.<br>
      A confirmation email will be sent to your email address via SendGrid.
    </p>
  </div>

  <script>
    document.getElementById("regForm").addEventListener("submit", async function(e) {{
      e.preventDefault();
      const name  = document.getElementById("name").value.trim();
      const phone = document.getElementById("phone").value.trim();
      const email = document.getElementById("email").value.trim();
      const msgEl = document.getElementById("formMsg");
      msgEl.innerHTML = "";

      // Disable button while submitting
      const btn = document.querySelector(".submit-btn");
      btn.disabled = true;
      btn.textContent = "Registering...";

      try {{
        const res  = await fetch("/users", {{
          method:  "POST",
          headers: {{"Content-Type": "application/json"}},
          body:    JSON.stringify({{name, phone, email}})
        }});
        const data = await res.json();

        if (res.ok) {{
          const emailLine = data.confirmation_email_sent
            ? "A confirmation email has been sent to <strong>" + email + "</strong>"
            : "Registration saved. (Confirmation email could not be sent)";

          msgEl.innerHTML = `
            <div class="msg-success">
              ✓ <strong>Registration successful!</strong><br>
              Welcome, <strong>${{data.registration.name}}</strong>.<br>
              Registration ID: <strong style="font-family:monospace">${{data.registration.registration_id}}</strong><br>
              Registered at: ${{data.registration.registered_at}}<br>
              <span style="font-size:.8rem">${{emailLine}} with your Registration ID and workshop joining link.</span>
            </div>
            <a class="records-link" href="/dashboard/records">View all registrations →</a>`;

          document.getElementById("name").value  = "";
          document.getElementById("phone").value = "";
          document.getElementById("email").value = "";

        }} else {{
          msgEl.innerHTML = `<div class="msg-error">
            ✗ ${{data.error}}${{data.details ? ": " + data.details.join(", ") : ""}}
          </div>`;
        }}
      }} catch(err) {{
        msgEl.innerHTML = `<div class="msg-error">✗ Network error: ${{err.message}}</div>`;
      }} finally {{
        btn.disabled = false;
        btn.textContent = "Register Now";
      }}
    }});
  </script>

</body>
</html>"""
        return html, 200

    @app.route("/dashboard/records")
    def dashboard_records():
        """
        Internal page — shows all workshop registrations from RDS with timestamps.
        Linked only from the success message on /dashboard.
        """
        try:
            with app.db_engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT id, registration_id, name, phone, email, registered_at FROM registrations ORDER BY registered_at DESC")
                ).fetchall()
        except OperationalError:
            rows = []

        if rows:
            table_rows = ""
            for row in rows:
                ts = row.registered_at.strftime("%d %b %Y  %H:%M:%S UTC") if row.registered_at else "—"
                # Show only first 8 chars of UUID for readability, full on hover
                short_id = row.registration_id[:8].upper() if row.registration_id else "—"
                table_rows += f"""
                <tr>
                    <td class="id-cell">{row.id}</td>
                    <td><span class="reg-id" title="{row.registration_id}">{short_id}...</span></td>
                    <td>{row.name}</td>
                    <td>{row.phone}</td>
                    <td>{row.email}</td>
                    <td class="timestamp">{ts}</td>
                </tr>"""
        else:
            table_rows = '<tr><td colspan="6" class="empty">No registrations yet.</td></tr>'

        total    = len(rows)
        rec_word = "registration" if total == 1 else "registrations"
        db_host  = app.config.get("DB_HOST", "unknown")

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Registrations — Micro Degree Workshop</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f4f6fb;color:#1a1a2e}}
    .navbar{{background:#1e3a5f;padding:0 32px;height:60px;display:flex;align-items:center;justify-content:space-between}}
    .navbar .brand{{color:#fff;font-size:1.2rem;font-weight:700}}
    .navbar .brand span{{color:#60a5fa}}
    .navbar .nav-tag{{color:#93c5fd;font-size:.8rem;border:1px solid #3b82f6;padding:3px 10px;border-radius:20px}}
    .main{{max-width:1060px;margin:0 auto;padding:32px 20px}}
    .back-link{{display:inline-flex;align-items:center;gap:5px;margin-bottom:20px;font-size:.85rem;color:#1e40af;text-decoration:none;font-weight:500}}
    .back-link:hover{{text-decoration:underline}}
    .page-title{{font-size:1.4rem;font-weight:700;margin-bottom:4px}}
    .page-sub{{font-size:.82rem;color:#6b7280;margin-bottom:24px}}
    .table-card{{background:#fff;border-radius:14px;box-shadow:0 4px 20px rgba(0,0,0,.08);overflow:hidden}}
    .table-card-header{{background:#1e3a5f;padding:16px 24px;display:flex;justify-content:space-between;align-items:center}}
    .table-card-header h2{{color:#fff;font-size:.95rem;font-weight:600}}
    .badge{{background:#3b82f6;color:#fff;font-size:.75rem;font-weight:700;padding:3px 12px;border-radius:20px}}
    .db-info{{font-size:.75rem;color:#9ca3af;padding:10px 24px;background:#f8faff;border-bottom:1px solid #e5e7eb}}
    table{{width:100%;border-collapse:collapse;font-size:.88rem}}
    thead tr{{background:#f8f9ff}}
    th{{text-align:left;padding:11px 16px;font-weight:600;color:#4b5563;border-bottom:2px solid #e5e7eb;font-size:.78rem;text-transform:uppercase;letter-spacing:.05em}}
    td{{padding:12px 16px;border-bottom:1px solid #f0f0f0;color:#374151}}
    tr:last-child td{{border-bottom:none}}
    tr:hover td{{background:#f8faff}}
    tr:first-child td{{background:#f0fdf4;font-weight:500}}
    .timestamp{{color:#6b7280;font-size:.8rem;font-family:"SF Mono","Fira Code",monospace}}
    td.empty{{text-align:center;color:#aaa;padding:40px}}
    .id-cell{{color:#9ca3af;font-size:.8rem;font-weight:600}}
    .reg-id{{
      font-family:"SF Mono","Fira Code",monospace;
      font-size:.78rem;
      background:#ede9fe;
      color:#5b21b6;
      padding:2px 8px;
      border-radius:4px;
      cursor:default;
    }}
  </style>
</head>
<body>

  <nav class="navbar">
    <div class="brand">Micro<span>Degree</span></div>
    <span class="nav-tag">Workshop 2026</span>
  </nav>

  <div class="main">
    <a class="back-link" href="/dashboard">← Back to Registration</a>

    <h1 class="page-title">Workshop Registrations</h1>
    <p class="page-sub">
      RDS Host: <strong>{db_host}</strong> &nbsp;·&nbsp;
      Credentials from <strong>SSM Parameter Store</strong> &nbsp;·&nbsp;
      Newest entry at top &nbsp;·&nbsp;
      Hover over Registration ID to see full UUID
    </p>

    <div class="table-card">
      <div class="table-card-header">
        <h2>Elastic Beanstalk &amp; SSM Workshop — Attendees</h2>
        <span class="badge">{total} {rec_word}</span>
      </div>
      <div class="db-info">
        Registration IDs are UUID4 — generated in the backend at registration time &nbsp;·&nbsp;
        DB credentials loaded from SSM Parameter Store &nbsp;·&nbsp;
        Timestamps UTC
      </div>
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Registration ID</th>
            <th>Name</th>
            <th>Phone</th>
            <th>Email</th>
            <th>Registered At</th>
          </tr>
        </thead>
        <tbody>
          {table_rows}
        </tbody>
      </table>
    </div>
  </div>

</body>
</html>"""
        return html, 200

    return app


# EB looks for a variable named 'application' by default
application = create_app()

if __name__ == "__main__":
    application.run(host="0.0.0.0", port=8080, debug=False)
