"""Browser session endpoints. Registration, email and passwords stay at the issuer."""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict
from starlette.responses import JSONResponse, RedirectResponse, Response

from bancaemdia.api.contracts import (
    AUTHENTICATED_ERROR_RESPONSES,
    COMMON_ERROR_RESPONSES,
    ErrorResponse,
)
from bancaemdia.auth.identity_service import identity_service
from bancaemdia.auth.oidc import IdentityError

router = APIRouter(prefix="/auth", tags=["Identity"])


class AuthFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: str
    code: str


class SessionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    usuario_id: int
    nome: str
    email: str
    session_version: str
    csrf_token: str
    access_expires_at: datetime
    session_expires_at: datetime
    refresh_required: bool


class LogoutStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    logged_out: bool


AUTH_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": AuthFailure, "description": description}
    for code, description in {
        400: "Callback, state or destination is invalid.",
        401: "Session/identity is missing, expired, revoked or inactive.",
        403: "Email is unconfirmed or origin/CSRF proof is invalid.",
        409: "Email conflicts with a different internal identity; never auto-linked.",
        429: "Authentication traffic is rate limited.",
        503: "Identity is not configured or issuer/database is temporarily unavailable.",
    }.items()
}
AUTH_ERRORS.update(COMMON_ERROR_RESPONSES)
AUTH_ERRORS[429] = AUTHENTICATED_ERROR_RESPONSES[429]
AUTH_ERRORS[503]["model"] = AuthFailure | ErrorResponse


def failure(error: IdentityError, *, legacy: bool = False) -> JSONResponse:
    headers = {"Cache-Control": "private, no-store", "X-Auth-Error": error.code}
    if error.status == 401:
        headers["WWW-Authenticate"] = 'Bearer error="invalid_token"'
    return JSONResponse(
        {
            "detail": "Identity request could not be completed",
            **({} if legacy else {"code": error.code}),
        },
        status_code=error.status,
        headers=headers,
    )


def session_cookie(response: Response, value: str) -> None:
    settings = identity_service().settings
    response.set_cookie(
        settings.session_cookie,
        value,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite="lax",
        path="/",
        max_age=settings.AUTH_SESSION_SECONDS,
    )


@router.get(
    "/start",
    response_model=None,
    status_code=302,
    response_class=RedirectResponse,
    responses={
        **AUTH_ERRORS,
        302: {
            "description": "Navigate to the issuer's hosted login, including registration and password recovery.",
            "headers": {"Location": {"schema": {"type": "string"}}},
        },
    },
    summary="Iniciar login hospedado",
    description="Authorization Code + S256 PKCE, nonce and browser-bound one-time state. Hosted login owns signup, email verification and password recovery.",
)
async def start(
    request: Request,
    return_to: Annotated[
        str,
        Query(
            description="Internal frontend path only, never an arbitrary origin.", max_length=2048
        ),
    ] = "/",
    intent: Annotated[
        Literal["login", "signup", "recover"],
        Query(
            description="Use the hosted registration/recovery link; a successful recovery login revokes older local sessions."
        ),
    ] = "login",
) -> Response:
    try:
        service = identity_service()
        url, browser = await service.begin(return_to, intent)
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(
            service.settings.flow_cookie,
            browser,
            httponly=True,
            secure=service.settings.AUTH_COOKIE_SECURE,
            samesite="lax",
            path="/",
            max_age=600,
        )
        return response
    except IdentityError as error:
        return failure(error)


