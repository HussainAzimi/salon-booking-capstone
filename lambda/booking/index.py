import json
import os
import logging
import uuid
import boto3
import stripe
from botocore.exceptions import ClientError


logger = logging.getLogger()
logger.setLevel(logging.INFO)

# -----------------------------------------------------------------------------
# Fetch environment variables
# -----------------------------------------------------------------------------
STRIPE_SECRET_ARN = os.environ.get("STRIPE_SECRET_ARN")
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN") or "*"  # Default to '*' if not set
QUEUE_URL = os.environ.get('QUEUE_URL')
DEPOSIT_AMOUNT_CENTS = int(os.environ.get("DEPOSIT_AMOUNT_CENTS", "1000"))

sqs = boto3.client('sqs')
secrets = boto3.client('secretsmanager')

_stripe_key_cache = None  # Cache for the Stripe secret key

# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------
def build_cors_headers():
    return {
        "Access-Control-Allow-Origin": FRONTEND_ORIGIN,
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Methods": "OPTIONS,POST,GET"
    }

def get_stripe_key():
    global _stripe_key_cache

    if _stripe_key_cache:
        return _stripe_key_cache

    if not STRIPE_SECRET_ARN:
        raise RuntimeError("Missing STRIPE_SECRET_ARN environment variable")

    response = secrets.get_secret_value(
        SecretId=STRIPE_SECRET_ARN
    )

    secret = response.get("SecretString")

    if not secret:
        raise RuntimeError("Stripe secret not found")

    _stripe_key_cache = secret.strip()

    return _stripe_key_cache


def validate_payload(body):

    required = [
        "customer_name",
        "stylist_id",
        "date",
        "time_slot"
    ]

    missing = []

    for field in required:
        if body.get(field) in ("", None):
            missing.append(field)

    if missing:
        return False, f"Missing required fields: {', '.join(missing)}"

    return True, None


def get_user_id(event):

    try:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
        return claims.get("sub")
    except Exception:
        return None
# -----------------------------------------------------------------------------
# Lambda Handler
# -----------------------------------------------------------------------------

def handler(event, context):

    headers = build_cors_headers()

    logger.info("Incoming Event: %s", json.dumps(event))

    # Handle CORS preflight
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return {
            "statusCode": 200,
            "headers": headers
        }

    # Configure Stripe
    try:
        stripe.api_key = get_stripe_key()
    except Exception as e:
        logger.exception("Unable to load Stripe Secret")

        return {
            "statusCode": 500,
            "headers": headers,
            "body": json.dumps({
                "error": str(e)
            })
        }

    request_id = context.aws_request_id

    logger.info("Request ID: %s", request_id)

    # Authentication
    user_id = get_user_id(event)

    if not user_id:

        return {
            "statusCode": 401,
            "headers": headers,
            "body": json.dumps({
                "error": "Unauthorized"
            })
        }

    # Parse JSON Body
    try:

        body = json.loads(event.get("body", "{}"))

    except json.JSONDecodeError:

        return {
            "statusCode": 400,
            "headers": headers,
            "body": json.dumps({
                "error": "Invalid JSON"
            })
        }

    # Validate Input
    valid, error = validate_payload(body)

    if not valid:

        return {
            "statusCode": 400,
            "headers": headers,
            "body": json.dumps({
                "error": error
            })
        }

    customer_name = body["customer_name"]
    stylist_id = body["stylist_id"]
    date = body["date"]
    time_slot = body["time_slot"]

    payment_method_id = body.get("payment_method_id")

    client_request_id = body.get(
        "client_request_id",
        str(uuid.uuid4())
    )

    idempotency_key = (
        f"{stylist_id}-{date}-{time_slot}-{client_request_id}"
    )

    # -------------------------------------------------------------------------
    # Stripe
    # -------------------------------------------------------------------------

    try:

        if payment_method_id:

            intent = stripe.PaymentIntent.create(
                amount=DEPOSIT_AMOUNT_CENTS,
                currency="usd",
                payment_method=payment_method_id,
                confirm=True,
                capture_method="manual",
                description=f"Salon Booking - {customer_name}",
                idempotency_key=idempotency_key,
            )

            if intent.status == "requires_action":

                return {
                    "statusCode": 402,
                    "headers": headers,
                    "body": json.dumps({
                        "client_secret": intent.client_secret,
                        "status": intent.status
                    })
                }

            if intent.status not in [
                "requires_capture",
                "succeeded"
            ]:

                return {
                    "statusCode": 402,
                    "headers": headers,
                    "body": json.dumps({
                        "error": intent.status
                    })
                }

        else:

            intent = stripe.PaymentIntent.create(
                amount=DEPOSIT_AMOUNT_CENTS,
                currency="usd",
                automatic_payment_methods={
                    "enabled": True
                },
                description=f"Salon Booking - {customer_name}",
                idempotency_key=idempotency_key,
            )

            return {
                "statusCode": 200,
                "headers": headers,
                "body": json.dumps({
                    "client_secret": intent.client_secret
                })
            }

    except stripe.error.StripeError as e:

        logger.exception("Stripe Error")

        return {
            "statusCode": 402,
            "headers": headers,
            "body": json.dumps({
                "error": str(e)
            })
        }

    except Exception:

        logger.exception("Unexpected Stripe Error")

        return {
            "statusCode": 500,
            "headers": headers,
            "body": json.dumps({
                "error": "Stripe Failure"
            })
        }

    # -------------------------------------------------------------------------
    # Queue Booking
    # -------------------------------------------------------------------------

    payload = {

        "request_id": request_id,

        "client_request_id": client_request_id,

        "user_id": user_id,

        "customer_name": customer_name,

        "stylist_id": stylist_id,

        "date": date,

        "time_slot": time_slot,

        "deposit_amount_cents": DEPOSIT_AMOUNT_CENTS,

        "payment_intent_id": intent.id,

        "payment_status": intent.status,
    }

    try:

        sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps(payload)
        )

    except ClientError:

        logger.exception("SQS Failure")

        try:
            stripe.PaymentIntent.cancel(intent.id)
        except Exception:
            logger.exception("Unable to cancel PaymentIntent")

        return {
            "statusCode": 502,
            "headers": headers,
            "body": json.dumps({
                "error": "Unable to queue booking"
            })
        }

    logger.info("Booking queued successfully")

    return {
        "statusCode": 202,
        "headers": headers,
        "body": json.dumps({
            "message": "Booking queued",
            "payment_status": intent.status
        })
    }