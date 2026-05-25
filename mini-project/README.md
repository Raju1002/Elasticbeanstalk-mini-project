# Zero-SSH Managed Application — Manual Setup Guide

## Does our flow match the project requirements?

| Requirement | How we cover it |
|---|---|
| Deploy web app on Elastic Beanstalk | `app/application.py` deployed as `app.zip` |
| Immutable deployment policy | Set manually in EB Console → Configuration → Deployments |
| ALL config in SSM Parameter Store (not env vars) | 7 SSM parameters — app reads them at startup via boto3 |
| SSM Session Manager — no port 22 | Connect via EC2 Console → Session Manager tab |
| SSM Run Command — restart all instances at once | `infrastructure/ssm_document.json` registered in Systems Manager |

**Yes — all 5 requirements are fully covered.**

---

## Project Structure (what you actually need)

```
mini-project/
├── app/
│   ├── application.py      ← your Flask app (reads ALL config from SSM)
│   └── requirements.txt    ← python dependencies
└── infrastructure/
    └── ssm_document.json   ← paste this in AWS Console to register Run Command
```

Everything else is done manually in the AWS Console. No scripts needed.

---

## Full Architecture

```
                    ┌─────────────────────────────────────────┐
                    │           AWS ACCOUNT                   │
                    │                                         │
  Browser ──────►  ALB  ──────►  EB EC2 Instance             │
                    │              │                          │
                    │              │  1. App starts           │
                    │              │  2. boto3 reads SSM      │
                    │              │  3. builds DB URL        │
                    │              │  4. connects to RDS      │
                    │              │                          │
                    │         ┌────┴──────────────────────┐   │
                    │         │   SSM Parameter Store     │   │
                    │         │  /myapp/prod/db_host      │   │
                    │         │  /myapp/prod/db_port      │   │
                    │         │  /myapp/prod/db_name      │   │
                    │         │  /myapp/prod/db_user      │   │
                    │         │  /myapp/prod/db_password ◄├───┼── KMS encrypted
                    │         │  /myapp/prod/api_key     ◄├───┼── KMS encrypted
                    │         │  /myapp/prod/secret_key  ◄├───┼── KMS encrypted
                    │         └───────────────────────────┘   │
                    │                                         │
                    │         ┌───────────────────────────┐   │
                    │         │   RDS PostgreSQL           │   │
                    │         │   port 5432 (VPC only)    │   │
                    │         └───────────────────────────┘   │
                    │                                         │
                    │  You (no SSH) ──► Session Manager ──►  EC2
                    │  You ──► Run Command ──► ALL instances  │
                    └─────────────────────────────────────────┘
```

---

## STEP 1 — Create RDS PostgreSQL (AWS Console)

**Go to:** AWS Console → RDS → Create database

| Setting | Value |
|---|---|
| Engine | PostgreSQL |
| Template | Free tier |
| DB instance identifier | `myappdb` |
| Master username | `myuser` |
| Master password | (set a strong password, note it down) |
| Instance class | `db.t3.micro` |
| Storage | 20 GB |
| VPC | Same VPC as your EB environment |
| Public access | No |

After creation, note down the **Endpoint** from:
RDS → Your DB → Connectivity & security → Endpoint

It looks like: `myappdb.xxxxxx.us-east-1.rds.amazonaws.com`

---

## STEP 2 — Create SSM Parameters (AWS Console)

**Go to:** AWS Console → Systems Manager → Parameter Store → Create parameter

Create these 7 parameters one by one:

### Non-sensitive (Type: String)

| Name | Value |
|---|---|
| `/myapp/prod/db_host` | `myappdb.xxxxxx.us-east-1.rds.amazonaws.com` ← your RDS endpoint |
| `/myapp/prod/db_port` | `5432` |
| `/myapp/prod/db_name` | `myappdb` |
| `/myapp/prod/db_user` | `myuser` |

### Sensitive — ENCRYPTED (Type: SecureString)

| Name | Value |
|---|---|
| `/myapp/prod/db_password` | your RDS master password |
| `/myapp/prod/api_key` | your third-party API key |
| `/myapp/prod/secret_key` | random string (run: `python3 -c "import secrets; print(secrets.token_hex(32))"`) |

> For SecureString, leave KMS key as `aws/ssm` (default) — AWS manages the encryption key for you.

---

## STEP 3 — Create IAM Role (AWS Console)

**Go to:** AWS Console → IAM → Roles → Create role

**Step 3a — Create the role**
- Trusted entity: `AWS service`
- Use case: `EC2`
- Role name: `myapp-eb-ec2-role`

**Step 3b — Attach these policies to the role**

| Policy | Why |
|---|---|
| `AmazonSSMManagedInstanceCore` | Enables Session Manager + Run Command on the instance |
| `AWSElasticBeanstalkWebTier` | Standard EB permissions (logs, CloudWatch) |

**Step 3c — Add inline policy** (for SSM parameter reads)

