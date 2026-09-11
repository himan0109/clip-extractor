#!/usr/bin/env python3
"""
Clipout Shorts automation script
Handles video trimming, editing, subtitle generation, and YouTube uploads
"""

import sys
import json
import os
import random
import shutil
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
import time
from pathlib import Path
import subprocess
import tempfile
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# Import database module
try:
    import cv2
except ImportError:
    cv2 = None  # Style 3 face detection unavailable; will fall back to center crop

try:
    from database import check_duplicate, add_video_record, mark_video_uploaded, update_platforms_details
except ImportError:
    # Fallback if database module not found
    def check_duplicate(title, script=None, platform_type=None, platform_id=None):
        return None
    def add_video_record(*args, **kwargs):
        return True
    def mark_video_uploaded(*args, **kwargs):
        return True
    def update_platforms_details(*args, **kwargs):
        return True

# Import Instagram/Facebook upload helpers from automation
try:
    from automation import (
        upload_to_instagram,
        upload_to_facebook_page,
        load_facebook_credentials,
        _split_multi_names,
        _get_row_value,
        _resolve_youtube_channel_ids,
        _resolve_instagram_account_ids,
        _resolve_facebook_page_ids,
    )
except ImportError:
    def upload_to_instagram(*args, **kwargs):
        raise RuntimeError("Instagram upload is not available (automation module missing).")

    def upload_to_facebook_page(*args, **kwargs):
        raise RuntimeError("Facebook upload is not available (automation module missing).")

    def load_facebook_credentials():
        return {}

    def _split_multi_names(value):
        return []

    def _get_row_value(row, keys):
        return ''

    def _resolve_youtube_channel_ids(*args, **kwargs):
        return []

    def _resolve_instagram_account_ids(*args, **kwargs):
        return []

    def _resolve_facebook_page_ids(*args, **kwargs):
        return []

# Configuration
SERVER_URL = "http://localhost:3000"
OUTPUT_DIR = "output"
TEMP_DIR = "uploads"
YOUTUBE_CREDENTIALS_FILE = "youtube_credentials.json"

def convert_ist_to_utc(date_str, time_str):
    """Convert IST (UTC+5:30) date and time to UTC ISO format"""
    try:
        date_str = str(date_str).strip() if date_str is not None else ''
        time_str = str(time_str).strip() if time_str is not None else ''
        
        if pd.isna(date_str) or pd.isna(time_str):
            return None
        
        if date_str.lower() == 'nan' or time_str.lower() == 'nan' or not date_str or not time_str:
            return None
        
        date_parts = date_str.split('-')
        time_parts = time_str.split(':')
        
        if len(date_parts) != 3 or len(time_parts) < 2:
            return None
        
        year, month, day = int(date_parts[0]), int(date_parts[1]), int(date_parts[2])
        hours, minutes = int(time_parts[0]), int(time_parts[1])
        
        dt = datetime(year, month, day, hours, minutes, 0)
        utc_dt = dt - timedelta(hours=5, minutes=30)
        
        return utc_dt.isoformat() + 'Z'
    except Exception as e:
        print(f"Error converting IST to UTC: {e}", file=sys.stderr)
        return None

def parse_timestamp(timestamp_str):
    """Parse timestamp string (HH:MM:SS or MM:SS, with optional milliseconds) to seconds"""
    try:
        # Remove milliseconds if present (format: HH:MM:SS,mmm or MM:SS,mmm)
        timestamp_str = timestamp_str.strip()
        if ',' in timestamp_str:
            timestamp_str = timestamp_str.split(',')[0]
        
        parts = timestamp_str.split(':')
        if len(parts) == 2:
            # MM:SS format
            minutes, seconds = int(parts[0]), int(parts[1])
            return minutes * 60 + seconds
        elif len(parts) == 3:
            # HH:MM:SS format
            hours, minutes, seconds = int(parts[0]), int(parts[1]), int(parts[2])
            return hours * 3600 + minutes * 60 + seconds
        else:
            raise ValueError(f"Invalid timestamp format: {timestamp_str}")
    except Exception as e:
        raise ValueError(f"Error parsing timestamp '{timestamp_str}': {e}")

