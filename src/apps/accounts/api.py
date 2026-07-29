from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_POST

from wisdome_writer.api.openapi import openapi_operation

from .services import issue_reauthentication_proof


@openapi_operation("reauthenticateAdmin")
@require_POST
def reauthenticate(request: HttpRequest) -> JsonResponse:
    payload = request.openapi_body
    action_scopes = payload["actionScopes"]
    proof = issue_reauthentication_proof(
        request=request,
        current_password=payload["currentPassword"],
        mfa_code=payload.get("mfaCode"),
        action_scopes=action_scopes,
    )
    return JsonResponse(
        {
            "id": str(proof.pk),
            "actionScopes": proof.action_scopes,
            "issuedAt": proof.issued_at.isoformat(),
            "expiresAt": proof.expires_at.isoformat(),
        },
        status=201,
    )

