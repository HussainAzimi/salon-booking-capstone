import json
import os
import logging
import boto3
from datetime import datetime, timedelta
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("TABLE_NAME")
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "*")

# Generate 30-minute appointment slots instead of using hardcoded times.
def generate_time_slots():
    slots = []

    current = datetime.strptime("09:00", "%H:%M")
    end = datetime.strptime("17:00", "%H:%M")

    while current < end:
        slots.append(current.strftime("%I:%M %p"))
        current += timedelta(minutes=30)

    return slots

# Build all available appointment slots automatically.
ALL_SLOTS = generate_time_slots()

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


def build_cors_headers():
    return {
        "Access-Control-Allow-Origin": FRONTEND_ORIGIN,
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Methods": "OPTIONS,GET",
    }


def handler(event, context):
    cors_headers = build_cors_headers()
    params = event.get("queryStringParameters") or {}
    stylist_id = params.get("stylist_id")
    date = params.get("date")

    if not stylist_id or not date:
        return {
            "statusCode": 400,
            "headers": cors_headers,
            "body": json.dumps({"error": "stylist_id and date query parameters are required"}),
        }

    logger.info(
        "Checking availability for stylist=%s date=%s",
        stylist_id,
        date
    )

    # PK format is STYLIST#<ID>#<DATE>#<TIME>, so a begins_with query on the shared
    # STYLIST#<ID>#<DATE># prefix returns every slot already booked that day.
    prefix = f"STYLIST#{stylist_id}#{date}#"
    try:
        resp = table.scan(
            FilterExpression="begins_with(PK, :prefix)",
            ExpressionAttributeValues={":prefix": prefix},
        )
        booked_slots = {item["TimeSlot"] for item in resp.get("Items", [])}

        logger.info(
            "Booked slots: %s",
            list(booked_slots)
        )

    except Exception:
        logger.exception("Failed to read availability", extra={"stylist_id": stylist_id, "date": date})
        return {"statusCode": 500, "headers": cors_headers, "body": json.dumps({"error": "Internal server error"})}

    available = [slot for slot in ALL_SLOTS if slot not in booked_slots]

    logger.info(
       "Available slots returned: %s",
        available
    )

    return {
        "statusCode": 200,
        "headers": cors_headers,
        "body": json.dumps({"stylist_id": stylist_id, "date": date, "available_slots": available}),
    }