import json
import os
import boto3

sqs = boto3.client('sqs')
QUEUE_URL = os.environ.get('QUEUE_URL')

def handler(event, context):
    try:
        # Parse incoming JSON body
        body = json.loads(event.get('body', '{}'))
        customer_name = body.get('customer_name')
        stylist_id = body.get('stylist_id')
        date = body.get('date')          # Format: YYYY-MM-DD
        time_slot = body.get('time_slot') # Format: HH:MM

        # Basic Validation
        if not all([customer_name, stylist_id, date, time_slot]):
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Missing required booking fields'})
            }

        # Send message to SQS Queue
        payload = {
            'customer_name': customer_name,
            'stylist_id': stylist_id,
            'date': date,
            'time_slot': time_slot
        }

        sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps(payload)
        )

        return {
            'statusCode': 202,
            'body': json.dumps({'message': 'Booking request received and queued for processing.'})
        }

    except Exception as e:
        print(f"Error queuing booking: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': 'Internal server error'})
        }