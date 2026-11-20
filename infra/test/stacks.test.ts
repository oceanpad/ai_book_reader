import { App } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { test } from "vitest";
import { ReaderApiStack, ReaderAuthStack, ReaderDataStack, ReaderWebStack } from "../lib/stacks.js";

test("foundation keeps media private and protects all non-health AWS routes", () => {
  const app = new App();
  const env = { account: "111111111111", region: "ap-southeast-1" };
  const data = new ReaderDataStack(app, "TestData", { env });
  const auth = new ReaderAuthStack(app, "TestAuth", { env });
  const api = new ReaderApiStack(app, "TestApi", { env, data, userPool: auth.userPool, userPoolClient: auth.userPoolClient, codePath: "backend" });
  const web = new ReaderWebStack(app, "TestWeb", { env, apiDomain: api.apiDomain, cognitoDomain: auth.domain, media: data.media });
  Template.fromStack(data).hasResourceProperties("AWS::SQS::Queue", {
    FifoQueue: true, ContentBasedDeduplication: true, RedrivePolicy: Match.objectLike({ maxReceiveCount: 3 }),
  });
  Template.fromStack(data).hasResourceProperties("AWS::S3::Bucket", {
    PublicAccessBlockConfiguration: { BlockPublicAcls: true, BlockPublicPolicy: true, IgnorePublicAcls: true, RestrictPublicBuckets: true },
    VersioningConfiguration: Match.absent(),
    LifecycleConfiguration: { Rules: [
      Match.objectLike({ Prefix: "incoming/", ExpirationInDays: 1 }),
      Match.objectLike({ Prefix: "audio/", ExpirationInDays: 1 }),
    ] },
  });
  Template.fromStack(data).hasResourceProperties("AWS::DynamoDB::Table", {
    DeletionProtectionEnabled: true,
    StreamSpecification: { StreamViewType: "NEW_AND_OLD_IMAGES" },
    GlobalSecondaryIndexes: Match.arrayWith([
      Match.objectLike({ IndexName: "MaintenanceIndex" }),
      Match.objectLike({ IndexName: "OwnerCapturesIndex", KeySchema: [
        { AttributeName: "owner", KeyType: "HASH" }, { AttributeName: "createdAt", KeyType: "RANGE" },
      ] }),
      Match.objectLike({ IndexName: "BookCapturesIndex", KeySchema: [
        { AttributeName: "ownerBook", KeyType: "HASH" }, { AttributeName: "createdAt", KeyType: "RANGE" },
      ] }),
    ]),
  });
  Template.fromStack(auth).hasResourceProperties("AWS::Cognito::UserPool", { AdminCreateUserConfig: { AllowAdminCreateUserOnly: true } });
  Template.fromStack(auth).hasResourceProperties("AWS::Cognito::UserPoolClient", {
    GenerateSecret: false,
    AllowedOAuthFlows: ["code"],
    AllowedOAuthScopes: Match.arrayWith([
      { "Fn::Join": ["", [{ Ref: Match.anyValue() }, "/read"]] },
      { "Fn::Join": ["", [{ Ref: Match.anyValue() }, "/write"]] },
    ]),
  });
  Template.fromStack(auth).hasResourceProperties("AWS::Cognito::UserPoolResourceServer", {
    Identifier: "reader",
    Scopes: Match.arrayWith([Match.objectLike({ ScopeName: "read" }), Match.objectLike({ ScopeName: "write" })]),
  });
  const apiTemplate = Template.fromStack(api);
  apiTemplate.resourceCountIs("AWS::ApiGatewayV2::Route", 17);
  for (const route of ["POST /api/books/{book_id}/completions", "PATCH /api/books/{book_id}", "POST /api/books/{book_id}/cover", "DELETE /api/books/{book_id}", "POST /api/readings/{job_id}/narration"]) {
    apiTemplate.hasResourceProperties("AWS::ApiGatewayV2::Route", { RouteKey: route, AuthorizationType: "JWT", AuthorizationScopes: ["reader/write"] });
  }
  apiTemplate.hasResourceProperties("AWS::ApiGatewayV2::Route", { RouteKey: "GET /api/books", AuthorizationType: "JWT", AuthorizationScopes: ["reader/read"] });
  apiTemplate.hasResourceProperties("AWS::ApiGatewayV2::Route", { RouteKey: "POST /api/books", AuthorizationType: "JWT", AuthorizationScopes: ["reader/write"] });
  apiTemplate.hasResourceProperties("AWS::ApiGatewayV2::Route", { RouteKey: "GET /api/readings/{job_id}/original", AuthorizationType: "JWT", AuthorizationScopes: ["reader/read"] });
  apiTemplate.hasResourceProperties("AWS::ApiGatewayV2::Route", { RouteKey: "GET /api/readings/{job_id}/thumbnail", AuthorizationType: "JWT", AuthorizationScopes: ["reader/read"] });
  apiTemplate.hasResourceProperties("AWS::ApiGatewayV2::Route", { RouteKey: "GET /api/readings", AuthorizationType: "JWT", AuthorizationScopes: ["reader/read"] });
  apiTemplate.hasResourceProperties("AWS::ApiGatewayV2::Route", { RouteKey: "GET /api/config", AuthorizationType: "JWT", AuthorizationScopes: ["reader/read"] });
  apiTemplate.hasResourceProperties("AWS::ApiGatewayV2::Route", { RouteKey: "GET /api/health", AuthorizationType: "NONE" });
  apiTemplate.hasResourceProperties("AWS::Lambda::Function", { Runtime: "python3.12", Architectures: ["arm64"], Environment: { Variables: Match.objectLike({ READER_MODE: "aws" }) } });
  apiTemplate.resourceCountIs("AWS::Lambda::Function", 4);
  apiTemplate.hasResourceProperties("AWS::Lambda::Function", { Handler: "backend.worker.handler", ReservedConcurrentExecutions: Match.absent() });
  apiTemplate.hasResourceProperties("AWS::Lambda::EventSourceMapping", { BatchSize: 1, FunctionResponseTypes: ["ReportBatchItemFailures"], ScalingConfig: { MaximumConcurrency: 2 } });
  apiTemplate.hasResourceProperties("AWS::ApiGatewayV2::Route", { RouteKey: "POST /api/readings/{job_id}", AuthorizationType: "JWT", AuthorizationScopes: ["reader/write"] });
  Template.fromStack(web).hasResourceProperties("AWS::CloudFront::Distribution", {
    DistributionConfig: Match.objectLike({ CacheBehaviors: Match.arrayWith([Match.objectLike({ PathPattern: "/api/*", CachePolicyId: "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" })]) }),
  });
});
