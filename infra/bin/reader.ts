import { App } from "aws-cdk-lib";
import { ReaderApiStack, ReaderAuthStack, ReaderDataStack, ReaderWebStack } from "../lib/stacks.js";

const app = new App();
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION ?? "ap-southeast-1",
};
const webOrigin = app.node.tryGetContext("webOrigin") as string | undefined;
const alertEmail = app.node.tryGetContext("alertEmail") as string | undefined;
const data = new ReaderDataStack(app, "ReaderDataStack", { env, webOrigin });
const auth = new ReaderAuthStack(app, "ReaderAuthStack", { env, webOrigin });
const api = new ReaderApiStack(app, "ReaderApiStack", {
  env,
  userPool: auth.userPool,
  userPoolClient: auth.userPoolClient,
  data, alertEmail,
});
new ReaderWebStack(app, "ReaderWebStack", { env, apiDomain: api.apiDomain, cognitoDomain: auth.domain, media: data.media });
