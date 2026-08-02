import json
import os
import logging
import boto3
import stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

STRIPE_SECRET_ARN = os.environ.get("STRIPE_SECRET_ARN")
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN") or "*"

secrets = boto3.client("secretsmanager")

_stripe_key_cache = None


def _get_stripe_key():
    global _stripe_key_cache
    if _stripe_key_cache:
        return _stripe_key_cache
    if not STRIPE_SECRET_ARN:
        raise RuntimeError("STRIPE_SECRET_ARN not configured")
    resp = secrets.get_secret_value(SecretId=STRIPE_SECRET_ARN)
    secret = resp.get("SecretString")
    if not secret:
        raise RuntimeError("Secrets Manager returned an empty secret")
    secret_json = json.loads(secret)
    _stripe_key_cache = secret_json["STRIPE_SECRET_KEY"].strip()
    return _stripe_key_cache


def build_cors_headers():
    return {
        "Access-Control-Allow-Origin": FRONTEND_ORIGIN,
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Methods": "OPTIONS,GET",
    }


# Maps Stripe's raw PaymentIntent status to what the frontend actually needs to know.
# "pending" means the Worker Lambda hasn't resolved the race yet — keep polling.
def _classify(stripe_status):
    if stripe_status == "succeeded":
        return "confirmed"
    if stripe_status == "canceled":
        return "duplicate_slot_taken"
    if stripe_status == "requires_capture":
        return "pending"  # worker hasn't processed the SQS message yet
    return "pending"


def handler(event, context):
    cors_headers = build_cors_headers()
    params = event.get("queryStringParameters") or {}
    payment_intent_id = params.get("payment_intent_id")

    if not payment_intent_id:
        return {
            "statusCode": 400,
            "headers": cors_headers,
            "body": json.dumps({"error": "payment_intent_id query parameter is required"}),
        }

    try:
        stripe.api_key = _get_stripe_key()
        intent = stripe.PaymentIntent.retrieve(payment_intent_id)
    except stripe.error.StripeError as se:
        logger.exception("Stripe error retrieving PaymentIntent")
        return {"statusCode": 502, "headers": cors_headers, "body": json.dumps({"error": str(se)})}
    except Exception:
        logger.exception("Unexpected error checking booking status")
        return {"statusCode": 500, "headers": cors_headers, "body": json.dumps({"error": "Internal server error"})}

    return {
        "statusCode": 200,
        "headers": cors_headers,
        "body": json.dumps({
            "stripe_status": intent.status,
            "booking_status": _classify(intent.status),
        }),
    }