def format_timestamp(seconds):
    """Convert seconds to HH:MM:SS format"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

def _safe_str(value, default=''):
    """Safely convert a value to a stripped string (handles None/NaN)."""
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    str_value = str(value)
    if str_value in ['None', 'nan', 'NaN', '']:
        return default
    return str_value.strip()

def resolve_source_video_path(row, default_source_video_path, csv_file_path):
    """
    Resolve the source video path for a CSV row.
    - If row contains a video path column, use it.
    - Else fallback to default_source_video_path.
    - Relative paths are resolved relative to the CSV file directory.
    """
    # Common per-row column names (parse_file normalizes headers to lowercase)
    row_path_raw = (
        row.get('source_video_path')
        or row.get('video_path')
        or row.get('video file path')
        or row.get('video_file_path')
        or row.get('file_path')
    )
    # Also try helper (supports more aliases) if present
    try:
        if not row_path_raw:
            row_path_raw = _get_row_value(row, ['video_path', 'source_video_path', 'video file path', 'video', 'file'])
    except Exception:
        pass

    row_path = _safe_str(row_path_raw)
    if row_path:
        # Strip accidental surrounding quotes
        if (row_path.startswith('"') and row_path.endswith('"')) or (row_path.startswith("'") and row_path.endswith("'")):
            row_path = row_path[1:-1].strip()
        candidate = os.path.expanduser(row_path)
        if not os.path.isabs(candidate):
            candidate = os.path.join(os.path.dirname(os.path.abspath(csv_file_path)), candidate)
        candidate = os.path.abspath(candidate)
        if not os.path.exists(candidate):
            raise FileNotFoundError(f"Row video file not found: {candidate}")
        return candidate

    default_path = _safe_str(default_source_video_path)
    if default_path:
        if not os.path.exists(default_path):
            raise FileNotFoundError(f"Default source video file not found: {default_path}")
        return default_path

    raise ValueError(
        "No source video provided. Upload a source video, or add a per-row CSV column "
        "'video_path' / 'source_video_path' with a valid file path."
    )

def trim_video(input_path, output_path, start_time, end_time, progress_callback=None):
    """Trim video using ffmpeg"""
    try:
        if progress_callback:
            progress_callback(f"Trimming video from {format_timestamp(start_time)} to {format_timestamp(end_time)}...")
        
        duration = end_time - start_time
        
        # IMPORTANT:
        # We prefer re-encoding trims (stable timestamps / no frozen first frames),
        # but some source files contain corrupt AAC packets or odd channel configs
        # that make ffmpeg's AAC decoder fail. When that happens we fall back to
        # copying audio (no decode), and only as a last resort drop audio.

        tolerant_input_flags = [
            # Be tolerant to corrupt streams (common in downloaded/merged MP4s)
            "-fflags",
            "+discardcorrupt+genpts",
            "-err_detect",
            "ignore_err",
            "-ignore_unknown",
            # Allow higher decoder error rates before failing
            "-max_error_rate",
            "1.0",
        ]

        def build_base_cmd(seek_after_input: bool) -> list[str]:
            """
            Build ffmpeg command with either:
              - fast seek: -ss/-t before -i (jumps near keyframe, less decode)
              - accurate seek: -ss/-t after -i (more decode, sometimes survives corrupt keyframes)
            """
            cmd = [
                "ffmpeg",
                "-y",
                "-v",
                "warning",
                "-hide_banner",
                *tolerant_input_flags,
            ]
            if not seek_after_input:
                cmd += ["-ss", str(start_time), "-t", str(duration)]
            cmd += ["-i", input_path]
            if seek_after_input:
                cmd += ["-ss", str(start_time), "-t", str(duration)]
            cmd += [
                # Explicit mapping; keep audio optional
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-sn",
                "-dn",
                "-avoid_negative_ts",
                "make_zero",
                # Video encode
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
            ]
            return cmd

        def _output_has_video_stream(path: str) -> bool:
            """
            Returns True if ffprobe can see a video stream in the output.
            Some corrupt segments can lead to "successful" ffmpeg runs that
            produce an audio-only MP4 (no frames encoded for video). We treat
            that as failure and retry with different seek/audio modes.
            """
            try:
                probe = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-select_streams",
                        "v:0",
                        "-show_entries",
                        "stream=codec_type,width,height",
                        "-of",
                        "json",
                        path,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if probe.returncode != 0:
                    return False
                data = json.loads(probe.stdout or "{}")
                streams = data.get("streams") or []
                if not streams:
                    return False
                s0 = streams[0] if isinstance(streams[0], dict) else {}
                if s0.get("codec_type") != "video":
                    return False
                # Width/height may be missing for some streams; just require stream presence
                return True
            except Exception:
                return False

        attempts = [
            # Attempt 1: re-encode audio (preferred when decode works)
            ("aac", ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]),
            # Attempt 2: copy audio (avoids AAC decoding issues)
            ("copy", ["-c:a", "copy"]),
            # Attempt 3: no audio (last resort)
            ("noaudio", ["-an"]),
        ]

        last_stderr = ""
        # Some sources trigger ffmpeg decoder errors like:
        #   "Late SEI is not implemented"
        # which can often be fixed by stream-copy remuxing while stripping SEI NAL units.
        sanitized_input: str | None = None
        effective_input = input_path

        def _maybe_sanitize_h264_sei(stderr_text: str) -> str | None:
            if "Late SEI is not implemented" not in (stderr_text or ""):
                return None
            try:
                safe_out = os.path.join(
                    tempfile.gettempdir(), f"clipout_sanitized_{int(time.time())}.mp4"
                )
                remux_cmd = [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "warning",
                    "-hide_banner",
                    "-ignore_unknown",
                    "-fflags",
                    "+genpts",
                    "-i",
                    input_path,
                    "-map",
                    "0",
                    "-c",
                    "copy",
                    "-bsf:v",
                    "filter_units=remove_types=6",
                    "-movflags",
                    "+faststart",
                    safe_out,
                ]
                proc = subprocess.run(remux_cmd, capture_output=True, text=True)
                if proc.returncode == 0 and os.path.exists(safe_out) and os.path.getsize(safe_out) > 0:
                    return safe_out
            except Exception:
                return None
            return None

        def _run_trim_loops() -> bool:
            nonlocal last_stderr, effective_input
            for seek_after_input in (False, True):
                for mode, audio_args in attempts:
                    cmd = build_base_cmd(seek_after_input=seek_after_input)
                    # Replace input path in the built cmd (last occurrence after "-i")
                    # build_base_cmd always emits ["-i", <path>] once.
                    try:
                        i_idx = cmd.index("-i")
                        cmd[i_idx + 1] = effective_input
                    except Exception:
                        pass
                    cmd = cmd + audio_args + [output_path]
                    proc = subprocess.run(cmd, capture_output=True, text=True)
                    if (
                        proc.returncode == 0
                        and os.path.exists(output_path)
                        and os.path.getsize(output_path) > 0
                        and _output_has_video_stream(output_path)
                    ):
                        if progress_callback:
                            seek_mode = "accurate" if seek_after_input else "fast"
                            progress_callback(f"Trim succeeded (seek={seek_mode}, audio={mode})")
                        return True
                    last_stderr = (proc.stderr or "").strip()
                    # Clean partial file between attempts
                    if os.path.exists(output_path):
                        try:
                            os.remove(output_path)
                        except Exception:
                            pass
            return False

        ok = _run_trim_loops()
        if not ok:
            if sanitized_input is None:
                maybe = _maybe_sanitize_h264_sei(last_stderr)
                if maybe:
                    sanitized_input = maybe
                    effective_input = sanitized_input
                    if progress_callback:
                        progress_callback("Retrying trim after SEI-strip remux…")
                    ok = _run_trim_loops()

        if not ok:
            # Last-resort fallback: stream-copy trim (no decode). This can salvage
            # clips where decoding yields 0 frames ("Output file is empty").
            if progress_callback:
                progress_callback("Trim re-encode failed; trying stream-copy fallback…")
            copy_cmd = [
                "ffmpeg",
                "-y",
                "-v",
                "warning",
                "-hide_banner",
                "-ignore_unknown",
                "-fflags",
                "+genpts",
                "-ss",
                str(start_time),
                "-t",
                str(duration),
                "-i",
                effective_input,
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-sn",
                "-dn",
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
                "-movflags",
                "+faststart",
                output_path,
            ]
            proc = subprocess.run(copy_cmd, capture_output=True, text=True)
            if (
                proc.returncode == 0
                and os.path.exists(output_path)
                and os.path.getsize(output_path) > 0
                and _output_has_video_stream(output_path)
            ):
                if progress_callback:
                    progress_callback("Trim succeeded (stream-copy fallback)")
            else:
                last_stderr = (proc.stderr or last_stderr or "").strip()
                raise subprocess.CalledProcessError(1, ["ffmpeg"], output="", stderr=last_stderr)
        
        if not os.path.exists(output_path):
            raise Exception(f"Trimmed video file was not created: {output_path}")
        
        if progress_callback:
            progress_callback("Video trimmed successfully")
        
        return output_path
    except subprocess.CalledProcessError as e:
        error_msg = f"FFmpeg error: {getattr(e, 'stderr', None) or str(e)}"
        print(error_msg, file=sys.stderr)
        raise Exception(error_msg)
    except Exception as e:
        print(f"Error trimming video: {e}", file=sys.stderr)
        raise

def ensure_portrait_9_16(video_path, output_path, progress_callback=None):
    """Ensure video is in 9:16 portrait format for YouTube Shorts"""
    try:
        if progress_callback:
            progress_callback("Checking video aspect ratio...")

        # Quick validation: if there is no video stream, fail with a clear message.
        try:
            vcheck = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "default=nw=1:nk=1",
                    video_path,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if vcheck.returncode != 0 or "video" not in (vcheck.stdout or ""):
                raise RuntimeError("No video stream found in input clip")
        except Exception as ve:
            raise Exception(f"Cannot convert to 9:16: {ve}")
        
        # Get video dimensions using ffprobe (best-effort; some corrupt/odd clips return empty streams)
        width = None
        height = None
        try:
            probe_cmd = [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                video_path,
            ]
            result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
            probe_data = json.loads(result.stdout or "{}")
            streams = probe_data.get("streams") or []
            if streams and isinstance(streams[0], dict):
                if "width" in streams[0] and "height" in streams[0]:
                    width = int(streams[0]["width"])
                    height = int(streams[0]["height"])
        except Exception as probe_err:
            if progress_callback:
                progress_callback(f"Could not probe dimensions; proceeding with conversion ({probe_err})")

        target_ratio = 9 / 16  # Portrait 9:16 (width:height)

        # If we were able to probe dimensions, we can skip conversion when already 9:16
        if width and height and height > 0:
            current_ratio = width / height
            # Check if already in 9:16 (allow small tolerance of 0.02)
            if abs(current_ratio - target_ratio) < 0.02:
                if progress_callback:
                    progress_callback("Video already in 9:16 format")
                # Just copy the file
                import shutil
                shutil.copy2(video_path, output_path)
                return output_path
        
        # For 9:16 portrait format (YouTube Shorts)
        # Target resolution: 1080x1920 (or scale proportionally)
        # Strategy: Scale to fit 9:16, then crop to exact dimensions
        
        # Use 1080x1920 as target (common YouTube Shorts resolution)
        target_width = 1080
        target_height = 1920
        
        # Determine if we should scale based on width or height
        # Scale to fill the 9:16 frame, then crop excess
        if progress_callback:
            progress_callback(f"Converting to 9:16 portrait ({target_width}x{target_height})...")
        
        # Convert to 9:16 portrait format
        # Scale to cover the entire 9:16 frame, then crop to exact dimensions
        # This ensures the video fills the frame properly
        cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "warning",
            "-hide_banner",
            "-fflags",
            "+discardcorrupt+genpts",
            "-err_detect",
            "ignore_err",
            "-ignore_unknown",
            "-max_error_rate",
            "1.0",
            "-i",
            video_path,
            # Explicit mapping; keep audio optional
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-sn",
            "-dn",
            "-vf",
            f"scale={target_width}:{target_height}:force_original_aspect_ratio=increase,crop={target_width}:{target_height}",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            # Prefer copying audio (avoid decode), fallback happens in trim step if needed
            "-c:a",
            "copy",
            output_path,
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        
        if not os.path.exists(output_path):
            raise Exception(f"Converted video file was not created: {output_path}")
        
        if progress_callback:
            progress_callback("Video converted to 9:16 portrait format")
        
        return output_path
    except subprocess.CalledProcessError as e:
        error_msg = f"FFmpeg error: {e.stderr}"
        print(error_msg, file=sys.stderr)
        raise Exception(error_msg)
    except Exception as e:
        print(f"Error converting video to 9:16: {e}", file=sys.stderr)
        raise


def _rects_overlap(a, b):
    """True if two (x, y, w, h) rects overlap."""
    ax2, ay2 = a[0] + a[2], a[1] + a[3]
    bx2, by2 = b[0] + b[2], b[1] + b[3]
    return not (ax2 <= b[0] or bx2 <= a[0] or ay2 <= b[1] or by2 <= a[1])


def _merge_overlapping_rects(rects):
    """Merge overlapping (x, y, w, h) rectangles; return list of distinct regions."""
    if not rects:
        return []
    rects = [tuple(r) for r in rects]
    used = [False] * len(rects)
    out = []
    for i in range(len(rects)):
        if used[i]:
            continue
        group = [rects[i]]
        used[i] = True
        added = True
        while added:
            added = False
            for r in group:
                for j in range(len(rects)):
                    if used[j]:
                        continue
                    if _rects_overlap(r, rects[j]):
                        group.append(rects[j])
                        used[j] = True
                        added = True
        xa = min(r[0] for r in group)
        ya = min(r[1] for r in group)
        xb = max(r[0] + r[2] for r in group)
        yb = max(r[1] + r[3] for r in group)
        out.append((xa, ya, xb - xa, yb - ya))
    return out


def _rects_to_list(rects):
    """
    Normalize OpenCV detectMultiScale output to a Python list of (x,y,w,h) tuples.
    OpenCV may return: (), tuple of tuples, numpy array, etc.
    """
    if rects is None:
        return []
    if hasattr(rects, "tolist"):
        rects = rects.tolist()
    # Now rects could be [] or () or list of lists/tuples
    if not rects:
        return []
    out = []
    for r in rects:
        if r is None:
            continue
        if isinstance(r, (list, tuple)) and len(r) == 4:
            out.append((int(r[0]), int(r[1]), int(r[2]), int(r[3])))
    return out


def _count_faces_in_video(video_path):
    """
    Sample several frames from the video and run face detection. Returns the maximum number
    of distinct faces detected in any single frame. If >= 2, Style 3 will split (right
    half top, left half bottom). Uses frontal + profile cascades and merges overlapping
    detections so the same person is not counted twice. If OpenCV is unavailable or
    detection fails, returns 0 (Style 3 will use center crop).
    """
    if not cv2:
        return 0
    try:
        data_dir = os.path.join(os.path.dirname(cv2.__file__), "data")
        frontal_path = os.path.join(data_dir, "haarcascade_frontalface_default.xml")
        profile_path = os.path.join(data_dir, "haarcascade_profileface.xml")
        if not os.path.exists(frontal_path):
            print("[clipout] Style 3: frontal cascade not found, face count = 0", file=sys.stderr)
            return 0
        frontal_cascade = cv2.CascadeClassifier(frontal_path)
        profile_cascade = cv2.CascadeClassifier(profile_path) if os.path.exists(profile_path) else None

        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1",
                video_path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        duration_sec = 1.0
        if probe.returncode == 0 and probe.stdout and probe.stdout.strip():
            try:
                duration_sec = max(0.5, float(probe.stdout.strip()))
            except ValueError:
                pass
        fractions = [0.2, 0.4, 0.5, 0.6, 0.8]
        max_faces = 0
        for frac in fractions:
            t = duration_sec * frac
            fd, frame_path = tempfile.mkstemp(suffix=".jpg")
            os.close(fd)
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-v", "warning", "-hide_banner",
                        "-i", video_path,
                        "-ss", str(t),
                        "-frames:v", "1",
                        "-vf", "scale=640:-1",
                        "-q:v", "2",
                        frame_path,
                    ],
                    capture_output=True,
                    check=True,
                )
                if not os.path.exists(frame_path) or os.path.getsize(frame_path) == 0:
                    continue
                img = cv2.imread(frame_path)
                if img is None:
                    continue
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                all_rects = []
                frontal = frontal_cascade.detectMultiScale(
                    gray, scaleFactor=1.08, minNeighbors=3, minSize=(22, 22)
                )
                all_rects.extend(_rects_to_list(frontal))
                if profile_cascade is not None:
                    profile_left = profile_cascade.detectMultiScale(
                        gray, scaleFactor=1.08, minNeighbors=3, minSize=(22, 22)
                    )
                    all_rects.extend(_rects_to_list(profile_left))
                    gray_flip = cv2.flip(gray, 1)
                    profile_right = profile_cascade.detectMultiScale(
                        gray_flip, scaleFactor=1.08, minNeighbors=3, minSize=(22, 22)
                    )
                    for (x, y, w, h) in _rects_to_list(profile_right):
                        all_rects.append((gray.shape[1] - x - w, y, w, h))
                distinct = _merge_overlapping_rects(all_rects)
                w_img = gray.shape[1]
                h_img = gray.shape[0]
                img_area = max(1, w_img * h_img)

                # Filter out tiny/implausible detections (common false positives on text/logos/mics).
                # Require at least ~1% of the frame area and a minimum size.
                filtered = []
                for (x, y, w, h) in distinct:
                    area = int(w) * int(h)
                    if w < 22 or h < 22:
                        continue
                    if area < int(0.01 * img_area):
                        continue
                    filtered.append((x, y, w, h))

                n = len(filtered)

                # Strong signal: at least 2 valid detections far apart horizontally.
                if n >= 2:
                    centers = sorted([(x + w / 2.0, y + h / 2.0, w, h) for (x, y, w, h) in filtered], key=lambda t: t[0])
                    left_cx = centers[0][0]
                    right_cx = centers[-1][0]
                    if (right_cx - left_cx) >= (0.35 * w_img):
                        print(f"[clipout] Style 3: detected {n} valid faces far apart, using split layout", file=sys.stderr)
                        return n

                # Fallback heuristic: need at least 2 valid detections AND one clearly on each side.
                left_seen = False
                right_seen = False
                for (x, y, w, h) in filtered:
                    cx = x + (w / 2.0)
                    if cx < (w_img * 0.45):
                        left_seen = True
                    if cx > (w_img * 0.55):
                        right_seen = True
                if n >= 2 and left_seen and right_seen:
                    print("[clipout] Style 3: two valid faces on both sides, using split layout", file=sys.stderr)
                    return 2

                max_faces = max(max_faces, n)
            finally:
                try:
                    os.unlink(frame_path)
                except OSError:
                    pass
        print(f"[clipout] Style 3: detected {max_faces} face(s), using center crop", file=sys.stderr)
        return max_faces
    except Exception as e:
        print(f"[clipout] Style 3: face detection error: {e}", file=sys.stderr)
        return 0


def _probe_video_dimensions(video_path):
    """Return (width, height) for the first video stream, or (None, None)."""
    try:
        probe_cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            video_path,
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        probe_data = json.loads(result.stdout or "{}")
        streams = probe_data.get("streams") or []
        if streams and isinstance(streams[0], dict):
            w = streams[0].get("width")
            h = streams[0].get("height")
            if w and h:
                return int(w), int(h)
    except Exception:
        pass
    return None, None


def _detect_best_face_center_x_norm(video_path):
    """
    Detect the most prominent face and return its center-x as a normalized value in [0,1].
    Uses the same cascades as Style 3. Returns None if no face is found.
    """
    if not cv2:
        return None
    try:
        data_dir = os.path.join(os.path.dirname(cv2.__file__), "data")
        frontal_path = os.path.join(data_dir, "haarcascade_frontalface_default.xml")
        profile_path = os.path.join(data_dir, "haarcascade_profileface.xml")
        if not os.path.exists(frontal_path):
            return None
        frontal_cascade = cv2.CascadeClassifier(frontal_path)
        profile_cascade = cv2.CascadeClassifier(profile_path) if os.path.exists(profile_path) else None

        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1",
                video_path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        duration_sec = 1.0
        if probe.returncode == 0 and probe.stdout and probe.stdout.strip():
            try:
                duration_sec = max(0.5, float(probe.stdout.strip()))
            except ValueError:
                pass

        fractions = [0.25, 0.5, 0.75]
        best = None  # (area, cx_norm)
        for frac in fractions:
            t = duration_sec * frac
            fd, frame_path = tempfile.mkstemp(suffix=".jpg")
            os.close(fd)
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-v", "warning", "-hide_banner",
                        "-i", video_path,
                        "-ss", str(t),
                        "-frames:v", "1",
                        "-vf", "scale=640:-1",
                        "-q:v", "2",
                        frame_path,
                    ],
                    capture_output=True,
                    check=True,
                )
                img = cv2.imread(frame_path)
                if img is None:
                    continue
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                all_rects = []
                frontal = frontal_cascade.detectMultiScale(
                    gray, scaleFactor=1.08, minNeighbors=3, minSize=(22, 22)
                )
                all_rects.extend(_rects_to_list(frontal))
                if profile_cascade is not None:
                    prof = profile_cascade.detectMultiScale(
                        gray, scaleFactor=1.08, minNeighbors=3, minSize=(22, 22)
                    )
                    all_rects.extend(_rects_to_list(prof))
                    gray_flip = cv2.flip(gray, 1)
                    prof_r = profile_cascade.detectMultiScale(
                        gray_flip, scaleFactor=1.08, minNeighbors=3, minSize=(22, 22)
                    )
                    for (x, y, w, h) in _rects_to_list(prof_r):
                        all_rects.append((gray.shape[1] - x - w, y, w, h))
                distinct = _merge_overlapping_rects(all_rects)
                if not distinct:
                    continue
                w_img = gray.shape[1]
                for (x, y, w, h) in distinct:
                    area = int(w) * int(h)
                    cx = x + (w / 2.0)
                    cx_norm = float(cx) / float(w_img) if w_img else 0.5
                    if best is None or area > best[0]:
                        best = (area, cx_norm)
            finally:
                try:
                    os.unlink(frame_path)
                except OSError:
                    pass
        return best[1] if best else None
    except Exception:
        return None


def _pick_background_from_folder(picture_folder, project_root=None):
    """
    Pick a random portrait image or video from the picture folder.
    Returns the absolute path to the file, or None if none found.
    When picture_folder is relative, it is resolved against project_root (preferred)
    or current working directory.
    """
    if not picture_folder or not picture_folder.strip():
        return None
    folder = picture_folder.strip()
    if not os.path.isabs(folder):
        root = project_root or os.getcwd()
        folder = os.path.join(root, folder)
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        return None
    exts = ('.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov', '.webm')
    candidates = []
    for f in os.listdir(folder):
        fp = os.path.join(folder, f)
        if os.path.isfile(fp) and f.lower().endswith(exts):
            candidates.append(fp)
    if not candidates:
        return None
    return random.choice(candidates)


def _build_watermark_drawtext(edit_settings: dict) -> str | None:
    """
    Return an ffmpeg drawtext filter string for the watermark, or None if not configured.
    Mirrors the logic in editor.py so we can apply the watermark inline in portrait conversion.
    """
    if not edit_settings:
        return None
    wm_enabled = edit_settings.get('enable_watermark') in ('on', True)
    wm_text = str(edit_settings.get('watermark_text') or '').strip()
    if not wm_enabled or not wm_text:
        return None

    def _escape(text):
        t = str(text)
        t = t.replace("\\", "\\\\")
        t = t.replace(":", "\\:")
        t = t.replace("%", "\\%")
        t = t.replace("'", "\\'")
        return t

    font_key = (edit_settings.get('watermark_font') or 'fire-sans').strip().lower()
    fonts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')
    font_map = {
        'fire-sans': os.path.join(fonts_dir, 'Fire_Sans', 'fira-sans.ultra.ttf'),
        'avalon':    os.path.join(fonts_dir, 'Avalon', 'Avalon_Bold.ttf'),
        'dejavu-sans': os.path.join(fonts_dir, 'DejaVu_Sans', 'DejaVuSans-Bold.ttf'),
    }
    fontfile = font_map.get(font_key, font_map['fire-sans'])

    size_key = (edit_settings.get('watermark_size') or 'medium').strip().lower()
    fontsize = {'small': 18, 'medium': 28, 'large': 38}.get(size_key, 28)

    opacity_raw = str(edit_settings.get('watermark_opacity') or '70')
    opacity = max(0.0, min(int(''.join(c for c in opacity_raw if c.isdigit()) or '70'), 100)) / 100.0

    pos_key = (edit_settings.get('watermark_position') or 'bottom-right').strip().lower()
    bottom_margin = '180'
    top_margin = '20'
    pos_map = {
        'top-left':    (top_margin, top_margin),
        'top-right':   (f'w-text_w-{top_margin}', top_margin),
        'bottom-left': (top_margin, f'h-text_h-{bottom_margin}'),
        'bottom-right':(f'w-text_w-{top_margin}', f'h-text_h-{bottom_margin}'),
        'top-middle':  ('(w-text_w)/2', top_margin),
        'bottom-middle':('(w-text_w)/2', f'h-text_h-{bottom_margin}'),
    }
    x, y = pos_map.get(pos_key, pos_map['bottom-right'])

    safe_text = _escape(wm_text)
    base = (
        f"drawtext=fontfile='{fontfile}':text='{safe_text}':"
        f"fontsize={fontsize}:fontcolor=white@{opacity}:"
        f"shadowcolor=black@0.75:shadowx=2:shadowy=2:"
    )
    wm_box = str(edit_settings.get('watermark_box', 'on')).lower()
    if wm_box == 'off':
        return base + f"x={x}:y={y}"
    else:
        return base + f"box=1:boxcolor=black@0.35:boxborderw=10:x={x}:y={y}"


def _only_watermark_edit(edit_settings: dict) -> bool:
    """Return True when the only active edit operation is the watermark (no autocut, transitions, etc.)."""
    if not edit_settings:
        return True
    non_wm_ops = [
        edit_settings.get('enable_autocut') in ('on', True),
        bool(edit_settings.get('transition_style', '').strip()),
        edit_settings.get('enable_sfx') == 'on',
        edit_settings.get('loudnorm') in ('on', True),
        bool(edit_settings.get('zoom_pattern', '').strip()),
        bool(edit_settings.get('broll_frequency')),
        edit_settings.get('enable_music') == 'on',
    ]
    return not any(non_wm_ops)


def ensure_portrait_9_16_style2(video_path, output_path, picture_folder, project_root=None, background_path=None, watermark_drawtext=None, progress_callback=None):
    """
    Style 2: Place the uncropped video in the center of a 1080x1920 portrait frame,
    with a portrait image or video from picture_folder as the background.
    """
    try:
        if progress_callback:
            progress_callback("Applying Style 2: uncropped video in portrait frame...")

        # Resolve folder for error message and picking
        resolved_folder = picture_folder.strip()
        if not os.path.isabs(resolved_folder):
            root = project_root or os.getcwd()
            resolved_folder = os.path.abspath(os.path.join(root, resolved_folder))
        else:
            resolved_folder = os.path.abspath(resolved_folder)
        bg_path = None
        if background_path and isinstance(background_path, str) and background_path.strip():
            # background_path can be a filename (from pictures/) or an absolute path.
            bg_candidate = background_path.strip()
            if not os.path.isabs(bg_candidate):
                # Resolve relative to picture folder (resolved against project_root)
                base_folder = picture_folder.strip() if isinstance(picture_folder, str) else ''
                if not os.path.isabs(base_folder):
                    root = project_root or os.getcwd()
                    base_folder = os.path.abspath(os.path.join(root, base_folder))
                else:
                    base_folder = os.path.abspath(base_folder)
                bg_candidate = os.path.abspath(os.path.join(base_folder, bg_candidate))
            else:
                bg_candidate = os.path.abspath(bg_candidate)
            if os.path.exists(bg_candidate) and os.path.isfile(bg_candidate):
                bg_path = bg_candidate
            else:
                raise FileNotFoundError(f"Selected background not found: {bg_candidate}")

        if not bg_path:
            bg_path = _pick_background_from_folder(picture_folder, project_root)
        if not bg_path:
            raise FileNotFoundError(
                f"No portrait images (.jpg, .png, .webp) or videos (.mp4, .mov, .webm) "
                f"found in picture folder: {resolved_folder} (resolved from: {picture_folder!r})"
            )

        is_image = bg_path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
        target_w, target_h = 1080, 1920
        # Background: scale to fill 1080x1920 (cover)
        # Foreground: video scaled to fit (contain), centered
        # [0:v] = background, [1:v] = main video
        filter_complex = (
            f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h}[bg];"
            f"[1:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1[overlaid];"
            f"[overlaid]{watermark_drawtext}[outv]"
            if watermark_drawtext else
            f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h}[bg];"
            f"[1:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1[outv]"
        )

        if is_image:
            cmd = [
                "ffmpeg", "-y", "-v", "warning", "-hide_banner",
                "-fflags", "+discardcorrupt+genpts", "-err_detect", "ignore_err", "-ignore_unknown",
                "-loop", "1", "-i", bg_path,
                "-i", video_path,
                "-filter_complex", filter_complex,
                "-map", "[outv]", "-map", "1:a?", "-c:v", "libx264", "-preset", "medium",
                "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                "-c:a", "copy", "-shortest",
                output_path,
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-v", "warning", "-hide_banner",
                "-fflags", "+discardcorrupt+genpts", "-err_detect", "ignore_err", "-ignore_unknown",
                "-stream_loop", "-1", "-i", bg_path,
                "-i", video_path,
                "-filter_complex", filter_complex,
                "-map", "[outv]", "-map", "1:a?", "-c:v", "libx264", "-preset", "medium",
                "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                "-c:a", "copy", "-shortest",
                output_path,
            ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise Exception(f"Style 2 output was not created: {output_path}")

        if progress_callback:
            progress_callback("Style 2 applied: uncropped video in portrait frame")
        return output_path
    except subprocess.CalledProcessError as e:
        error_msg = f"FFmpeg error: {getattr(e, 'stderr', '') or str(e)}"
        print(error_msg, file=sys.stderr)
        raise Exception(error_msg)
    except Exception as e:
        print(f"Error applying Style 2: {e}", file=sys.stderr)
        raise


def ensure_portrait_9_16_style3(video_path, output_path, progress_callback=None):
    """
    Style 3: If two or more faces are detected, split the frame down the middle:
    right half on top, left half on bottom (9:16 portrait). Otherwise center-crop to 9:16 portrait.
    Output is always 1080x1920 portrait for reels/shorts.
    """
    try:
        if progress_callback:
            progress_callback("Checking for faces (Style 3)...")
        face_count = _count_faces_in_video(video_path)
        target_w, target_h = 1080, 1920
        half_h = target_h // 2  # 960

        if face_count >= 2:
            if progress_callback:
                progress_callback("Two faces detected: splitting frame (right top, left bottom)...")
            # Right half → top, left half → bottom; each scaled to 1080x960 then vstack
            filter_complex = (
                "[0:v]split=2[A][B];"
                "[A]crop=iw/2:ih:iw/2:0,scale={}:{}[top];"
                "[B]crop=iw/2:ih:0:0,scale={}:{}[bottom];"
                "[top][bottom]vstack=inputs=2[vout]"
            ).format(target_w, half_h, target_w, half_h)
            cmd = [
                "ffmpeg", "-y", "-v", "warning", "-hide_banner",
                "-fflags", "+discardcorrupt+genpts", "-err_detect", "ignore_err", "-ignore_unknown",
                "-i", video_path,
                "-filter_complex", filter_complex,
                "-map", "[vout]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "medium", "-crf", "23",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                "-c:a", "copy",
                output_path,
            ]
        else:
            if progress_callback:
                progress_callback("Single or no face: face-aware crop to 9:16 portrait...")
            # Face-aware crop: shift crop window so face is centered (when possible).
            # IMPORTANT: if the source is already close to 9:16, scaling to exactly 1080 wide
            # leaves no horizontal room to shift. So we scale a bit wider first, then crop.
            cx_norm = _detect_best_face_center_x_norm(video_path)
            if cx_norm is None:
                cx_norm = 0.5
            # Clamp to avoid extreme crops that can cut off face
            cx_norm = max(0.2, min(0.8, float(cx_norm)))
            # Scale slightly wider to allow horizontal panning even for portrait-ish sources
            scale_w = 1200
            # NOTE: commas inside expressions must be escaped for ffmpeg filter args.
            x_expr = f"min(max(iw*{cx_norm}-({target_w}/2)\\,0)\\,iw-{target_w})"
            y_expr = f"max(0\\,(ih-{target_h})/2)"
            vf = (
                f"scale={scale_w}:{target_h}:force_original_aspect_ratio=increase,"
                f"crop={target_w}:{target_h}:{x_expr}:{y_expr}"
            )
            print(f"[clipout] Style 3: face-aware crop cx_norm={cx_norm:.3f}", file=sys.stderr)

            cmd = [
                "ffmpeg", "-y", "-v", "warning", "-hide_banner",
                "-fflags", "+discardcorrupt+genpts", "-err_detect", "ignore_err", "-ignore_unknown",
                "-i", video_path,
                "-map", "0:v:0", "-map", "0:a?",
                "-vf", vf,
                "-c:v", "libx264", "-preset", "medium", "-crf", "23",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                "-c:a", "copy",
                output_path,
            ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise Exception(f"Style 3 output was not created: {output_path}")
        if progress_callback:
            progress_callback("Style 3 applied: portrait 9:16")
        return output_path
    except subprocess.CalledProcessError as e:
        error_msg = getattr(e, "stderr", "") or str(e)
        print(f"FFmpeg error: {error_msg}", file=sys.stderr)
        raise Exception(error_msg)
    except Exception as e:
        print(f"Error applying Style 3: {e}", file=sys.stderr)
        raise


def edit_video(video_path, edit_settings, progress_callback=None):
    """Edit video using /generate-edit endpoint"""
    try:
        if progress_callback:
            progress_callback("Editing video...")
        
        # Read video file
        with open(video_path, 'rb') as f:
            video_content = f.read()
        
        # Prepare form data
        files = {'video': (os.path.basename(video_path), video_content, 'video/mp4')}
        data = {}
        
        # Add edit settings
        if edit_settings.get('enable_autocut') in ('on', True):
            data['enable_autocut'] = 'on'
            if edit_settings.get('gap_threshold'):
                data['gap_threshold'] = edit_settings.get('gap_threshold')
        
        if edit_settings.get('transition_style'):
            data['transition_style'] = edit_settings.get('transition_style')
        
        if edit_settings.get('transition_sfx'):
            data['transition_sfx'] = edit_settings.get('transition_sfx')
        
        if edit_settings.get('enable_sfx') == 'on':
            data['enable_sfx'] = 'on'
        
        if edit_settings.get('loudnorm') in ('on', True):
            data['loudnorm'] = 'on'
        
        if edit_settings.get('zoom_pattern'):
            data['zoom_pattern'] = edit_settings.get('zoom_pattern')
        
        if edit_settings.get('zoom_percent'):
            data['zoom_percent'] = edit_settings.get('zoom_percent')
        
        # Watermark settings (same as Video Generator fields) — always send when enabled so /generate-edit applies it
        edit_wm_enabled = edit_settings.get('enable_watermark') in ('on', True)
        wm_text = str(edit_settings.get('watermark_text') or '').strip()
        if edit_wm_enabled and wm_text:
            data['enable_watermark'] = 'on'
            data['watermark_text'] = wm_text
            data['watermark_position'] = str(edit_settings.get('watermark_position') or 'bottom-right').strip()
            data['watermark_font'] = str(edit_settings.get('watermark_font') or 'fire-sans')
            data['watermark_size'] = str(edit_settings.get('watermark_size') or 'medium')
            # pass box preference through; default to 'on' if missing
            wm_box = edit_settings.get('watermark_box', 'on')
            data['watermark_box'] = 'off' if str(wm_box).lower() in ('off', 'false', '0') else 'on'
            data['watermark_opacity'] = str(edit_settings.get('watermark_opacity') or '70')
            if progress_callback:
                progress_callback(f"Applying watermark: {wm_text[:30]}...")

        if edit_settings.get('enable_music') == 'on':
            data['enable_music'] = 'on'
            if edit_settings.get('music_file'):
                # Music file would need to be uploaded separately
                pass
        
        if edit_settings.get('broll_frequency'):
            data['broll_frequency'] = edit_settings.get('broll_frequency')
        
        if edit_settings.get('broll_duration'):
            data['broll_duration'] = edit_settings.get('broll_duration')
        
        response = requests.post(f"{SERVER_URL}/generate-edit", 
                               files=files, data=data, timeout=600,
                               headers={'X-Automation-Request': 'true'})
        response.raise_for_status()
        
        # Save edited video
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        video_name = os.path.basename(video_path)
        output_path = os.path.join(OUTPUT_DIR, f"{os.path.splitext(video_name)[0]}_edited.mp4")
        
        with open(output_path, 'wb') as f:
            f.write(response.content)
        
        if progress_callback:
            progress_callback("Video edited successfully")
        
        return output_path
    except Exception as e:
        print(f"Error editing video: {e}", file=sys.stderr)
        raise

def generate_subtitles(video_path, subtitle_settings, progress_callback=None):
    """Generate subtitles for video"""
    try:
        if progress_callback:
            progress_callback("Generating subtitles...")
        
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")
        
        with open(video_path, 'rb') as f:
            video_content = f.read()
        
        files = {'video': (os.path.basename(video_path), video_content, 'video/mp4')}
        data = {
            'max_words': str(subtitle_settings.get('max_words', 1)),
            'model_name': subtitle_settings.get('model_name', 'small'),
            'font_name': subtitle_settings.get('font_name', 'Fira Sans Ultra'),
            'font_size': str(subtitle_settings.get('font_size', 15)),
            'alignment': str(subtitle_settings.get('alignment', 2)),
            'margin_v': str(subtitle_settings.get('margin_v', 75)),
            'outline': str(subtitle_settings.get('outline', 0)),
            'shadow': str(subtitle_settings.get('shadow', 2)),
            'primary_color_hex': subtitle_settings.get('primary_color_hex', '&H007DD1F7&')
        }
        
        response = requests.post(f"{SERVER_URL}/generate-subtitles-gui", 
                               files=files, data=data, timeout=600,
                               headers={'X-Automation-Request': 'true'})
        response.raise_for_status()
        
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        video_name = os.path.basename(video_path)
        output_path = os.path.join(OUTPUT_DIR, f"{os.path.splitext(video_name)[0]}_subtitled.mp4")
        
        with open(output_path, 'wb') as f:
            f.write(response.content)
        
        if progress_callback:
            progress_callback("Subtitles generated successfully")
        
        return output_path
    except Exception as e:
        print(f"Error generating subtitles: {e}", file=sys.stderr)
        raise

def generate_hf_captions(video_path, hf_settings, progress_callback=None):
    """Burn HyperFrames captions onto video via /hf-captions-gui"""
    try:
        if progress_callback:
            progress_callback("Generating HyperFrames captions…")

        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        with open(video_path, 'rb') as f:
            video_content = f.read()

        tpl = hf_settings.get('hf_template', 'kinetic')
        files = {'video': (os.path.basename(video_path), video_content, 'video/mp4')}
        data = {
            'max_words':    str(hf_settings.get('max_words', 6)),
            'model_name':   hf_settings.get('model_name', 'base.en'),
            'hf_template':  tpl,
            'animation':    hf_settings.get('animation', 'bounce'),
        }
        if tpl == 'single':
            data.update({
                'font_name':       hf_settings.get('font_name', 'Fira Sans Ultra'),
                'font_file':       hf_settings.get('font_file', ''),
                'font_size':       str(hf_settings.get('font_size', 52)),
                'text_color':      hf_settings.get('text_color', '#ffffff'),
                'box_color':       hf_settings.get('box_color', '#000000'),
                'box_opacity':     '100',
                'border_radius':   str(hf_settings.get('border_radius', 20)),
                'pad_x':           str(hf_settings.get('pad_x', 0)),
                'pad_y':           str(hf_settings.get('pad_y', 0)),
                'hf_align':        hf_settings.get('hf_align', 'center'),
                'hf_bottom_margin': str(hf_settings.get('hf_bottom_margin', 150)),
                'hf_sfx':          str(hf_settings.get('hf_sfx', '0')),
                'hf_sfx_type':     hf_settings.get('hf_sfx_type', 'click'),
            })
        elif tpl == 'kinetic':
            data.update({
                'kinetic_context_font':   hf_settings.get('kinetic_context_font', 'Avalon Bold'),
                'highlight_color':        hf_settings.get('kinetic_context_color', '#ffffff'),
                'kinetic_text_color':     hf_settings.get('kinetic_emphasis_color', '#FFD700'),
                'kinetic_context_font':   hf_settings.get('kinetic_context_font', 'Avalon Bold'),
                'kinetic_emphasis_font':  hf_settings.get('kinetic_emphasis_font', 'League Gothic'),
                'show_bg':                str(hf_settings.get('show_bg', '0')),
                'enable_icons':           str(hf_settings.get('enable_icons', '0')),
                'show_shadow':            str(hf_settings.get('show_shadow', '0')),
            })

        try:
            response = requests.post(
                f"{SERVER_URL}/hf-captions-gui",
                files=files, data=data, timeout=900,
                headers={'X-Automation-Request': 'true'}
            )
            response.raise_for_status()

            os.makedirs(OUTPUT_DIR, exist_ok=True)
            video_name = os.path.basename(video_path)
            output_path = os.path.join(OUTPUT_DIR, f"{os.path.splitext(video_name)[0]}_hf.mp4")
            with open(output_path, 'wb') as f:
                f.write(response.content)

            if progress_callback:
                progress_callback("HyperFrames captions generated successfully")
            return output_path
        except requests.exceptions.ConnectionError:
            print("[clipout] Server unreachable, falling back to direct subgen.py call", file=sys.stderr)
            return _generate_hf_captions_direct(video_path, hf_settings, progress_callback)
    except Exception as e:
        print(f"Error generating HF captions: {e}", file=sys.stderr)
        raise


def _generate_hf_captions_direct(video_path, hf_settings, progress_callback=None):
    """Fallback: call subgen.py directly when server is unavailable"""
    import subprocess
    tpl = hf_settings.get('hf_template', 'kinetic')
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    video_name = os.path.basename(video_path)
    output_path = os.path.join(OUTPUT_DIR, f"{os.path.splitext(video_name)[0]}_hf.mp4")

    args = [
        sys.executable, '-u', 'subgen.py',
        video_path,
        str(hf_settings.get('max_words', 6)),
        hf_settings.get('model_name', 'base.en'),
        '--hyperframes',
        '--output', output_path,
        '--hf-template', tpl,
        '--hf-animation', hf_settings.get('animation', 'bounce'),
        '--hf-border-radius', str(hf_settings.get('border_radius', 0)),
        '--hf-box-color', hf_settings.get('box_color', '#000000'),
        '--hf-box-opacity', '100',
        '--font-name', hf_settings.get('font_name', 'Fira Sans Ultra'),
        '--hf-text-color', hf_settings.get('text_color', '#ffffff'),
        '--hf-font-size', str(hf_settings.get('font_size', 52)),
        '--hf-pad-x', str(hf_settings.get('pad_x', 0)),
        '--hf-pad-y', str(hf_settings.get('pad_y', 0)),
        '--hf-align', hf_settings.get('hf_align', 'center'),
        '--hf-bottom-margin', str(hf_settings.get('hf_bottom_margin', 150)),
    ]
    if hf_settings.get('font_file'):
        args += ['--font-file', hf_settings['font_file']]
    if hf_settings.get('hf_sfx') == '1':
        args += ['--hf-sfx-type', hf_settings.get('hf_sfx_type', 'click')]

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"subgen.py failed: {result.stderr[-500:]}")

    if progress_callback:
        progress_callback("HyperFrames captions generated successfully")
    return output_path


def load_youtube_credentials():
    """Load YouTube credentials from file"""
    try:
        if os.path.exists(YOUTUBE_CREDENTIALS_FILE):
            with open(YOUTUBE_CREDENTIALS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"Error loading YouTube credentials: {e}", file=sys.stderr)
    return {}

def load_youtube_oauth_config():
    """Load YouTube OAuth config from file or env"""
    oauth_config_file = "youtube_oauth_config.json"
    try:
        if os.path.exists(oauth_config_file):
            with open(oauth_config_file, 'r') as f:
                data = json.load(f)
            # New multi-profile format: {profiles:[...], active:{clientId,clientSecret}}
            if 'active' in data and data['active'].get('clientId'):
                return data['active']
            # Old flat format
            if data.get('clientId'):
                return data
    except Exception as e:
        print(f"Error loading OAuth config: {e}", file=sys.stderr)

    return {
        'clientId': os.getenv('YOUTUBE_CLIENT_ID', ''),
        'clientSecret': os.getenv('YOUTUBE_CLIENT_SECRET', '')
    }

def get_youtube_service(channel_id):
    """Get authenticated YouTube service for a channel"""
    credentials_data = load_youtube_credentials()

    if channel_id not in credentials_data:
        raise ValueError(f"YouTube channel {channel_id} not connected.")

    channel_creds = credentials_data[channel_id]
    channel_title = channel_creds.get('channelTitle', '')

    # Pick the OAuth profile whose name matches the channel title; fall back to active
    oauth_config = None
    try:
        with open('youtube_oauth_config.json') as _f:
            _oauth_data = json.load(_f)
        for _profile in _oauth_data.get('profiles', []):
            if _profile.get('name', '').lower() == channel_title.lower():
                oauth_config = _profile
                break
    except Exception:
        pass
    if oauth_config is None:
        oauth_config = load_youtube_oauth_config()

    if not oauth_config.get('clientId') or not oauth_config.get('clientSecret'):
        raise ValueError("YouTube OAuth credentials not configured.")

    creds = Credentials(
        token=channel_creds.get('accessToken'),
        refresh_token=channel_creds.get('refreshToken'),
        token_uri='https://oauth2.googleapis.com/token',
        client_id=oauth_config['clientId'],
        client_secret=oauth_config['clientSecret']
    )
    
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            channel_creds['accessToken'] = creds.token
            channel_creds['expiryDate'] = creds.expiry.timestamp() * 1000 if creds.expiry else None
            credentials_data[channel_id] = channel_creds
            with open(YOUTUBE_CREDENTIALS_FILE, 'w') as f:
                json.dump(credentials_data, f, indent=2)
        except Exception as refresh_error:
            error_msg = str(refresh_error)
            if 'unauthorized_client' in error_msg.lower() or 'invalid_grant' in error_msg.lower():
                raise ValueError(f"YouTube authorization expired for channel '{channel_creds.get('channelTitle', channel_id)}'.")
            else:
                raise Exception(f"Failed to refresh YouTube token: {refresh_error}")
    
    return build('youtube', 'v3', credentials=creds)

def upload_to_youtube(video_path, title, description, publish_at, youtube_channel, progress_callback=None):
    """Upload video to YouTube"""
    try:
        if progress_callback:
            progress_callback("Uploading to YouTube...")
        
        youtube = get_youtube_service(youtube_channel)
        
        # Ensure title has #shorts tag for YouTube Shorts
        shorts_title = title
        if '#shorts' not in title.lower() and '#short' not in title.lower():
            shorts_title = f"{title} #shorts"
        
        body = {
            'snippet': {
                'title': shorts_title,
                'description': description,
                'categoryId': '22',
                'tags': ['shorts', 'clip', '#shorts']
            },
            'status': {
                'privacyStatus': 'private',
                'publishAt': publish_at,
                'selfDeclaredMadeForKids': False
            }
        }
        
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
        insert_request = youtube.videos().insert(
            part=','.join(body.keys()),
            body=body,
            media_body=media
        )
        
        response = None
        retry = 0
        while response is None:
            try:
                status, response = insert_request.next_chunk()
                if response is not None:
                    if 'id' in response:
                        video_id = response['id']
                        if progress_callback:
                            progress_callback(f"YouTube upload successful: {video_id}")
                        return {
                            'success': True,
                            'videoId': video_id,
                            'url': f'https://www.youtube.com/watch?v={video_id}'
                        }
                    else:
                        raise Exception('Upload failed: No video ID in response')
            except HttpError as e:
                error_details = str(e)
                if 'uploadLimitExceeded' in error_details or 'upload limit' in error_details.lower():
                    raise Exception("YOUTUBE_UPLOAD_LIMIT_EXCEEDED")
                if 'unauthorized' in error_details.lower() or e.resp.status == 401:
                    raise Exception("YOUTUBE_AUTHORIZATION_EXPIRED")
                if e.resp.status in [500, 502, 503, 504]:
                    retry += 1
                    if retry > 3:
                        raise Exception(f"Upload failed after retries: {e}")
                    time.sleep(2 ** retry)
                else:
                    raise Exception(f"YouTube upload failed: {e}")
        
        raise Exception("Upload did not complete")
    except Exception as e:
        print(f"Error uploading to YouTube: {e}", file=sys.stderr)
        raise

def process_clip_row(row, index, total, source_video_path, edit_settings, subtitle_settings,
                    youtube_channels=None, instagram_accounts=None, facebook_pages=None,
                    use_platforms_from_csv=False,
                    use_watermark_text_from_csv=False,
                    style=1,
                    picture_folder=None,
                    picture_background=None,
                    project_root=None,
                    clipout_id=None,
                    review_before_upload=False,
                    progress_callback=None, excluded_channels=None,
                    browser_post_account=None,
                    youtube_browser_account=None,
                    enable_auto_edit=True,
                    enable_subtitles=True,
                    enable_hf=False,
                    hf_settings=None):
    """Process a single clip row from CSV"""
    if excluded_channels is None:
        excluded_channels = set()
    
    if progress_callback:
        progress_callback(f"Starting processing for clip {index + 1}")
    
    try:
        # Parse timestamps - handle None values properly
        timestamp_start_raw = row.get('timestamp_start')
        timestamp_end_raw = row.get('timestamp_end')
        title_raw = row.get('title')
        description_raw = row.get('description')
        upload_date_raw = row.get('upload_date')
        upload_time_raw = row.get('upload_time')
        
        # Helper function to safely convert to string
        def safe_str(value, default=''):
            if value is None:
                return default
            try:
                if pd.isna(value):
                    return default
            except (TypeError, ValueError):
                pass
            str_value = str(value)
            if str_value in ['None', 'nan', 'NaN', '']:
                return default
            return str_value.strip()
        
        # Convert to string, handling None and NaN values
        timestamp_start_str = safe_str(timestamp_start_raw)
        timestamp_end_str = safe_str(timestamp_end_raw)
        title = safe_str(title_raw, f'Clip {index + 1}')
        description = safe_str(description_raw)
        
        if upload_date_raw is None or pd.isna(upload_date_raw) or upload_time_raw is None or pd.isna(upload_time_raw):
            raise ValueError(f"Row {index + 1}: Missing upload_date or upload_time")
        
        upload_date = safe_str(upload_date_raw)
        upload_time = safe_str(upload_time_raw)
        
        if not timestamp_start_str or not timestamp_end_str:
            raise ValueError(f"Row {index + 1}: Missing timestamp_start or timestamp_end")
        
        if not title:
            raise ValueError(f"Row {index + 1}: Missing title")
        
        # Parse timestamps to seconds
        start_time = parse_timestamp(timestamp_start_str)
        end_time = parse_timestamp(timestamp_end_str)
        
        if start_time >= end_time:
            raise ValueError(f"Row {index + 1}: timestamp_start must be before timestamp_end")
        
        # Check duration - must be 150 seconds or less for YouTube Shorts
        duration = end_time - start_time
        if duration > 150:
            skip_msg = f"Row {index + 1}: SKIPPING CLIP - Duration ({duration}s) exceeds 150 seconds limit for YouTube Shorts"
            print(skip_msg, file=sys.stderr, flush=True)
            if progress_callback:
                progress_callback(f"⏭️ Skipping clip {index + 1} - duration ({duration}s) exceeds 150 seconds")
            return {
                'success': False,
                'index': index,
                'title': title,
                'error': 'DURATION_TOO_LONG',
                'message': f'Duration {duration}s exceeds 150 seconds limit for YouTube Shorts.'
            }
        
        # Convert IST to UTC
        publish_at = convert_ist_to_utc(upload_date, upload_time)
        if not publish_at:
            raise ValueError(f"Row {index + 1}: Invalid date/time format")
        
        # Resolve per-row platforms from CSV if enabled
        if use_platforms_from_csv:
            yt_names = _split_multi_names(_get_row_value(row, ['youtube channel', 'youtube_channel', 'youtube channels', 'youtube_channels']))
            ig_names = _split_multi_names(_get_row_value(row, ['instagram page', 'instagram_account', 'instagram', 'instagram accounts', 'instagram_accounts']))
            fb_names = _split_multi_names(_get_row_value(row, ['facebook page', 'facebook_page', 'facebook', 'facebook pages', 'facebook_pages']))

            youtube_channels = _resolve_youtube_channel_ids(yt_names, load_youtube_credentials())
            instagram_accounts = _resolve_instagram_account_ids(ig_names, load_facebook_credentials())
            facebook_pages = _resolve_facebook_page_ids(fb_names, load_facebook_credentials())

            if (not youtube_channels) and (not instagram_accounts) and (not facebook_pages):
                raise ValueError(
                    "No platforms provided in row. Provide at least one of: youtube channel, instagram page, facebook page."
                )

        # Normalize to lists
        if youtube_channels is None:
            youtube_channels = []
        if instagram_accounts is None:
            instagram_accounts = []
        if facebook_pages is None:
            facebook_pages = []
        if isinstance(instagram_accounts, str) and instagram_accounts.strip():
            instagram_accounts = [instagram_accounts.strip()]
        if isinstance(facebook_pages, str) and facebook_pages.strip():
            facebook_pages = [facebook_pages.strip()]

        # Watermark: use CSV text when "Use watermark text from CSV" is on; auto-enable if CSV has value
        if use_watermark_text_from_csv:
            wm_text = str(_get_row_value(row, ['watermark text', 'watermark_text', 'watermark'])).strip()
            if wm_text:
                edit_settings = dict(edit_settings)  # Do not mutate caller dict
                edit_settings['watermark_text'] = wm_text
                edit_settings['enable_watermark'] = True  # Auto-enable when CSV has watermark
            elif edit_settings.get('enable_watermark') in ('on', True):
                raise ValueError("Missing watermark text in row (CSV column: 'watermark text' / 'watermark').")

        # Check for duplicates per platform BEFORE doing any heavy processing
        filtered_youtube_channels = []
        filtered_instagram_accounts = []
        filtered_facebook_pages = []
        skipped_platforms = []

        if youtube_channels and len(youtube_channels) > 0:
            for channel_id in youtube_channels:
                if channel_id in excluded_channels:
                    skipped_platforms.append(f"YouTube channel {channel_id[:8]}... (limit exceeded)")
                    continue

                duplicate = check_duplicate(title, None, 'YouTube', channel_id)
                if duplicate:
                    print(f"Row {index + 1}: DUPLICATE DETECTED for YouTube channel {channel_id[:8]}...", file=sys.stderr, flush=True)
                    skipped_platforms.append(f"YouTube channel {channel_id[:8]}... (duplicate)")
                else:
                    filtered_youtube_channels.append(channel_id)

        # If YouTube channels were configured and ALL are duplicates, skip the entire clip
        if youtube_channels and len(youtube_channels) > 0 and len(filtered_youtube_channels) == 0:
            skip_msg = f"Row {index + 1}: SKIPPING CLIP - already uploaded to YouTube (all channels are duplicates)"
            print(skip_msg, file=sys.stderr, flush=True)
            if progress_callback:
                progress_callback(f"⏭️ Skipping clip {index + 1} - already uploaded to YouTube")
            return {
                'success': False,
                'index': index,
                'title': title,
                'error': 'YOUTUBE_DUPLICATE',
                'message': 'Already uploaded to YouTube - skipping entire clip.'
            }

        if instagram_accounts and len(instagram_accounts) > 0:
            for account_id in instagram_accounts:
                duplicate = check_duplicate(title, None, 'Instagram', account_id)
                if duplicate:
                    print(f"Row {index + 1}: DUPLICATE DETECTED for Instagram account {account_id[:8]}...", file=sys.stderr, flush=True)
                    skipped_platforms.append(f"Instagram account {account_id[:8]}... (duplicate)")
                else:
                    filtered_instagram_accounts.append(account_id)

        if facebook_pages and len(facebook_pages) > 0:
            for page_id in facebook_pages:
                duplicate = check_duplicate(title, None, 'Facebook', page_id)
                if duplicate:
                    print(f"Row {index + 1}: DUPLICATE DETECTED for Facebook page {page_id[:8]}...", file=sys.stderr, flush=True)
                    skipped_platforms.append(f"Facebook page {page_id[:8]}... (duplicate)")
                else:
                    filtered_facebook_pages.append(page_id)

        filtered_browser_post_account = browser_post_account
        if browser_post_account:
            duplicate = check_duplicate(title, None, 'BrowserReel', browser_post_account)
            if duplicate:
                print(f"Row {index + 1}: DUPLICATE DETECTED for BrowserReel account {browser_post_account} (duplicate)", file=sys.stderr, flush=True)
                skipped_platforms.append(f"BrowserReel account {browser_post_account} (duplicate)")
                filtered_browser_post_account = None

        filtered_youtube_browser_account = youtube_browser_account
        if youtube_browser_account:
            duplicate = check_duplicate(title, None, 'YouTubeBrowser', youtube_browser_account)
            if duplicate:
                print(f"Row {index + 1}: DUPLICATE DETECTED for YouTubeBrowser account {youtube_browser_account} (duplicate)", file=sys.stderr, flush=True)
                skipped_platforms.append(f"YouTubeBrowser account {youtube_browser_account} (duplicate)")
                filtered_youtube_browser_account = None

        if len(filtered_youtube_channels) == 0 and len(filtered_instagram_accounts) == 0 and len(filtered_facebook_pages) == 0 and not filtered_browser_post_account and not filtered_youtube_browser_account:
            skip_msg = f"Row {index + 1}: SKIPPING CLIP - All selected platforms are duplicates or exceeded limits!"
            print(skip_msg, file=sys.stderr, flush=True)
            if progress_callback:
                progress_callback(f"⏭️ Skipping clip {index + 1} - all selected platforms are duplicates or exceeded limits")
            return {
                'success': False,
                'index': index,
                'title': title,
                'error': 'ALL_PLATFORMS_DUPLICATE',
                'message': 'All selected platforms are duplicates or exceeded limits.'
            }
        
        # Rebuild platforms_list
        platforms_list = []
        if filtered_youtube_channels and len(filtered_youtube_channels) > 0:
            platforms_list.append('YouTube')
        if filtered_instagram_accounts and len(filtered_instagram_accounts) > 0:
            platforms_list.append('Instagram')
        if filtered_facebook_pages and len(filtered_facebook_pages) > 0:
            platforms_list.append('Facebook')
        if filtered_browser_post_account:
            platforms_list.append('Browser/Reel')
        
        # Add record to database (Postgres)
        add_video_record(
            title=title,
            script=None,  # No script for clips
            description=description,
            platforms=platforms_list,
            scheduled_datetime=publish_at,
            created_datetime=datetime.now().isoformat(),
            source='clipout_shorts'
        )
        
        # Trim video
        if progress_callback:
            progress_callback(f"Step 1/5: Trimming video from {format_timestamp(start_time)} to {format_timestamp(end_time)}")
        os.makedirs(TEMP_DIR, exist_ok=True)
        trimmed_path = os.path.join(TEMP_DIR, f"clip_{index + 1}_trimmed_{int(time.time())}.mp4")
        trimmed_path = trim_video(source_video_path, trimmed_path, start_time, end_time,
                                 lambda msg: progress_callback(f"Step 1/5: {msg}") if progress_callback else None)
        
        # Convert to 9:16 portrait format for YouTube Shorts (after trimming, before editing)
        if progress_callback:
            progress_callback(f"Step 2/5: Converting to 9:16 portrait format")
        portrait_path = os.path.join(TEMP_DIR, f"clip_{index + 1}_portrait_{int(time.time())}.mp4")
        if style == 3:
            portrait_path = ensure_portrait_9_16_style3(
                trimmed_path, portrait_path,
                progress_callback=lambda msg: progress_callback(f"Step 2/5: {msg}") if progress_callback else None
            )
        elif style == 2 and picture_folder:
            # Apply watermark inline when it's the only edit operation (avoids server roundtrip)
            inline_wm = None
            if enable_auto_edit and _only_watermark_edit(edit_settings):
                inline_wm = _build_watermark_drawtext(edit_settings)
            portrait_path = ensure_portrait_9_16_style2(
                trimmed_path, portrait_path, picture_folder,
                project_root=project_root,
                background_path=picture_background,
                watermark_drawtext=inline_wm,
                progress_callback=lambda msg: progress_callback(f"Step 2/5: {msg}") if progress_callback else None
            )
        else:
            portrait_path = ensure_portrait_9_16(trimmed_path, portrait_path,
                                                lambda msg: progress_callback(f"Step 2/5: {msg}") if progress_callback else None)
            inline_wm = None

        # Edit video (now editing the portrait-formatted video)
        # Skip if watermark was already applied inline during portrait conversion
        if enable_auto_edit and not (style == 2 and picture_folder and inline_wm and _only_watermark_edit(edit_settings)):
            if progress_callback:
                progress_callback(f"Step 3/5: Editing video with auto-edit settings")
            edited_path = edit_video(portrait_path, edit_settings,
                                    lambda msg: progress_callback(f"Step 3/5: {msg}") if progress_callback else None)
        else:
            edited_path = portrait_path

        # Generate subtitles
        if enable_subtitles:
            if progress_callback:
                progress_callback(f"Step 4/5: Generating subtitles")
            subtitled_path = generate_subtitles(edited_path, subtitle_settings,
                                              lambda msg: progress_callback(f"Step 4/5: {msg}") if progress_callback else None)
        else:
            subtitled_path = edited_path

        # HyperFrames captions (runs after subtitles if both enabled)
        if enable_hf and hf_settings:
            if progress_callback:
                progress_callback(f"Step 4b/5: Generating HyperFrames captions")
            subtitled_path = generate_hf_captions(subtitled_path, hf_settings,
                                                   lambda msg: progress_callback(f"Step 4b/5: {msg}") if progress_callback else None)

        # Optional manual review gating before upload
        if review_before_upload:
            if not clipout_id:
                raise ValueError("review_before_upload enabled but clipout_id missing")
            # Notify server so UI can show the current short
            try:
                requests.post(
                    f"{SERVER_URL}/clipout-review/{clipout_id}/notify",
                    json={
                        "index": index,
                        "total": total,
                        "title": title,
                        "video_path": os.path.abspath(subtitled_path),
                    },
                    timeout=10,
                ).raise_for_status()
            except Exception as e:
                raise RuntimeError(f"Failed to notify review server: {e}")

            # Poll until user decides approve/skip
            if progress_callback:
                progress_callback("Waiting for review decision (approve/skip)…")
            decision = None
            for _ in range(60 * 60):  # up to ~1 hour (1s interval)
                try:
                    r = requests.get(
                        f"{SERVER_URL}/clipout-review/{clipout_id}/decision",
                        params={"index": str(index)},
                        timeout=10,
                    )
                    if r.ok:
                        data = r.json() or {}
                        if data.get("decided"):
                            decision = data.get("decision")
                            break
                except Exception:
                    pass
                time.sleep(1)
            if decision not in ("approve", "skip"):
                raise TimeoutError("Timed out waiting for review decision")
            if decision == "skip":
                if progress_callback:
                    progress_callback("Review decision: skip upload")
                # Cleanup temp files (keep subtitled output for manual use)
                for path in (trimmed_path, portrait_path, edited_path):
                    if os.path.exists(path):
                        try:
                            os.remove(path)
                        except Exception:
                            pass
                return {
                    "success": True,
                    "index": index,
                    "title": title,
                    "scheduledFor": publish_at,
                    "review": "skipped",
                    "output": os.path.abspath(subtitled_path),
                }
            if progress_callback:
                progress_callback("Review decision: approve upload")
        
        # Step 5/5: Uploading to selected platforms
        if progress_callback:
            progress_callback(f"Step 5/5: Uploading to platforms")
        results = {}
        limit_exceeded_channels = []

        if filtered_youtube_channels and len(filtered_youtube_channels) > 0:
            youtube_results = []
            for channel_id in filtered_youtube_channels:
                try:
                    youtube_result = upload_to_youtube(subtitled_path, title, description, publish_at, channel_id,
                                                      lambda msg: progress_callback(f"[{index + 1}/{total}] YouTube[{channel_id[:8]}...]: {msg}") if progress_callback else None)
                    youtube_results.append({
                        'channelId': channel_id,
                        'result': youtube_result
                    })
                except Exception as e:
                    error_str = str(e)
                    if 'YOUTUBE_UPLOAD_LIMIT_EXCEEDED' in error_str:
                        limit_exceeded_channels.append(channel_id)
                        youtube_results.append({
                            'channelId': channel_id,
                            'error': 'YOUTUBE_UPLOAD_LIMIT_EXCEEDED',
                            'limitExceeded': True
                        })
                    else:
                        print(f"❌ Error uploading to YouTube channel {channel_id}: {e}", file=sys.stderr, flush=True)
                        youtube_results.append({
                            'channelId': channel_id,
                            'error': str(e)
                        })

            results['youtube'] = youtube_results
            if limit_exceeded_channels:
                results['limit_exceeded_channels'] = limit_exceeded_channels

        # Upload to Instagram
        if filtered_instagram_accounts:
            results['instagram'] = []
            caption = f"{title}\n\n{description}" if description else title
            for account_id in filtered_instagram_accounts:
                try:
                    instagram_result = upload_to_instagram(
                        subtitled_path,
                        caption,
                        account_id,
                        lambda msg: progress_callback(f"[{index + 1}/{total}] IG[{account_id[:8]}...]: {msg}") if progress_callback else None
                    )
                    results['instagram'].append({'accountId': account_id, 'result': instagram_result})
                except Exception as e:
                    print(f"❌ Error uploading to Instagram account {account_id}: {e}", file=sys.stderr, flush=True)
                    results['instagram'].append({'accountId': account_id, 'error': str(e)})

        # Facebook Page upload (supports scheduled_publish_time window)
        if filtered_facebook_pages:
            results['facebook'] = []
            for page_id in filtered_facebook_pages:
                try:
                    facebook_result = upload_to_facebook_page(
                        subtitled_path,
                        title,
                        description,
                        publish_at,
                        page_id,
                        lambda msg: progress_callback(f"[{index + 1}/{total}] FB[{page_id[:8]}...]: {msg}") if progress_callback else None
                    )
                    results['facebook'].append({'pageId': page_id, 'result': facebook_result})
                except Exception as e:
                    print(f"❌ Error uploading to Facebook page {page_id}: {e}", file=sys.stderr, flush=True)
                    results['facebook'].append({'pageId': page_id, 'error': str(e)})

        # Browser reel upload (BeWise Reel via fb_browser.py)
        if filtered_browser_post_account:
            caption = f"{title}\n\n{description}" if description else title
            try:
                if progress_callback:
                    progress_callback(f"Step 5/5: Uploading browser reel to {filtered_browser_post_account}...")
                fb_browser_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fb_browser.py')
                cmd = [
                    sys.executable, fb_browser_script,
                    '--video', subtitled_path,
                    '--caption', caption,
                    '--account', filtered_browser_post_account,
                ]
                if publish_at:
                    cmd += ['--schedule', publish_at]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                if proc.returncode == 0:
                    results['browser_reel'] = {'success': True, 'account': filtered_browser_post_account}
                    if progress_callback:
                        progress_callback(f"Browser reel posted successfully ({filtered_browser_post_account})")
                    print(f"✅ Browser reel posted to {filtered_browser_post_account}", flush=True)
                else:
                    err = (proc.stderr or proc.stdout or 'unknown error').strip()
                    results['browser_reel'] = {'success': False, 'error': err}
                    if progress_callback:
                        progress_callback(f"Browser reel failed: {err[:200]}")
                    print(f"❌ Browser reel failed for {filtered_browser_post_account}: {err[:300]}", file=sys.stderr, flush=True)
            except Exception as e:
                results['browser_reel'] = {'success': False, 'error': str(e)}
                print(f"❌ Browser reel exception: {e}", file=sys.stderr, flush=True)

        # YouTube browser upload via youtube_browser.py — only as fallback when API quota is exhausted
        if filtered_youtube_browser_account and (limit_exceeded_channels or not filtered_youtube_channels):
            description_text = f"{title}\n\n{description}" if description else title
            try:
                if progress_callback:
                    progress_callback(f"Step 5/5: Uploading to YouTube (browser) as {filtered_youtube_browser_account}...")
                yt_browser_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'youtube_browser.py')
                cmd = [
                    sys.executable, yt_browser_script,
                    '--video', subtitled_path,
                    '--title', title,
                    '--description', description_text,
                    '--account', filtered_youtube_browser_account,
                ]
                if publish_at:
                    cmd += ['--schedule', publish_at]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
                if proc.returncode == 0:
                    results['youtube_browser'] = {'success': True, 'account': filtered_youtube_browser_account}
                    if progress_callback:
                        progress_callback(f"YouTube browser upload complete ({filtered_youtube_browser_account})")
                    print(f"✅ YouTube browser upload complete for {filtered_youtube_browser_account}", flush=True)
                else:
                    err = (proc.stderr or proc.stdout or 'unknown error').strip()
                    results['youtube_browser'] = {'success': False, 'error': err}
                    if progress_callback:
                        progress_callback(f"YouTube browser upload failed: {err[:200]}")
                    print(f"❌ YouTube browser upload failed for {filtered_youtube_browser_account}: {err[:300]}", file=sys.stderr, flush=True)
                if proc.stdout:
                    print(proc.stdout, flush=True)
            except Exception as e:
                results['youtube_browser'] = {'success': False, 'error': str(e)}
                print(f"❌ YouTube browser exception: {e}", file=sys.stderr, flush=True)

        # Mark each platform as uploaded immediately after it succeeds.
        # Calling mark_video_uploaded per-platform (not once at the end) ensures
        # that if the process crashes mid-run, earlier successful uploads are
        # still recorded in the DB and detected as duplicates on the next run.
        youtube_creds  = load_youtube_credentials()
        facebook_creds = load_facebook_credentials()

        # Full social caption used for the Notion row comment.
        _notion_cap = (f"{title}\n\n{description}" if description else title).strip()

        def _mark(details_patch: dict):
            if _notion_cap:
                details_patch["notion_caption"] = _notion_cap
            ok = mark_video_uploaded(title, None, details_patch)
            if not ok:
                print(f"⚠️ mark_video_uploaded returned False for '{title}'", file=sys.stderr, flush=True)

        if 'youtube' in results and isinstance(results['youtube'], list):
            for yt_result in results['youtube']:
                if 'result' in yt_result and yt_result['result'].get('success'):
                    channel_id = yt_result['channelId']
                    channel_title = youtube_creds.get(channel_id, {}).get('channelTitle', channel_id) if isinstance(youtube_creds, dict) else channel_id
                    _mark({
                        'youtube_channels': [channel_id],
                        'youtube_channel_names': {channel_id: channel_title},
                        'instagram_accounts': [], 'instagram_account_names': {},
                        'facebook_pages': [], 'facebook_page_names': {},
                        'browser_reel_accounts': [],
                    })

        if 'instagram' in results and isinstance(results['instagram'], list):
            for ig_res in results['instagram']:
                account_id = ig_res.get('accountId')
                if not account_id:
                    continue
                # Only count actually posted Reels — queued/future jobs must not update DB here.
                if not (ig_res.get('result') and ig_res['result'].get('success')):
                    continue
                ig_key = f"ig_{account_id}"
                username = facebook_creds.get(ig_key, {}).get('username', account_id) if isinstance(facebook_creds, dict) else account_id
                _mark({
                    'youtube_channels': [], 'youtube_channel_names': {},
                    'instagram_accounts': [account_id],
                    'instagram_account_names': {account_id: username},
                    'facebook_pages': [], 'facebook_page_names': {},
                    'browser_reel_accounts': [],
                })

        if 'facebook' in results and isinstance(results['facebook'], list):
            for fb_res in results['facebook']:
                page_id = fb_res.get('pageId')
                if not page_id:
                    continue
                if fb_res.get('result') and fb_res['result'].get('success'):
                    page_key = f"page_{page_id}"
                    page_name = facebook_creds.get(page_key, {}).get('pageName', page_id) if isinstance(facebook_creds, dict) else page_id
                    _mark({
                        'youtube_channels': [], 'youtube_channel_names': {},
                        'instagram_accounts': [], 'instagram_account_names': {},
                        'facebook_pages': [page_id],
                        'facebook_page_names': {page_id: page_name},
                        'browser_reel_accounts': [],
                    })

        if results.get('browser_reel', {}).get('success'):
            _mark({
                'youtube_channels': [], 'youtube_channel_names': {},
                'instagram_accounts': [], 'instagram_account_names': {},
                'facebook_pages': [], 'facebook_page_names': {},
                'browser_reel_accounts': [filtered_browser_post_account],
            })

        if results.get('youtube_browser', {}).get('success'):
            _mark({
                'youtube_channels': [], 'youtube_channel_names': {},
                'instagram_accounts': [], 'instagram_account_names': {},
                'facebook_pages': [], 'facebook_page_names': {},
                'browser_reel_accounts': [f"yt_browser_{filtered_youtube_browser_account}"],
            })

        # Log Facebook failures if any
        _fb_failed = [r.get('error') for r in (results.get('facebook') or []) if 'error' in r]
        if _fb_failed:
            print(f"⚠️ Facebook upload failed for '{title}': {_fb_failed[0]}", file=sys.stderr, flush=True)
        
        # Cleanup temporary files (keep subtitled_path when HF is enabled — it IS the final rendered clip)
        cleanup_paths = [trimmed_path, portrait_path, edited_path]
        if not enable_hf:
            cleanup_paths.append(subtitled_path)

        for path in cleanup_paths:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    print(f"⚠️ Could not delete {path}: {e}", flush=True)
        
        return {
            'success': True,
            'index': index,
            'title': title,
            'scheduledFor': publish_at,
            'youtube': results.get('youtube', []),
            'instagram': results.get('instagram'),
            'facebook': results.get('facebook')
        }
    except Exception as e:
        error_msg = str(e)
        if progress_callback:
            progress_callback(f"❌ Error: {error_msg}")
        print(f"[clipout] Error in process_clip_row for clip {index + 1}: {error_msg}", file=sys.stderr, flush=True)
        import traceback
        print(f"[clipout] Traceback: {traceback.format_exc()}", file=sys.stderr, flush=True)
        return {
            'success': False,
            'index': index,
            'error': error_msg
        }

def parse_file(file_path):
    """Parse CSV or Excel file"""
    try:
        if file_path.endswith('.csv'):
            df = pd.read_csv(file_path)
        elif file_path.endswith('.xlsx') or file_path.endswith('.xls'):
            df = pd.read_excel(file_path)
        else:
            raise ValueError("Unsupported file format. Use CSV or Excel files.")
        
        df.columns = df.columns.str.lower().str.strip()
        
        # Map common column name variations to standard names
        column_mapping = {}
        for col in df.columns:
            col_lower = col.lower().strip()
            if 'timestamp' in col_lower or 'timestamps' in col_lower:
                column_mapping[col] = 'timestamps'
            elif 'title' in col_lower or 'video title' in col_lower:
                column_mapping[col] = 'title'
            elif (
                ('video' in col_lower and 'path' in col_lower) or
                ('source' in col_lower and 'video' in col_lower) or
                col_lower in ('video_path', 'source_video_path', 'video file path', 'video_filepath', 'file_path', 'filepath')
            ):
                column_mapping[col] = 'source_video_path'
            elif 'description' in col_lower or 'video description' in col_lower:
                column_mapping[col] = 'description'
            elif 'upload_date' in col_lower or 'upload date' in col_lower or 'date' in col_lower:
                column_mapping[col] = 'upload_date'
            elif 'upload_time' in col_lower or 'upload time' in col_lower or 'time' in col_lower:
                column_mapping[col] = 'upload_time'
        
        # Rename columns
        df = df.rename(columns=column_mapping)
        
        # Check if we have timestamps column (needs to be split) or separate start/end columns
        if 'timestamps' in df.columns:
            # Parse timestamp format: 
            # "00:00:00,000 --> 00:00:30,000" or "00:00:00 --> 00:00:30" (SRT format)
            # "00:00:12 - 00:00:42" (dash format)
            def split_timestamps(timestamp_str):
                if pd.isna(timestamp_str) or timestamp_str is None:
                    return pd.NA, pd.NA
                timestamp_str = str(timestamp_str).strip()
                
                # Handle SRT format with -->
                if '-->' in timestamp_str:
                    parts = timestamp_str.split('-->')
                    if len(parts) == 2:
                        start = parts[0].strip().split(',')[0].strip()  # Remove milliseconds if present
                        end = parts[1].strip().split(',')[0].strip()    # Remove milliseconds if present
                        if start and end:
                            return start, end
                
                # Handle dash format: "00:00:12 - 00:00:42"
                if ' - ' in timestamp_str or timestamp_str.count('-') == 1:
                    # Split on dash, but be careful with single dash (could be in time format)
                    parts = timestamp_str.split(' - ', 1)  # Split on ' - ' first
                    if len(parts) == 2:
                        start = parts[0].strip()
                        end = parts[1].strip()
                        if start and end:
                            return start, end
                    # Try single dash if ' - ' didn't work
                    elif '-' in timestamp_str:
                        parts = timestamp_str.split('-', 1)
                        if len(parts) == 2:
                            start = parts[0].strip()
                            end = parts[1].strip()
                            if start and end:
                                return start, end
                
                return pd.NA, pd.NA
            
            result = df['timestamps'].apply(lambda x: pd.Series(split_timestamps(x), index=['timestamp_start', 'timestamp_end']))
            df[['timestamp_start', 'timestamp_end']] = result
            df = df.drop('timestamps', axis=1)
        
        # Strip "IST" from upload_time if present
        if 'upload_time' in df.columns:
            df['upload_time'] = df['upload_time'].astype(str).str.replace(' IST', '', regex=False).str.replace('IST', '', regex=False).str.strip()
        
        # Validate required columns
        required_columns = ['timestamp_start', 'timestamp_end', 'title', 'upload_date', 'upload_time']
        missing_columns = [col for col in required_columns if col not in df.columns]
        if missing_columns:
            raise ValueError(f"Missing required columns: {', '.join(missing_columns)}. Found columns: {', '.join(df.columns)}")
        
        # Filter out rows with missing data
        df = df[
            df['timestamp_start'].notna() &
            df['timestamp_end'].notna() &
            df['title'].notna() &
            df['upload_date'].notna() &
            df['upload_time'].notna()
        ]
        
        # Convert timestamp columns to string to avoid pd.NA issues
        df['timestamp_start'] = df['timestamp_start'].astype(str).str.strip()
        df['timestamp_end'] = df['timestamp_end'].astype(str).str.strip()
        
        # Filter out empty strings
        df = df[
            (df['timestamp_start'] != '') &
            (df['timestamp_end'] != '') &
            (df['title'].astype(str).str.strip() != '') &
            (df['upload_date'].astype(str).str.strip() != '') &
            (df['upload_time'].astype(str).str.strip() != '')
        ]
        
        # Convert to dict
        records = df.to_dict('records')
        
        # Clean up any None or pd.NA values that might have slipped through
        for record in records:
            for key, value in list(record.items()):
                if value is None:
                    record[key] = ''
                elif isinstance(value, float) and pd.isna(value):
                    record[key] = ''
                elif hasattr(value, '__class__') and str(value.__class__.__name__) == 'NAType':
                    record[key] = ''
        
        return records
    except Exception as e:
        print(f"Error parsing file: {e}", file=sys.stderr)
        raise

def main():
    """Main function"""
    if len(sys.argv) < 2:
        print("Usage: python3 clipout_shorts.py <config_json>", file=sys.stderr)
        sys.exit(1)
    
    try:
        from dotenv import load_dotenv
        load_dotenv()
        
        config_path = sys.argv[1]
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        # Validate required config keys
        required_keys = ['csv_file_path']
        missing_keys = [key for key in required_keys if key not in config]
        if missing_keys:
            raise KeyError(f"Missing required config keys: {', '.join(missing_keys)}. Config keys present: {list(config.keys())}")
        
        source_video_path = config.get('source_video_path') or ''
        csv_file_path = config['csv_file_path']
        edit_settings = config.get('edit_settings', {})
        subtitle_settings = config.get('subtitle_settings', {})
        enable_auto_edit = bool(config.get('enable_auto_edit', True))
        enable_subtitles = bool(config.get('enable_subtitles', True))
        enable_hf        = bool(config.get('enable_hf', False))
        hf_settings      = config.get('hf_settings') or {}
        youtube_channels = config.get('youtube_channels', [])
        instagram_account = config.get('instagram_account')
        facebook_page = config.get('facebook_page')
        browser_post_account = config.get('browser_post_account') or None
        youtube_browser_account = config.get('youtube_browser_account') or None
        use_platforms_from_csv = bool(config.get('use_platforms_from_csv', False))
        use_watermark_text_from_csv = bool(config.get('use_watermark_text_from_csv', False))
        review_before_upload = bool(config.get('review_before_upload', False))
        style = 3 if config.get('style') == 3 else (2 if config.get('style') == 2 else 1)
        picture_folder = config.get('picture_folder') or None
        picture_background = config.get('picture_background') or None
        if picture_folder and isinstance(picture_folder, str):
            picture_folder = picture_folder.strip() or None
        if picture_background and isinstance(picture_background, str):
            picture_background = picture_background.strip() or None
        # Resolve picture_folder relative to the directory containing this script (project root)
        project_root = os.path.dirname(os.path.abspath(__file__))
        if style == 2 and not picture_folder:
            print("ERROR: Style 2 requires picture_folder. Please specify a folder with portrait images/videos.", file=sys.stderr)
            sys.exit(1)

        # Validate at least one platform is selected (unless using per-row CSV platforms or browser post)
        if (not use_platforms_from_csv and (not youtube_channels or len(youtube_channels) == 0) and not instagram_account and not facebook_page and not browser_post_account and not youtube_browser_account):
            print("ERROR: No platform selected. Please select at least one platform (YouTube, Instagram, Facebook, Browser Post, or YouTube Browser).", file=sys.stderr)
            sys.exit(1)

        # Validate Instagram/Facebook selections if provided (UI mode)
        if (not use_platforms_from_csv) and (instagram_account or facebook_page):
            creds = load_facebook_credentials()
            if instagram_account:
                ig_key = f"ig_{instagram_account}"
                if ig_key not in creds:
                    print(f"ERROR: Instagram account {instagram_account} not connected.", file=sys.stderr)
                    print("Please connect your Instagram account first using the web interface.", file=sys.stderr)
                    sys.exit(1)
            if facebook_page:
                page_key = f"page_{facebook_page}"
                if page_key not in creds:
                    print(f"ERROR: Facebook Page {facebook_page} not connected.", file=sys.stderr)
                    print("Please connect your Facebook Page first using the web interface.", file=sys.stderr)
                    sys.exit(1)
        
        # Default source video is optional if CSV provides per-row paths
        if source_video_path and not os.path.exists(source_video_path):
            print(f"ERROR: Source video file not found: {source_video_path}", file=sys.stderr)
            sys.exit(1)
        
        print(f"Parsing CSV file: {csv_file_path}", flush=True)
        clip_data = parse_file(csv_file_path)
        total = len(clip_data)
        
        print(f"Found {total} clips to process", flush=True)
        
        results = []
        excluded_channels = set()
        
        for i, row in enumerate(clip_data):
            title = row.get('title', 'Untitled')
            print(f"\n[clipout] Processing clip {i + 1}/{total}: {title}", flush=True)
            
            try:
                effective_source_video_path = resolve_source_video_path(row, source_video_path, csv_file_path)
                result = process_clip_row(
                    row, i, total, effective_source_video_path, edit_settings, subtitle_settings,
                    youtube_channels,
                    [instagram_account] if (instagram_account and not use_platforms_from_csv) else [],
                    [facebook_page] if (facebook_page and not use_platforms_from_csv) else [],
                    use_platforms_from_csv=use_platforms_from_csv,
                    use_watermark_text_from_csv=use_watermark_text_from_csv,
                    style=style,
                    picture_folder=picture_folder,
                    picture_background=picture_background,
                    project_root=project_root,
                    clipout_id=config.get("clipout_id"),
                    review_before_upload=review_before_upload,
                    progress_callback=lambda msg: print(f"[clipout] [{i + 1}/{total}] {msg}", flush=True),
                    excluded_channels=excluded_channels,
                    browser_post_account=browser_post_account,
                    youtube_browser_account=youtube_browser_account,
                    enable_auto_edit=enable_auto_edit,
                    enable_subtitles=enable_subtitles,
                    enable_hf=enable_hf,
                    hf_settings=hf_settings,
                )
                
                results.append(result)
                
                if result.get('success'):
                    print(f"[clipout] ✅ Clip {i + 1}/{total} completed successfully", flush=True)
                else:
                    error = result.get('error', 'Unknown error')
                    print(f"[clipout] ❌ Clip {i + 1}/{total} failed: {error}", flush=True)
                
                # Track excluded channels
                if 'youtube' in result and isinstance(result.get('youtube'), list):
                    for yt_result in result['youtube']:
                        if yt_result.get('limitExceeded') or (yt_result.get('error') and 'YOUTUBE_UPLOAD_LIMIT_EXCEEDED' in str(yt_result.get('error'))):
                            channel_id = yt_result.get('channelId')
                            if channel_id:
                                excluded_channels.add(channel_id)
                
                if 'limit_exceeded_channels' in result:
                    for channel_id in result['limit_exceeded_channels']:
                        excluded_channels.add(channel_id)
                
            except KeyboardInterrupt:
                print("\n🛑 PROCESSING STOPPED BY USER", flush=True)
                break
            except Exception as e:
                error_msg = str(e)
                import traceback
                print(f"[clipout] ❌ Exception processing clip {i + 1}: {error_msg}", flush=True)
                print(f"[clipout] Traceback: {traceback.format_exc()}", flush=True, file=sys.stderr)
                if 'YOUTUBE_UPLOAD_LIMIT_EXCEEDED' in error_msg:
                    print("\n🛑 PROCESSING STOPPED - YOUTUBE UPLOAD LIMIT EXCEEDED", flush=True)
                    break
                results.append({
                    'success': False,
                    'index': i,
                    'error': error_msg
                })
                continue
            
            if i < total - 1:
                time.sleep(2)
        
        output = {
            'total': total,
            'successful': sum(1 for r in results if r.get('success')),
            'failed': sum(1 for r in results if not r.get('success')),
            'results': results
        }
        
        print(f"\n=== CLIPOUT SHORTS COMPLETE ===", flush=True)
        print(f"Total: {output['total']}", flush=True)
        print(f"Successful: {output['successful']}", flush=True)
        print(f"Failed: {output['failed']}", flush=True)
        
        results_file = os.path.join(OUTPUT_DIR, f"clipout_results_{int(time.time())}.json")
        with open(results_file, 'w') as f:
            json.dump(output, f, indent=2)
        
        print(f"Results saved to: {results_file}", flush=True)
        
        print(f"\nJSON_OUTPUT_START", flush=True)
        print(json.dumps(output), flush=True)
        print(f"JSON_OUTPUT_END", flush=True)
        
    except Exception as e:
        print(f"FATAL_ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
