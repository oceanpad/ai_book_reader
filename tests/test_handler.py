import pytest
from mangum import Mangum

from backend.api import create_app
from backend.config import Settings


@pytest.mark.parametrize(
    "scopes,token_use,status",
    [
        ([], "access", 401),
        (["reader/read"], "access", 200),
        (["aws.cognito.signin.user.admin"], "access", 200),
        (["aws.cognito.signin.user.admin"], "id", 401),
    ],
)
def test_config_through_lambda_adapter(scopes: list[str], token_use: str, status: int) -> None:
    event = {
        "version": "2.0",
        "routeKey": "GET /api/config",
        "rawPath": "/api/config",
        "rawQueryString": "",
        "headers": {"host": "reader.example.invalid"},
        "requestContext": {
            "http": {
                "method": "GET",
                "path": "/api/config",
                "sourceIp": "127.0.0.1",
                "protocol": "HTTP/1.1",
            },
            "authorizer": {
                "jwt": {"claims": {"sub": "test-owner", "token_use": token_use}, "scopes": scopes}
            }
            if scopes
            else {},
        },
        "isBase64Encoded": False,
    }
    handler = Mangum(create_app(Settings(mode="aws")), lifespan="off")
    assert handler(event, {})["statusCode"] == status
