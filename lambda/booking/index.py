import json
import os
import boto3
import stripe

# Fetch environment variables
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
sqs = boto3.client('sqs')
QUEUE_URL = os.environ.get('QUEUE_URL')


def handler(event, context):
    # CORS headers required for browser requests
    cors_headers = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token',
        'Access-Control-Allow-Methods': 'OPTIONS,POST'
    }

    # Handle options preflight request directly
    if event.get('httpMethod') == 'OPTIONS':
        return {
            'statusCode': 200,
            'headers': cors_headers,
            'body': json.dumps({'message': 'CORS check successful'})
        }

    try:
        body = json.loads(event.get('body', '{}'))
        customer_name = body.get('customer_name')
        stylist_id = body.get('stylist_id')
        date = body.get('date')
        time_slot = body.get('time_slot')
        payment_method_id = body.get('payment_method_id', 'pm_card_visa')
        deposit_amount = body.get('deposit_amount', 0) 



        if not all([customer_name, stylist_id, date, time_slot]):
            return {
                'statusCode': 400,
                'headers': cors_headers,
                'body': json.dumps({'error': 'Missing required booking fields'})
            }

        # Charge Deposit via Stripe PaymentIntent
        intent = stripe.PaymentIntent.create(
            amount=deposit_amount,
            currency='usd',
            payment_method=payment_method_id,
            confirm=True,
            automatic_payment_methods={'enabled': True,
                                       'allow_redirects':'never'
                                       },
            description=f"Salon Deposit for {customer_name} ({stylist_id}) on {date} at {time_slot}",
        )
        # Check if Payment Succeeded
        if intent.status != 'succeeded':
            return {
                'statusCode': 402,
                'headers': cors_headers,
                'body': json.dumps({'error': f'Payment failed with status: {intent.status}'})
            }

        # Queue payload to SQS with payment confirmation
        payload = {
            'customer_name': customer_name,
            'stylist_id': stylist_id,
            'date': date,
            'time_slot': time_slot,
            'deposit_amount': deposit_amount / 100,  #Convert cents to dollars
            'payment_intent_id': intent.id,
            'payment_status': intent.status

        }

        sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps(payload)
        )

        return {
            'statusCode': 202,
            'headers': cors_headers,
            'body': json.dumps({'message': 'Deposit Charged and booking request queued for processing.',
                                })
        }

    except Exception as e:
        print(f"Error queuing booking: {str(e)}")
        return {
            'statusCode': 500,
            'headers': cors_headers,
            'body': json.dumps({'error': 'Internal server error'})
        }