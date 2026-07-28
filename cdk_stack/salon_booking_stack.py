import os
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_s3 as s3,
    aws_dynamodb as dynamodb,
    aws_apigateway as apigateway,
    aws_sqs as sqs,
    aws_lambda as _lambda,
    aws_lambda_event_sources as lambda_events,
    CfnOutput,
)
from constructs import Construct

class SalonBookingStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # 1. VPC Setup (2 Availability Zones, Public + Private Subnets)
        vpc = ec2.Vpc(
            self, "SalonVPC",
            max_azs=2,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="Private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=24
                )
            ]
        )

        # 2. DynamoDB Table with Primary Key (PK = STYLIST#ID#DATE#TIME)
        table = dynamodb.Table(
            self, "AppointmentsTable",
            partition_key=dynamodb.Attribute(
                name="PK",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY  # Change to RETAIN for production
        )

        # 3. DynamoDB VPC Gateway Endpoint (Private routing)
        vpc.add_gateway_endpoint(
            "DynamoDbEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB
        )

        # 4. SQS Queue + Dead-Letter Queue (DLQ)
        dlq = sqs.Queue(
            self, "BookingDLQ",
            retention_period=Duration.days(14)
        )

        booking_queue = sqs.Queue(
            self, "BookingQueue",
            visibility_timeout=Duration.seconds(30),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3,
                queue=dlq
            )
        )

        # 5. Frontend S3 Bucket (Static Site)
        frontend_bucket = s3.Bucket(
            self, "SalonFrontendBucket",
            website_index_document="index.html",
            public_read_access=True,
            block_public_access=s3.BlockPublicAccess(
                block_public_policy=False,
                block_public_acls=False,
                ignore_public_acls=False,
                restrict_public_buckets=False
            ),
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True
        )

        # 6. Booking Lambda Function (Private Subnet)
        booking_lambda = _lambda.Function(
            self, "BookingLambda",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=_lambda.Code.from_asset("lambda/booking"),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            environment={
                "QUEUE_URL": booking_queue.queue_url,
                "STRIPE_SECRET_KEY": os.environ.get("STRIPE_SECRET_KEY", "")
            }
        )
        # IAM Scope: Grant SendMessage permissions to SQS
        booking_queue.grant_send_messages(booking_lambda)

        # 7. Worker Lambda Function (Private Subnet)
        worker_lambda = _lambda.Function(
            self, "WorkerLambda",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=_lambda.Code.from_asset("lambda/worker"),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            environment={
                "TABLE_NAME": table.table_name
            }
        )
        # IAM Scope: SQS Receive & DynamoDB Write
        worker_lambda.add_event_source(lambda_events.SqsEventSource(booking_queue))
        table.grant_write_data(worker_lambda)

        # 8. Stable REST API Gateway Endpoint
        api = apigateway.RestApi(
            self, "SalonBookingApi",
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=apigateway.Cors.ALL_ORIGINS,
                allow_methods=["POST", "OPTIONS"],
                allow_headers=["Content-Type", "X-Amz-Date", "Authorization", "X-Api-Key", "X-Amz-Security-Token"]
            )
       ) 

        # POST /book Route
        book_resource = api.root.add_resource("book")
        book_resource.add_method(
            "POST",
            apigateway.LambdaIntegration(booking_lambda) # type: ignore[arg-type]
        )

        # 9. Stack Outputs (CRITICAL for GitHub Actions & Frontend)
        CfnOutput(
            self, "SalonFrontendBucketName",
            value=frontend_bucket.bucket_name,
            description="Frontend S3 Bucket Name for GitHub Actions deployment"
        )

        CfnOutput(
            self, "SalonFrontendWebsiteUrl",
            value=frontend_bucket.bucket_website_url,
            description="URL for the hosted Frontend Website"
        )

        CfnOutput(
            self, "SalonBookingApiUrl",
            value=api.url,
            description="Base URL for REST API Gateway"
        )