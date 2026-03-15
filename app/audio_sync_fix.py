#!/usr/bin/env python3
"""
Audio Sync Fix for MeTube — re-encodes audio to AAC after SponsorBlock cutting.

When SponsorBlock's ModifyChapters postprocessor cuts sponsor segments using
stream-copy mode, video cuts at keyframe boundaries while audio cuts precisely.
Across multiple splice points, these misalignments accumulate causing audio to
progressively fall behind video.

Re-encoding audio to AAC regenerates timestamps from scratch, eliminating drift.
"""

import json
import logging
import os
import subprocess
import sys
import tempfile

logging.basicConfig(
    level=logging.INFO, format="[audio_sync_fix] %(levelname)s: %(message)s"
)
log = logging.getLogger(__name__)


def has_video_stream(filepath: str) -> bool:
    """Check if the file contains a video stream using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-select_streams", "v",
                "-show_entries", "stream=codec_type",
                "-of", "json",
                filepath,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        probe = json.loads(result.stdout)
        streams = probe.get("streams", [])
        return len(streams) > 0
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        log.warning(f"ffprobe failed: {e}")
        return False


def fix_audio_sync(filepath: str) -> bool:
    """
    Re-encode audio to AAC while copying all other streams.

    Returns:
        True if successful or skipped, False on error.
    """
    if not os.path.isfile(filepath):
        log.error(f"File not found: {filepath}")
        return False

    _, ext = os.path.splitext(filepath)
    if ext.lower() != ".mp4":
        log.info(f"Skipping non-MP4 file: {filepath}")
        return True

    if not has_video_stream(filepath):
        log.info(f"Skipping audio-only file: {filepath}")
        return True

    # Create temp file in the same directory to avoid cross-device moves
    dirpath = os.path.dirname(filepath)
    fd, tmp_path = tempfile.mkstemp(suffix=".mp4", dir=dirpath)
    os.close(fd)

    try:
        cmd = [
            "ffmpeg", "-y",
            "-loglevel", "warning",
            "-i", filepath,
            "-map", "0",
            "-dn", "-ignore_unknown",
            "-c", "copy",
            "-c:a", "aac",
            "-b:a", "256k",
            "-movflags", "+faststart",
            tmp_path,
        ]

        log.info(f"Re-encoding audio: {filepath}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            log.error(f"ffmpeg failed (exit {result.returncode}): {result.stderr}")
            return False

        os.replace(tmp_path, filepath)
        log.info(f"Audio sync fix applied: {filepath}")
        return True

    except subprocess.TimeoutExpired:
        log.error("ffmpeg timed out")
        return False
    except Exception as e:
        log.error(f"Unexpected error: {e}")
        return False
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <video_filepath>")
        sys.exit(1)

    filepath = sys.argv[1]
    log.info(f"Processing: {filepath}")

    success = fix_audio_sync(filepath)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
