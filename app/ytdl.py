import os
import shutil
import yt_dlp
import collections
import collections.abc
import copy
import glob
import json
import pickle
from collections import OrderedDict
import time
import asyncio
import multiprocessing
import subprocess
import threading
from functools import partial
import logging
import re
import types
from typing import Any, Optional

import yt_dlp.networking.impersonate
from yt_dlp.utils import STR_FORMAT_RE_TMPL, STR_FORMAT_TYPES
from dl_formats import get_format, get_opts, AUDIO_FORMATS
from datetime import datetime
from state_store import AtomicJsonStore, from_json_compatible, read_legacy_shelf, to_json_compatible
from subscriptions import _entry_id

log = logging.getLogger('ytdl')


# Characters that are invalid in Windows/NTFS path components. These are pre-
# sanitised when substituting playlist/channel titles into output templates so
# that downloads do not fail on NTFS-mounted volumes or Windows Docker hosts.
_WINDOWS_INVALID_PATH_CHARS = re.compile(r'[\\:*?"<>|]')


def _sanitize_path_component(value: Any) -> Any:
    """Replace characters that are invalid in Windows path components with '_'.

    Non-string values (int, float, None, …) are passed through unchanged so
    that numeric format specs (e.g. ``%(playlist_index)02d``) still work.
    Only string values are sanitised because Windows-invalid characters are
    only a concern for human-readable strings (titles, channel names, etc.)
    that may end up as directory names.
    """
    if not isinstance(value, str):
        return value
    return _WINDOWS_INVALID_PATH_CHARS.sub('_', value)


