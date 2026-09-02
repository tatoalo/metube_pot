from __future__ import annotations

from unittest.mock import MagicMock, patch

from jellyfin_sync import refresh_jellyfin_library


def test_refresh_jellyfin_library_posts_refresh_request():
    response = MagicMock()
    response.status = 204
    response.__enter__.return_value = response
    response.__exit__.return_value = None

    with patch("jellyfin_sync.urllib.request.urlopen", return_value=response) as urlopen:
        status = refresh_jellyfin_library(
            base_url="http://jellyfin:8096/",
            api_key="secret",
            timeout=12,
        )

    assert status == 204
    request = urlopen.call_args.args[0]
    assert request.full_url == "http://jellyfin:8096/Library/Refresh"
    assert request.get_method() == "POST"
    assert request.data is None
    assert request.get_header("Authorization") == 'MediaBrowser Token="secret"'
    assert urlopen.call_args.kwargs["timeout"] == 12
