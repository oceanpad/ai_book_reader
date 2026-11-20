import { fileURLToPath } from "node:url";
import {
  CfnOutput,
  Duration,
  Fn,
  RemovalPolicy,
  Stack,
  type StackProps,
  aws_apigatewayv2 as apigw,
  aws_cloudfront as cloudfront,
  aws_cloudfront_origins as origins,
  aws_cognito as cognito,
  aws_dynamodb as dynamodb,
  aws_lambda as lambda,
  aws_logs as logs,
  aws_s3 as s3,
  aws_sqs as sqs,
  aws_secretsmanager as secrets,
  aws_sns as sns,
  aws_sns_subscriptions as subscriptions,
  aws_cloudwatch as cloudwatch,
  aws_cloudwatch_actions as actions,
  aws_events as events,
  aws_events_targets as targets,
} from "aws-cdk-lib";
import { HttpJwtAuthorizer } from "aws-cdk-lib/aws-apigatewayv2-authorizers";
import { HttpLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import { DynamoEventSource, SqsEventSource, SqsDlq } from "aws-cdk-lib/aws-lambda-event-sources";
import { Construct } from "constructs";

export class ReaderDataStack extends Stack {
  readonly table: dynamodb.Table;
  readonly media: s3.Bucket;
  readonly queue: sqs.Queue;
  readonly deadLetters: sqs.Queue;
  readonly providerSecret: secrets.Secret;

  constructor(scope: Construct, id: string, props: StackProps & { webOrigin?: string } = {}) {
    super(scope, id, props);
    const table = this.table = new dynamodb.Table(this, "ReaderTable", {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
      timeToLiveAttribute: "expiresAt",
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      deletionProtection: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    table.addGlobalSecondaryIndex({
      indexName: "MaintenanceIndex",
      partitionKey: { name: "workState", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "updatedAt", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    table.addGlobalSecondaryIndex({
      indexName: "OwnerCapturesIndex",
      partitionKey: { name: "owner", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "createdAt", type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ["state"],
    });
    this.media = new s3.Bucket(this, "ReaderMedia", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      versioned: false,
      removalPolicy: RemovalPolicy.RETAIN,
      lifecycleRules: [
        { prefix: "incoming/", expiration: Duration.days(1), abortIncompleteMultipartUploadAfter: Duration.days(1) },
        { prefix: "audio/", expiration: Duration.days(1) },
        // Published thumbnails and audio under saved/ have no time-based expiry.
      ],
      cors: props.webOrigin ? [{ allowedOrigins: [props.webOrigin], allowedMethods: [s3.HttpMethods.GET], allowedHeaders: ["Range"], exposedHeaders: ["Content-Length", "Content-Range", "Accept-Ranges"], maxAge: 300 }] : undefined,
    });
    const deadLetters = this.deadLetters = new sqs.Queue(this, "ProcessingDeadLetters", {
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: Duration.days(14),
    });
    this.queue = new sqs.Queue(this, "ProcessingQueue", {
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      visibilityTimeout: Duration.minutes(60),
      retentionPeriod: Duration.days(1),
      deadLetterQueue: { queue: deadLetters, maxReceiveCount: 3 },
    });
    this.providerSecret = new secrets.Secret(this, "ProviderCredentials", {
      description: "Server-only book reader provider credentials; populated outside CloudFormation",
    });
    new CfnOutput(this, "TableName", { value: this.table.tableName });
    new CfnOutput(this, "MediaBucketName", { value: this.media.bucketName });
    new CfnOutput(this, "ProviderSecretArn", { value: this.providerSecret.secretArn });
    new CfnOutput(this, "QueueUrl", { value: this.queue.queueUrl });
  }
}

export class ReaderAuthStack extends Stack {
  readonly userPool: cognito.UserPool;
  readonly userPoolClient: cognito.UserPoolClient;
  readonly domain: string;

  constructor(scope: Construct, id: string, props: StackProps & { webOrigin?: string } = {}) {
    super(scope, id, props);
    this.userPool = new cognito.UserPool(this, "AdultAccounts", {
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      autoVerify: { email: true },
      passwordPolicy: { minLength: 12, requireDigits: true, requireLowercase: true, requireUppercase: true, requireSymbols: true },
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const read = new cognito.ResourceServerScope({ scopeName: "read", scopeDescription: "Read private book sessions" });
    const write = new cognito.ResourceServerScope({ scopeName: "write", scopeDescription: "Manage private book sessions" });
    const server = this.userPool.addResourceServer("ReaderScopes", {
      identifier: "reader",
      scopes: [read, write],
    });
    this.userPoolClient = this.userPool.addClient("BrowserClient", {
      generateSecret: false,
      preventUserExistenceErrors: true,
      enableTokenRevocation: true,
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.resourceServer(server, read), cognito.OAuthScope.resourceServer(server, write)],
        callbackUrls: [props.webOrigin ? `${props.webOrigin}/auth/callback` : "http://localhost:5173/auth/callback"],
        logoutUrls: [props.webOrigin ? `${props.webOrigin}/` : "http://localhost:5173/"],
      },
    });
    const domain = this.userPool.addDomain("ManagedLogin", {
      cognitoDomain: { domainPrefix: `little-pages-${this.account}-${this.region}` },
    });
    this.domain = domain.baseUrl();
    new CfnOutput(this, "UserPoolId", { value: this.userPool.userPoolId });
    new CfnOutput(this, "UserPoolClientId", { value: this.userPoolClient.userPoolClientId });
    new CfnOutput(this, "CognitoDomain", { value: this.domain });
  }
}

export class ReaderApiStack extends Stack {
  readonly apiDomain: string;

  constructor(scope: Construct, id: string, props: StackProps & {
    userPool: cognito.IUserPool;
    userPoolClient: cognito.IUserPoolClient;
    data: ReaderDataStack;
    alertEmail?: string;
    codePath?: string;
  }) {
    super(scope, id, props);
    const code = lambda.Code.fromAsset(props.codePath ?? fileURLToPath(new URL("../../.build/lambda", import.meta.url)));
    const environment = {
      READER_MODE: "aws", READER_AWS_REGION: this.region,
      READER_TABLE_NAME: props.data.table.tableName,
      READER_MEDIA_BUCKET: props.data.media.bucketName,
      READER_QUEUE_URL: props.data.queue.queueUrl,
      READER_SECRET_ARN: props.data.providerSecret.secretArn,
    };
    const apiFunction = new lambda.Function(this, "HealthApi", {
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "backend.handler.handler",
      code,
      memorySize: 512,
      timeout: Duration.seconds(28),
      environment,
      logGroup: new logs.LogGroup(this, "ApiLogs", { retention: logs.RetentionDays.TWO_WEEKS }),
    });
    props.data.table.grantReadWriteData(apiFunction);
    props.data.media.grantReadWrite(apiFunction);
    const worker = new lambda.Function(this, "ReadingWorker", {
      runtime: lambda.Runtime.PYTHON_3_12, architecture: lambda.Architecture.ARM_64,
      handler: "backend.worker.handler", code, memorySize: 1024,
      timeout: Duration.minutes(10), environment,
      logGroup: new logs.LogGroup(this, "WorkerLogs", { retention: logs.RetentionDays.TWO_WEEKS }),
    });
    const dispatcher = new lambda.Function(this, "JobDispatcher", {
      runtime: lambda.Runtime.PYTHON_3_12, architecture: lambda.Architecture.ARM_64,
      handler: "backend.dispatcher.handler", code, memorySize: 256,
      timeout: Duration.seconds(30), environment,
      logGroup: new logs.LogGroup(this, "DispatcherLogs", { retention: logs.RetentionDays.TWO_WEEKS }),
    });
    const maintenance = new lambda.Function(this, "Maintenance", {
      runtime: lambda.Runtime.PYTHON_3_12, architecture: lambda.Architecture.ARM_64,
      handler: "backend.maintenance.handler", code, memorySize: 256,
      timeout: Duration.minutes(5), environment,
      logGroup: new logs.LogGroup(this, "MaintenanceLogs", { retention: logs.RetentionDays.TWO_WEEKS }),
    });
    for (const fn of [worker, maintenance]) {
      props.data.table.grantReadWriteData(fn);
      props.data.media.grantReadWrite(fn);
    }
    props.data.providerSecret.grantRead(worker);
    props.data.queue.grantSendMessages(dispatcher);
    props.data.queue.grantSendMessages(maintenance);
    dispatcher.addEventSource(new DynamoEventSource(props.data.table, {
      startingPosition: lambda.StartingPosition.TRIM_HORIZON, batchSize: 10,
      retryAttempts: 5, bisectBatchOnError: true, reportBatchItemFailures: true,
      onFailure: new SqsDlq(props.data.deadLetters),
    }));
    worker.addEventSource(new SqsEventSource(props.data.queue, {
      batchSize: 1, reportBatchItemFailures: true, maxConcurrency: 2,
    }));
    new events.Rule(this, "RepairAndExpire", {
      schedule: events.Schedule.rate(Duration.minutes(5)), targets: [new targets.LambdaFunction(maintenance)],
    });
    const alerts = new sns.Topic(this, "ReaderAlerts");
    if (props.alertEmail) alerts.addSubscription(new subscriptions.EmailSubscription(props.alertEmail));
    for (const [name, fn] of [["Api", apiFunction], ["Worker", worker], ["Dispatcher", dispatcher], ["Maintenance", maintenance]] as const) {
      const alarm = new cloudwatch.Alarm(this, `${name}Errors`, { metric: fn.metricErrors(), threshold: 1, evaluationPeriods: 1, treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING });
      alarm.addAlarmAction(new actions.SnsAction(alerts));
    }
    for (const [name, metric, threshold] of [["DeadLetters", props.data.deadLetters.metricApproximateNumberOfMessagesVisible(), 1], ["QueueAge", props.data.queue.metricApproximateAgeOfOldestMessage(), 600]] as const) {
      new cloudwatch.Alarm(this, name, { metric, threshold, evaluationPeriods: 1, treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING }).addAlarmAction(new actions.SnsAction(alerts));
    }
    const api = new apigw.HttpApi(this, "ReaderHttpApi", { apiName: "ai-book-reader" });
    const integration = new HttpLambdaIntegration("ReaderApiIntegration", apiFunction);
    const issuer = `https://cognito-idp.${this.region}.${this.urlSuffix}/${props.userPool.userPoolId}`;
    const authorizer = new HttpJwtAuthorizer("ReaderJwt", issuer, {
      jwtAudience: [props.userPoolClient.userPoolClientId],
    });
    api.addRoutes({ path: "/api/health", methods: [apigw.HttpMethod.GET], integration });
    api.addRoutes({ path: "/api/config", methods: [apigw.HttpMethod.GET], integration, authorizer, authorizationScopes: ["reader/read"] });
    for (const [route, method, scope] of [
      ["/api/readings", apigw.HttpMethod.GET, "reader/read"],
      ["/api/readings/{job_id}", apigw.HttpMethod.POST, "reader/write"],
      ["/api/readings/{job_id}", apigw.HttpMethod.GET, "reader/read"],
      ["/api/readings/{job_id}", apigw.HttpMethod.DELETE, "reader/write"],
      ["/api/readings/{job_id}/retry", apigw.HttpMethod.POST, "reader/write"],
      ["/api/readings/{job_id}/audio/{chunk}", apigw.HttpMethod.GET, "reader/read"],
      ["/api/readings/{job_id}/thumbnail", apigw.HttpMethod.GET, "reader/read"],
    ] as const) api.addRoutes({ path: route, methods: [method], integration, authorizer, authorizationScopes: [scope] });
    const stage = api.defaultStage?.node.defaultChild as apigw.CfnStage;
    stage.defaultRouteSettings = { throttlingBurstLimit: 20, throttlingRateLimit: 10 };
    const accessLogs = new logs.LogGroup(this, "ApiAccessLogs", { retention: logs.RetentionDays.TWO_WEEKS });
    stage.accessLogSettings = { destinationArn: accessLogs.logGroupArn, format: JSON.stringify({ requestId: "$context.requestId", routeKey: "$context.routeKey", status: "$context.status" }) };
    this.apiDomain = Fn.select(2, Fn.split("/", api.apiEndpoint));
    new CfnOutput(this, "ApiEndpoint", { value: api.apiEndpoint });
    new CfnOutput(this, "WorkerFunctionName", { value: worker.functionName });
    new CfnOutput(this, "MaintenanceFunctionName", { value: maintenance.functionName });
    new CfnOutput(this, "AlertTopicArn", { value: alerts.topicArn });
  }
}

export class ReaderWebStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps & { apiDomain: string; cognitoDomain: string; media: s3.IBucket }) {
    super(scope, id, props);
    const bucket = new s3.Bucket(this, "ReaderFrontend", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const origin = origins.S3BucketOrigin.withOriginAccessControl(bucket);
    const rewrite = new cloudfront.Function(this, "SpaRewrite", {
      code: cloudfront.FunctionCode.fromInline("function handler(event){var r=event.request;if(r.uri==='/'||!r.uri.includes('.'))r.uri='/index.html';return r;}"),
    });
    const headers = new cloudfront.ResponseHeadersPolicy(this, "ReaderHeaders", {
      securityHeadersBehavior: {
        contentSecurityPolicy: {
          contentSecurityPolicy: `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: https://${props.media.bucketRegionalDomainName}; media-src 'self' blob:; connect-src 'self' ${props.cognitoDomain} https://${props.media.bucketRegionalDomainName}; object-src 'none'; base-uri 'self'; frame-ancestors 'none'`,
          override: true,
        },
        contentTypeOptions: { override: true },
        frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
        referrerPolicy: { referrerPolicy: cloudfront.HeadersReferrerPolicy.NO_REFERRER, override: true },
        strictTransportSecurity: { accessControlMaxAge: Duration.days(365), override: true },
      },
      customHeadersBehavior: { customHeaders: [{ header: "Permissions-Policy", value: "camera=(self), microphone=()", override: true }] },
    });
    const distribution = new cloudfront.Distribution(this, "ReaderDistribution", {
      defaultRootObject: "index.html",
      defaultBehavior: {
        origin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        responseHeadersPolicy: headers,
        functionAssociations: [{ eventType: cloudfront.FunctionEventType.VIEWER_REQUEST, function: rewrite }],
      },
      additionalBehaviors: {
        "/assets/*": { origin, viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS, cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED, responseHeadersPolicy: headers },
        "/api/*": {
          origin: new origins.HttpOrigin(props.apiDomain),
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
          responseHeadersPolicy: headers,
        },
      },
    });
    new CfnOutput(this, "FrontendBucketName", { value: bucket.bucketName });
    new CfnOutput(this, "ReaderUrl", { value: `https://${distribution.distributionDomainName}` });
    new CfnOutput(this, "DistributionId", { value: distribution.distributionId });
  }
}
