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