In the role → Add permissions → Create inline policy → JSON tab → paste this:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter",
        "ssm:GetParameters",
        "ssm:GetParametersByPath"
      ],
      "Resource": "arn:aws:ssm:us-east-1:YOUR_ACCOUNT_ID:parameter/myapp/*"
    }
  ]
}
```

Replace `YOUR_ACCOUNT_ID` with your actual AWS account ID.
Policy name: `myapp-ssm-parameter-read`

**Step 3d — Create Instance Profile**

IAM → Instance profiles → Create instance profile
- Name: `myapp-eb-instance-profile`
- Add role: `myapp-eb-ec2-role`

---

## STEP 4 — Register the Run Command Document (AWS Console)

**Go to:** AWS Console → Systems Manager → Documents → Create document

- Name: `MyApp-RestartWebService`
- Document type: `Command`
- Content: copy-paste the contents of `infrastructure/ssm_document.json`

Click Create.

---

## STEP 5 — Package and Deploy to Elastic Beanstalk

**Step 5a — Zip the app**

Select `application.py` and `requirements.txt` → right-click → compress → `app.zip`

The zip must contain the files at the root (not inside a folder):
```
app.zip
├── application.py
└── requirements.txt
```

**Step 5b — Create EB Environment (AWS Console)**

Go to: AWS Console → Elastic Beanstalk → Create application

| Setting | Value |
|---|---|
| Application name | `myapp` |
| Environment name | `myapp-prod` |
| Platform | Python → Python 3.11 on Amazon Linux 2023 |
| Application code | Upload `app.zip` |

**Service access:**
| Setting | Value |
|---|---|
| EC2 instance profile | `myapp-eb-instance-profile` ← the one from Step 3 |

**Networking:**
- Same VPC as your RDS instance
- Do NOT add port 22 to the security group

**Configuration → Updates, monitoring, and logging → Deployments:**
| Setting | Value |
|---|---|
| Deployment policy | **Immutable** ← this is the requirement |

**Environment properties (only non-secret config):**
| Key | Value |
|---|---|
| `APP_ENV` | `prod` |
| `AWS_REGION` | `us-east-1` |

> Notice: NO db password, NO api key here. Those are in SSM only.

Click Submit. EB takes ~5 minutes to launch.

---

## STEP 6 — Allow EC2 to reach RDS (Security Group)

**Go to:** AWS Console → RDS → Your DB → Connectivity → Security group

Add inbound rule:
| Type | Port | Source |
|---|---|---|
| PostgreSQL | 5432 | Security group of your EB instances |

This allows your EC2 instances to connect to RDS on port 5432 inside the VPC.

---

## STEP 7 — Verify Everything Works

Hit these URLs on your EB environment URL:

**`GET /health`** — EB health check
```json
{ "status": "healthy", "db": "connected" }
```

**`GET /ssm-demo`** — proves SSM is the config source
```json
{
  "message": "All config loaded from SSM Parameter Store at startup",
  "ssm_parameters": {
    "/myapp/prod/db_host":     { "type": "String",       "value": "myappdb.xxx.rds.amazonaws.com" },
    "/myapp/prod/db_password": { "type": "SecureString", "value": "*** (KMS encrypted, not shown)" }
  }
}
```

**`GET /db-check`** — proves DB connection works
```json
{ "connection": "ok", "rds_host": "myappdb.xxx.rds.amazonaws.com", "pg_version": "..." }
```

---

## STEP 8 — Connect to Instance Without SSH (Session Manager)

**Go to:** AWS Console → EC2 → Instances → select your EB instance → Connect

Click the **Session Manager** tab → Connect

You get a shell. Port 22 is closed. No key pair needed.

Or from AWS CLI (if you have Session Manager plugin installed):
```bash
aws ssm start-session --target i-0xxxxxxxxxxxxxxxxx --region us-east-1
```

---

## STEP 9 — Restart App on ALL Instances (Run Command)

**Go to:** AWS Console → Systems Manager → Run Command → Run command

| Setting | Value |
|---|---|
| Document | `MyApp-RestartWebService` |
| Target selection | Specify instance tags |
| Tag key | `elasticbeanstalk:environment-name` |
| Tag value | `myapp-prod` |

Click Run.

This sends the restart command to **every EC2 instance** in your EB environment simultaneously. You see per-instance results in the Output section.

**When to use this:**
- You updated a secret in SSM Parameter Store → restart app to reload it
- You need to restart the service without doing a full redeployment

---

## How the App Reads SSM at Startup (summary)

```
EC2 starts → application.py runs → load_config_from_ssm() called
    │
    ├── GET /myapp/prod/db_host     → "myappdb.xxx.rds.amazonaws.com"
    ├── GET /myapp/prod/db_port     → "5432"
    ├── GET /myapp/prod/db_name     → "myappdb"
    ├── GET /myapp/prod/db_user     → "myuser"
    ├── GET /myapp/prod/db_password → KMS decrypts → "yourpassword"
    ├── GET /myapp/prod/api_key     → KMS decrypts → "yourapikey"
    └── GET /myapp/prod/secret_key  → KMS decrypts → "yoursecretkey"
    │
    └── assembles: "postgresql://myuser:yourpassword@myappdb.xxx.../myappdb"
    └── SQLAlchemy connects to RDS
    └── Flask app ready → /health returns 200 → EB marks instance healthy
```
