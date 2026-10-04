from unittest.mock import Mock

import pytest

from adapters.publishers.blogger.client import BloggerPublisher


def test_blogger_credential_revocation_enforces_the_supplied_write_guard():
    client = Mock()
    client.post.return_value.status_code = 200

    def deny():
        raise PermissionError("external write is not authorized")

    publisher = BloggerPublisher(
        blog_id="fixture-blog",
        access_token="fixture-access",
        revocation_token="fixture-revocation",
        client=client,
        write_guard=deny,
    )
    with pytest.raises(PermissionError, match="not authorized"):
        publisher.revoke_credentials()
    client.post.assert_not_called()
