"""Jellyfin library refresh integration."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request


class JellyfinSyncError(RuntimeError):
    """Raised when Jellyfin rejects or cannot complete a library refresh request."""


def refresh_jellyfin_library(
    *,
    base_url: str,
    api_key: str,
    library_id: str,
    timeout: float,
    metadata_refresh_mode: str = "Default",
    image_refresh_mode: str = "Default",
) -> int:
    """Trigger a Jellyfin recursive library refresh and return the HTTP status."""
    base_url = base_url.rstrip("/")
    if not base_url:
        raise JellyfinSyncError("JELLYFIN_URL is required")
    if not api_key:
        raise JellyfinSyncError("JELLYFIN_API_KEY is required")
    if not library_id:
        raise JellyfinSyncError("JELLYFIN_LIBRARY_ID is required")

    query = urllib.parse.urlencode(
        {
            "Recursive": "true",
            "MetadataRefreshMode": metadata_refresh_mode or "Default",
            "ImageRefreshMode": image_refresh_mode or "Default",
            "ReplaceAllMetadata": "false",
            "ReplaceAllImages": "false",
        }
    )
    quoted_library_id = urllib.parse.quote(library_id, safe="")
    url = f"{base_url}/Items/{quoted_library_id}/Refresh?{query}"
    request = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Accept": "application/json",
            "X-Emby-Token": api_key,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(details)
            details = payload.get("message") or payload.get("Message") or details
        except json.JSONDecodeError:
            pass
        raise JellyfinSyncError(f"Jellyfin refresh failed with HTTP {exc.code}: {details}") from exc
    except OSError as exc:
        raise JellyfinSyncError(f"Jellyfin refresh request failed: {exc}") from exc
