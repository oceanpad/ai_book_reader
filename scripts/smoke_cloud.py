"""Isolated live AWS/OAuth/provider smoke; never prints passwords, tokens or signed URLs."""

import argparse
import base64
import hashlib
import json
import secrets
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from uuid import uuid4

import boto3
import httpx

from backend.cloud import CloudStore
from backend.config import Settings
from scripts.smoke_providers import EXPECTED_TEXT, synthetic_spread


class Forms(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self.current = {"action": attributes.get("action", ""), "values": {}, "password": False}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None:
            if attributes.get("type") == "password":
                self.current["password"] = True
            if attributes.get("type") == "hidden" and attributes.get("name"):
                self.current["values"][attributes["name"]] = attributes.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.current = None


def login(domain: str, client_id: str, origin: str, username: str, password: str) -> str:
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    redirect = origin + "/auth/callback"
    url = (
        domain
        + "/oauth2/authorize?"
        + urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "scope": "openid email reader/read reader/write",
                "redirect_uri": redirect,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    code = None
    with httpx.Client(timeout=45, follow_redirects=False) as client:
        response = client.get(url)
        for _ in range(10):
            if response.is_redirect:
                target = urljoin(str(response.url), response.headers["location"])
                if target.startswith(redirect + "?"):
                    params = parse_qs(urlsplit(target).query)
                    if params.get("state") != [state] or not params.get("code"):
                        raise RuntimeError("Managed OAuth redirect could not be verified")
                    code = params["code"][0]
                    break
                if urlsplit(target).hostname != urlsplit(domain).hostname:
                    raise RuntimeError("Managed login redirected to an unexpected host")
                response = client.get(target)
                continue
            form = Forms()
            form.feed(response.text)
            matches = [entry for entry in form.forms if entry["password"]]
            if response.status_code != 200 or not matches:
                raise RuntimeError(
                    f"Managed login form was unavailable (HTTP {response.status_code})"
                )
            values = {
                **matches[0]["values"],
                "username": username,
                "password": password,
                "signInSubmitButton": "Sign in",
            }
            action = urljoin(str(response.url), matches[0]["action"] or str(response.url))
            if urlsplit(action).hostname != urlsplit(domain).hostname:
                raise RuntimeError("Managed login form had an unexpected target")
            response = client.post(action, data=values)
        if code is None:
            raise RuntimeError(
                "Managed login did not return an OAuth code; interactive login must be checked"
            )
        response = client.post(
            domain + "/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "redirect_uri": redirect,
                "code": code,
                "code_verifier": verifier,
            },
        )
        if response.status_code != 200:
            raise RuntimeError(f"OAuth token exchange failed (HTTP {response.status_code})")
        value = response.json().get("access_token")
        if not isinstance(value, str):
            raise RuntimeError("OAuth returned no access token")
        return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Allow temporary users and billable synthetic reading"
    )
    parser.add_argument("--profile")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to authorize the isolated deployed smoke check")
    state_dir = Path(".local/deployment")
    target = json.loads((state_dir / "target.json").read_text())
    outputs = json.loads((state_dir / "outputs.json").read_text())
    session = boto3.Session(profile_name=args.profile, region_name=target["region"])
    if session.client("sts").get_caller_identity()["Account"] != target["account"]:
        raise RuntimeError("Active account does not match deployment state")
    data, auth, api = (
        outputs[name] for name in ("ReaderDataStack", "ReaderAuthStack", "ReaderApiStack")
    )
    store = CloudStore(
        Settings(
            mode="aws",
            aws_region=target["region"],
            table_name=data["TableName"],
            media_bucket=data["MediaBucketName"],
            queue_url=data["QueueUrl"],
            secret_arn=data["ProviderSecretArn"],
        ),
        session=session,
    )
    cognito = session.client("cognito-idp")
    pool = auth["UserPoolId"]
    users: list[str] = []
    owner = None
    run_id, job_id = str(uuid4()), str(uuid4())
    results: dict[str, Any] = {"origin": target["origin"], "passed": False}
    try:
        tokens = []
        for _ in range(2):
            username = "reader-smoke-" + secrets.token_hex(8) + "@example.invalid"
            password = secrets.token_urlsafe(28) + "aA1!"
            cognito.admin_create_user(
                UserPoolId=pool,
                Username=username,
                TemporaryPassword=password,
                MessageAction="SUPPRESS",
                UserAttributes=[
                    {"Name": "email", "Value": username},
                    {"Name": "email_verified", "Value": "true"},
                ],
            )
            users.append(username)
            cognito.admin_set_user_password(
                UserPoolId=pool, Username=username, Password=password, Permanent=True
            )
            if owner is None:
                attributes = cognito.admin_get_user(UserPoolId=pool, Username=username)[
                    "UserAttributes"
                ]
                owner = next(entry["Value"] for entry in attributes if entry["Name"] == "sub")
            tokens.append(
                login(
                    auth["CognitoDomain"],
                    auth["UserPoolClientId"],
                    target["origin"],
                    username,
                    password,
                )
            )
        results["oauth_pkce_login"] = True
        origin = target["origin"]
        path = origin + "/api/readings/" + job_id
        headers = {"Authorization": "Bearer " + tokens[0], "X-Reader-Run": run_id}
        foreign = {**headers, "Authorization": "Bearer " + tokens[1]}
        with httpx.Client(timeout=45) as client:
            assert client.get(origin + "/api/health").status_code == 200
            assert client.get(origin + "/api/config").status_code in (401, 403)
            configured = client.get(origin + "/api/config", headers=headers)
            assert configured.status_code == 200, (
                f"Authenticated config returned HTTP {configured.status_code}"
            )
            assert configured.json()["recognition_available"]
            start = time.monotonic()
            response = client.post(
                path, headers={**headers, "Content-Type": "image/jpeg"}, content=synthetic_spread()
            )
            assert response.status_code == 202, (
                f"Capture submission returned HTTP {response.status_code}"
            )
            assert client.get(path, headers=foreign).status_code == 404
            assert client.get(path + "/audio/0", headers=foreign).status_code == 404
            results["ownership_isolation"] = True
            while time.monotonic() - start < 240:
                response = client.get(path, headers=headers)
                assert response.status_code == 200, (
                    f"Capture status returned HTTP {response.status_code}"
                )
                job = response.json()
                if job["state"] not in ("UPLOADING", "QUEUED", "PROCESSING"):
                    break
                time.sleep(1)
            assert job["state"] == "READY", (
                f"Reading outcome: {job['state']}; {job.get('reason', 'no reason')}"
            )
            assert " ".join(job["narration"].split()) == EXPECTED_TEXT
            results["ready_seconds"] = round(time.monotonic() - start, 3)
            results["exact_synthetic_text"] = True
            history_url = origin + "/api/readings"
            for _ in range(10):
                history = client.get(history_url, headers=headers)
                history.raise_for_status()
                match = next((x for x in history.json()["items"] if x["id"] == job_id), None)
                if match:
                    break
                time.sleep(1)
            assert match and match["thumbnail_url"]
            assert client.get(path + "/thumbnail", headers=headers).json().get("url")
            assert client.get(path + "/thumbnail", headers=foreign).status_code == 404
            thumb = client.get(match["thumbnail_url"], headers={"Origin": origin})
            assert thumb.status_code == 200 and thumb.content.startswith(b"\xff\xd8")
            assert client.get(match["thumbnail_url"].split("?")[0]).status_code in (401, 403)
            assert client.get(history_url, headers=foreign).json()["items"] == []
            results["private_history_thumbnail"] = True
            media = client.get(path + "/audio/0", headers=headers)
            assert media.status_code == 200
            signed = media.json()["url"]
            audio = client.get(signed, headers={"Origin": origin})
            assert audio.status_code == 200, f"Private audio returned HTTP {audio.status_code}"
            assert audio.content.startswith(b"RIFF") and len(audio.content) > 44
            assert audio.headers.get("access-control-allow-origin") == origin
            unsigned = signed.split("?")[0]
            assert client.get(unsigned).status_code in (401, 403)
            assert client.get(signed).content == audio.content
            results.update(
                audio_bytes=len(audio.content),
                private_media=True,
                exact_origin_cors=True,
                replay_reuses_audio=True,
            )
            assert client.delete(path, headers=headers).status_code == 204
            assert client.get(path, headers=headers).status_code == 404
            assert all(
                item["id"] != job_id
                for item in client.get(history_url, headers=headers).json()["items"]
            )
            results["discard_blocks_access"] = True
        results["passed"] = True
    finally:
        if owner is not None and store.raw(owner, run_id, job_id):
            store.cancel(owner, run_id, job_id)
            store.cleanup(store.raw(owner, run_id, job_id) or {})
            results["synthetic_media_removed"] = True
        for username in users:
            cognito.admin_delete_user(UserPoolId=pool, Username=username)
        results["temporary_users_removed"] = True
        (state_dir / "smoke-result.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
