# -*- coding: utf-8 -*-
import whisper_timestamped as whisper
import sys
import srt
import datetime
import os
import argparse
from langdetect import detect
import subprocess
import tempfile
import math
from typing import Any, Dict, List, Optional

def chunk_words(words, max_words):
    for i in range(0, len(words), max_words):
        yield words[i:i + max_words]

def format_srt(segments, max_words):
    subtitles = []
    index = 1
    for segment in segments:
        words = segment.get("words", [])
        if not words:
            continue
        for chunk in chunk_words(words, max_words):
            start = chunk[0]["start"]
            end = chunk[-1]["end"]
            text = " ".join([w["text"] for w in chunk]).strip()

            sub = srt.Subtitle(
                index=index,
                start=datetime.timedelta(seconds=start),
                end=datetime.timedelta(seconds=end),
                content=text
            )
            subtitles.append(sub)
            index += 1
    return srt.compose(subtitles)

def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

def ffprobe_duration_seconds(path: str) -> Optional[float]:
    """
    Returns media duration in seconds, or None if unknown.
    """
    proc = _run([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ])
    if proc.returncode != 0:
        return None
    try:
        dur = float(proc.stdout.decode("utf-8", errors="ignore").strip())
        if math.isfinite(dur) and dur > 0:
            return dur
    except Exception:
        pass
    return None

def extract_wav_segment(
    input_path: str,
    output_wav: str,
    start_seconds: float = 0.0,
    duration_seconds: Optional[float] = None,
    sample_rate: int = 16000,
    mono: bool = True,
    prefer_left_channel: bool = True,
) -> None:
    """
    Extracts a PCM WAV segment using ffmpeg, using more tolerant flags.
    If direct extraction fails, tries a remux-to-m4a workaround and retries.
    """
    # Using pan avoids some rematrix errors when the AAC stream is weird.
    pan = "pan=1c|c0=c0" if (mono and prefer_left_channel) else None
    filters = []
    if pan:
        filters.append(pan)
    filters.append(f"aresample={sample_rate}")
    filters_str = ",".join(filters) if filters else "anull"

    def _ffmpeg_extract(src: str) -> subprocess.CompletedProcess:
        cmd = [
            "ffmpeg",
            "-y",
            "-v", "warning",
            "-fflags", "+discardcorrupt+genpts",
            "-err_detect", "ignore_err",
            "-ignore_unknown",
        ]
        if start_seconds and start_seconds > 0:
            cmd += ["-ss", str(start_seconds)]
        cmd += ["-i", src, "-vn", "-map", "0:a:0"]
        if duration_seconds is not None and duration_seconds > 0:
            cmd += ["-t", str(duration_seconds)]
        cmd += ["-af", filters_str]
        if mono:
            cmd += ["-ac", "1"]
        cmd += ["-ar", str(sample_rate), "-c:a", "pcm_s16le", output_wav]
        return _run(cmd)

    proc = _ffmpeg_extract(input_path)
    if proc.returncode == 0 and os.path.isfile(output_wav) and os.path.getsize(output_wav) > 0:
        return

    # Fallback: remux audio to m4a (no decode), then decode that.
    with tempfile.TemporaryDirectory(prefix="srtgen_") as td:
        remux_m4a = os.path.join(td, "audio.m4a")
        remux_cmd = [
            "ffmpeg",
            "-y",
            "-v", "warning",
            "-fflags", "+genpts",
            "-ignore_unknown",
            "-i", input_path,
            "-vn",
            "-map", "0:a:0",
            "-c", "copy",
            remux_m4a,
        ]
        remux_proc = _run(remux_cmd)
        if remux_proc.returncode == 0 and os.path.isfile(remux_m4a) and os.path.getsize(remux_m4a) > 0:
            proc2 = _ffmpeg_extract(remux_m4a)
            if proc2.returncode == 0 and os.path.isfile(output_wav) and os.path.getsize(output_wav) > 0:
                return

    stderr = proc.stderr.decode("utf-8", errors="ignore")
    raise RuntimeError(f"Failed to extract audio with ffmpeg.\n{stderr}")

def offset_segments(result: Dict[str, Any], offset_seconds: float) -> Dict[str, Any]:
    if offset_seconds == 0:
        return result
    out = dict(result)
    segs = []
    for seg in result.get("segments", []):
        seg2 = dict(seg)
        if "start" in seg2:
            seg2["start"] = float(seg2["start"]) + offset_seconds
        if "end" in seg2:
            seg2["end"] = float(seg2["end"]) + offset_seconds
        words2 = []
        for w in seg2.get("words", []) or []:
            w2 = dict(w)
            if "start" in w2:
                w2["start"] = float(w2["start"]) + offset_seconds
            if "end" in w2:
                w2["end"] = float(w2["end"]) + offset_seconds
            words2.append(w2)
        seg2["words"] = words2
        segs.append(seg2)
    out["segments"] = segs
    return out

