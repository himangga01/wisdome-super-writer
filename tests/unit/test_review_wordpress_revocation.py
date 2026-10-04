import httpx
import pytest

from adapters.publishers.wordpress.client import WordPressPublisher
from apps.publishing.contracts import PublisherError


@pytest.mark.parametrize(
    ("deleted", "previous_uuid", "verification_code", "success"),
    [
        (True, "review-application", "incorrect_password", True),
        (True, "different-application", "incorrect_password", False),
        (False, "review-application", "incorrect_password", False),
        (True, "review-application", "unrelated_authentication_error", False),
    ],
)
def test_self_revocation_binds_deleted_identity_before_accepting_authentication_failure(
    deleted,
    previous_uuid,
    verification_code,
    success,
):
    methods = []

    def transport(request):
        methods.append(request.method)
        if request.url.path.endswith("/users/me"):
            return httpx.Response(200, json={"id": 42})
        if request.url.path.endswith("/introspect"):
            return httpx.Response(200, json={"uuid": "review-application"})
        if request.method == "DELETE":
            return httpx.Response(
                200, json={"deleted": deleted, "previous": {"uuid": previous_uuid}}
            )
        return httpx.Response(401, json={"code": verification_code, "data": {"status": 401}})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        publisher = WordPressPublisher(
            base_url="https://wordpress.example.com",
            username="fixture-user",
            application_password="fixture-password",
            client=client,
            write_guard=lambda: None,
        )
        if success:
            publisher.revoke_credentials()
        else:
            with pytest.raises(PublisherError):
                publisher.revoke_credentials()
    assert methods == ["GET", "GET", "DELETE", "GET"]
