import json
import os
import logging
import time
import uuid
import boto3
import stripe
from botocore.exceptions import ClientError
from decimal import Decimal
from datetime import datetime, timezone


logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get('TABLE_NAME')
STRIPE_SECRET_ARN = os.environ.get("STRIPE_SECRET_ARN")
STRIPE_RETRY_ATTEMPTS = int(os.environ.get("STRIPE_RETRY_ATTEMPTS", "3"))
STRIPE_RETRY_BACKOFF = float(os.environ.get("STRIPE_RETRY_BACKOFF", "0.5"))

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(TABLE_NAME)
secrets = boto3.client("secretsmanager")

_stripe_key_cache = None

def _get_stripe_key():
    global _stripe_key_cache

    if _stripe_key_cache:
        return _stripe_key_cache

    if not STRIPE_SECRET_ARN:
        raise RuntimeError("STRIPE_SECRET_ARN not configured")

    resp = secrets.get_secret_value(
        SecretId=STRIPE_SECRET_ARN
    )

    secret = resp.get("SecretString")

    if not secret:
        raise RuntimeError("Secrets Manager returned an empty secret")

    
    secret_json = json.loads(secret)
    _stripe_key_cache = secret_json["STRIPE_SECRET_KEY"].strip()

    logger.info(
        "Stripe key loaded successfully. Prefix: %s",
        _stripe_key_cache[:7]
    )

    return _stripe_key_cache


def _with_retries(fn, *, label, payment_intent_id):
    last_exc = None
    for attempt in range(1, STRIPE_RETRY_ATTEMPTS + 1):
        try:
            result = fn()
            logger.info(f"{label} succeeded", extra={"payment_intent_id": payment_intent_id, "attempt": attempt})
            return result
        except stripe.error.StripeError as se:
            last_exc = se
            logger.warning(f"{label} attempt failed", extra={"attempt": attempt, "error": str(se)})
            time.sleep(STRIPE_RETRY_BACKOFF * attempt)
        except Exception as e:
            last_exc = e
            logger.exception(f"Unexpected error during {label}", extra={"attempt": attempt})
            time.sleep(STRIPE_RETRY_BACKOFF * attempt)
    logger.error(f"{label} failed after retries", extra={"payment_intent_id": payment_intent_id})
    raise last_exc

def _capture_payment(payment_intent_id):
    return _with_retries(
        lambda: stripe.PaymentIntent.capture(payment_intent_id),
        label="Capture",
        payment_intent_id=payment_intent_id,
    )
def _cancel_payment(payment_intent_id):
    return _with_retries(
        lambda: stripe.PaymentIntent.cancel(payment_intent_id),
        label="Cancel",
        payment_intent_id=payment_intent_id,
    )

def handler(event, context):
    stripe.api_key = _get_stripe_key()

    batch_id = str(uuid.uuid4())
    logger.info("Worker invoked", extra={"batch_id": batch_id, "records": len(event.get("Records", []))})

    for record in event.get('Records', []):
        record_id = record.get("messageId", str(uuid.uuid4()))     
        try:
            body = json.loads(record.get('body', {}))
        except Exception:
            logger.exception("Invalid SQS message body; raising to trigger retry/DLQ", extra={"record_id": record_id})
            raise
        customer_name = body.get('customer_name')
        user_id = body.get("user_id")
        stylist_id = body.get('stylist_id')
        stylist_name = body.get("stylist_name")
        date = body.get('date')
        time_slot = body.get('time_slot')
        payment_intent_id = body.get('payment_intent_id')
        booking_reference = body.get("booking_reference") or record_id[:8].upper()
        deposit_amount_cents = body.get("deposit_amount_cents")
        

        try:
            deposit_amount_cents = int(deposit_amount_cents)
        except (TypeError, ValueError):
            logger.error("Invalid deposit_amount_cents; raising to trigger retry/DLQ", extra={"record_id": record_id, "value": deposit_amount_cents})
            raise
        # Create a booking reference that customers can use when contacting the salon.
        # booking_reference = body.get("booking_reference") or record_id[:8].upper()

        # Primary Key format: STYLIST#<ID>#<DATE>#<TIME>
        primary_key = f"STYLIST#{stylist_id}#{date}#{time_slot}"
        item = {
            "PK": primary_key,
            "SK": "BOOKING", # MUST stay static — do not randomize, or duplicate-booking prevention breaks silently.
            "UserId": user_id,
            "CustomerName": customer_name,
            "StylistID": stylist_id,
            "StylistName": stylist_name,
            "Date": date,
            "TimeSlot": time_slot,
            "DepositAmountCents": Decimal(deposit_amount_cents),
            "PaymentIntentId": payment_intent_id,
            "PaymentStatus": "CONFIRMED",
            "BookingReference": booking_reference,
            "CreatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "CreatedAtEpoch": int(time.time()),  # kept as a number for sorting/filtering if ever needed.
            # GSI so a user can list their own bookings later.
            "GSI1PK": f"USER#{user_id}",
            "GSI1SK": f"{date}#{time_slot}",
        }

        logger.info("Attempting conditional write", extra={"record_id": record_id, "pk": primary_key},)

        try:
            table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
            logger.info("Booking recorded", extra={"record_id": record_id, "pk": primary_key},)

            if payment_intent_id:
                try:
                    _capture_payment(payment_intent_id)
                except Exception:
                    logger.exception("Payment capture failed; raising to trigger retry/DLQ", extra={"record_id": record_id, "payment_intent_id": payment_intent_id},
                    )
                    
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "ConditionalCheckFailedException":
                logger.warning("Duplicate booking detected; initiating refund", extra={"record_id": record_id, "payment_intent_id": payment_intent_id},
                )
                
                if payment_intent_id:
                    try:
                        _cancel_payment(payment_intent_id)
                        logger.info(
                            "Payment hold released for duplicate booking",
                            extra={"record_id": record_id, "payment_intent_id": payment_intent_id},
                        )
                    except Exception:
                        logger.exception(
                            "Failed to release payment hold after retries; raising to trigger retry/DLQ",
                            extra={"record_id": record_id, "payment_intent_id": payment_intent_id},
                        )
                        raise
                else:
                    logger.error("No payment_intent_id present for duplicate booking; cannot release hold",
                                 extra={"record_id": record_id})
                # Duplicate handled — move on to the next message.
                continue
            else:
                logger.exception("Unhandled DynamoDB ClientError; re-raising to trigger retry", extra={"record_id": record_id})
                raise
        except Exception:
            logger.exception("Unexpected error writing to DynamoDB; re-raising to trigger retry", extra={"record_id": record_id})
            raise
 
    logger.info("Worker batch processing complete", extra={"batch_id": batch_id})
    return {"status": "ok"}
 