import json

from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_POST

from wisdome_writer.domain.errors import InvalidInput

from .services import issue_reauthentication_proof


@require_POST
def reauthenticate(request: HttpRequest) -> JsonResponse:
    try:
        payload = json.loads(request.body or b"{}")
    except (TypeError, ValueError) as exc:
        raise InvalidInput("Request body must be valid JSON") from exc
    if set(payload) - {"currentPassword", "mfaCode", "actionScopes"}:
        raise InvalidInput("Request contains unsupported fields")
    proof = issue_reauthentication_proof(
        request=request,
        current_password=payload.get("currentPassword", ""),
        mfa_code=payload.get("mfaCode"),
        action_scopes=payload.get("actionScopes") or [],
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

