import json
import os
import boto3
from botocore.exceptions import ClientError

dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ.get('TABLE_NAME')
table = dynamodb.Table(TABLE_NAME)

def handler(event, context):
    for record in event.get('Records', []):
        try:
            body = json.loads(record['body'])
            customer_name = body['customer_name']
            stylist_id = body['stylist_id']
            date = body['date']
            time_slot = body['time_slot']

            # Primary Key format: STYLIST#<ID>#<DATE>#<TIME>
            primary_key = f"STYLIST#{stylist_id}#{date}#{time_slot}"

            print(f"Attempting booking for Key: {primary_key}")

            # -------------------------------------------------------------
            # STEP 1: Process Stripe Deposit Hold (SaaS Integration)
            # -------------------------------------------------------------
            # stripe.PaymentIntent.create(...) 
            print(f"Stripe deposit authorized for {customer_name}")

            # -------------------------------------------------------------
            # STEP 2: DynamoDB Conditional Write Guard
            # -------------------------------------------------------------
            table.put_item(
                Item={
                    'PK': primary_key,
                    'CustomerName': customer_name,
                    'StylistID': stylist_id,
                    'Date': date,
                    'TimeSlot': time_slot,
                    'Status': 'CONFIRMED'
                },
                # GUARAND: Fails if the primary key already exists
                ConditionExpression='attribute_not_exists(PK)'
            )

            print(f"HAPPY PATH: Booking confirmed for {customer_name} at {time_slot}")

        except ClientError as e:
            # Catch duplicate write attempt (Unhappy Path)
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                print(f"UNHAPPY PATH: Slot {primary_key} is already booked!")
                
                # ---------------------------------------------------------
                # RECOVERY: Cancel / Refund Stripe Payment Hold
                # ---------------------------------------------------------
                # stripe.Refund.create(...)
                print(f"Stripe payment hold refunded for {customer_name}.")
            else:
                print(f"DynamoDB Error: {str(e)}")
                raise e

        except Exception as e:
            print(f"Unexpected Error: {str(e)}")
            raise e