@router.get(
    "/callback",
    response_model=None,
    status_code=302,
    response_class=RedirectResponse,
    responses={
        **AUTH_ERRORS,
        302: {
            "description": "Verified identity provisioned and internal server session created; navigate to the stored internal destination.",
            "headers": {"Location": {"schema": {"type": "string"}}},
        },
    },
    summary="Concluir identidade verificada",
    description="Consumes state once, checks browser cookie, nonce, issuer, audience, signature and confirmed email; external sub is linked to an internal numeric ID.",
)
async def callback(
    request: Request,
    state: Annotated[
        str, Query(description="One-time state issued by this API.", max_length=4096)
    ] = "",
    code: Annotated[
        str, Query(description="Issuer's one-time authorization code.", max_length=4096)
    ] = "",
    error: Annotated[
        str | None,
        Query(description="Issuer denial; never reflected into redirects or logs.", max_length=200),
    ] = None,
) -> Response:
    try:
        service = identity_service()
        if error is not None:
            raise IdentityError("identity_rejected")
        cookie, target = await service.callback(
            state, request.cookies.get(service.settings.flow_cookie, ""), code
        )
        response: Response = RedirectResponse(target, status_code=302)
        session_cookie(response, cookie)
    except IdentityError as reason:
        response = failure(reason)
    from bancaemdia.auth.identity_config import identity_settings

    settings = identity_settings()
    response.delete_cookie(
        settings.flow_cookie,
        path="/",
        secure=settings.AUTH_COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get(
    "/session",
    response_model=SessionStatus,
    responses=AUTH_ERRORS,
    summary="Consultar sessão do navegador",
    description="Returns identity, generation and CSRF proof, never an access/refresh token. A live server session can report refresh_required when its internal access token expires.",
)
async def session_status(request: Request) -> Response:
    try:
        service = identity_service()
        result = await service.status(request.cookies.get(service.settings.session_cookie, ""))
        return JSONResponse(SessionStatus(**result).model_dump(mode="json"))
    except IdentityError as error:
        return failure(error)


@router.post(
    "/refresh",
    response_model=SessionStatus,
    responses=AUTH_ERRORS,
    summary="Renovar e rotacionar sessão",
    description="Requires live HttpOnly cookie, exact Origin and X-CSRF-Token. Uses the issuer refresh grant, preserves identity, rotates cookie and generation; refresh-cookie reuse revokes the family. Serialize renewal in the frontend.",
)
async def refresh(request: Request) -> Response:
    try:
        service = identity_service()
        cookie = await service.refresh(request.cookies.get(service.settings.session_cookie, ""))
        result = await service.status(cookie)
        response = JSONResponse(SessionStatus(**result).model_dump(mode="json"))
        session_cookie(response, cookie)
        return response
    except IdentityError as error:
        return failure(error)


@router.post(
    "/logout",
    response_model=LogoutStatus,
    responses=AUTH_ERRORS,
    summary="Revogar sessão",
    description="Exact Origin and X-CSRF-Token required. Revokes current or all local sessions immediately, including already issued internal JWTs; issuer refresh revocation is persisted and retried. Does not claim to delete the provider SSO cookie.",
)
async def logout(
    request: Request,
    all_sessions: Annotated[
        bool, Query(description="Revoke every local session of this verified identity.")
    ] = False,
) -> Response:
    try:
        service = identity_service()
        await service.logout(request.cookies.get(service.settings.session_cookie, ""), all_sessions)
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(
            service.settings.session_cookie,
            path="/",
            secure=service.settings.AUTH_COOKIE_SECURE,
            httponly=True,
            samesite="lax",
        )
        return response
    except IdentityError as error:
        return failure(error)


class PublicKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kid: str
    kty: Literal["RSA"]
    use: Literal["sig"]
    alg: Literal["RS256"]
    n: str
    e: str
    key_ops: list[Literal["verify"]]


class PublicKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys: list[PublicKey]


@router.get(
    "/jwks",
    response_model=PublicKeys,
    summary="Consultar chaves públicas da API",
    description="Only public RSA signing keys, including retained keys during rotation. Private signing and encryption material never leave the server.",
    responses=AUTH_ERRORS,
)
async def jwks() -> Response:
    try:
        return JSONResponse(identity_service().keys.jwks)
    except IdentityError as error:
        return failure(error)
