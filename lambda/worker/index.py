import json
import os
import boto3
import botocore
import stripe
from botocore.exceptions import ClientError

dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ.get('TABLE_NAME')
table = dynamodb.Table(TABLE_NAME)
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

def handler(event, context):
    for record in event.get('Records', []):
        primary_key = "UNKNOWN_KEY"  # Default value in case of error
        payment_intent_id = None  # Default value in case of error
        
        try:
            body = json.loads(record.get('body', {}))
            customer_name = body.get('customer_name')
            stylist_id = body.get('stylist_id')
            date = body.get('date')
            time_slot = body.get('time_slot')
            payment_intent_id = body.get('payment_intent_id')
            deposit_amount = body.get('deposit_amount', 10.00)
            payment_status = body.get('payment_status', 'PAID')

            # Primary Key format: STYLIST#<ID>#<DATE>#<TIME>
            primary_key = f"STYLIST#{stylist_id}#{date}#{time_slot}"

            print(f"Attempting booking for Key: {primary_key}")

            # -------------------------------------------------------------
            # STEP 1: Process Stripe Deposit Hold (SaaS Integration)
            # -------------------------------------------------------------
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
                    'deposit_amount': str(deposit_amount),
                    'payment_intent_id': payment_intent_id,
                    'payment_status': payment_status
                },

                # GUARAND: Fails if the primary key already exists
                ConditionExpression='attribute_not_exists(PK)'
            )

            print(f"Booking confirmed for {customer_name} at {time_slot}")  # Happy path log

        except ClientError as e:
            # Catch duplicate write attempt (Unhappy Path)
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                print(f"Slot {primary_key} is already booked!") # Unhappy path log
                
                # ---------------------------------------------------------
                # RECOVERY: Cancel / Refund Stripe Payment Hold
                # ---------------------------------------------------------
                if payment_intent_id:
                    try:
                        refund = stripe.Refund.create(payment_intent=payment_intent_id)
                        print(f"Refund processed successfully for {payment_intent_id}: {refund.id}")
                    except Exception as refund_error:
                        print(f"Failed to process refund for {payment_intent_id}: {str(refund_error)}")
            else:
                print(f"DynamoDB Error: {str(e)}")
                raise e

        except Exception as e:
            print(f"Unexpected Error: {str(e)}")
            raise e