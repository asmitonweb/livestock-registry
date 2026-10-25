"""Staff API for verifying a farmer with Fayda (eSignet) during intake.

Start opens core's registrant authentication without a browser-supplied FIN; Fayda
returns to core's /registrant-auth/callback, which completes it; result gives
the outcome and the verified values to put on the form. The staff member is the
one signed in (the token), never a value the browser sends.
"""

import logging
from datetime import datetime

from fastapi import Request
from iam_core.user_auth.decorators import require_permissions
from openg2p_fastapi_common.controller import BaseController
from openg2p_fastapi_common.schemas import (
    G2PRequest,
    G2PRequestBody,
    G2PResponse,
    G2PResponseBody,
    G2PResponseHeader,
    G2PResponseStatus,
)
from openg2p_registry_core.errors import G2PRegistryErrorCodes, G2PRegistryException
from pydantic import BaseModel

from ..register_domain.services.identity_verification import (
    OANRegistrantAuthenticationService,
)

_logger = logging.getLogger("oan-identity-verification")

# Core's permission for registrant authentication.
PERMISSION = "register:authenticate"


class StartPayload(BaseModel):
    register_id: str
    foundational_id: str | None = None
    internal_record_id: str | None = None


class StartBody(G2PRequestBody):
    request_payload: StartPayload


class StartRequest(G2PRequest):
    request_body: StartBody


class ResultPayload(BaseModel):
    verification_id: str


class ResultBody(G2PRequestBody):
    request_payload: ResultPayload


class ResultRequest(G2PRequest):
    request_body: ResultBody


def _response(
    g2p_request: G2PRequest, payload: dict | None = None, error: Exception | None = None
) -> G2PResponse:
    if isinstance(error, G2PRegistryException):
        code, message = error.code, error.message
    elif error is not None:
        code = G2PRegistryErrorCodes.INVALID_REQUEST.value[1]
        message = "Fayda verification could not be reached. Try again; if it keeps failing, ask the administrator to check the eSignet connection"
    return G2PResponse(
        response_header=G2PResponseHeader(
            request_id=g2p_request.request_header.request_id,
            response_status=G2PResponseStatus.ERROR
            if error
            else G2PResponseStatus.SUCCESS,
            response_error_code=code if error else None,
            response_error_message=message if error else None,
            response_timestamp=datetime.now(),
        ),
        response_body=G2PResponseBody(response_payload=payload),
    )


def _staff(request: Request) -> str:
    auth = getattr(request.state, "auth", None)
    staff = getattr(auth, "sub", None) or getattr(auth, "name", None)
    if not staff:
        raise G2PRegistryException(
            code=G2PRegistryErrorCodes.INVALID_REQUEST.value[1],
            message="Sign in again: your session has no staff identity",
        )
    return staff


class OANIdentityVerificationController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.router.tags += ["/oan/identity-verification"]
        self.router.add_api_route(
            "/oan/identity-verification/start", self.start, methods=["POST"]
        )
        self.router.add_api_route(
            "/oan/identity-verification/result", self.result, methods=["POST"]
        )

    @property
    def service(self) -> OANRegistrantAuthenticationService:
        return OANRegistrantAuthenticationService.get_component()

    @require_permissions({PERMISSION})
    async def start(self, request: Request, g2p_request: StartRequest) -> G2PResponse:
        payload = g2p_request.request_body.request_payload
        try:
            result = await self.service.start_identity_verification(
                register_id=payload.register_id,
                foundational_id=payload.foundational_id,
                internal_record_id=payload.internal_record_id,
                staff_id=_staff(request),
            )
            return _response(g2p_request, result)
        except Exception as error:
            if not isinstance(error, G2PRegistryException):
                _logger.exception("Starting a Fayda verification failed")
            return _response(g2p_request, error=error)

    @require_permissions({PERMISSION})
    async def result(self, request: Request, g2p_request: ResultRequest) -> G2PResponse:
        try:
            result = await self.service.get_identity_verification(
                verification_id=g2p_request.request_body.request_payload.verification_id,
                staff_id=_staff(request),
            )
            return _response(g2p_request, result)
        except Exception as error:
            if not isinstance(error, G2PRegistryException):
                _logger.exception("Reading a Fayda verification failed")
            return _response(g2p_request, error=error)
