# SPECVision marketplace backend (phase 1)

Built on the repo's FastAPI + SQLModel + PostgreSQL + Alembic stack (not Node/TypeScript).
This phase delivers the security-critical backend core. **Everything not listed under
"Operational" is NOT implemented**; related features are off in the `feature_flag` table.

## Architecture
`backend/app/marketplace/` contains the domain logic; routes live in
`backend/app/api/routes/marketplace_*.py` and `paypal_webhook.py`.

| Module | Purpose |
|---|---|
| `models.py`, migration `34360a491f30` | Schema: profile, membership_plan, subscription, subscription_payment, project, bid, lead_unlock, seller_account, milestone, payment, webhook_event, project_modifier, discount_rule, feature_flag, platform_setting, audit_log |
| `entitlements.py` | Server-side tier/feature checks; lead fee is read from the plan row and never waived |
| `privacy.py`, `contact_scanner.py` | Per-role project views (preview / protected / owner) and contact-exchange detection |
| `paypal.py` | PayPal REST client behind a `PayPalGateway` protocol (subscriptions, orders with delayed disbursement, capture, refund, webhook verification) |
| `payments.py` | Payment state machine (all 13 states), milestone funding/approval, refunds, release-deadline queue |
| `webhooks.py` | Idempotent, signature-verified event ingestion; raw body stored; failed events retryable via admin |
| `catalog.py` | Seeds 8 plans (no PayPal plan IDs), modifiers, inactive discount rule, feature flags |
| `remote_support.py` | Placeholder provider that always refuses sessions |

Rules enforced: membership and payment status change only from verified webhooks; browser
redirects never grant access; funds are "held" only when PayPal reports `DELAYED` disbursement;
direct-payment fallback needs explicit client acceptance; customer wording is "protected project
payment", not "escrow".

## Operational (covered by tests, `uv run pytest`)
Subscription entitlements, plan/modifier catalog and quote, project posting with contact scanning,
sanitized browse, bids/shortlist/award, lead unlock via verified capture, milestone fund/approve/
release/refund, webhook idempotency, admin webhook viewer/reprocess, release queue, fee and plan
mapping settings, audit logs, access control.

## Not implemented / flagged off
React frontend and membership comparison page, email verification and MFA flows beyond the template,
seller onboarding link creation (`POST /seller/onboard` returns 501), estimator, project-management
suites (AV/IT Elite), white-label portal, in-platform messaging, file upload and malware scanning,
reviews, disputes/support workflows, notifications, encryption of contact fields at rest, live
remote support (placeholder only), support-staff role, account deletion, Docker/deploy tuning for
these features. Verify in the PayPal sandbox before relying on: payout webhook payload shape, the
`DELAY_FUNDS_DISBURSEMENT` capability name, the 90-day hold limit, referenced-payouts endpoint.

## Setup
```bash
cd backend && uv sync
sudo pg_ctlcluster 16 main start   # or docker compose up db
uv run bash scripts/prestart.sh    # migrations + seed catalog
FASTAPI_ENV=development uv run pytest tests
```

## PayPal sandbox
1. Create a sandbox REST app; set `PAYPAL_ENV=sandbox`, `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`
   in `.env` (never commit real values).
2. Create products/plans in PayPal (7-day $0 trial, then monthly/annual) and map each with
   `PUT /api/v1/admin/plans/{code}`. Plan IDs are never hard-coded.
3. Add a webhook to `https://<host>/api/v1/webhooks/paypal` and set `PAYPAL_WEBHOOK_ID`.
4. Enable partner/marketplace features for delayed disbursement and set
   `PAYPAL_PARTNER_MERCHANT_ID`, `PAYPAL_PARTNER_BN_CODE`.
Marketplace fee: admin setting `marketplace_fee_bps` (default 0).
Demo/seed data is sandbox-only and must be labeled as such; do not run against live credentials.

## Deployment
Use the existing Docker Compose setup; provide secrets via environment variables, set
`ENVIRONMENT=production`, `PAYPAL_ENV=live` only after sandbox verification.
