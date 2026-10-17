"""Firebase authentication for the student API, independent of administrator JWTs."""

import threading
from typing import Annotated

import firebase_admin
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth

_app_lock = threading.Lock()


class FirebaseTokenVerifier:
    def __init__(self, project_id):
        self.project_id = project_id

    def __call__(self, token):
        if not self.project_id:
            raise HTTPException(503, "Student authentication is not configured")
        # Named apps prevent accidental reuse of another Firebase project's verifier.
        name = "vocard-ai-" + self.project_id
        try:
            with _app_lock:
                try:
                    app = firebase_admin.get_app(name)
                except ValueError:
                    app = firebase_admin.initialize_app(
                        options={"projectId": self.project_id}, name=name
                    )
        except Exception as exc:
            raise HTTPException(503, "Student authentication is unavailable") from exc
        return auth.verify_id_token(token, app=app, check_revoked=True)


def student_dependency(verifier):
    bearer = HTTPBearer(auto_error=False)

    def verified_uid(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(401, "Firebase sign-in required")
        try:
            claims = verifier(credentials.credentials)
        except HTTPException:
            raise
        except (
            auth.InvalidIdTokenError,
            auth.RevokedIdTokenError,
            auth.UserDisabledError,
            ValueError,
        ) as exc:
            raise HTTPException(401, "Invalid or expired Firebase ID token") from exc
        except Exception as exc:
            raise HTTPException(503, "Student authentication is unavailable") from exc
        uid = claims.get("uid")
        if not isinstance(uid, str) or not uid or len(uid) > 128:
            raise HTTPException(401, "Invalid Firebase identity")
        if claims.get("firebase", {}).get("sign_in_provider") == "anonymous":
            raise HTTPException(403, "Sign in to use Ask AI")
        return uid

    return verified_uid


class FirebaseRestTokenVerifier:
    """Verify signature/project and online revocation without an admin service key.

    The web API key is the same public project identifier shipped in Firebase
    clients. Tokens go only to Google's documented accounts:lookup endpoint.
    """

    def __init__(self, project_id, api_key, verify_token=None, client=None):
        import httpx
        from google.auth.transport.requests import Request
        from google.oauth2.id_token import verify_firebase_token

        self.project_id, self.api_key = project_id, api_key
        self.verify_token = verify_token or (
            lambda token: verify_firebase_token(token, Request(), audience=project_id)
        )
        self.client = client or httpx.Client(timeout=15, trust_env=False)

    def __call__(self, token):
        import httpx

        if not self.project_id or not self.api_key:
            raise HTTPException(503, "Student authentication is not configured")
        try:
            claims = self.verify_token(token)
        except ValueError as exc:
            raise HTTPException(401, "Invalid or expired Firebase ID token") from exc
        except Exception as exc:
            raise HTTPException(503, "Firebase signature verification is unavailable") from exc
        if claims.get("iss") != "https://securetoken.google.com/" + self.project_id:
            raise HTTPException(401, "Invalid Firebase token issuer")
        uid = claims.get("sub")
        if not isinstance(uid, str) or not 1 <= len(uid) <= 128:
            raise HTTPException(401, "Invalid Firebase identity")
        try:
            response = self.client.post(
                "https://identitytoolkit.googleapis.com/v1/accounts:lookup",
                params={"key": self.api_key},
                json={"idToken": token},
            )
        except httpx.HTTPError as exc:
            raise HTTPException(503, "Firebase account lookup is unavailable") from exc
        if response.status_code in (400, 401, 403):
            raise HTTPException(401, "Invalid or expired Firebase ID token")
        if response.status_code != 200:
            raise HTTPException(503, "Firebase account lookup is unavailable")
        try:
            users = response.json()["users"]
            if len(users) != 1 or users[0]["localId"] != uid:
                raise ValueError("Identity mismatch")
            user = users[0]
            valid_since = int(user["validSince"])
            auth_time = claims["auth_time"]
            if not isinstance(auth_time, (int, float)):
                raise ValueError("Invalid authentication time")
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(503, "Firebase account status is unavailable") from exc
        if user.get("disabled") or auth_time < valid_since:
            raise HTTPException(401, "Invalid or revoked Firebase ID token")
        return {**claims, "uid": uid}