def _number(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _calculate_progress_percent(status: dict[str, Any], previous_percent: Optional[float] = None) -> Optional[float]:
    """Calculate a UI-safe progress percentage from yt-dlp/downloader status.

    yt-dlp's ``total_bytes_estimate`` can briefly equal ``downloaded_bytes`` at
    the beginning of HLS downloads, causing a false 100% update. Fragment counts
    give a more stable bound, so use them to cap estimate-based progress while
    keeping active downloads below 100 until the final finished status arrives.
    """
    if status.get("status") == "finished":
        return 100.0

    downloaded = _number(status.get("downloaded_bytes"))
    exact_total = _number(status.get("total_bytes"))
    estimate_total = _number(status.get("total_bytes_estimate"))
    fragment_index = _number(status.get("fragment_index"))
    fragment_count = _number(status.get("fragment_count"))

    percent = None
    if downloaded is not None and exact_total and exact_total > 0:
        percent = downloaded / exact_total * 100
    else:
        estimate_percent = None
        if downloaded is not None and estimate_total and estimate_total > 0:
            estimate_percent = downloaded / estimate_total * 100

        if fragment_count and fragment_count > 0 and fragment_index is not None:
            bounded_index = min(max(fragment_index, 0), fragment_count)
            fragment_floor = bounded_index / fragment_count * 100
            fragment_ceiling = min((bounded_index + 1) / fragment_count * 100, 99.9)
            percent = fragment_floor
            if estimate_percent is not None:
                percent = min(max(estimate_percent, fragment_floor), fragment_ceiling)
        elif estimate_percent is not None:
            # Ignore the common early HLS estimate that reports 1 KiB / 1 KiB.
            if not (downloaded is not None and estimate_total <= downloaded):
                percent = estimate_percent

    if percent is None:
        return previous_percent

    percent = max(0.0, min(percent, 99.9))
    if previous_percent is not None and percent < previous_percent:
        return previous_percent
    return percent


# Regex matching yt-dlp output-template field references, e.g. ``%(title)s``
# or ``%(playlist_index)03d``.  Built from yt-dlp's own ``STR_FORMAT_RE_TMPL``
# so that it stays in sync with upstream changes to the template syntax.
_OUTTMPL_FIELD_RE = re.compile(
    STR_FORMAT_RE_TMPL.format('[^)]+', f'[{STR_FORMAT_TYPES}ljhqBUDS]')
)


def _resolve_outtmpl_fields(template: str, info_dict: dict, prefixes: tuple[str, ...]) -> str:
    """Resolve specific fields in an output template using yt-dlp's template engine.

    Only field references whose root name starts with one of *prefixes* are
    evaluated.  All other references are left untouched so that yt-dlp can
    resolve them later during the actual download.

    This delegates to ``YoutubeDL.evaluate_outtmpl`` for each targeted field
    reference, giving access to the full yt-dlp template syntax (defaults,
    conditional formatting, math operations, datetime formatting, etc.).
    """
    matches = list(_OUTTMPL_FIELD_RE.finditer(template))
    if not matches:
        return template

    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        for match in reversed(matches):
            key = match.group('key')
            if key is None:
                continue
            root = re.match(r'\w+', key)
            if root is None or not root.group(0).startswith(prefixes):
                continue
            resolved = ydl.evaluate_outtmpl(match.group(0), info_dict)
            template = template[:match.start()] + resolved + template[match.end():]

    return template

_MAX_ENTRY_SANITIZE_DEPTH = 64


def _sanitize_entry_for_pickle(obj, _depth=0):
    """Recursively normalize yt-dlp ``info_dict`` data so it can be stored in shelve/pickle.

    Live streams and newer yt-dlp versions may nest generators, iterators, sets, or
    non-serializable objects (e.g. locks) inside the extracted metadata. The previous
    helper only walked plain dict/list/tuple and only expanded ``types.GeneratorType``.
    """
    if _depth > _MAX_ENTRY_SANITIZE_DEPTH:
        return None
    if obj is None or isinstance(obj, (bool, int, float, str, bytes)):
        return obj
    if isinstance(obj, types.GeneratorType):
        return _sanitize_entry_for_pickle(list(obj), _depth + 1)
    if isinstance(obj, collections.abc.Mapping):
        return {k: _sanitize_entry_for_pickle(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(_sanitize_entry_for_pickle(x, _depth + 1) for x in obj)
    if isinstance(obj, (set, frozenset)):
        return [_sanitize_entry_for_pickle(x, _depth + 1) for x in obj]
    if isinstance(obj, collections.deque):
        return [_sanitize_entry_for_pickle(x, _depth + 1) for x in obj]
    if isinstance(obj, collections.abc.Iterator):
        try:
            return _sanitize_entry_for_pickle(list(obj), _depth + 1)
        except Exception:
            return None
    try:
        pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
        return obj
    except Exception:
        return None


def _convert_srt_to_txt_file(subtitle_path: str):
    """Convert an SRT subtitle file into plain text by stripping cue numbers/timestamps."""
    txt_path = os.path.splitext(subtitle_path)[0] + ".txt"
    try:
        with open(subtitle_path, "r", encoding="utf-8", errors="replace") as infile:
            content = infile.read()

        # Normalize newlines so cue splitting is consistent across platforms.
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        cues = []
        for block in re.split(r"\n{2,}", content):
            lines = [line.strip() for line in block.split("\n") if line.strip()]
            if not lines:
                continue
            if re.fullmatch(r"\d+", lines[0]):
                lines = lines[1:]
            if lines and "-->" in lines[0]:
                lines = lines[1:]

            text_lines = []
            for line in lines:
                if "-->" in line:
                    continue
                clean_line = re.sub(r"<[^>]+>", "", line).strip()
                if clean_line:
                    text_lines.append(clean_line)
            if text_lines:
                cues.append(" ".join(text_lines))

        with open(txt_path, "w", encoding="utf-8") as outfile:
            if cues:
                outfile.write("\n".join(cues))
                outfile.write("\n")
        return txt_path
    except OSError as exc:
        log.warning(f"Failed to convert subtitle file {subtitle_path} to txt: {exc}")
        return None

class DownloadQueueNotifier:
    async def added(self, dl):
        raise NotImplementedError

    async def updated(self, dl):
        raise NotImplementedError

    async def completed(self, dl):
        raise NotImplementedError

    async def canceled(self, id):
        raise NotImplementedError

    async def cleared(self, id):
        raise NotImplementedError

class DownloadInfo:
    def __init__(
        self,
        id,
        title,
        url,
        quality,
        download_type,
        codec,
        format,
        folder,
        custom_name_prefix,
        error,
        entry,
        playlist_item_limit,
        split_by_chapters,
        chapter_template,
        subtitle_language="en",
        subtitle_mode="prefer_manual",
        ytdl_options_presets=None,
        ytdl_options_overrides=None,
    ):
        self.id = id if len(custom_name_prefix) == 0 else f'{custom_name_prefix}.{id}'
        self.title = title if len(custom_name_prefix) == 0 else f'{custom_name_prefix}.{title}'
        self.url = url
        self.quality = quality
        self.download_type = download_type
        self.codec = codec
        self.format = format
        self.folder = folder
        self.custom_name_prefix = custom_name_prefix
        self.msg = self.percent = self.speed = self.eta = None
        self.downloaded_bytes = None
        self.total_bytes = None
        self.total_bytes_estimate = None
        self.fragment_index = None
        self.fragment_count = None
        self.status = "pending"
        self.size = None
        self.timestamp = time.time_ns()
        self.error = error
        # Strip non-pickleable values (generators, iterators, locks, etc.) for shelve
        self.entry = _sanitize_entry_for_pickle(entry) if entry is not None else None
        self.playlist_item_limit = playlist_item_limit
        self.split_by_chapters = split_by_chapters
        self.chapter_template = chapter_template
        self.subtitle_language = subtitle_language
        self.subtitle_mode = subtitle_mode
        self.ytdl_options_presets = list(ytdl_options_presets or [])
        self.ytdl_options_overrides = dict(ytdl_options_overrides or {})
        self.subtitle_files = []

    def __setstate__(self, state):
        """BACKWARD COMPATIBILITY: migrate old DownloadInfo from persistent queue files."""
        self.__dict__.update(state)
        if 'download_type' not in state:
            old_format = state.get('format', 'any')
            old_video_codec = state.get('video_codec', 'auto')
            old_quality = state.get('quality', 'best')
            old_subtitle_format = state.get('subtitle_format', 'srt')

            if old_format in AUDIO_FORMATS:
                self.download_type = 'audio'
                self.codec = 'auto'
            elif old_format == 'thumbnail':
                self.download_type = 'thumbnail'
                self.codec = 'auto'
                self.format = 'jpg'
            elif old_format == 'captions':
                self.download_type = 'captions'
                self.codec = 'auto'
                self.format = old_subtitle_format
            else:
                self.download_type = 'video'
                self.codec = old_video_codec
                if old_quality == 'best_ios':
                    self.format = 'ios'
                    self.quality = 'best'
                elif old_quality == 'audio':
                    self.download_type = 'audio'
                    self.codec = 'auto'
                    self.format = 'm4a'
                    self.quality = 'best'
            self.__dict__.pop('video_codec', None)
            self.__dict__.pop('subtitle_format', None)

        if not getattr(self, "codec", None):
            self.codec = "auto"
        if not hasattr(self, "folder"):
            self.folder = ""
        if not hasattr(self, "custom_name_prefix"):
            self.custom_name_prefix = ""
        if not hasattr(self, "playlist_item_limit"):
            self.playlist_item_limit = 0
        if not hasattr(self, "split_by_chapters"):
            self.split_by_chapters = False
        if not hasattr(self, "chapter_template"):
            self.chapter_template = ""
        if not hasattr(self, "subtitle_language"):
            self.subtitle_language = "en"
        if not hasattr(self, "subtitle_mode"):
            self.subtitle_mode = "prefer_manual"
        legacy_preset = self.__dict__.pop("ytdl_options_preset", None)
        if "ytdl_options_presets" not in self.__dict__:
            if isinstance(legacy_preset, str) and legacy_preset.strip():
                self.ytdl_options_presets = [legacy_preset.strip()]
            elif isinstance(legacy_preset, list):
                self.ytdl_options_presets = [str(x).strip() for x in legacy_preset if str(x).strip()]
            else:
                self.ytdl_options_presets = []
        if not hasattr(self, "ytdl_options_overrides"):
            self.ytdl_options_overrides = {}
        if not hasattr(self, "entry"):
            self.entry = None
        if not hasattr(self, "subtitle_files"):
            self.subtitle_files = []
        if not hasattr(self, "chapter_files"):
            self.chapter_files = []
        if not hasattr(self, "downloaded_bytes"):
            self.downloaded_bytes = None
        if not hasattr(self, "total_bytes"):
            self.total_bytes = None
        if not hasattr(self, "total_bytes_estimate"):
            self.total_bytes_estimate = None
        if not hasattr(self, "fragment_index"):
            self.fragment_index = None
        if not hasattr(self, "fragment_count"):
            self.fragment_count = None


_PERSISTED_DOWNLOAD_FIELDS = (
    "id",
    "title",
    "url",
    "quality",
    "download_type",
    "codec",
    "format",
    "folder",
    "custom_name_prefix",
    "playlist_item_limit",
    "split_by_chapters",
    "chapter_template",
    "subtitle_language",
    "subtitle_mode",
    "ytdl_options_presets",
    "ytdl_options_overrides",
    "status",
    "timestamp",
    "error",
    "msg",
    "filename",
    "size",
    "chapter_files",
)


_COMPACT_ENTRY_EXTRA_KEYS = frozenset(("n_entries", "__last_playlist_index"))


def _compact_persisted_entry(entry: Any) -> Optional[dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    if "streamingcommunity" in str(entry.get("extractor", "")).lower():
        return entry
    compact = {
        key: value
        for key, value in entry.items()
        if key.startswith("playlist") or key.startswith("channel") or key in _COMPACT_ENTRY_EXTRA_KEYS
    }
    return compact or None


def _download_info_to_record(
    info: DownloadInfo,
    *,
    include_entry: bool,
) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key in _PERSISTED_DOWNLOAD_FIELDS:
        if hasattr(info, key):
            value = getattr(info, key)
            if value is not None:
                record[key] = to_json_compatible(value)
    if include_entry:
        compact_entry = _compact_persisted_entry(getattr(info, "entry", None))
        if compact_entry is not None:
            record["entry"] = to_json_compatible(compact_entry)
    return record


def _download_info_from_record(record: dict[str, Any]) -> DownloadInfo:
    info = DownloadInfo.__new__(DownloadInfo)
    info.__setstate__({key: from_json_compatible(value) for key, value in record.items()})
    if not hasattr(info, "msg"):
        info.msg = None
    if not hasattr(info, "percent"):
        info.percent = None
    if not hasattr(info, "speed"):
        info.speed = None
    if not hasattr(info, "eta"):
        info.eta = None
    if not hasattr(info, "status"):
        info.status = "pending"
    if not hasattr(info, "size"):
        info.size = None
    if not hasattr(info, "error"):
        info.error = None
    return info

class Download:
    manager = None

    @classmethod
    def shutdown_manager(cls):
        if cls.manager is not None:
            cls.manager.shutdown()
            cls.manager = None

    def __init__(self, download_dir, temp_dir, output_template, output_template_chapter, quality, format, ytdl_opts, info):
        self.download_dir = download_dir
        self.temp_dir = temp_dir
        self.output_template = output_template
        self.output_template_chapter = output_template_chapter
        self.info = info
        self.format = get_format(
            getattr(info, 'download_type', 'video'),
            getattr(info, 'codec', 'auto'),
            format,
            quality,
        )
        self.ytdl_opts = get_opts(
            getattr(info, 'download_type', 'video'),
            getattr(info, 'codec', 'auto'),
            format,
            quality,
            ytdl_opts,
            subtitle_language=getattr(info, 'subtitle_language', 'en'),
            subtitle_mode=getattr(info, 'subtitle_mode', 'prefer_manual'),
        )
        if "impersonate" in self.ytdl_opts:
            self.ytdl_opts["impersonate"] = yt_dlp.networking.impersonate.ImpersonateTarget.from_str(self.ytdl_opts["impersonate"])
        self.canceled = False
        self.tmpfilename = None
        self.status_queue = None
        self.proc = None
        self.loop = None
        self.notifier = None
        self._progress_source = None

    def _download_streamingcommunity(self):
        is_streamingcommunity = (
            self.info.entry
            and "streamingcommunity" in self.info.entry.get("extractor", "").lower()
        )
        if not is_streamingcommunity or not self.info.entry:
            return None
        if not self.info.entry.get("_sc_needs_m3u8_extraction"):
            return None

        from extractors.streamingcommunity import StreamingCommunityExtractor

        log.info(f"Extracting fresh m3u8 URL for: {self.info.title}")
        fresh_result = StreamingCommunityExtractor.get_fresh_m3u8(
            self.info.entry.get("_sc_base_url"),
            self.info.url,
        )
        if not fresh_result:
            log.error("Failed to extract fresh m3u8 URL")
            self.status_queue.put({"status": "error", "msg": "Failed to extract video URL"})
            return 1

        m3u8_url = fresh_result["m3u8_url"]
        http_headers = fresh_result["http_headers"]
        cookies = fresh_result.get("cookies", "")
        log.info(f"Got fresh m3u8 URL: {m3u8_url[:80]}...")

        safe_title = re.sub(r'[<>:"/\\|?*]', "_", self.info.title).strip(". ")
        output_path = os.path.join(self.download_dir, f"{safe_title}.mp4")
        info_json_path = os.path.join(self.download_dir, f"{safe_title}.info.json")

        try:
            os.makedirs(os.path.dirname(output_path) or self.download_dir, exist_ok=True)
            with open(info_json_path, "w", encoding="utf-8") as f:
                json.dump(self.info.entry, f, indent=2, ensure_ascii=False)
            log.info(f"Wrote StreamingCommunity info.json: {info_json_path}")
        except OSError as exc:
            log.warning(f"Failed to write info.json: {exc}")

        use_ffmpeg = os.environ.get("SC_USE_FFMPEG", "false").lower() in ("true", "1", "on")
        if use_ffmpeg:
            self.status_queue.put({"status": "downloading", "msg": "Starting ffmpeg download..."})
            return self._download_streamingcommunity_ffmpeg(m3u8_url, http_headers, cookies, output_path)

        self.status_queue.put({"status": "downloading", "msg": "Starting N_m3u8DL-RE download..."})
        ret = self._download_streamingcommunity_nm3u8(
            m3u8_url,
            http_headers,
            cookies,
            safe_title,
            output_path,
            report_error=False,
        )
        if ret == 0:
            return 0

        log.warning("N_m3u8DL-RE failed; retrying StreamingCommunity download with ffmpeg")
        self.status_queue.put({"status": "downloading", "msg": "N_m3u8DL-RE failed, retrying with ffmpeg..."})
        self._cleanup_streamingcommunity_partial(output_path, safe_title)
        return self._download_streamingcommunity_ffmpeg(m3u8_url, http_headers, cookies, output_path)

    def _download_streamingcommunity_ffmpeg(self, m3u8_url, http_headers, cookies, output_path):
        log.info(f"Running ffmpeg for StreamingCommunity download: {self.info.title}")
        header_str = f"User-Agent: {http_headers.get('User-Agent', '')}\r\n"
        header_str += f"Referer: {http_headers.get('Referer', '')}\r\n"
        header_str += f"Origin: {http_headers.get('Origin', '')}\r\n"
        if cookies:
            header_str += f"Cookie: {cookies}\r\n"

        total_duration = None
        try:
            probe_cmd = [
                "ffprobe",
                "-v", "error",
                "-headers", header_str,
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                m3u8_url,
            ]
            probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
            if probe_result.returncode == 0 and probe_result.stdout.strip():
                total_duration = float(probe_result.stdout.strip())
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log.warning(f"Could not get duration: {exc}")

        ffmpeg_cmd = [
            "ffmpeg",
            "-y",
            "-headers", header_str,
            "-i", m3u8_url,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-progress", "pipe:1",
            output_path,
        ]
        try:
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
        except OSError as exc:
            log.error(f"Could not start ffmpeg: {exc}")
            self.status_queue.put({"status": "error", "msg": f"Could not start ffmpeg: {exc}"})
            return 1

        stderr_lines = []

        def _drain_stderr():
            for line in process.stderr:
                stderr_lines.append(line)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        current_time = 0
        current_size = 0
        current_speed = 0
        last_update = time.time()
        for line in process.stdout:
            line = line.strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key == "out_time_ms" and value.isdigit():
                current_time = int(value) / 1_000_000
            elif key == "total_size" and value.isdigit():
                current_size = int(value)
            elif key == "speed" and value.endswith("x"):
                try:
                    current_speed = float(value[:-1])
                except ValueError:
                    pass
            elif key == "progress" and time.time() - last_update >= 0.5:
                last_update = time.time()
                status_update = {"status": "downloading"}
                if current_size > 0:
                    status_update["downloaded_bytes"] = current_size
                if total_duration and total_duration > 0 and current_time > 0:
                    status_update["total_bytes_estimate"] = int(current_size / (current_time / total_duration))
                    if current_speed > 0:
                        status_update["eta"] = int((total_duration - current_time) / current_speed)
                        status_update["speed"] = current_speed * (current_size / current_time)
                self.status_queue.put(status_update)

        process.wait()
        stderr_thread.join(timeout=5)
        if process.returncode == 0 and os.path.exists(output_path):
            self.status_queue.put({"status": "finished", "filename": output_path})
            return 0
        stderr_tail = "".join(stderr_lines[-20:])
        log.error(f"FFmpeg failed with code {process.returncode}, stderr:\n{stderr_tail}")
        msg = f"FFmpeg failed with code {process.returncode}"
        if stderr_tail.strip():
            msg += f": {stderr_tail.strip()[-500:]}"
        self.status_queue.put({"status": "error", "msg": msg})
        return process.returncode or 1

    def _cleanup_streamingcommunity_partial(self, output_path, safe_title):
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except OSError as exc:
            log.debug(f"Could not remove partial StreamingCommunity output {output_path}: {exc}")

        temp_dir = self.temp_dir or os.path.join(self.download_dir, ".tmp")
        for path in (
            os.path.join(temp_dir, safe_title),
            os.path.join(temp_dir, f"{safe_title}.tmp"),
            os.path.join(self.download_dir, safe_title),
        ):
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)

    def _download_streamingcommunity_nm3u8(self, m3u8_url, http_headers, cookies, safe_title, output_path, report_error=True):
        thread_count = os.environ.get("SC_THREAD_COUNT", "16")
        temp_dir = self.temp_dir or os.path.join(self.download_dir, ".tmp")
        log.info(f"Running N_m3u8DL-RE for StreamingCommunity download: {self.info.title} (threads={thread_count})")
        nm3u8_cmd = [
            "N_m3u8DL-RE", m3u8_url,
            "--save-dir", self.download_dir,
            "--save-name", safe_title,
            "--tmp-dir", temp_dir,
            "--thread-count", thread_count,
            "--auto-select",
            "--del-after-done",
            "--no-log",
            "--mux-after-done", "format=mp4:muxer=ffmpeg",
            "--log-level", "INFO",
            "-H", f"User-Agent: {http_headers.get('User-Agent', '')}",
            "-H", f"Referer: {http_headers.get('Referer', '')}",
            "-H", f"Origin: {http_headers.get('Origin', '')}",
        ]
        if cookies:
            nm3u8_cmd.extend(["-H", f"Cookie: {cookies}"])

        ansi_re = re.compile(r'\x1b\[[0-9;]*[A-Za-z]|\x1b\].*?\x07|\r')
        seg_pct_re = re.compile(r'(\d+)/(\d+)\s+([\d.]+)%')
        size_re = re.compile(r'([\d.]+)\s*(KB|MB|GB)\s*/\s*([\d.]+)\s*(KB|MB|GB)')
        speed_re = re.compile(r'([\d.]+)\s*(KB|MB|GB)ps')
        eta_re = re.compile(r'(\d{2}):(\d{2}):(\d{2})\s*$')

        def _parse_size_bytes(value, unit):
            value = float(value)
            if unit == "KB":
                return value * 1024
            if unit == "MB":
                return value * 1024 * 1024
            if unit == "GB":
                return value * 1024 * 1024 * 1024
            return value

        output_lines = []
        try:
            process = subprocess.Popen(
                nm3u8_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
        except OSError as exc:
            log.error(f"Could not start N_m3u8DL-RE: {exc}")
            if report_error:
                self.status_queue.put({"status": "error", "msg": f"Could not start N_m3u8DL-RE: {exc}"})
            return 1

        last_update = time.time()
        for raw_line in process.stdout:
            line = ansi_re.sub("", raw_line).strip()
            if line:
                output_lines.append(line)
                output_lines = output_lines[-30:]
            if not line or time.time() - last_update < 0.5:
                continue
            last_update = time.time()
            status_update = {"status": "downloading"}

            match = seg_pct_re.search(line)
            if match:
                status_update["downloaded_bytes"] = int(match.group(1))
                status_update["total_bytes"] = int(match.group(2))
            match = size_re.search(line)
            if match:
                status_update["downloaded_bytes"] = int(_parse_size_bytes(match.group(1), match.group(2)))
                status_update["total_bytes"] = int(_parse_size_bytes(match.group(3), match.group(4)))
            match = speed_re.search(line)
            if match:
                status_update["speed"] = _parse_size_bytes(match.group(1), match.group(2))
            match = eta_re.search(line)
            if match:
                status_update["eta"] = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
            if len(status_update) > 1:
                self.status_queue.put(status_update)

        process.wait()
        if process.returncode != 0:
            output_tail = "\n".join(output_lines[-20:])
            log.error(f"N_m3u8DL-RE failed with code {process.returncode}, output:\n{output_tail}")
            if report_error:
                msg = f"N_m3u8DL-RE failed with code {process.returncode}"
                if output_tail.strip():
                    msg += f": {output_tail.strip()[-500:]}"
                self.status_queue.put({"status": "error", "msg": msg})
            return process.returncode or 1

        final_path = output_path if os.path.exists(output_path) else None
        if final_path is None:
            pattern = os.path.join(self.download_dir, f"{safe_title}*.mp4")
            candidates = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
            final_path = candidates[0] if candidates else None

        if final_path and os.path.exists(final_path):
            self.status_queue.put({"status": "finished", "filename": final_path})
            return 0

        seg_dir = os.path.join(self.download_dir, safe_title)
        if not os.path.isdir(seg_dir):
            log.error(f"N_m3u8DL-RE exited OK but output not found: {output_path}")
            self.status_queue.put({"status": "error", "msg": "Download finished but output file not found"})
            return 1

        seg_files = sorted(
            [
                os.path.join(dp, f)
                for dp, _, fns in os.walk(seg_dir)
                for f in fns
                if f.endswith((".m4s", ".ts", ".mp4", ".m4a", ".aac"))
            ],
            key=os.path.getmtime,
        )
        if not seg_files:
            self.status_queue.put({"status": "error", "msg": "Download finished but no segments to mux"})
            return 1
        try:
            concat_path = os.path.join(seg_dir, "_concat.txt")
            with open(concat_path, "w", encoding="utf-8") as concat_file:
                for segment_file in seg_files:
                    concat_file.write(f"file '{segment_file}'\n")
            ffmpeg_cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_path, "-c", "copy", output_path]
            ff_result = subprocess.run(ffmpeg_cmd, capture_output=True, timeout=600)
            if ff_result.returncode == 0 and os.path.exists(output_path):
                self.status_queue.put({"status": "finished", "filename": output_path})
                shutil.rmtree(seg_dir, ignore_errors=True)
                return 0
            log.error(f"Manual ffmpeg mux failed: {ff_result.stderr.decode(errors='replace')[-500:]}")
        except (OSError, subprocess.SubprocessError) as exc:
            log.error(f"Manual ffmpeg mux exception: {exc}")
        self.status_queue.put({"status": "error", "msg": "Download finished but muxing failed"})
        return 1

    def _download(self):
        log.info(f"Starting download for: {self.info.title} ({self.info.url})")
        try:
            debug_logging = logging.getLogger().isEnabledFor(logging.DEBUG)
            def put_status(st):
                self.status_queue.put({k: v for k, v in st.items() if k in (
                    'tmpfilename',
                    'filename',
                    'status',
                    'msg',
                    'total_bytes',
                    'total_bytes_estimate',
                    'downloaded_bytes',
                    'fragment_index',
                    'fragment_count',
                    'speed',
                    'eta',
                )})

            def put_status_postprocessor(d):
                if d['postprocessor'] == 'MoveFiles' and d['status'] == 'finished':
                    filepath = d['info_dict']['filepath']
                    if '__finaldir' in d['info_dict']:
                        finaldir = d['info_dict']['__finaldir']
                        filename = os.path.join(finaldir, os.path.basename(filepath))
                    else:
                        filename = filepath
                    self.status_queue.put({'status': 'finished', 'filename': filename})
                    # For captions-only downloads, yt-dlp may still report a media-like
                    # filepath in MoveFiles. Capture subtitle outputs explicitly so the
                    # UI can link to real caption files.
                    if getattr(self.info, 'download_type', '') == 'captions':
                        requested_subtitles = d.get('info_dict', {}).get('requested_subtitles', {}) or {}
                        for subtitle in requested_subtitles.values():
                            if isinstance(subtitle, dict) and subtitle.get('filepath'):
                                self.status_queue.put({'subtitle_file': subtitle['filepath']})

                # Capture all chapter files when SplitChapters finishes
                elif d.get('postprocessor') == 'SplitChapters' and d.get('status') == 'finished':
                    chapters = d.get('info_dict', {}).get('chapters', [])
                    if chapters:
                        for chapter in chapters:
                            if isinstance(chapter, dict) and 'filepath' in chapter:
                                log.info(f"Captured chapter file: {chapter['filepath']}")
                                self.status_queue.put({'chapter_file': chapter['filepath']})
                    else:
                        log.warning("SplitChapters finished but no chapter files found in info_dict")

            ytdl_params = {
                'quiet': not debug_logging,
                'verbose': debug_logging,
                'no_color': True,
                'paths': {"home": self.download_dir, "temp": self.temp_dir},
                'outtmpl': { "default": self.output_template, "chapter": self.output_template_chapter },
                'format': self.format,
                'socket_timeout': 30,
                'ignore_no_formats_error': True,
                'progress_hooks': [put_status],
                'postprocessor_hooks': [put_status_postprocessor],
                **self.ytdl_opts,
            }

            # Add chapter splitting options if enabled
            if self.info.split_by_chapters:
                ytdl_params['outtmpl']['chapter'] = self.info.chapter_template
                if 'postprocessors' not in ytdl_params:
                    ytdl_params['postprocessors'] = []
                ytdl_params['postprocessors'].append({
                    'key': 'FFmpegSplitChapters',
                    'force_keyframes': False
                })

            ret = self._download_streamingcommunity()
            if ret is None:
                ret = yt_dlp.YoutubeDL(params=ytdl_params).download([self.info.url])
                self.status_queue.put({'status': 'finished' if ret == 0 else 'error'})
            elif ret == 0:
                self.status_queue.put({'status': 'finished'})
            log.info(f"Finished download for: {self.info.title}")
        except yt_dlp.utils.YoutubeDLError as exc:
            log.error(f"Download error for {self.info.title}: {str(exc)}")
            self.status_queue.put({'status': 'error', 'msg': str(exc)})
        except Exception as exc:
            log.exception(f"Unexpected download error for {self.info.title}")
            self.status_queue.put({'status': 'error', 'msg': str(exc)})

    async def start(self, notifier):
        log.info(f"Preparing download for: {self.info.title}")
        if Download.manager is None:
            Download.manager = multiprocessing.Manager()
        self.status_queue = Download.manager.Queue()
        self.proc = multiprocessing.Process(target=self._download)
        self.proc.start()
        self.loop = asyncio.get_running_loop()
        self.notifier = notifier
        self.info.status = 'preparing'
        await self.notifier.updated(self.info)
        self.status_task = asyncio.create_task(self.update_status())
        await self.loop.run_in_executor(None, self.proc.join)
        # Signal update_status to stop and wait for it to finish
        # so that all status updates (including MoveFiles with correct
        # file size) are processed before _post_download_cleanup runs.
        if self.status_queue is not None:
            self.status_queue.put(None)
        await self.status_task

    def cancel(self):
        log.info(f"Cancelling download: {self.info.title}")
        if self.running():
            try:
                self.proc.kill()
            except Exception as e:
                log.error(f"Error killing process for {self.info.title}: {e}")
        self.canceled = True
        if self.status_queue is not None:
            self.status_queue.put(None)

    def close(self):
        log.info(f"Closing download process for: {self.info.title}")
        if self.started():
            self.proc.close()

    def running(self):
        try:
            return self.proc is not None and self.proc.is_alive()
        except ValueError:
            return False

    def started(self):
        return self.proc is not None

    async def update_status(self):
        while True:
            status = await self.loop.run_in_executor(None, self.status_queue.get)
            if status is None:
                log.info(f"Status update finished for: {self.info.title}")
                return
            if self.canceled:
                log.info(f"Download {self.info.title} is canceled; stopping status updates.")
                return
            self.tmpfilename = status.get('tmpfilename')
            if 'filename' in status:
                fileName = status.get('filename')
                rel_name = os.path.relpath(fileName, self.download_dir)
                # For captions mode, ignore media-like placeholders and let subtitle_file
                # statuses define the final file shown in the UI.
                if getattr(self.info, 'download_type', '') == 'captions':
                    requested_subtitle_format = str(getattr(self.info, 'format', '')).lower()
                    allowed_caption_exts = ('.txt',) if requested_subtitle_format == 'txt' else ('.vtt', '.srt', '.sbv', '.scc', '.ttml', '.dfxp')
                    if not rel_name.lower().endswith(allowed_caption_exts):
                        continue
                self.info.filename = rel_name
                self.info.size = os.path.getsize(fileName) if os.path.exists(fileName) else None
                if getattr(self.info, 'download_type', '') == 'thumbnail':
                    self.info.filename = re.sub(r'\.webm$', '.jpg', self.info.filename)

            # Handle chapter files
            log.debug(f"Update status for {self.info.title}: {status}")
            if 'chapter_file' in status:
                chapter_file = status.get('chapter_file')
                if not hasattr(self.info, 'chapter_files'):
                    self.info.chapter_files = []
                rel_path = os.path.relpath(chapter_file, self.download_dir)
                file_size = os.path.getsize(chapter_file) if os.path.exists(chapter_file) else None
                #Postprocessor hook called multiple times with chapters. Only insert if not already present.
                existing = next((cf for cf in self.info.chapter_files if cf['filename'] == rel_path), None)
                if not existing:
                    self.info.chapter_files.append({'filename': rel_path, 'size': file_size})
                # Skip the rest of status processing for chapter files
                continue

            if 'subtitle_file' in status:
                subtitle_file = status.get('subtitle_file')
                if not subtitle_file:
                    continue
                subtitle_output_file = subtitle_file

                # txt mode is derived from SRT by stripping cue metadata.
                if getattr(self.info, 'download_type', '') == 'captions' and str(getattr(self.info, 'format', '')).lower() == 'txt':
                    converted_txt = _convert_srt_to_txt_file(subtitle_file)
                    if converted_txt:
                        subtitle_output_file = converted_txt
                        if converted_txt != subtitle_file:
                            try:
                                os.remove(subtitle_file)
                            except OSError as exc:
                                log.debug(f"Could not remove temporary SRT file {subtitle_file}: {exc}")

                rel_path = os.path.relpath(subtitle_output_file, self.download_dir)
                file_size = os.path.getsize(subtitle_output_file) if os.path.exists(subtitle_output_file) else None
                existing = next((sf for sf in self.info.subtitle_files if sf['filename'] == rel_path), None)
                if not existing:
                    self.info.subtitle_files.append({'filename': rel_path, 'size': file_size})
                # Prefer first subtitle file as the primary result link in captions mode.
                if getattr(self.info, 'download_type', '') == 'captions' and (
                    not getattr(self.info, 'filename', None) or
                    str(getattr(self.info, 'format', '')).lower() == 'txt'
                ):
                    self.info.filename = rel_path
                    self.info.size = file_size
                continue

            self.info.status = status['status']
            self.info.msg = status.get('msg')
            if 'downloaded_bytes' in status:
                self.info.downloaded_bytes = status.get('downloaded_bytes')
            if 'total_bytes' in status:
                self.info.total_bytes = status.get('total_bytes')
            if 'total_bytes_estimate' in status:
                self.info.total_bytes_estimate = status.get('total_bytes_estimate')
            if 'fragment_index' in status:
                self.info.fragment_index = status.get('fragment_index')
            if 'fragment_count' in status:
                self.info.fragment_count = status.get('fragment_count')

            progress_source = status.get('filename') or status.get('tmpfilename')
            previous_percent = self.info.percent
            if progress_source and getattr(self, '_progress_source', None) not in (None, progress_source):
                previous_percent = None
            if progress_source:
                self._progress_source = progress_source
            self.info.percent = _calculate_progress_percent(status, previous_percent)
            self.info.speed = status.get('speed')
            self.info.eta = status.get('eta')
            log.debug(f"Updating status for {self.info.title}: {status}")
            await self.notifier.updated(self.info)

class PersistentQueue:
    def __init__(self, name, path):
        self.identifier = name
        pdir = os.path.dirname(path)
        if not os.path.isdir(pdir):
            os.mkdir(pdir)
        self.legacy_path = path
        self.path = f"{path}.json"
        self.store = AtomicJsonStore(self.path, kind=f"persistent_queue:{name}")
        self.dict = OrderedDict()

    def load(self):
        for k, v in self.saved_items():
            self.dict[k] = Download(None, None, None, None, getattr(v, 'quality', 'best'), getattr(v, 'format', 'any'), {}, v)

    def exists(self, key):
        return key in self.dict

    def get(self, key):
        return self.dict[key]

    def items(self):
        return self.dict.items()

    def saved_items(self):
        items = [
            (item["key"], _download_info_from_record(item["info"]))
            for item in self._load_state_items()
        ]
        return sorted(items, key=lambda item: item[1].timestamp)

    def _should_persist_entry(self) -> bool:
        return self.identifier != "completed"

    def _serialize_items(self):
        return [
            {
                "key": key,
                "info": _download_info_to_record(
                    download.info,
                    include_entry=self._should_persist_entry(),
                ),
            }
            for key, download in self.dict.items()
        ]

    def _save_dict(self):
        self.store.save({"items": self._serialize_items()})

    def _load_state_items(self):
        payload = self.store.load()
        if payload is not None:
            items = payload.get("items")
            if isinstance(items, list):
                compact_items = [
                    {
                        "key": item["key"],
                        "info": _download_info_to_record(
                            _download_info_from_record(item["info"]),
                            include_entry=self._should_persist_entry(),
                        ),
                    }
                    for item in items
                    if isinstance(item, dict) and "key" in item and "info" in item
                ]
                if payload.get("schema_version") != self.store.schema_version or compact_items != items:
                    self.store.save({"items": compact_items})
                return compact_items
            log.warning("PersistentQueue:%s state file did not contain an items list", self.identifier)
            return []

        legacy_items = read_legacy_shelf(self.legacy_path)
        if legacy_items is None:
            return []

        items = [
            {
                "key": key,
                "info": _download_info_to_record(
                    value,
                    include_entry=self._should_persist_entry(),
                ),
            }
            for key, value in sorted(legacy_items, key=lambda item: item[1].timestamp)
        ]
        self.store.save({"items": items})
        return items

    def put(self, value):
        key = value.info.url
        old = self.dict.get(key)
        self.dict[key] = value
        try:
            self._save_dict()
        except Exception:
            if old is None:
                del self.dict[key]
            else:
                self.dict[key] = old
            raise

    def delete(self, key):
        if key in self.dict:
            old = self.dict[key]
            del self.dict[key]
            try:
                self._save_dict()
            except Exception:
                self.dict[key] = old
                raise

    def next(self):
        k, v = next(iter(self.dict.items()))
        return k, v

    def empty(self):
        return not bool(self.dict)

class DownloadQueue:
    def __init__(self, config, notifier):
        self.config = config
        self.notifier = notifier
        self.queue = PersistentQueue("queue", self.config.STATE_DIR + '/queue')
        self.done = PersistentQueue("completed", self.config.STATE_DIR + '/completed')
        self.pending = PersistentQueue("pending", self.config.STATE_DIR + '/pending')
        self.active_downloads = set()
        self.semaphore = asyncio.Semaphore(int(self.config.MAX_CONCURRENT_DOWNLOADS))
        self.done.load()
        self._add_generation = 0
        self._canceled_urls = set()  # URLs canceled during current playlist add

    def cancel_add(self):
        self._add_generation += 1
        log.info('Playlist add operation canceled by user')

    async def __import_queue(self):
        for k, v in self.queue.saved_items():
            await self.__add_download(v, True)

    async def __import_pending(self):
        for k, v in self.pending.saved_items():
            await self.__add_download(v, False)

    async def initialize(self):
        log.info("Initializing DownloadQueue")
        asyncio.create_task(self.__import_queue())
        asyncio.create_task(self.__import_pending())

    async def __start_download(self, download):
        if download.canceled:
            log.info(f"Download {download.info.title} was canceled, skipping start.")
            return
        async with self.semaphore:
            if download.canceled:
                log.info(f"Download {download.info.title} was canceled, skipping start.")
                return
            await download.start(self.notifier)
            self._post_download_cleanup(download)

    def _post_download_cleanup(self, download):
        if download.info.status != 'finished':
            if download.tmpfilename and os.path.isfile(download.tmpfilename):
                try:
                    os.remove(download.tmpfilename)
                except OSError:
                    pass
            download.info.status = 'error'
        download.close()
        if self.queue.exists(download.info.url):
            self.queue.delete(download.info.url)
            if download.canceled:
                asyncio.create_task(self.notifier.canceled(download.info.url))
            else:
                self.done.put(download)
                asyncio.create_task(self.notifier.completed(download.info))
                try:
                    clear_after = int(self.config.CLEAR_COMPLETED_AFTER)
                except ValueError:
                    log.error(f'CLEAR_COMPLETED_AFTER is set to an invalid value "{self.config.CLEAR_COMPLETED_AFTER}", expected an integer number of seconds')
                    clear_after = 0
                if clear_after > 0:
                    task = asyncio.create_task(self.__auto_clear_after_delay(download.info.url, clear_after))
                    task.add_done_callback(lambda t: log.error(f'Auto-clear task failed: {t.exception()}') if not t.cancelled() and t.exception() else None)

    async def __auto_clear_after_delay(self, url, delay_seconds):
        await asyncio.sleep(delay_seconds)
        if self.done.exists(url):
            log.debug(f'Auto-clearing completed download: {url}')
            await self.clear([url])

    def _build_ytdl_options(self, ytdl_options_presets=None, ytdl_options_overrides=None):
        """Merge global options, presets (in order), and per-download overrides."""
        opts = dict(self.config.YTDL_OPTIONS)
        for preset_name in ytdl_options_presets or []:
            opts.update(self.config.YTDL_OPTIONS_PRESETS.get(preset_name, {}))
        opts.update(ytdl_options_overrides or {})
        return opts

    def __extract_info(self, url, ytdl_options_presets=None, ytdl_options_overrides=None):
        from extractors.streamingcommunity import StreamingCommunityExtractor

        if StreamingCommunityExtractor.can_extract(url):
            entry = StreamingCommunityExtractor.extract_info(url)
            if entry:
                return entry

        debug_logging = logging.getLogger().isEnabledFor(logging.DEBUG)
        user_opts = self._build_ytdl_options(ytdl_options_presets, ytdl_options_overrides)
        params = {
            **user_opts,
            'quiet': not debug_logging,
            'verbose': debug_logging,
            'no_color': True,
            'extract_flat': True,
            'ignore_no_formats_error': True,
            'noplaylist': True,
            'paths': {"home": self.config.DOWNLOAD_DIR, "temp": self.config.TEMP_DIR},
        }
        imp = user_opts.get('impersonate')
        if imp is not None:
            params['impersonate'] = yt_dlp.networking.impersonate.ImpersonateTarget.from_str(imp)
        return yt_dlp.YoutubeDL(params=params).extract_info(url, download=False)

    def __calc_download_path(self, download_type, folder):
        base_directory = self.config.AUDIO_DOWNLOAD_DIR if download_type == 'audio' else self.config.DOWNLOAD_DIR
        if folder:
            if not self.config.CUSTOM_DIRS:
                return None, {'status': 'error', 'msg': 'A folder for the download was specified but CUSTOM_DIRS is not true in the configuration.'}
            dldirectory = os.path.realpath(os.path.join(base_directory, folder))
            real_base_directory = os.path.realpath(base_directory)
            if not dldirectory.startswith(real_base_directory):
                return None, {'status': 'error', 'msg': f'Folder "{folder}" must resolve inside the base download directory "{real_base_directory}"'}
            if not os.path.isdir(dldirectory):
                if not self.config.CREATE_CUSTOM_DIRS:
                    return None, {'status': 'error', 'msg': f'Folder "{folder}" for download does not exist inside base directory "{real_base_directory}", and CREATE_CUSTOM_DIRS is not true in the configuration.'}
                os.makedirs(dldirectory, exist_ok=True)
        else:
            dldirectory = base_directory
        return dldirectory, None

    async def __add_download(self, dl, auto_start):
        dldirectory, error_message = self.__calc_download_path(dl.download_type, dl.folder)
        if error_message is not None:
            return error_message
        output = self.config.OUTPUT_TEMPLATE if len(dl.custom_name_prefix) == 0 else f'{dl.custom_name_prefix}.{self.config.OUTPUT_TEMPLATE}'
        output_chapter = self.config.OUTPUT_TEMPLATE_CHAPTER
        entry = getattr(dl, 'entry', None)
        if entry is not None and entry.get('playlist_index') is not None:
            if len(self.config.OUTPUT_TEMPLATE_PLAYLIST):
                output = self.config.OUTPUT_TEMPLATE_PLAYLIST
            sanitized = {k: _sanitize_path_component(v) for k, v in entry.items()}
            output = _resolve_outtmpl_fields(output, sanitized, ('playlist',))
        if entry is not None and entry.get('channel_index') is not None:
            if len(self.config.OUTPUT_TEMPLATE_CHANNEL):
                output = self.config.OUTPUT_TEMPLATE_CHANNEL
            sanitized = {k: _sanitize_path_component(v) for k, v in entry.items()}
            output = _resolve_outtmpl_fields(output, sanitized, ('channel',))
        ytdl_options = self._build_ytdl_options(
            getattr(dl, 'ytdl_options_presets', None),
            getattr(dl, 'ytdl_options_overrides', {}) or {},
        )
        playlist_item_limit = getattr(dl, 'playlist_item_limit', 0)
        if playlist_item_limit > 0:
            log.info(f'playlist limit is set. Processing only first {playlist_item_limit} entries')
            ytdl_options['playlistend'] = playlist_item_limit
        download = Download(dldirectory, self.config.TEMP_DIR, output, output_chapter, dl.quality, dl.format, ytdl_options, dl)
        if auto_start is True:
            self.queue.put(download)
            asyncio.create_task(self.__start_download(download))
        else:
            self.pending.put(download)
        await self.notifier.added(dl)

    async def __add_entry(
        self,
        entry,
        download_type,
        codec,
        format,
        quality,
        folder,
        custom_name_prefix,
        playlist_item_limit,
        auto_start,
        split_by_chapters,
        chapter_template,
        subtitle_language,
        subtitle_mode,
        ytdl_options_presets,
        ytdl_options_overrides,
        already,
        _add_gen=None,
    ):
        if not entry:
            return {'status': 'error', 'msg': "Invalid/empty data was given."}

        error = None
        if "live_status" in entry and "release_timestamp" in entry and entry.get("live_status") == "is_upcoming":
            dt_ts = datetime.fromtimestamp(entry.get("release_timestamp")).strftime('%Y-%m-%d %H:%M:%S %z')
            error = f"Live stream is scheduled to start at {dt_ts}"
        else:
            if "msg" in entry:
                error = entry["msg"]

        etype = entry.get('_type') or 'video'

        if etype.startswith('url'):
            log.debug('Processing as a url')
            return await self.add(
                entry['url'],
                download_type,
                codec,
                format,
                quality,
                folder,
                custom_name_prefix,
                playlist_item_limit,
                auto_start,
                split_by_chapters,
                chapter_template,
                subtitle_language,
                subtitle_mode,
                ytdl_options_presets,
                ytdl_options_overrides,
                already,
                _add_gen,
            )
        elif etype == 'playlist' or etype == 'channel':
            log.debug(f'Processing as a {etype}')
            entries = entry['entries']
            # Convert generator to list if needed (for len() and slicing operations)
            if isinstance(entries, types.GeneratorType):
                entries = list(entries)
            total_entries = len(entries)
            log.info(f'{etype} detected with {total_entries} entries')
            index_digits = len(str(total_entries))
            results = []
            if playlist_item_limit > 0:
                log.info(f'Item limit is set. Processing only first {playlist_item_limit} entries')
                entries = entries[:playlist_item_limit]
            for index, etr in enumerate(entries, start=1):
                if _add_gen is not None and self._add_generation != _add_gen:
                    log.info(f'Playlist add canceled after processing {len(already)} entries')
                    return {'status': 'ok', 'msg': f'Canceled - added {len(already)} items before cancel'}
                if "id" not in etr:
                    etr["id"] = _entry_id(etr)
                etr["_type"] = "video"
                etr[etype] = entry.get("id") or entry.get("channel_id") or entry.get("channel")
                etr[f"{etype}_index"] = '{{0:0{0:d}d}}'.format(index_digits).format(index)
                etr[f"{etype}_count"] = total_entries
                etr[f"{etype}_autonumber"] = index
                # n_entries: standard yt-dlp field for total count (used by template engine)
                # __last_playlist_index: yt-dlp internal field for auto-padding autonumber
                etr["n_entries"] = total_entries
                etr["__last_playlist_index"] = total_entries
                for property in ("id", "title", "uploader", "uploader_id"):
                    if property in entry:
                        etr[f"{etype}_{property}"] = entry[property]
                results.append(
                    await self.__add_entry(
                        etr,
                        download_type,
                        codec,
                        format,
                        quality,
                        folder,
                        custom_name_prefix,
                        playlist_item_limit,
                        auto_start,
                        split_by_chapters,
                        chapter_template,
                        subtitle_language,
                        subtitle_mode,
                        ytdl_options_presets,
                        ytdl_options_overrides,
                        already,
                        _add_gen,
                    )
                )
            if any(res['status'] == 'error' for res in results):
                return {'status': 'error', 'msg': ', '.join(res['msg'] for res in results if res['status'] == 'error' and 'msg' in res)}
            return {'status': 'ok'}
        elif etype == 'video' or (etype.startswith('url') and 'id' in entry and 'title' in entry):
            log.debug('Processing as a video')
            key = entry.get('webpage_url') or entry['url']
            if key in self._canceled_urls:
                log.info(f'Skipping canceled URL: {entry.get("title") or key}')
                return {'status': 'ok'}
            if not self.queue.exists(key):
                dl = DownloadInfo(
                    id=entry['id'],
                    title=entry.get('title') or entry['id'],
                    url=key,
                    quality=quality,
                    download_type=download_type,
                    codec=codec,
                    format=format,
                    folder=folder,
                    custom_name_prefix=custom_name_prefix,
                    error=error,
                    entry=entry,
                    playlist_item_limit=playlist_item_limit,
                    split_by_chapters=split_by_chapters,
                    chapter_template=chapter_template,
                    subtitle_language=subtitle_language,
                    subtitle_mode=subtitle_mode,
                    ytdl_options_presets=ytdl_options_presets,
                    ytdl_options_overrides=ytdl_options_overrides,
                )
                await self.__add_download(dl, auto_start)
            return {'status': 'ok'}
        return {'status': 'error', 'msg': f'Unsupported resource "{etype}"'}

    async def add(
        self,
        url,
        download_type,
        codec,
        format,
        quality,
        folder,
        custom_name_prefix,
        playlist_item_limit,
        auto_start=True,
        split_by_chapters=False,
        chapter_template=None,
        subtitle_language="en",
        subtitle_mode="prefer_manual",
        ytdl_options_presets=None,
        ytdl_options_overrides=None,
        already=None,
        _add_gen=None,
    ):
        if ytdl_options_presets is None:
            ytdl_options_presets = []
        log.info(
            f'adding {url}: {download_type=} {codec=} {format=} {quality=} {already=} {folder=} {custom_name_prefix=} '
            f'{playlist_item_limit=} {auto_start=} {split_by_chapters=} {chapter_template=} '
            f'{subtitle_language=} {subtitle_mode=} {ytdl_options_presets=}'
        )
        if already is None:
            _add_gen = self._add_generation
            self._canceled_urls.clear()
        already = set() if already is None else already
        if url in already:
            log.info('recursion detected, skipping')
            return {'status': 'ok'}
        else:
            already.add(url)
        try:
            entry = await asyncio.get_running_loop().run_in_executor(
                None,
                partial(self.__extract_info, url, ytdl_options_presets, ytdl_options_overrides),
            )
        except yt_dlp.utils.YoutubeDLError as exc:
            return {'status': 'error', 'msg': str(exc)}
        except Exception as exc:
            log.exception(f'Unexpected error while extracting {url}')
            return {'status': 'error', 'msg': str(exc)}
        return await self.__add_entry(
            entry,
            download_type,
            codec,
            format,
            quality,
            folder,
            custom_name_prefix,
            playlist_item_limit,
            auto_start,
            split_by_chapters,
            chapter_template,
            subtitle_language,
            subtitle_mode,
            ytdl_options_presets,
            ytdl_options_overrides,
            already,
            _add_gen,
        )

    async def add_entry(
        self,
        entry,
        download_type,
        codec,
        format,
        quality,
        folder,
        custom_name_prefix,
        playlist_item_limit,
        auto_start=True,
        split_by_chapters=False,
        chapter_template=None,
        subtitle_language="en",
        subtitle_mode="prefer_manual",
        ytdl_options_presets=None,
        ytdl_options_overrides=None,
    ):
        if ytdl_options_presets is None:
            ytdl_options_presets = []
        normalized_entry = copy.deepcopy(entry) if isinstance(entry, dict) else entry
        already = set()
        return await self.__add_entry(
            normalized_entry,
            download_type,
            codec,
            format,
            quality,
            folder,
            custom_name_prefix,
            playlist_item_limit,
            auto_start,
            split_by_chapters,
            chapter_template,
            subtitle_language,
            subtitle_mode,
            ytdl_options_presets,
            ytdl_options_overrides,
            already,
            None,
        )

    async def start_pending(self, ids):
        for id in ids:
            if not self.pending.exists(id):
                log.warning(f'requested start for non-existent download {id}')
                continue
            dl = self.pending.get(id)
            self.queue.put(dl)
            self.pending.delete(id)
            asyncio.create_task(self.__start_download(dl))
        return {'status': 'ok'}

    async def cancel(self, ids):
        for id in ids:
            # Track URL so playlist add loop won't re-queue it
            self._canceled_urls.add(id)
            if self.pending.exists(id):
                self.pending.delete(id)
                await self.notifier.canceled(id)
                continue
            if not self.queue.exists(id):
                log.warning(f'requested cancel for non-existent download {id}')
                continue
            dl = self.queue.get(id)
            if dl.started():
                dl.cancel()
            else:
                dl.canceled = True
                self.queue.delete(id)
                await self.notifier.canceled(id)
        return {'status': 'ok'}

    async def clear(self, ids):
        for id in ids:
            if not self.done.exists(id):
                log.warning(f'requested delete for non-existent download {id}')
                continue
            if self.config.DELETE_FILE_ON_TRASHCAN:
                dl = self.done.get(id)
                try:
                    dldirectory, _ = self.__calc_download_path(dl.info.download_type, dl.info.folder)
                    os.remove(os.path.join(dldirectory, dl.info.filename))
                except Exception as e:
                    log.warning(f'deleting file for download {id} failed with error message {e!r}')
            self.done.delete(id)
            await self.notifier.cleared(id)
        return {'status': 'ok'}

    def get(self):
        return (list((k, v.info) for k, v in self.queue.items()) +
                list((k, v.info) for k, v in self.pending.items()),
                list((k, v.info) for k, v in self.done.items()))
