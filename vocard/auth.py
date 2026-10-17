from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from vocard.config import Settings


@dataclass(frozen=True)
class Administrator:
    subject: str
    book_ids: frozenset[str] | None = None

    def authorize_book(self, book_id: str) -> frozenset[str]:
        if self.book_ids is not None and book_id not in self.book_ids:
            raise HTTPException(403, "Book is outside the authorized scope")
        return frozenset({book_id})


def administrator_dependency(settings: Settings):
    bearer = HTTPBearer(auto_error=False)

    def require_admin(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Administrator:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                401, "Administrator authentication required", headers={"WWW-Authenticate": "Bearer"}
            )
        try:
            claims = jwt.decode(
                credentials.credentials,
                settings.jwt_secret,
                algorithms=["HS256"],
                issuer=settings.jwt_issuer,
                audience=settings.jwt_audience,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(
                401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}
            ) from exc
        if claims.get("role") != "administrator":
            raise HTTPException(403, "Administrator role required")
        if not isinstance(claims["sub"], str) or not claims["sub"].strip():
            raise HTTPException(401, "Invalid administrator identity")
        books = claims.get("book_ids")
        if "book_ids" in claims and (
            not isinstance(books, list)
            or any(not isinstance(b, str) or not b.strip() for b in books)
        ):
            raise HTTPException(401, "Invalid book authorization claims")
        return Administrator(claims["sub"], frozenset(books) if books is not None else None)

    return require_admin


class AdminBoundary:
    """Authenticate before multipart parsing; cap streamed bodies, including chunked uploads."""

    def __init__(self, app, settings):
        self.app = app
        self.require_admin = administrator_dependency(settings)
        self.max_body = 2 * settings.max_upload_bytes + 1024 * 1024

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/admin/"):
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, token = authorization.partition(" ")
        credentials = (
            HTTPAuthorizationCredentials(scheme=scheme, credentials=token) if token else None
        )
        try:
            self.require_admin(credentials)
        except HTTPException as exc:
            return await JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
            )(scope, receive, send)
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(
                scope, receive, send
            )
        if content_length < 0 or content_length > self.max_body:
            return await JSONResponse(
                {"detail": "Request body exceeds byte limit"}, status_code=413
            )(scope, receive, send)
        consumed = 0

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if consumed > self.max_body:
                raise StarletteHTTPException(413, "Request body exceeds byte limit")
            return message

        await self.app(scope, limited_receive, send)
