import json
import os
import boto3

sqs = boto3.client('sqs')
QUEUE_URL = os.environ.get('QUEUE_URL')

def handler(event, context):
    # CORS headers required for browser requests
    cors_headers = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token',
        'Access-Control-Allow-Methods': 'OPTIONS,POST'
    }

    try:
        body = json.loads(event.get('body', '{}'))
        customer_name = body.get('customer_name')
        stylist_id = body.get('stylist_id')
        date = body.get('date')
        time_slot = body.get('time_slot')

        if not all([customer_name, stylist_id, date, time_slot]):
            return {
                'statusCode': 400,
                'headers': cors_headers,
                'body': json.dumps({'error': 'Missing required booking fields'})
            }

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
            'headers': cors_headers,
            'body': json.dumps({'message': 'Booking request received and queued for processing.'})
        }

    except Exception as e:
        print(f"Error queuing booking: {str(e)}")
        return {
            'statusCode': 500,
            'headers': cors_headers,
            'body': json.dumps({'error': 'Internal server error'})
        }