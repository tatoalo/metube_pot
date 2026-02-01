"""
StreamingCommunity extractor for metube.
Extracts m3u8 URLs from StreamingCommunity streaming service.
"""

import re
import json
import logging
from typing import Optional, Dict, List, Any
from urllib.parse import urlparse, urlencode

from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup

log = logging.getLogger("streamingcommunity")

# Chrome user agent for header passthrough
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


class StreamingCommunityExtractor:
    """Extract video information from StreamingCommunity."""

    def __init__(self, base_url: str):
        self.base_url = base_url
        # Use curl_cffi with Chrome impersonation for anti-bot bypass
        self.session = curl_requests.Session(impersonate="chrome")
        self.version = None

    def get_version(self) -> Optional[str]:
        """Get x-inertia-version from the site."""
        if self.version:
            return self.version

        response = self.session.get(f"{self.base_url}/it")
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        app_div = soup.find("div", {"id": "app"})
        if app_div and app_div.get("data-page"):
            data = json.loads(app_div.get("data-page"))
            self.version = data.get("version")
            return self.version
        return None

    def _inertia_get(self, path: str) -> dict:
        """Make an Inertia API request."""
        version = self.get_version()
        if not version:
            raise Exception("Could not get site version")

        headers = {"x-inertia": "true", "x-inertia-version": version}
        response = self.session.get(f"{self.base_url}{path}", headers=headers)
        response.raise_for_status()
        return response.json()

    def get_m3u8_from_embed(self, embed_url: str) -> Optional[Dict[str, str]]:
        """Extract m3u8 URL from vixcloud embed page, forcing highest quality."""
        from urllib.parse import urlparse as _urlparse, parse_qs, urlunparse

        response = self.session.get(embed_url)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        for script in soup.find_all("script"):
            if script.string and "masterPlaylist" in script.string:
                text = script.string
                log.debug(f"Raw masterPlaylist JS: {text[:600]}")

                # Extract auth params from window.masterPlaylist
                token_match = re.search(r"'token':\s*['\"]([^'\"]+)['\"]", text)
                expires_match = re.search(r"'expires':\s*['\"](\d+)['\"]", text)

                # Extract the active stream URL from window.streams
                # This URL contains required server params (e.g. ub=1, ab=1)
                stream_url = None
                streams_match = re.search(r"window\.streams\s*=\s*(\[.*?\]);", text, re.DOTALL)
                if streams_match:
                    try:
                        streams = json.loads(streams_match.group(1))
                        # Pick the active server
                        for s in streams:
                            if s.get("active"):
                                stream_url = s.get("url", "").replace("\\/", "/")
                                break
                        # Fallback to first server
                        if not stream_url and streams:
                            stream_url = streams[0].get("url", "").replace("\\/", "/")
                    except (json.JSONDecodeError, KeyError) as e:
                        log.warning(f"Failed to parse window.streams: {e}")

                # Fallback: extract URL from masterPlaylist
                if not stream_url:
                    url_match = re.search(r"url:\s*['\"]([^'\"]+)['\"]", text)
                    if url_match:
                        stream_url = url_match.group(1).replace("\\/", "/")

                if stream_url:
                    log.info(f"Stream URL: {stream_url}")

                    # Parse the stream URL and preserve its existing query params
                    parsed = _urlparse(stream_url)
                    existing_params = parse_qs(parsed.query)

                    # Build final params: keep existing (ub, ab, b, etc.) + add auth
                    params = {}
                    for k, v in existing_params.items():
                        params[k] = v[0]
                    # Only request FHD if the server supports it
                    can_play_fhd = re.search(r"window\.canPlayFHD\s*=\s*(true|false)", text)
                    if can_play_fhd and can_play_fhd.group(1) == "true":
                        params['h'] = '1'
                    if token_match:
                        params["token"] = token_match.group(1)
                    if expires_match:
                        params["expires"] = expires_match.group(1)

                    m3u8_url = urlunparse(parsed._replace(query=urlencode(params)))
                    log.info(f"Built m3u8 URL: {m3u8_url}")

                    return {
                        "m3u8_url": m3u8_url,
                        "referer": embed_url,
                    }

        return None

    def extract_episode(
        self,
        title_id: str,
        episode_id: str,
        title_name: str,
        season_num: int,
        ep_num: int,
        ep_name: str,
    ) -> Optional[Dict[str, Any]]:
        """Extract info for a specific episode."""
        try:
            watch_data = self._inertia_get(f"/it/watch/{title_id}?e={episode_id}")
            props = watch_data.get("props", {})

            embed_url = props.get("embedUrl")
            if not embed_url:
                log.warning(f"No embed URL for {title_name} S{season_num:02d}E{ep_num:02d}")
                return None

            response = self.session.get(embed_url)
            soup = BeautifulSoup(response.text, "html.parser")
            iframe = soup.find("iframe")

            if not iframe or not iframe.get("src"):
                log.warning(f"No iframe for {title_name} S{season_num:02d}E{ep_num:02d}")
                return None

            m3u8_result = self.get_m3u8_from_embed(iframe.get("src"))
            if not m3u8_result:
                log.warning(f"No m3u8 for {title_name} S{season_num:02d}E{ep_num:02d}")
                return None

            m3u8_url = m3u8_result["m3u8_url"]
            referer = m3u8_result["referer"]

            safe_name = re.sub(r"[^\w\s-]", "", title_name).strip().replace(" ", "_")
            video_id = f"sc_{title_id}_{episode_id}"
            title = f"{title_name} S{season_num:02d}E{ep_num:02d}"
            if ep_name:
                title += f" - {ep_name}"

            return {
                "id": video_id,
                "title": title,
                # Store SC URL for just-in-time m3u8 extraction (tokens expire quickly)
                "url": f"{self.base_url}/it/watch/{title_id}?e={episode_id}",
                "webpage_url": f"{self.base_url}/it/watch/{title_id}?e={episode_id}",
                "ext": "mp4",
                "_type": "video",
                "extractor": "streamingcommunity",
                "extractor_key": "StreamingCommunity",
                "season_number": season_num,
                "episode_number": ep_num,
                "episode": ep_name,
                "series": title_name,
                # Store for just-in-time extraction
                "_sc_needs_m3u8_extraction": True,
                "_sc_base_url": self.base_url,
            }

        except Exception as e:
            log.error(f"Error extracting episode {title_name} S{season_num:02d}E{ep_num:02d}: {e}")
            return None

    def extract_season(self, url: str) -> Optional[Dict[str, Any]]:
        """Extract all episodes from a season URL as a playlist."""
        match = re.search(r"/titles/(\d+)-([^/]+)/season-(\d+)", url)
        if not match:
            log.error(f"Invalid season URL: {url}")
            return None

        title_id = match.group(1)
        title_slug = match.group(2)
        season_num = int(match.group(3))

        title_data = self._inertia_get(f"/it/titles/{title_id}-{title_slug}")
        title_name = title_data.get("props", {}).get("title", {}).get("name", "Unknown")

        log.info(f"Extracting {title_name} Season {season_num}")

        season_data = self._inertia_get(f"/it/titles/{title_id}-{title_slug}/season-{season_num}")
        loaded_season = season_data.get("props", {}).get("loadedSeason", {})
        episodes = loaded_season.get("episodes", [])

        log.info(f"Found {len(episodes)} episodes")

        entries = []
        for ep in episodes:
            ep_id = ep.get("id")
            ep_num = ep.get("number")
            ep_name = ep.get("name", "")

            log.info(f"Extracting S{season_num:02d}E{ep_num:02d}: {ep_name[:40]}...")

            info = self.extract_episode(
                title_id, str(ep_id), title_name, season_num, ep_num, ep_name
            )
            if info:
                entries.append(info)

        if not entries:
            return None

        safe_name = re.sub(r"[^\w\s-]", "", title_name).strip().replace(" ", "_")

        return {
            "id": f"sc_{title_id}_s{season_num}",
            "title": f"{title_name} Season {season_num}",
            "original_url": url,
            "_type": "playlist",
            "entries": entries,
            "extractor": "streamingcommunity",
            "extractor_key": "StreamingCommunity",
        }

    def extract_watch(self, url: str) -> Optional[Dict[str, Any]]:
        """Extract video info from a watch URL."""
        match = re.search(r"/watch/(\d+)(?:\?e=(\d+))?", url)
        if not match:
            log.error(f"Invalid watch URL: {url}")
            return None

        title_id = match.group(1)
        episode_param = match.group(2)

        try:
            path = f"/it/watch/{title_id}"
            if episode_param:
                path += f"?e={episode_param}"

            watch_data = self._inertia_get(path)
            props = watch_data.get("props", {})

            title = props.get("title", {})
            title_name = title.get("name", "Unknown")
            title_type = title.get("type", "movie")

            embed_url = props.get("embedUrl")
            if not embed_url:
                log.error("No embed URL found")
                return None

            episode = props.get("episode")
            season_num = episode.get("season", {}).get("number") if episode else None
            ep_num = episode.get("number") if episode else None
            ep_name = episode.get("name", "") if episode else ""

            response = self.session.get(embed_url)
            soup = BeautifulSoup(response.text, "html.parser")
            iframe = soup.find("iframe")

            if not iframe or not iframe.get("src"):
                log.error("No iframe found")
                return None

            m3u8_result = self.get_m3u8_from_embed(iframe.get("src"))
            if not m3u8_result:
                log.error("No m3u8 URL found")
                return None

            m3u8_url = m3u8_result["m3u8_url"]
            referer = m3u8_result["referer"]

            safe_name = re.sub(r"[^\w\s-]", "", title_name).strip().replace(" ", "_")

            if title_type == "tv" and season_num and ep_num:
                video_id = f"sc_{title_id}_{episode_param or ep_num}"
                video_title = f"{title_name} S{season_num:02d}E{ep_num:02d}"
                if ep_name:
                    video_title += f" - {ep_name}"
            else:
                video_id = f"sc_{title_id}"
                video_title = title_name

            return {
                "id": video_id,
                "title": video_title,
                # Store SC URL for just-in-time m3u8 extraction (tokens expire quickly)
                "url": url,
                "webpage_url": url,
                "ext": "mp4",
                "_type": "video",
                "extractor": "streamingcommunity",
                "extractor_key": "StreamingCommunity",
                "season_number": season_num,
                "episode_number": ep_num,
                "episode": ep_name,
                "series": title_name if title_type == "tv" else None,
                # Store for just-in-time extraction
                "_sc_needs_m3u8_extraction": True,
                "_sc_base_url": self.base_url,
            }

        except Exception as e:
            log.error(f"Error extracting watch URL: {e}")
            return None

    def extract(self, url: str) -> Optional[Dict[str, Any]]:
        """Extract from any supported URL type."""
        if "/season-" in url:
            return self.extract_season(url)
        elif "/watch/" in url:
            return self.extract_watch(url)
        else:
            log.error(f"Unsupported URL format: {url}")
            return None

    @staticmethod
    def get_fresh_m3u8(base_url: str, watch_url: str) -> Optional[Dict[str, Any]]:
        """
        Extract fresh m3u8 URL just-in-time before download.
        Tokens expire quickly, so this must be called right before downloading.
        Returns dict with 'm3u8_url' and 'http_headers'.
        """
        try:
            extractor = StreamingCommunityExtractor(base_url)

            # Get watch page data
            path = watch_url.replace(base_url, "")
            watch_data = extractor._inertia_get(path)
            props = watch_data.get("props", {})

            embed_url = props.get("embedUrl")
            if not embed_url:
                log.error("No embed URL found for fresh m3u8 extraction")
                return None

            # Get iframe from embed page
            response = extractor.session.get(embed_url)
            soup = BeautifulSoup(response.text, "html.parser")
            iframe = soup.find("iframe")

            if not iframe or not iframe.get("src"):
                log.error("No iframe found for fresh m3u8 extraction")
                return None

            # Extract m3u8 with fresh token
            iframe_src = iframe.get("src")
            m3u8_result = extractor.get_m3u8_from_embed(iframe_src)
            if not m3u8_result:
                log.error("Could not extract fresh m3u8 URL")
                return None

            # Extract origin from the iframe src (vixcloud domain)
            from urllib.parse import urlparse
            parsed_iframe = urlparse(iframe_src)
            origin = f"{parsed_iframe.scheme}://{parsed_iframe.netloc}"

            # Get cookies from session as string (curl_cffi uses dict-like cookies)
            try:
                cookies = "; ".join([f"{k}={v}" for k, v in extractor.session.cookies.items()])
            except Exception:
                cookies = ""

            m3u8_url = m3u8_result["m3u8_url"]
            referer = m3u8_result["referer"]

            # TEST: Verify the m3u8 URL works with the same session
            log.info(f"Testing m3u8 URL access with same session...")
            try:
                test_headers = {
                    "Referer": referer,
                    "Origin": origin,
                }
                test_response = extractor.session.get(m3u8_url, headers=test_headers)
                log.info(f"M3U8 test response: status={test_response.status_code}, length={len(test_response.text)}")
                if test_response.status_code == 200:
                    log.info(f"M3U8 content preview: {test_response.text[:300]}...")
                else:
                    log.error(f"M3U8 test failed with status {test_response.status_code}")
                    log.error(f"Response body preview: {test_response.text[:500]}")
            except Exception as e:
                log.error(f"M3U8 test request failed: {e}")

            return {
                "m3u8_url": m3u8_url,
                "http_headers": {
                    "Referer": referer,
                    "Origin": origin,
                    "User-Agent": USER_AGENT,
                },
                "cookies": cookies,
            }
        except Exception as e:
            log.error(f"Fresh m3u8 extraction failed: {e}")
            return None

    @staticmethod
    def can_extract(url: str) -> bool:
        """Check if this URL can be handled by StreamingCommunity extractor."""
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname or ""
            return "streamingcommunity" in hostname.lower()
        except Exception:
            return False

    @staticmethod
    def extract_info(url: str) -> Optional[Dict[str, Any]]:
        """
        Static method to extract info from a URL.
        Returns a dict compatible with yt-dlp's extract_info output.
        """
        try:
            parsed = urlparse(url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"

            extractor = StreamingCommunityExtractor(base_url)
            return extractor.extract(url)
        except Exception as e:
            log.error(f"StreamingCommunity extraction failed: {e}")
            return None
