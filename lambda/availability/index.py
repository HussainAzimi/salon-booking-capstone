import json
import os
import logging
import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("TABLE_NAME")
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "*")

# All slots the salon offers in a day — booked ones get filtered out below.
ALL_SLOTS = ["09:00", "11:00", "14:00", "16:00"]

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

    # PK format is STYLIST#<ID>#<DATE>#<TIME>, so a begins_with query on the shared
    # STYLIST#<ID>#<DATE># prefix returns every slot already booked that day.
    prefix = f"STYLIST#{stylist_id}#{date}#"
    try:
        resp = table.scan(
            FilterExpression="begins_with(PK, :prefix)",
            ExpressionAttributeValues={":prefix": prefix},
        )
        booked_slots = {item["TimeSlot"] for item in resp.get("Items", [])}
    except Exception:
        logger.exception("Failed to read availability", extra={"stylist_id": stylist_id, "date": date})
        return {"statusCode": 500, "headers": cors_headers, "body": json.dumps({"error": "Internal server error"})}

    available = [slot for slot in ALL_SLOTS if slot not in booked_slots]

    return {
        "statusCode": 200,
        "headers": cors_headers,
        "body": json.dumps({"stylist_id": stylist_id, "date": date, "available_slots": available}),
    }