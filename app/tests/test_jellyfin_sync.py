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
            library_id="library id",
            timeout=12,
            metadata_refresh_mode="FullRefresh",
            image_refresh_mode="Default",
        )

    assert status == 204
    request = urlopen.call_args.args[0]
    assert request.full_url == (
        "http://jellyfin:8096/Items/library%20id/Refresh?"
        "Recursive=true&MetadataRefreshMode=FullRefresh&ImageRefreshMode=Default&"
        "ReplaceAllMetadata=false&ReplaceAllImages=false"
    )
    assert request.get_method() == "POST"
    assert request.get_header("X-emby-token") == "secret"
    assert urlopen.call_args.kwargs["timeout"] == 12
