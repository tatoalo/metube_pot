"""Jellyfin library refresh integration."""

from __future__ import annotations

import json
import urllib.request


class JellyfinSyncError(RuntimeError):
    """Raised when Jellyfin rejects or cannot complete a library refresh request."""


def refresh_jellyfin_library(
    *,
    base_url: str,
    api_key: str,
    timeout: float,
) -> int:
    """Trigger a Jellyfin media library scan and return the HTTP status."""
    base_url = base_url.rstrip("/")
    if not base_url:
        raise JellyfinSyncError("JELLYFIN_URL is required")
    if not api_key:
        raise JellyfinSyncError("JELLYFIN_API_KEY is required")

    request = urllib.request.Request(
        f"{base_url}/Library/Refresh",
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f'MediaBrowser Token="{api_key}"',
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
