"""Deploy the explicitly selected private AWS reader, keeping all credentials off stdout."""

import argparse
import json
import mimetypes
import os
import secrets
import subprocess
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from backend.config import FileSettings

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".local/deployment"
STACKS = ["ReaderDataStack", "ReaderAuthStack", "ReaderApiStack", "ReaderWebStack"]


def command(args: list[str], env: dict[str, str], cwd: Path = ROOT) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)


def cdk(action: str, env: dict[str, str], email: str, origin: str | None) -> None:
    args = ["npx", "--no-install", "cdk", action, *STACKS, "-c", f"alertEmail={email}"]
    if origin:
        args += ["-c", f"webOrigin={origin}"]
    if action == "deploy":
        args += ["--require-approval", "never", "--outputs-file", str(STATE / "outputs.json")]
    command(args, env, ROOT / "infra")


def bootstrap(session: boto3.Session, outputs: dict[str, Any], email: str) -> Path:
    settings = FileSettings()
    keys = {
        "openrouter_api_key": settings.openrouter_api_key.get_secret_value(),
    }
    if not all(keys.values()):
        raise RuntimeError(
            "The OpenRouter credential must be configured server-side before bootstrap"
        )
    session.client("secretsmanager").put_secret_value(
        SecretId=outputs["ReaderDataStack"]["ProviderSecretArn"], SecretString=json.dumps(keys)
    )
    cognito = session.client("cognito-idp")
    pool = outputs["ReaderAuthStack"]["UserPoolId"]
    password_file = STATE / "first-login.json"
    try:
        user = cognito.admin_get_user(UserPoolId=pool, Username=email)
        if user["UserStatus"] == "FORCE_CHANGE_PASSWORD" and not password_file.exists():
            raise RuntimeError(
                "The existing adult account needs password recovery; no password was reset"
            )
        if user["UserStatus"] == "CONFIRMED" and password_file.exists():
            password_file.unlink()
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "UserNotFoundException":
            raise
        password = secrets.token_urlsafe(28) + "aA1!"
        descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as file:
            json.dump(
                {
                    "username": email,
                    "temporaryPassword": password,
                    "url": outputs["ReaderWebStack"]["ReaderUrl"],
                },
                file,
            )
        try:
            cognito.admin_create_user(
                UserPoolId=pool,
                Username=email,
                TemporaryPassword=password,
                MessageAction="SUPPRESS",
                UserAttributes=[
                    {"Name": "email", "Value": email},
                    {"Name": "email_verified", "Value": "true"},
                ],
            )
        except ClientError:
            password_file.unlink()
            raise
    return password_file


def publish(session: boto3.Session, outputs: dict[str, Any], env: dict[str, str]) -> None:
    if not session.region_name:
        raise RuntimeError("Select an AWS region before publishing the frontend")
    auth = outputs["ReaderAuthStack"]
    build_env = {
        name: value
        for name, value in env.items()
        if "OPENAI" not in name and "OPENROUTER" not in name
    }
    build_env.update(
        VITE_COGNITO_DOMAIN=auth["CognitoDomain"],
        VITE_COGNITO_CLIENT_ID=auth["UserPoolClientId"],
        VITE_COGNITO_REGION=session.region_name,
    )
    command(["npm", "run", "build"], build_env)
    s3 = session.client("s3")
    bucket = outputs["ReaderWebStack"]["FrontendBucketName"]
    for path in (ROOT / "frontend/dist").rglob("*"):
        if not path.is_file():
            continue
        key = path.relative_to(ROOT / "frontend/dist").as_posix()
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        s3.upload_file(
            str(path),
            bucket,
            key,
            ExtraArgs={
                "ContentType": content_type,
                "CacheControl": "public,max-age=31536000,immutable"
                if key.startswith("assets/")
                else "no-store",
            },
        )
    session.client("cloudfront").create_invalidation(
        DistributionId=outputs["ReaderWebStack"]["DistributionId"],
        InvalidationBatch={
            "Paths": {"Quantity": 2, "Items": ["/", "/index.html"]},
            "CallerReference": secrets.token_hex(16),
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--email", required=True)
    parser.add_argument("--profile")
    parser.add_argument(
        "--execute", action="store_true", help="Apply the reviewed stacks and publish"
    )
    parser.add_argument(
        "--cleanup-login-file",
        action="store_true",
        help="Remove the temporary password file only after the first password change",
    )
    args = parser.parse_args()
    if "@" not in args.email or "." not in args.email.split("@")[-1]:
        parser.error("A valid adult login/alert email is required")
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    identity = session.client("sts").get_caller_identity()
    if identity["Account"] != args.account:
        raise RuntimeError("Active AWS account does not match the explicitly selected account")
    STATE.mkdir(parents=True, exist_ok=True)
    env = dict(
        os.environ,
        CDK_DEFAULT_ACCOUNT=args.account,
        CDK_DEFAULT_REGION=args.region,
        AWS_DEFAULT_REGION=args.region,
    )
    if args.profile:
        env["AWS_PROFILE"] = args.profile
    target_file = STATE / "target.json"
    target = json.loads(target_file.read_text()) if target_file.exists() else {}
    if target and (target["account"] != args.account or target["region"] != args.region):
        raise RuntimeError(
            "Deployment state belongs to another AWS target; use a separate checkout"
        )
    if target and target["email"] != args.email:
        raise RuntimeError("Adult email changed. Explicit account migration is required.")
    if args.cleanup_login_file:
        outputs = json.loads((STATE / "outputs.json").read_text())
        user = session.client("cognito-idp").admin_get_user(
            UserPoolId=outputs["ReaderAuthStack"]["UserPoolId"],
            Username=args.email,
        )
        if user["UserStatus"] != "CONFIRMED":
            raise RuntimeError(
                "Complete the initial password change before removing its local file"
            )
        password_file = STATE / "first-login.json"
        if password_file.exists():
            password_file.unlink()
        print("Temporary first-login credentials removed.")
        return
    origin = target.get("origin")
    cdk("diff", env, args.email, origin)
    if not args.execute:
        print("Diff only; pass --execute after reviewing the changes.")
        return
    cdk("deploy", env, args.email, origin)
    outputs = json.loads((STATE / "outputs.json").read_text())
    deployed_origin = outputs["ReaderWebStack"]["ReaderUrl"]
    if origin != deployed_origin:
        # Literal origin avoids a cross-stack cycle between Cognito/S3 and CloudFront.
        cdk("diff", env, args.email, deployed_origin)
        cdk("deploy", env, args.email, deployed_origin)
        outputs = json.loads((STATE / "outputs.json").read_text())
    target_file.write_text(
        json.dumps(
            {
                "account": args.account,
                "region": args.region,
                "origin": deployed_origin,
                "email": args.email,
            },
            indent=2,
        )
        + "\n"
    )
    password_file = bootstrap(session, outputs, args.email)
    publish(session, outputs, env)
    print("Private reader URL:", deployed_origin)
    print("Adult username:", args.email)
    if password_file.exists():
        print("Temporary first-login credentials saved with mode 0600 at:", password_file)
    print("Confirm the SNS subscription email to enable operational alerts.")


if __name__ == "__main__":
    main()
