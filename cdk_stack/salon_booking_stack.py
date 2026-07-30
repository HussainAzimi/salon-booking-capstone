import os
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_s3 as s3,
    aws_dynamodb as dynamodb,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as integrations,
    aws_apigatewayv2_authorizers as authorizers,
    aws_sqs as sqs,
    aws_lambda as _lambda,
    aws_lambda_python_alpha as python_lambda,
    aws_lambda_event_sources as lambda_events,
    aws_secretsmanager as secretsmanager,
    aws_logs as logs,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    CfnOutput,
)
from constructs import Construct

class SalonBookingStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        AUTH0_DOMAIN = self.node.try_get_context("auth0_domain") or os.environ.get(
            "AUTH0_DOMAIN", "dev-bycoupho2ffes63s.us.auth0.com"
        )
        AUTH0_AUDIENCE = self.node.try_get_context("auth0_audience") or os.environ.get(
            "AUTH0_AUDIENCE", "https://salon-booking-api"
        )
        FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN") or "*"
        DEPOSIT_AMOUNT_CENTS = "1000"  # $10.00 
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

        # 2. DynamoDB Table with Primary Key and SK, TTL, and a GSI for stylist queries (PK = STYLIST#ID#DATE#TIME)
        table = dynamodb.Table(
            self, "AppointmentsTable",
            partition_key=dynamodb.Attribute(
                name="PK",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="SK",
                type=dynamodb.AttributeType.STRING
            ),

            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY  # Change to RETAIN for production
        )

        table.add_global_secondary_index(
            index_name="GSI1",
            partition_key=dynamodb.Attribute(
                name="GSI1PK",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="GSI1SK",
                type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL
        )

        # 3. DynamoDB VPC Gateway Endpoint (Private routing)
        vpc.add_gateway_endpoint("S3Endpoint",service=ec2.GatewayVpcEndpointAwsService.S3)
        vpc.add_gateway_endpoint("DynamoDBEndpoint", service=ec2.GatewayVpcEndpointAwsService.DYNAMODB)
        vpc.add_interface_endpoint("SecretsManagerEndpoint", service=ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER)
        vpc.add_interface_endpoint("STSEndpoint", service=ec2.InterfaceVpcEndpointAwsService.STS)

        # 4. SQS Queue + Dead-Letter Queue (DLQ)
        dlq = sqs.Queue(
            self, "BookingDLQ",
            retention_period=Duration.days(14)
        )

        booking_queue = sqs.Queue(
            self, "BookingQueue",
            visibility_timeout=Duration.seconds(60),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3,
                queue=dlq
            ),
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

        # 5b. CloudFront distribution in front of the S3 website endpoint.
        #     S3 static website hosting only ever serves plain HTTP — it cannot serve
        #     HTTPS directly, even with a public bucket. Auth0's SPA SDK refuses to run
        #     at all on an insecure origin, and Stripe.js requires HTTPS outside test
        #     mode, so the site must be served through something that terminates TLS.
        #     CloudFront's default *.cloudfront.net domain comes with a valid HTTPS
        #     certificate out of the box — no ACM certificate or custom domain needed.

        frontend_distribution = cloudfront.Distribution(
            self, "SalonFrontendDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.HttpOrigin(
                    frontend_bucket.bucket_website_domain_name,
                    protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            default_root_object="index.html",
        )

        # 6. Stripe secret in AWS Secrets Manager
        stripe_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "StripesSecret",
            secret_name="salon/stripe/secret"
        )

        # 7. Booking Lambda Function (Private Subnet)
        booking_lambda = python_lambda.PythonFunction(
            self,
            "BookingLambda",

            runtime=_lambda.Runtime.PYTHON_3_11,

            entry="lambda/booking",
            index="index.py",
            handler="handler",

            timeout=Duration.seconds(20),
            memory_size=512,

            tracing=_lambda.Tracing.ACTIVE,

            log_retention=logs.RetentionDays.ONE_WEEK,

            environment={
                "QUEUE_URL": booking_queue.queue_url,
                "STRIPE_SECRET_ARN": stripe_secret.secret_arn,
                "FRONTEND_ORIGIN": FRONTEND_ORIGIN,
                "DEPOSIT_AMOUNT_CENTS": DEPOSIT_AMOUNT_CENTS,
            },
        )
        booking_queue.grant_send_messages(booking_lambda)
        stripe_secret.grant_read(booking_lambda)

        # 8. Availability Lambda — read-only query so the frontend can show real open slots.
        availability_lambda = _lambda.Function(
            self, "AvailabilityLambda",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=_lambda.Code.from_asset("lambda/availability"),
            timeout=Duration.seconds(10),
            memory_size=256,
            log_retention=logs.RetentionDays.ONE_WEEK,
            environment={
                "TABLE_NAME": table.table_name,
                "FRONTEND_ORIGIN": FRONTEND_ORIGIN,
            },
        )
        table.grant_read_data(availability_lambda)

        # 9. Worker Lambda — runs in the private subnet, does the conditional write + refund logic.
        worker_lambda = python_lambda.PythonFunction(
            self, "WorkerLambda",
            runtime=_lambda.Runtime.PYTHON_3_11,
            entry="lambda/worker",
            index="index.py",
            handler="handler",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            timeout=Duration.seconds(30),
            memory_size=512,
            tracing=_lambda.Tracing.ACTIVE,
            log_retention=logs.RetentionDays.ONE_WEEK,
            environment={
                "TABLE_NAME": table.table_name,
                "STRIPE_SECRET_ARN": stripe_secret.secret_arn,
            },
        )
       
        worker_lambda.add_event_source(lambda_events.SqsEventSource(booking_queue, batch_size=1))
        table.grant_write_data(worker_lambda)
        stripe_secret.grant_read(worker_lambda)

         # 10. HTTP API (v2) with an Auth0 JWT authorizer.
        jwt_authorizer = authorizers.HttpJwtAuthorizer(
        "Auth0Authorizer",
        jwt_issuer=f"https://{AUTH0_DOMAIN}/",
        jwt_audience=[AUTH0_AUDIENCE],
        )

        http_api = apigwv2.HttpApi(
            self, "SalonBookingApiV2",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"] if FRONTEND_ORIGIN == "*" else [FRONTEND_ORIGIN],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["Content-Type","Authorization"],
            ),
        ) 

        http_api.add_routes(
            path="/book",
            methods=[apigwv2.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("BookingIntegration", booking_lambda),
            authorizer=jwt_authorizer,
        )

        http_api.add_routes(
            path="/availability",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("AvailabilityIntegration", availability_lambda),
            authorizer=jwt_authorizer,
        )

        # 11. Stack Outputs (CRITICAL for GitHub Actions & Frontend)
        CfnOutput(self, "SalonFrontendBucketName", value=frontend_bucket.bucket_name,
                  description="Frontend S3 bucket name for GitHub Actions deployment")
        CfnOutput(self, "SalonFrontendWebsiteUrl", value=frontend_bucket.bucket_website_url,
                  description="Raw S3 website URL — HTTP only, will NOT work with Auth0/Stripe. Use the CloudFront URL below instead.")
        CfnOutput(self, "SalonFrontendHttpsUrl", value=f"https://{frontend_distribution.distribution_domain_name}",
                  description="HTTPS URL to actually use/share — required by Auth0 and Stripe.js")
        CfnOutput(self, "SalonFrontendDistributionId", value=frontend_distribution.distribution_id,
                  description="CloudFront distribution ID, used by CI to invalidate the cache after deploys")
        CfnOutput(self, "SalonBookingApiUrl", value=http_api.api_endpoint,
                  description="Base URL for the HTTP API (routes: POST /book, GET /availability)")
        CfnOutput(self, "AppointmentsTableName", value=table.table_name, description="DynamoDB table name")
        CfnOutput(self, "BookingQueueUrl", value=booking_queue.queue_url, description="SQS booking queue URL")
        CfnOutput(self, "StripeSecretArn", value=stripe_secret.secret_arn,
                  description="Secrets Manager ARN for the Stripe secret")