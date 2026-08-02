#  Resilient Hair Salon Booking System

**A serverless, event-driven appointment booking platform on AWS — This Hair Salon Booking System uses an event-driven, decoupled cloud architecture to safely queue incoming requests and process payment deposits without dropping appointments or crashing under heavy load.**

---

## Table of Contents

- [Project Overview & Features](#project-overview--features)
  - [Problem Statement](#problem-statement)
  - [Key Features](#key-features)
- [Architecture & Tech Stack](#architecture--tech-stack)
  - [Tech Stack](#tech-stack)
  - [Architecture Diagram](#architecture-diagram)
  - [Duplicate-Booking Prevention (Core Design)](#duplicate-booking-prevention-core-design)
- [Getting Started / Installation Guide](#getting-started--installation-guide)
  - [Prerequisites](#prerequisites)
  - [Environment Variables & Configuration](#environment-variables--configuration)
  - [Installation & Deployment](#installation--deployment)
- [Usage & Testing](#usage--testing)
  - [Usage Instructions](#usage-instructions)
  - [Testing the Duplicate-Booking Guarantee](#testing-the-duplicate-booking-guarantee)
  - [Testing Payment Failure Paths](#testing-payment-failure-paths)
- [Future Enhancements & Challenges](#future-enhancements--challenges)
  - [Lessons Learned](#lessons-learned)
  - [Roadmap](#roadmap)
- [Contact & License](#contact--license)

---

## Project Overview & Features

### Problem Statement

Local hair salons often struggle with booking management during peak hours. When multiple clients try to book appointments at the same time, traditional booking software can slow down or crash, resulting in lost business and frustrated customers.

This project was built as a hands-on cloud computing capstone to solve that problem correctly using AWS-native primitives — DynamoDB conditional writes — rather than application-level locking, while also standing up a complete, production-shaped serverless stack: authentication, payments, infrastructure-as-code, and CI/CD.

### Key Features

- 🔐 **Secure login** via Auth0 Universal Login (no custom auth code, no password handling in-app)
- 📅 **Real-time availability** — booked slots are filtered out of the picker per stylist/date
- 💳 **$10 deposit hold** via Stripe, authorized on submit and only captured once the booking is confirmed
- ⚔️ **Guaranteed no double-bookings** — enforced atomically at the database layer, not in application logic
- 🔄 **Automatic refund/release** — if a customer loses a booking race, their deposit hold is released automatically, no manual intervention
- ✅ **Live status polling** — the frontend doesn't just trust an initial "queued" response; it polls for the real, final outcome and tells the customer clearly whether they got the slot
- 🚫 **Past-date protection**, enforced both in the UI and again server-side
- 🏗️ **Fully defined as code** — the entire stack (VPC, Lambdas, API Gateway, DynamoDB, SQS, CloudFront, IAM) is provisioned via AWS CDK
- 🚀 **One-push deployment** — GitHub Actions handles CDK deploy, frontend sync, and CDN cache invalidation on every push to `main`

---

## Architecture & Tech Stack

### Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Static HTML/CSS/JavaScript, [Stripe.js](https://stripe.com/docs/js) v3, [Auth0 SPA SDK](https://github.com/auth0/auth0-spa-js) |
| Hosting/CDN | Amazon S3 (static website) + Amazon CloudFront (HTTPS termination) |
| API Layer | Amazon API Gateway (HTTP API v2) with a native JWT authorizer |
| Compute | AWS Lambda (Python 3.11) — Booking, Worker, Availability, Status functions |
| Async Processing | Amazon SQS (with a Dead Letter Queue for poison messages) |
| Database | Amazon DynamoDB (single-table design, on-demand billing) |
| Networking | Amazon VPC with Gateway Endpoints (S3, DynamoDB) and Interface Endpoints (Secrets Manager, STS) |
| Authentication | Auth0 (Universal Login, Authorization Code + PKCE flow, JWT access tokens) |
| Payments | Stripe (PaymentIntents API, manual capture) |
| Secrets | AWS Secrets Manager |
| Infrastructure as Code | AWS CDK (Python) |
| CI/CD | GitHub Actions |

### Architecture Diagram

```
                     ┌──────────────┐
                     │   Customer   │
                     │   Browser    │
                     └──────┬───────┘
                            │ HTTPS
                            ▼
                  ┌───────────────────┐
                  │   CloudFront CDN   │  (HTTPS termination — required by
                  │  (*.cloudfront.net)│   Auth0 SPA SDK and Stripe.js)
                  └─────────┬──────────┘
                            │ HTTP (origin)
                            ▼
                  ┌───────────────────┐
                  │   S3 Bucket        │  Static frontend (HTML/CSS/JS)
                  │ (Website Hosting)  │
                  └────────────────────┘

        ┌─────────────────────────────────────────────┐
        │        Auth0 Universal Login (external)       │
        │   Issues JWT access tokens (Authorization      │
        │   Code + PKCE flow) — no custom login UI        │
        └───────────────────┬───────────────────────────┘
                             │ Bearer token
                             ▼
                 ┌────────────────────────┐
                 │  API Gateway (HTTP API)  │
                 │   + Auth0 JWT Authorizer  │
                 └───┬─────────┬─────────┬──┘
                     │         │         │
         POST /book  │  GET /availability │ GET /status
                     ▼         ▼         ▼
             ┌───────────┐ ┌───────────┐ ┌───────────┐
             │  Booking  │ │Availability│ │  Status   │
             │  Lambda   │ │  Lambda    │ │  Lambda   │
             └─────┬─────┘ └─────┬──────┘ └─────┬─────┘
                   │             │              │
        Stripe hold│             │ read-only    │ polls Stripe
       (manual cap.)│            │ DynamoDB     │ PaymentIntent
                   │             ▼              │ status
                   │       ┌───────────┐        │
                   │       │ DynamoDB   │◄───────┘  
                   │       │  Table     │
                   ▼       └─────▲──────┘
             ┌───────────┐       │
             │    SQS     │      │ conditional write
             │   Queue    │      │ attribute_not_exists(PK)
             │  (+ DLQ)   │      │
             └─────┬──────┘      │
                   │             │
                   ▼             │
             ┌───────────┐       │
             │  Worker    │──────┘
             │  Lambda    │
             │ (VPC,      │───► Stripe: capture (win) or cancel (lose race)
             │ private    │
             │ subnet)    │
             └─────┬──────┘
                   │ via VPC Gateway Endpoint (private routing,
                   ▼ no NAT/internet traversal for DynamoDB calls)
             ┌───────────┐
             │ DynamoDB   │
             │  Table     │
             └───────────┘
```

### Duplicate-Booking Prevention (Core Design)


1. **Booking Lambda** receives the request, authorizes (not charges) a $10 Stripe deposit hold using `capture_method="manual"`, and pushes a job to SQS. It responds `202 Accepted` immediately — this is a "queued," not a "confirmed," response.
2. **Worker Lambda** (running in a private VPC subnet) consumes the SQS message and attempts a DynamoDB write with:
   ```python
   table.put_item(
       Item=item,
       ConditionExpression="attribute_not_exists(PK)"
   )
   ```
   where `PK = STYLIST#<ID>#<DATE>#<TIME>` and `SK` is a **fixed** value (`"BOOKING"`) — not randomized. Because DynamoDB's primary key is the `(PK, SK)` pair together, every concurrent booking attempt for the same slot competes for the *identical* composite key.
3. **First write wins.** The Worker Lambda captures that customer's Stripe deposit hold, confirming the charge.
4. **Every subsequent write fails** with `ConditionalCheckFailedException`. The Worker Lambda catches this and cancels that customer's payment hold — no charge is ever made, and no manual refund process is needed.
5. **The frontend polls** a dedicated `/status` endpoint (backed by a Stripe PaymentIntent status check) for a few seconds after submitting, so each customer is told the real, final outcome — "🎉 Confirmed" or "😔 Sorry, that slot was just taken" — rather than trusting the initial `202`.

---

## Getting Started / Installation Guide

### Prerequisites

- **AWS Account** with an IAM user/role that has permissions for CloudFormation, VPC, Lambda, DynamoDB, API Gateway, SQS, S3, CloudFront, Secrets Manager, and IAM role creation
- **AWS CLI**, configured (`aws configure`) with the account above
- **Python 3.11**
- **Node.js 20+** (for the AWS CDK CLI, invoked via `npx` — no global install or Docker required)
- **An Auth0 account** (free tier is sufficient) with:
  - A **Single Page Application** registered, with **Authorization Code** and **Refresh Token** grant types enabled
  - An **API** registered with identifier `https://salon-booking-api` (or your own identifier)
  - That application authorized against that API (Application Access / Client Access) for **both** `client` and `user` subject types
- **A Stripe account** (test mode is sufficient) with a publishable key and secret key

### Environment Variables & Configuration

| Variable | Where it's used | Example |
|---|---|---|
| `AUTH0_DOMAIN` | CDK context / GitHub secret | `your-tenant.us.auth0.com` |
| `AUTH0_AUDIENCE` | CDK context / GitHub secret | `https://salon-booking-api` |
| `FRONTEND_ORIGIN` | GitHub secret | `https://xxxxxxxxxxxx.cloudfront.net` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | GitHub secrets (CI deploy) | — |
| Stripe secret key | AWS Secrets Manager, **not** an env var | stored at `salon/stripe/secret` as `{"STRIPE_SECRET_KEY": "sk_test_..."}` |
| `STRIPE_PUBLISHABLE_KEY` | Hardcoded in `frontend/index.html` (safe to be public) | `pk_test_...` |
| `AUTH0_CLIENT_ID` | Hardcoded in `frontend/index.html` | — |

### Installation & Deployment

1. **Clone the repo:**
   ```bash
   git clone https://github.com/HussainAzimi/salon-booking-capstone.git
   cd salon-booking-capstone
   ```

2. **Create the Stripe secret in AWS Secrets Manager:**
   ```bash
   aws secretsmanager create-secret \
     --name salon/stripe/secret \
     --secret-string '{"STRIPE_SECRET_KEY":"sk_test_YOUR_KEY"}' \
     --region us-east-1
   ```

3. **Install Python dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Install each Lambda's third-party dependencies into its own asset folder** (no Docker bundling is used, so this step does what `PythonFunction`'s Docker build would otherwise do):
   ```bash
   pip install -r lambda/booking/requirements.txt -t lambda/booking --upgrade
   pip install -r lambda/worker/requirements.txt -t lambda/worker --upgrade
   pip install -r lambda/status/requirements.txt -t lambda/status --upgrade
   ```

5. **Deploy the stack:**
   ```bash
   npx aws-cdk@2 bootstrap
   npx aws-cdk@2 deploy --all --require-approval never \
     -c auth0_domain="your-tenant.us.auth0.com" \
     -c auth0_audience="https://salon-booking-api"
   ```

6. **Grab the outputs** (frontend HTTPS URL, API base URL, bucket name):
   ```bash
   aws cloudformation describe-stacks --stack-name SalonBookingStack \
     --query "Stacks[0].Outputs"
   ```

7. **Update `frontend/index.html`** with the real `API_BASE_URL`, `STRIPE_PUBLISHABLE_KEY`, `AUTH0_DOMAIN`, `AUTH0_CLIENT_ID`, and `AUTH0_AUDIENCE`, then sync it to the bucket:
   ```bash
   aws s3 sync ./frontend s3://YOUR_BUCKET_NAME --delete
   ```

8. **Update Auth0** with the deployed CloudFront URL under Allowed Callback URLs, Allowed Logout URLs, and Allowed Web Origins.

> For ongoing changes, push to `main` and let `.github/workflows/deploy.yml` handle steps 4–7 automatically.

---

## Usage & Testing

### Usage Instructions

1. Open the CloudFront HTTPS URL (the raw S3 website URL is HTTP-only and will not work — Auth0 and Stripe.js both require a secure origin).
2. Click **Log in** and authenticate via Auth0 Universal Login.
3. Select a stylist and date — available time slots load automatically, filtered against existing bookings.
4. Fill in your name, pick a time slot, and enter a test card (`4242 4242 4242 4242`, any future expiry, any CVC).
5. Submit. You'll see a brief "⏳ Confirming…" state while the backend resolves the booking, followed by a final confirmation or an alert if the slot was taken.

### Testing the Duplicate-Booking Guarantee

1. Open two browser tabs, both logged in.
2. In both tabs, select the **identical** stylist, date, and time slot.
3. Submit both within a second or two of each other.
4. Expected result:
   - Exactly **one** DynamoDB item exists for that slot.
   - In the Stripe Dashboard (test mode), one payment shows **Succeeded**, the other **Canceled**.
   - One browser tab shows "🎉 Booking Confirmed," the other shows "😔 Sorry, that slot was just taken" with an explicit note that no charge occurred.

### Testing Payment Failure Paths

Stripe provides dedicated test cards for exercising the "unsuccessful request" user story:

| Scenario | Test card |
|---|---|
| Generic decline | `4000 0000 0000 0002` |
| Insufficient funds | `4000 0000 0000 9995` |
| Requires authentication (3D Secure) | `4000 0025 0000 3155` |

---

## Future Enhancements & Challenges

### Lessons Learned

- **DynamoDB's primary key is the full `(PK, SK)` pair, not just `PK`.** Early iterations randomized `SK` per write (thinking it just needed to be "unique"), which silently defeated the entire `ConditionExpression`-based duplicate-prevention mechanism — both racing writes succeeded because they landed at two different composite keys. The fix was making `SK` a fixed value per logical resource, so concurrent writes for the same slot genuinely compete for the same key.
- **S3 static website hosting cannot serve HTTPS**, full stop — no ACM certificate option exists for it. Since both Auth0's SPA SDK and Stripe.js refuse to initialize over plain HTTP, CloudFront turned out to be a required piece of the architecture, not an optional nice-to-have, even for a "just get it working" capstone scope.
- **Auth0 has two independent authorization layers** for a custom API: one for `client`-subject grants (machine-to-machine style) and a separate one for `user`-subject grants (interactive Universal Login). A client can be fully authorized on one and still get rejected on the other — this cost significant debugging time before finding the distinction via Auth0's Management API directly rather than an ambiguous dashboard toggle.
- **`Code.from_asset()` (no Docker) doesn't run `pip install` for me.** Switching away from a Docker-based `PythonFunction` construct (to avoid a Docker dependency in CI) meant manually installing each Lambda's third-party packages into its own source folder before `cdk deploy` — otherwise the deployed zip is missing dependencies and fails at cold start with `ImportModuleError`.
- **An initial `202 Accepted` from an async, queue-based API isn't the same as a final answer.** The first version of this system told both racing customers "success" because that was true at the moment of the HTTP response — the actual win/lose outcome wasn't resolved until the Worker Lambda processed the SQS message afterward. Fixing this required adding a genuine status-polling endpoint so the frontend reports what actually happened, not just what was initially queued.

### Roadmap

- [ ] Replace the availability Lambda's DynamoDB `Scan` with a proper `Query` against a `GSI1PK = STYLIST#<id>#<date>` index for better performance at scale
- [ ] Add a "My Bookings" view using the existing `GSI1PK = USER#<id>` index, so customers can see/cancel their own upcoming appointments
- [ ] Replace short-polling on `/status` with WebSocket (API Gateway WebSocket API) push notifications for instant confirmation instead of a multi-second poll loop
- [ ] Add stylist-side admin views (daily schedule, manual booking management)
- [ ] Add automated tests (unit tests for Lambda handlers, integration tests against a local DynamoDB, contract tests for the Stripe/Auth0 integration boundaries)

---

## Contact & License

**Author:** Hussain Azimi
Advanced Certificate in Applied Computer Science, Washington University in St. Louis (WashU Empower Program)

- LinkedIn: https://www.linkedin.com/in/hussain-azimi/
- Email: hussainazimi.career@gmail.com
- GitHub: [github.com/HussainAzimi](https://github.com/HussainAzimi)
