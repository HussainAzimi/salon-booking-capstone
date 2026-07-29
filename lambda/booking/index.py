import json
import os
import logging
import uuid
import boto3
import stripe
from botocore.exceptions import ClientError


logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Fetch environment variables
STRIPE_SECRET_ARN = os.environ.get("STRIPE_SECRET_ARN")
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "*")  # Default to '*' if not set
QUEUE_URL = os.environ.get('QUEUE_URL')
DEPOSIT_AMOUNT_CENTS = int(os.environ.get("DEPOSIT_AMOUNT_CENTS", "1000"))

sqs = boto3.client('sqs')
secrets = boto3.client('secretsmanager')

# Stripe key at cold start
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
    _stripe_key_cache = secret.strip()
    return _stripe_key_cache

def build_cors_headers():
    return {
        "Access-Control-Allow-Origin": FRONTEND_ORIGIN,
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Methods": "OPTIONS,POST,GET"
    }

def validate_payload(body):
    required = ["customer_name", "stylist_id", "date", "time_slot"]
    missing = [k for k in required if k not in body or body.get(k) in (None, "")]
    if missing:
        return False, f"Missing required fields: {', '.join(missing)}"
    return True, None
def get_user_id(event):
    # HTTP API v2 + JWT authorizer puts verified claims here.
    try:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
        return claims.get("sub")
    except (KeyError, TypeError):
        return None
    

def handler(event, context):
    cors_headers = build_cors_headers()
    stripe.api_key = _get_stripe_key()

    request_id = (context.aws_request_id if context else str(uuid.uuid4()))
    logger.info("Booking request received", extra={"request_id": request_id})

    user_id = get_user_id(event)
    if not user_id:
        return {
            "statusCode": 401,
            "headers": cors_headers,
            "body": json.dumps({"error": "Unauthorized: missing or invalid token"}),
        }
    
    try:
        body = json.loads(event.get("body" or "{}"))
    except Exception:
        return {"statusCode": 400, "headers": cors_headers, "body": json.dumps({"error": "Invalid JSON body"})}

    valid, err = validate_payload(body)
    if not valid:
        return {"statusCode": 400, "headers": cors_headers, "body": json.dumps({"error": err})}

    customer_name = body["customer_name"]
    stylist_id = body["stylist_id"]
    date = body["date"]
    time_slot = body["time_slot"]
    payment_method_id = body.get("payment_method_id")
    deposit_amount = DEPOSIT_AMOUNT_CENTS
    client_request_id = body.get("client_request_id") or str(uuid.uuid4())

    idempotency_key = f"booking-{stylist_id}-{date}-{time_slot}-{client_request_id}"

    try:
        if payment_method_id:
            intent = stripe.PaymentIntent.create(
                amount=deposit_amount,
                currency="usd",
                payment_method=payment_method_id,
                confirm=True,
                capture_method="manual",
                description=f"Salon deposit for {customer_name} ({stylist_id}) on {date} at {time_slot}",
                idempotency_key=idempotency_key,
            )
            if intent.status =="requires_action":
                return {
                    "statusCode": 402,
                    "headers": cors_headers,
                    "body": json.dumps({"error": "Payment requires additional action", "client_secret": intent.client_secret}),
                }

            if intent.status not in ("requires_capture", "succeeded"):
                return {
                    "statusCode": 402,
                    "headers": cors_headers,
                    "body": json.dumps({"error": f"Payment could not be authorized (status: {intent.status})"}),
                }
        else:
            intent = stripe.PaymentIntent.create(
                amount=deposit_amount,
                currency="usd",
                automatic_payment_methods={"enabled": True},
                description=f"Salon deposit for {customer_name} ({stylist_id}) on {date} at {time_slot}",
                idempotency_key=idempotency_key,
            )
            return {
                "statusCode": 200,
                "headers": cors_headers,
                "body": json.dumps({"client_secret": intent.client_secret, "message": "Confirm payment on client"}),
            }

    except stripe.error.StripeError as se:
        logger.exception("Stripe error creating PaymentIntent", extra={"request_id": request_id})
        err_msg = getattr(se, "user_message", None) or str(se)
        return {"statusCode": 402, "headers": cors_headers, "body": json.dumps({"error": f"Payment failed: {err_msg}"})}
    except Exception:
        logger.exception("Unexpected error creating PaymentIntent", extra={"request_id": request_id})
        return {"statusCode": 500, "headers": cors_headers, "body": json.dumps({"error": "Internal server error"})}

    payload = {
        "request_id": request_id,
        "client_request_id": client_request_id,
        "user_id": user_id,
        "customer_name": customer_name,
        "stylist_id": stylist_id,
        "date": date,
        "time_slot": time_slot,
        "deposit_amount_cents": deposit_amount,
        "payment_intent_id": intent.id,
        "payment_status": intent.status,
    }

    try:
            send_resp = sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(payload))
            logger.info("Queued booking request", extra={"request_id": request_id, "sqs_message_id": send_resp.get("MessageId")})
    except ClientError:
        logger.exception("Failed to send message to SQS", extra={"request_id": request_id})

        # The Stripe hold was authorized but we couldn't queue the job — cancel the hold
        # rather than leaving an orphaned authorization on the customer's card.
        try:
            stripe.PaymentIntent.cancel(intent.id)
        except Exception:
            logger.exception("Failed to cancel orphaned PaymentIntent", extra={"request_id": request_id})
        return {"statusCode": 502, "headers": cors_headers, "body": json.dumps({"error": "Failed to queue booking request"})}
 
    return {
        "statusCode": 202,
        "headers": cors_headers,
        "body": json.dumps({"message": "Booking request queued", "payment_status": intent.status}),
    }
 