def transcribe_in_chunks(
    model: Any,
    input_path: str,
    chunk_seconds: int,
    sample_rate: int,
    mono: bool,
    prefer_left_channel: bool,
) -> Dict[str, Any]:
    """
    Extract -> transcribe chunk-by-chunk, then merge with time offsets.
    This is more robust for long files and avoids ffmpeg decode failures
    from breaking the entire transcription.
    """
    dur = ffprobe_duration_seconds(input_path)
    if dur is None:
        # Unknown duration: just try direct transcription.
        return whisper.transcribe(model, input_path)

    merged_segments: List[Dict[str, Any]] = []
    n_chunks = max(1, int(math.ceil(dur / float(chunk_seconds))))

    with tempfile.TemporaryDirectory(prefix="srtgen_chunks_") as td:
        for i in range(n_chunks):
            start = i * float(chunk_seconds)
            remaining = max(0.0, dur - start)
            seg_dur = min(float(chunk_seconds), remaining)
            if seg_dur <= 0:
                break

            wav_path = os.path.join(td, f"chunk_{i:04d}.wav")
            try:
                extract_wav_segment(
                    input_path=input_path,
                    output_wav=wav_path,
                    start_seconds=start,
                    duration_seconds=seg_dur,
                    sample_rate=sample_rate,
                    mono=mono,
                    prefer_left_channel=prefer_left_channel,
                )
            except Exception as e:
                # Skip broken chunks instead of failing the whole run.
                print(f"⚠️ Skipping chunk {i+1}/{n_chunks} (start={start:.1f}s): {e}")
                continue

            r = whisper.transcribe(model, wav_path)
            r = offset_segments(r, start)
            merged_segments.extend(r.get("segments", []) or [])

    return {"segments": merged_segments}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate SRT from video")
    parser.add_argument("video_path", help="Path to input video")
    parser.add_argument("max_words", type=int, help="Maximum words per subtitle line")
    parser.add_argument("model_name", nargs="?", default="base.en", help="Whisper model name (default: base.en)")
    parser.add_argument("--output", dest="output_path", help="Optional custom output SRT path")
    parser.add_argument(
        "--chunk-seconds",
        type=int,
        default=0,
        help="Transcribe long media in N-second chunks (0 = auto for long files).",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=16000,
        help="Sample rate used when extracting WAV for chunked mode (default: 16000).",
    )
    parser.add_argument(
        "--no-mono",
        action="store_true",
        help="Do not downmix to mono when extracting WAV for chunked mode.",
    )
    parser.add_argument(
        "--prefer-left-channel",
        action="store_true",
        default=True,
        help="When downmixing to mono, prefer left channel (helps with some broken AAC).",
    )

    args = parser.parse_args()

    video_path = args.video_path
    max_words = args.max_words
    model_name = args.model_name
    output_arg = args.output_path
    chunk_seconds = args.chunk_seconds
    sample_rate = args.sample_rate
    mono = not args.no_mono
    prefer_left_channel = args.prefer_left_channel

    if not os.path.isfile(video_path):
        print(f"❌ File not found: {video_path}")
        sys.exit(1)

    print(f"⏳ Loading model '{model_name}'...")
    model = whisper.load_model(model_name)

    print("🎧 Transcribing...")
    # Auto-enable chunked mode for long inputs, unless explicitly disabled.
    if chunk_seconds == 0:
        dur = ffprobe_duration_seconds(video_path)
        # If duration is unknown, fall back to direct transcription.
        if dur is not None and dur > 600:
            chunk_seconds = 300

    if chunk_seconds and chunk_seconds > 0:
        print(f"🧩 Chunked mode: {chunk_seconds}s chunks")
        result = transcribe_in_chunks(
            model=model,
            input_path=video_path,
            chunk_seconds=chunk_seconds,
            sample_rate=sample_rate,
            mono=mono,
            prefer_left_channel=prefer_left_channel,
        )
    else:
        result = whisper.transcribe(model, video_path)

    all_text = " ".join(
        w["text"] for segment in result["segments"] for w in segment.get("words", [])
    )
    if all_text.strip():
        language = detect(all_text)
        print(f"🌍 Detected language: {language}")
    else:
        print("⚠️ Unable to detect language (empty transcript).")

    print("📝 Formatting subtitles...")
    srt_data = format_srt(result["segments"], max_words)

    # Create subtitles directory if it doesn't exist
    subtitles_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subtitles")
    os.makedirs(subtitles_dir, exist_ok=True)
    
    output_path = output_arg if output_arg else os.path.join(subtitles_dir, "output.srt")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(srt_data)

    print(f"✅ SRT file generated: {output_path}")
