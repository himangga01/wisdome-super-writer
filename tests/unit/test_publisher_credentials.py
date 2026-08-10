from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
import yaml
from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase

from adapters.publishers.blogger.oauth import BloggerOAuthClient
from adapters.publishers.blogger.client import BloggerPublisher
from adapters.publishers.wordpress.client import WordPressPublisher
from apps.publishing.contracts import PublisherError
from apps.publishing.models import (
    ChannelCode,
    ChannelRole,
    PublicationTarget,
    TargetDisconnectDecision,
    TargetEnvironment,
)
from apps.audit.services import AuditContext
from apps.audit.models import AuditEvent
from wisdome_writer.infrastructure.secrets import (
    SecretRef,
    SecretResolver,
    normalize_oauth_token_bundle,
)


BLOGGER_SCOPE = "https://www.googleapis.com/auth/blogger"
NOW = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class _JSONSecretProvider:
    def __init__(self, payload):
        self.payload = payload

    def resolve(self, locator: str, *, version: str = "") -> str:
        assert locator == "targets/blogger-token"
        assert version == "v1"
        return json.dumps(self.payload)


class OAuthTokenBundleContractTests(TestCase):
    def test_normalizes_exact_scope_expiry_and_secret_version(self):
        bundle = normalize_oauth_token_bundle(
            {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "token_type": "Bearer",
                "scope": f"openid {BLOGGER_SCOPE}",
                "expires_in": 3600,
                "provider_extra": "ignored",
            },
            version="v1",
            now=NOW,
        )

        self.assertEqual(
            bundle,
            {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "token_type": "Bearer",
                "scope": ["openid", BLOGGER_SCOPE],
                "expires_at": "2026-08-11T01:00:00Z",
                "version": "v1",
            },
        )

    def test_rejects_missing_blogger_scope_or_refresh_token(self):
        base = {
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "token_type": "Bearer",
            "scope": BLOGGER_SCOPE,
            "expires_in": 3600,
        }
        for changed in (
            {**base, "scope": "openid"},
            {**base, "refresh_token": ""},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    normalize_oauth_token_bundle(
                        changed,
                        version="v1",
                        now=NOW,
                    )

    def test_resolver_returns_validated_bundle_not_raw_json(self):
        provider = _JSONSecretProvider(
            {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "token_type": "Bearer",
                "scope": [BLOGGER_SCOPE],
                "expires_at": "2026-08-11T01:00:00Z",
                "version": "v1",
            }
        )
        resolver = SecretResolver(providers={"vault": provider})

        bundle = resolver.resolve_oauth_token_bundle(
            SecretRef(
                provider="vault",
                locator="targets/blogger-token",
                version="v1",
            )
        )

        self.assertEqual(bundle["version"], "v1")
        self.assertEqual(bundle["scope"], [BLOGGER_SCOPE])
        self.assertNotIsInstance(bundle, str)


class PublisherCredentialApiContractTests(TestCase):
    def test_publication_target_exposes_version_without_secret_reference(self):
        document = yaml.safe_load(
            (
                REPOSITORY_ROOT
                / "specs"
                / "001-automated-content-publishing"
                / "contracts"
                / "admin-api.openapi.yaml"
            ).read_text(encoding="utf-8")
        )
        schema = document["components"]["schemas"]["PublicationTarget"][
            "allOf"
        ][1]

        self.assertIn("credentialVersion", schema["required"])
        self.assertEqual(
            schema["properties"]["credentialVersion"]["type"],
            ["string", "null"],
        )
        self.assertNotIn("credentialRef", schema["properties"])


class BloggerOAuthClientContractTests(TestCase):
    def _client(self, handler) -> BloggerOAuthClient:
        return BloggerOAuthClient(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="https://writer.example/oauth/callback",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            clock=lambda: NOW,
        )

    def test_exchange_requires_scope_and_returns_versioned_bundle(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/token")
            return httpx.Response(
                200,
                json={
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "expires_in": 3600,
                    "scope": BLOGGER_SCOPE,
                    "token_type": "Bearer",
                },
            )

        client = self._client(handler)
        try:
            bundle = client.exchange_code("authorization-code", version="v1")
        finally:
            client.close()

        self.assertEqual(bundle["version"], "v1")
        self.assertEqual(bundle["expires_at"], "2026-08-11T01:00:00Z")

    def test_refresh_preserves_refresh_token_when_google_does_not_rotate_it(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/token")
            return httpx.Response(
                200,
                json={
                    "access_token": "access-2",
                    "expires_in": 1800,
                    "scope": BLOGGER_SCOPE,
                    "token_type": "Bearer",
                },
            )

        client = self._client(handler)
        try:
            refreshed = client.refresh_token(
                {
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "token_type": "Bearer",
                    "scope": [BLOGGER_SCOPE],
                    "expires_at": (NOW - timedelta(seconds=1)).isoformat(),
                    "version": "v1",
                },
                version="v2",
            )
        finally:
            client.close()

        self.assertEqual(refreshed["access_token"], "access-2")
        self.assertEqual(refreshed["refresh_token"], "refresh-1")
        self.assertEqual(refreshed["version"], "v2")


class CredentialRevocationAdapterTests(TestCase):
    def test_blogger_revokes_the_refresh_token(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={})

        publisher = BloggerPublisher(
            blog_id="blog-123",
            access_token="access-secret",
            revocation_token="refresh-secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        try:
            publisher.revoke_credentials()
        finally:
            publisher.close()

        self.assertEqual(len(requests), 1)
        self.assertEqual(
            requests[0].url,
            httpx.URL("https://oauth2.googleapis.com/revoke"),
        )
        self.assertIn(b"token=refresh-secret", requests[0].content)
        self.assertNotIn(b"access-secret", requests[0].content)

    def test_blogger_accepts_only_explicit_invalid_token_as_idempotent_revoke(self):
        for payload, raises in (
            ({"error": "invalid_token"}, False),
            ({"error": "invalid_request"}, True),
        ):
            with self.subTest(payload=payload):
                publisher = BloggerPublisher(
                    blog_id="blog-123",
                    access_token="access-secret",
                    revocation_token="refresh-secret",
                    client=httpx.Client(
                        transport=httpx.MockTransport(
                            lambda request: httpx.Response(
                                400,
                                json=payload,
                            )
                        )
                    ),
                )
                try:
                    if raises:
                        with self.assertRaises(PublisherError):
                            publisher.revoke_credentials()
                    else:
                        publisher.revoke_credentials()
                finally:
                    publisher.close()

    def test_wordpress_deletes_and_verifies_the_exact_application_password(self):
        paths = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append((request.method, request.url.path))
            if request.url.path.endswith("/users/me"):
                return httpx.Response(200, json={"id": 42})
            if request.url.path.endswith("/application-passwords/introspect"):
                return httpx.Response(200, json={"uuid": "app-password-uuid"})
            if request.method == "DELETE":
                return httpx.Response(200, json={"deleted": True})
            return httpx.Response(404, json={"code": "rest_application_password_not_found"})

        publisher = WordPressPublisher(
            base_url="https://wp.example.com",
            username="admin",
            application_password="application-password",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        try:
            publisher.revoke_credentials()
        finally:
            publisher.close()

        exact_path = (
            "/wp-json/wp/v2/users/42/application-passwords/"
            "app-password-uuid"
        )
        self.assertEqual(paths[-2:], [("DELETE", exact_path), ("GET", exact_path)])


class OAuthSessionBindingTests(TestCase):
    def test_session_hash_is_stable_only_for_same_authenticated_session(self):
        from apps.publishing.services import _oauth_session_hash

        first = SimpleNamespace(session=SimpleNamespace(session_key="session-a"))
        same = SimpleNamespace(session=SimpleNamespace(session_key="session-a"))
        different = SimpleNamespace(session=SimpleNamespace(session_key="session-b"))

        self.assertEqual(_oauth_session_hash(first), _oauth_session_hash(same))
        self.assertNotEqual(
            _oauth_session_hash(first),
            _oauth_session_hash(different),
        )


class _FakeOAuthClient:
    def __init__(self):
        self.refresh_calls = 0
        self.exchange_calls = 0

    def authorization_url(self, *, state: str) -> str:
        return f"https://accounts.example/authorize?state={state}"

    def exchange_code(self, code: str, *, version: str):
        self.exchange_calls += 1
        raise AssertionError("different-session callback must not exchange a code")

    def refresh_token(self, current, *, version: str):
        self.refresh_calls += 1
        return {
            "access_token": "access-2",
            "refresh_token": "refresh-2",
            "token_type": "Bearer",
            "scope": [BLOGGER_SCOPE],
            "expires_at": "2026-08-11T02:00:00Z",
            "version": version,
        }

    def close(self):
        return None


class _MutableBundleResolver:
    def __init__(self, bundle):
        self.bundle = dict(bundle)

    def resolve_oauth_token_bundle(self, reference):
        return dict(self.bundle)


class OAuthServiceContractTests(DjangoTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email=f"oauth-{uuid.uuid4().hex}@example.com",
            password="unused",
            is_staff=True,
            is_active=True,
        )
        self.target = PublicationTarget.objects.create(
            channel=ChannelCode.BLOGGER,
            role=ChannelRole.SECONDARY,
            environment=TargetEnvironment.TEST,
            display_name="Blogger OAuth target",
            remote_blog_id="blog-123",
            base_url="https://blog.example.com/",
            credential_ref="vault://targets/blogger-token",
            credential_version="v1",
        )
        from apps.publishing.services import _snapshot_locked

        _snapshot_locked(self.target)

    def _request(self, session_key: str):
        return SimpleNamespace(
            user=self.user,
            session=SimpleNamespace(session_key=session_key),
            correlation_id=uuid.uuid4(),
        )

    def test_oauth_callback_rejects_a_different_admin_session_before_exchange(self):
        from apps.publishing import services

        start_request = self._request("session-a")
        context = AuditContext.for_admin(
            request=start_request,
            reason_code="connect blogger",
            request_key="oauth-connect-001",
        )
        fake = _FakeOAuthClient()
        with patch.object(services, "_blogger_oauth_client", return_value=fake):
            started = services.start_blogger_oauth(
                str(self.target.id),
                user=self.user,
                request=start_request,
                redirect_uri="https://writer.example/oauth/callback",
                audit_context=context,
            )
            state = parse_qs(
                urlparse(started["authorizationUrl"]).query
            )["state"][0]
            with self.assertRaises(services.Forbidden):
                services.complete_blogger_oauth(
                    code="authorization-code",
                    state=state,
                    request=self._request("session-b"),
                    redirect_uri="https://writer.example/oauth/callback",
                )

        self.assertEqual(fake.exchange_calls, 0)

    def test_blogger_target_rejects_manual_credential_reference_mutation(self):
        from apps.publishing import services

        create_request = self._request("manual-create-session")
        create_context = AuditContext.for_admin(
            request=create_request,
            reason_code="create blogger target",
            request_key="create-blogger-target-001",
        )
        with self.assertRaises(services.InvalidInput):
            services.create_target(
                {
                    "channel": ChannelCode.BLOGGER,
                    "channelRole": ChannelRole.SECONDARY,
                    "environment": TargetEnvironment.TEST,
                    "displayName": "Manual credential target",
                    "baseUrl": "https://manual-blog.example.com",
                    "remoteBlogId": "manual-blog",
                    "credentialRef": "vault://manual-token",
                    "requestKey": create_context.request_key,
                    "reason": create_context.reason_code,
                },
                audit_context=create_context,
            )

        update_request = self._request("manual-update-session")
        update_context = AuditContext.for_admin(
            request=update_request,
            reason_code="replace blogger credential",
            request_key="update-blogger-target-001",
        )
        with self.assertRaises(services.InvalidInput):
            services.update_target(
                str(self.target.id),
                {
                    "credentialRef": "vault://replacement-token",
                    "requestKey": update_context.request_key,
                    "reason": update_context.reason_code,
                },
                audit_context=update_context,
            )

    def test_expired_bundle_refreshes_once_and_persists_new_secret_version(self):
        from apps.publishing import services

        resolver = _MutableBundleResolver(
            {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "token_type": "Bearer",
                "scope": [BLOGGER_SCOPE],
                "expires_at": "2026-08-10T23:59:59Z",
                "version": "v1",
            }
        )
        oauth_client = _FakeOAuthClient()
        store_calls = []

        def token_store(**kwargs):
            store_calls.append(kwargs)
            resolver.bundle = dict(kwargs["token_bundle"])
            return {
                "credential_ref": "vault://targets/blogger-token",
                "version": resolver.bundle["version"],
            }

        request = self._request("refresh-session")
        audit_context = AuditContext.for_admin(
            request=request,
            reason_code="refresh expired blogger credential",
            request_key="refresh-blogger-credential-001",
        )
        first = services.resolve_blogger_token_bundle_for_target(
            self.target.id,
            resolver=resolver,
            token_store=token_store,
            oauth_client=oauth_client,
            now=NOW,
            audit_context=audit_context,
        )
        second = services.resolve_blogger_token_bundle_for_target(
            self.target.id,
            resolver=resolver,
            token_store=token_store,
            oauth_client=oauth_client,
            now=NOW,
            audit_context=audit_context,
        )

        self.target.refresh_from_db()
        self.assertEqual(first["version"], "v2")
        self.assertEqual(second, first)
        self.assertEqual(oauth_client.refresh_calls, 1)
        self.assertEqual(len(store_calls), 1)
        self.assertEqual(store_calls[0]["expected_version"], "v1")
        self.assertEqual(self.target.credential_version, "v2")
        event = AuditEvent.objects.get(
            action="publication_target.oauth_refreshed"
        )
        self.assertEqual(
            event.metadata_redacted["oauth_bundle_version"],
            "v2",
        )
        self.assertNotIn("access_token", str(event.metadata_redacted))
        self.assertNotIn("refresh_token", str(event.metadata_redacted))

    def test_blogger_publisher_receives_refresh_token_for_revocation(self):
        from apps.publishing import services

        bundle = {
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "token_type": "Bearer",
            "scope": [BLOGGER_SCOPE],
            "expires_at": "2026-08-11T01:00:00Z",
            "version": "v1",
        }
        with patch.object(
            services,
            "resolve_blogger_token_bundle_for_target",
            return_value=bundle,
        ):
            publisher = services.publisher_for_target(self.target)
        try:
            self.assertEqual(publisher.revocation_token, "refresh-1")
        finally:
            publisher.close()

    def test_blogger_revoke_factory_does_not_refresh_an_expired_bundle(self):
        from apps.publishing import services

        resolver = _MutableBundleResolver(
            {
                "access_token": "expired-access",
                "refresh_token": "refresh-1",
                "token_type": "Bearer",
                "scope": [BLOGGER_SCOPE],
                "expires_at": "2026-08-10T23:00:00Z",
                "version": "v1",
            }
        )
        with patch.object(
            services,
            "resolve_blogger_token_bundle_for_target",
            side_effect=AssertionError("disconnect must not refresh first"),
        ):
            publisher = services.publisher_for_target(
                self.target,
                resolver=resolver,
                refresh_blogger=False,
            )
        try:
            self.assertEqual(publisher.revocation_token, "refresh-1")
        finally:
            publisher.close()

    def test_successful_remote_revoke_clears_secret_ref_and_version_together(self):
        from apps.publishing import services

        decision = TargetDisconnectDecision.objects.create(
            target=self.target,
            expected_target_snapshot_id=self.target.current_snapshot_id,
            expected_target_config_hash=self.target.current_config_hash,
            request_key="disconnect-credential-001",
            request_hash="a" * 64,
            reauth_proof_id=uuid.uuid4(),
            reason="disconnect target credential",
            state=TargetDisconnectDecision.State.REVOKING,
            decided_by=self.user,
        )
        context = AuditContext.for_worker(
            correlation_id=uuid.uuid4(),
            event_key=str(uuid.uuid4()),
            consumer_name="publishing.target_disconnect",
            lease_token=uuid.uuid4(),
            lease_generation=1,
        )
        fence = services.TargetCredentialRevokeFence(
            decision_id=decision.id,
            target_id=self.target.id,
            target_snapshot_id=self.target.current_snapshot_id,
            target_snapshot_version=self.target.current_snapshot_version,
            target_config_hash=self.target.current_config_hash,
        )
        with (
            patch.object(services, "_require_worker_event"),
            patch.object(services, "_worker_audit_replay", return_value=None),
            patch.object(services, "_record_publishing_audit"),
        ):
            services.persist_target_credential_revoke_result(
                fence,
                succeeded=True,
                outcome_hash="b" * 64,
                audit_context=context,
            )

        self.target.refresh_from_db()
        self.assertIsNone(self.target.credential_ref)
        self.assertEqual(self.target.credential_version, "")
