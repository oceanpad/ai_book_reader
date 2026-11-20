import pytest
from mangum import Mangum

from backend.api import create_app
from backend.config import Settings


@pytest.mark.parametrize("authenticated,status", [(False, 401), (True, 200)])
def test_config_through_lambda_adapter(authenticated: bool, status: int) -> None:
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
            "authorizer": {"jwt": {"claims": {"sub": "test-owner"}, "scopes": ["reader/read"]}}
            if authenticated
            else {},
        },
        "isBase64Encoded": False,
    }
    handler = Mangum(create_app(Settings(mode="aws")), lifespan="off")
    assert handler(event, {})["statusCode"] == status
