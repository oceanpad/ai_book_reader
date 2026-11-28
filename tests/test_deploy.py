import json
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from backend.config import Settings
from scripts import deploy_cloud


def test_bootstrap_suppresses_invitation_and_writes_password_once_with_private_permissions(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(deploy_cloud, "STATE", tmp_path)
    monkeypatch.setattr(
        deploy_cloud,
        "FileSettings",
        lambda: Settings(openrouter_api_key="test-router"),
    )
    session = MagicMock()
    cognito, secrets_client = MagicMock(), MagicMock()
    session.client.side_effect = lambda name: cognito if name == "cognito-idp" else secrets_client
    cognito.admin_get_user.side_effect = ClientError(
        {"Error": {"Code": "UserNotFoundException", "Message": "test"}}, "AdminGetUser"
    )
    outputs = {
        "ReaderDataStack": {"ProviderSecretArn": "test-secret"},
        "ReaderAuthStack": {"UserPoolId": "test-pool"},
        "ReaderWebStack": {"ReaderUrl": "https://reader.example.invalid"},
    }
    path = deploy_cloud.bootstrap(session, outputs, "adult@example.invalid")
    original = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert cognito.admin_create_user.call_args.kwargs["MessageAction"] == "SUPPRESS"
    password = json.loads(original)["temporaryPassword"]
    assert password not in capsys.readouterr().out
    cognito.admin_get_user.side_effect = None
    cognito.admin_get_user.return_value = {"UserStatus": "FORCE_CHANGE_PASSWORD"}
    deploy_cloud.bootstrap(session, outputs, "adult@example.invalid")
    assert path.read_bytes() == original
    assert cognito.admin_create_user.call_count == 1
    cognito.admin_get_user.return_value = {"UserStatus": "CONFIRMED"}
    deploy_cloud.bootstrap(session, outputs, "adult@example.invalid")
    assert not path.exists()


def test_publish_includes_cognito_region_without_provider_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_cloud, "ROOT", tmp_path)
    command = MagicMock()
    monkeypatch.setattr(deploy_cloud, "command", command)
    session = MagicMock(region_name="ap-southeast-1")
    outputs = {
        "ReaderAuthStack": {
            "CognitoDomain": "https://login.example.invalid",
            "UserPoolClientId": "client",
        },
        "ReaderWebStack": {"FrontendBucketName": "frontend", "DistributionId": "distribution"},
    }
    deploy_cloud.publish(session, outputs, {"PATH": "/bin", "OPENROUTER_API_KEY": "private"})
    env = command.call_args.args[1]
    assert env["VITE_COGNITO_REGION"] == "ap-southeast-1"
    assert env["VITE_COGNITO_CLIENT_ID"] == "client"
    assert "OPENROUTER_API_KEY" not in env
