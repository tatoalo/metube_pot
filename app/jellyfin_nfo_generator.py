#!/usr/bin/env python3
"""
NFO Generator for MeTube + Jellyfin Integration
"""

import json
import logging
import os
import sys
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

logging.basicConfig(
    level=logging.INFO, format="[nfo_generator] %(levelname)s: %(message)s"
)
log = logging.getLogger(__name__)


def get_info_json_path(filepath: str) -> str:
    """
    Derive the .info.json path from a video/audio filepath.
    """
    base, _ = os.path.splitext(filepath)
    return base + ".info.json"


def parse_upload_date(date_str: str) -> tuple[str, str]:
    """
    Parse yt-dlp upload_date (YYYYMMDD) to year and premiered date tuple.
    """
    if not date_str or len(date_str) != 8:
        return "", ""

    try:
        year = date_str[:4]
        month = date_str[4:6]
        day = date_str[6:8]
        premiered = f"{year}-{month}-{day}"
        return year, premiered
    except (ValueError, IndexError):
        return "", ""


def seconds_to_minutes(seconds: int | float | None) -> str:
    """
    Convert duration in seconds to minutes.
    """
    if seconds is None:
        return ""
    try:
        return str(int(float(seconds) / 60))
    except (ValueError, TypeError):
        return ""


def create_nfo_xml(info: dict) -> str:
    """
    Create Jellyfin-compatible NFO XML from yt-dlp info dict.
    """
    root = Element("movie")

    title = info.get("title", "Unknown Title")
    SubElement(root, "title").text = title
    SubElement(root, "originaltitle").text = title

    description = info.get("description", "")
    SubElement(root, "plot").text = description

    upload_date = info.get("upload_date", "")
    year, premiered = parse_upload_date(upload_date)
    if year:
        SubElement(root, "year").text = year
    if premiered:
        SubElement(root, "premiered").text = premiered

    uploader = info.get("uploader", info.get("channel", ""))
    if uploader:
        SubElement(root, "studio").text = uploader
        SubElement(root, "director").text = uploader

    # YouTube ID
    video_id = info.get("id", "")
    if video_id:
        uniqueid = SubElement(root, "uniqueid", type="youtube")
        uniqueid.text = video_id

    webpage_url = info.get("webpage_url", "")
    if webpage_url:
        SubElement(root, "website").text = webpage_url

    tags = info.get("tags", [])
    if tags:
        for tag in tags[:20]:  # Limit to 20 tags
            if tag:
                SubElement(root, "tag").text = str(tag)

    duration = info.get("duration")
    runtime = seconds_to_minutes(duration)
    if runtime:
        SubElement(root, "runtime").text = runtime

    xml_str = tostring(root, encoding="unicode")

    dom = minidom.parseString(xml_str)
    pretty_xml = dom.toprettyxml(indent="  ", encoding=None)

    lines = [line for line in pretty_xml.split("\n") if line.strip()]

    return "\n".join(lines)


def generate_nfo(filepath: str) -> bool:
    """
    Generate NFO file from the video filepath.

    Returns:
        True if successful, False otherwise
    """
    # Derive the info.json path from the video filepath
    info_json_path = get_info_json_path(filepath)

    if not os.path.exists(info_json_path):
        log.warning(f"info.json not found: {info_json_path} (video: {filepath})")
        # Not an error - some downloads might not have writeinfojson enabled
        return True

    try:
        with open(info_json_path, "r", encoding="utf-8") as f:
            info = json.load(f)

        # Generate NFO path (same base as video, with .nfo extension)
        base, _ = os.path.splitext(filepath)
        nfo_path = base + ".nfo"

        nfo_content = create_nfo_xml(info)

        with open(nfo_path, "w", encoding="utf-8") as f:
            f.write(nfo_content)

        log.info(f"Created NFO: {nfo_path}")

        # Delete the original info.json
        os.remove(info_json_path)
        log.info(f"Deleted: {info_json_path}")

        return True

    except json.JSONDecodeError as e:
        log.error(f"Failed to parse JSON: {e}")
        return False
    except IOError as e:
        log.error(f"File I/O error: {e}")
        return False
    except Exception as e:
        log.error(f"Unexpected error: {e}")
        return False


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <video_filepath>")
        sys.exit(1)

    filepath = sys.argv[1]
    log.info(f"Processing: {filepath}")

    success = generate_nfo(filepath)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
