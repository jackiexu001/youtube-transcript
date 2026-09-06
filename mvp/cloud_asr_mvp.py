#!/usr/bin/env python3
"""Isolated Groq transcription MVP for YouTube Caption Archive.

This script intentionally does not modify the production archive. It downloads
only a temporary audio stream, transcodes it for speech recognition, sends
10-minute overlapping chunks to Groq, writes comparison artifacts, and removes
all temporary audio unless --keep-audio is explicitly requested.
"""

import argparse
import getpass
import hashlib
import json
import math
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PROVIDERS = {
    "groq": {
        "name": "Groq",
        "api_url": "https://api.groq.com/openai/v1/audio/transcriptions",
        "model": "whisper-large-v3-turbo",
        "key_prefix": "gsk_",
        "key_url": "https://console.groq.com/keys",
    },
    "openai": {
        "name": "OpenAI",
        "api_url": "https://api.openai.com/v1/audio/transcriptions",
        "model": "whisper-1",
        "key_prefix": "sk-",
        "key_url": "https://platform.openai.com/api-keys",
    },
}
DEFAULT_PROVIDER = "groq"
DEFAULT_API_URL = PROVIDERS[DEFAULT_PROVIDER]["api_url"]
DEFAULT_MODEL = PROVIDERS[DEFAULT_PROVIDER]["model"]
DEFAULT_CHUNK_SECONDS = 600
DEFAULT_OVERLAP_SECONDS = 10
DEFAULT_AUDIO_KBPS = 48
GROQ_TURBO_USD_PER_AUDIO_HOUR = 0.04
DEFAULT_KEYCHAIN_SERVICE = "YouTube Caption Archive Groq MVP"
_LOG_CALLBACK = None


class TranscriptionAPIError(RuntimeError):
    """An API failure that must not be retried by switching YouTube audio sources."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


# Backwards-compatible name for the standalone Groq MVP and older tests.
GroqAPIError = TranscriptionAPIError


def provider_config(provider: str) -> Dict[str, str]:
    key = str(provider or DEFAULT_PROVIDER).strip().lower()
    if key not in PROVIDERS:
        raise ValueError("不支持的 AI 字幕服务：{}".format(provider))
    return dict(PROVIDERS[key])


def log(message: str) -> None:
    if _LOG_CALLBACK:
        _LOG_CALLBACK(message)
    else:
        print(message, flush=True)


def set_log_callback(callback) -> None:
    global _LOG_CALLBACK
    _LOG_CALLBACK = callback


def require_tool(name: str) -> str:
    environment_names = {"yt-dlp": "YCA_YTDLP", "ffmpeg": "YCA_FFMPEG", "ffprobe": "YCA_FFPROBE"}
    configured = os.environ.get(environment_names.get(name, ""), "").strip()
    if configured and Path(configured).is_file():
        return configured
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        resource_dir = executable_dir.parent / "Resources"
        names = {
            "yt-dlp": ("yt-dlp.exe", "yt-dlp", "yt-dlp_macos"),
            "ffmpeg": ("ffmpeg.exe", "ffmpeg", "ffmpeg_macos"),
            "ffprobe": ("ffprobe.exe", "ffprobe", "ffprobe_macos"),
        }.get(name, (name,))
        for directory in (executable_dir, resource_dir):
            for filename in names:
                candidate = directory / filename
                if candidate.is_file():
                    return str(candidate)
    path = shutil.which(name)
    if not path:
        raise RuntimeError("缺少必要工具：{}".format(name))
    return path


def load_keychain_secret(service: str, account: str) -> str:
    """Read a macOS Keychain generic password without printing it."""
    security = shutil.which("security")
    if not security or sys.platform != "darwin":
        return ""
    completed = subprocess.run(
        [security, "find-generic-password", "-a", account, "-s", service, "-w"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.decode("utf-8", errors="replace").strip()


def run(command: Sequence[str], capture: bool = False) -> str:
    completed = subprocess.run(
        list(command),
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    return completed.stdout.strip() if capture else ""


def safe_name(value: str) -> str:
    clean = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)
    clean = clean.strip("_")
    return clean[:80] or "video"


def video_key(url: str) -> str:
    if "youtu.be/" in url:
        return safe_name(url.split("youtu.be/", 1)[1].split("?", 1)[0])
    if "v=" in url:
        return safe_name(url.split("v=", 1)[1].split("&", 1)[0])
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


def download_audio(video_url: str, work_dir: Path) -> Path:
    yt_dlp = require_tool("yt-dlp")
    template = str(work_dir / "youtube_source.%(ext)s")
    log("[1/5] 正在读取YouTube纯音频流（不会下载视频画面）……")
    run(
        [
            yt_dlp,
            "--no-playlist",
            "--no-warnings",
            "--format",
            "bestaudio/best",
            "--output",
            template,
            video_url,
        ]
    )
    candidates = sorted(work_dir.glob("youtube_source.*"))
    if not candidates:
        raise RuntimeError("yt-dlp没有生成临时音频文件")
    return candidates[0]


def transcode_audio(source: Path, work_dir: Path, kbps: int) -> Path:
    ffmpeg = require_tool("ffmpeg")
    output = work_dir / "speech_audio.mp3"
    log("[2/5] 正在转为16kHz单声道、{}kbps临时音频……".format(kbps))
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "{}k".format(kbps),
            str(output),
        ]
    )
    return output


def audio_duration(path: Path) -> float:
    ffprobe = require_tool("ffprobe")
    value = run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture=True,
    )
    return float(value)


def split_audio(
    audio: Path,
    work_dir: Path,
    duration: float,
    chunk_seconds: int,
    overlap_seconds: int,
    kbps: int,
) -> List[Tuple[Path, float]]:
    if duration <= chunk_seconds:
        return [(audio, 0.0)]
    if overlap_seconds >= chunk_seconds:
        raise ValueError("切片重叠时间必须小于切片长度")

    ffmpeg = require_tool("ffmpeg")
    chunks: List[Tuple[Path, float]] = []
    step = chunk_seconds - overlap_seconds
    start = 0.0
    index = 1
    while start < duration:
        remaining = duration - start
        length = min(float(chunk_seconds), remaining)
        chunk = work_dir / "chunk_{:03d}.mp3".format(index)
        run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                "{:.3f}".format(start),
                "-i",
                str(audio),
                "-t",
                "{:.3f}".format(length),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                "{}k".format(kbps),
                str(chunk),
            ]
        )
        chunks.append((chunk, start))
        if start + length >= duration:
            break
        start += step
        index += 1
    return chunks


def multipart_body(
    fields: Iterable[Tuple[str, str]], file_field: str, file_path: Path
) -> Tuple[bytes, str]:
    boundary = "----yca{}".format(uuid.uuid4().hex)
    body = bytearray()

    def add(value: bytes) -> None:
        body.extend(value)
        body.extend(b"\r\n")

    for name, value in fields:
        add(("--{}".format(boundary)).encode("ascii"))
        add(('Content-Disposition: form-data; name="{}"'.format(name)).encode("utf-8"))
        add(b"")
        add(value.encode("utf-8"))

    mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    add(("--{}".format(boundary)).encode("ascii"))
    add(
        (
            'Content-Disposition: form-data; name="{}"; filename="{}"'.format(
                file_field, file_path.name
            )
        ).encode("utf-8")
    )
    add(("Content-Type: {}".format(mime)).encode("ascii"))
    add(b"")
    body.extend(file_path.read_bytes())
    body.extend(b"\r\n")
    body.extend(("--{}--\r\n".format(boundary)).encode("ascii"))
    return bytes(body), boundary


def transcribe_audio(
    audio: Path,
    api_key: str,
    api_url: str,
    model: str,
    language: str,
    prompt: str,
    provider_name: str = "AI",
    retries: int = 3,
) -> Dict[str, Any]:
    fields = [
        ("model", model),
        ("response_format", "verbose_json"),
        ("temperature", "0"),
        ("timestamp_granularities[]", "segment"),
    ]
    if language and language != "auto":
        fields.append(("language", language))
    if prompt:
        fields.append(("prompt", prompt))
    body, boundary = multipart_body(fields, "file", audio)

    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            api_url,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer {}".format(api_key),
                "Content-Type": "multipart/form-data; boundary={}".format(boundary),
                "Accept": "application/json",
                "User-Agent": "YouTube-Caption-Archive-MVP/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            if exc.code not in (429, 500, 502, 503, 504) or attempt == retries:
                raise TranscriptionAPIError(
                    exc.code,
                    "{} API错误 HTTP {}：{}".format(provider_name, exc.code, message[:600]),
                )
            retry_after = exc.headers.get("Retry-After", "")
            try:
                wait_seconds = min(15.0, max(1.0, float(retry_after)))
            except ValueError:
                wait_seconds = float(2 ** (attempt - 1))
            log("API暂时不可用，{:.0f}秒后重试（{}/{}）……".format(wait_seconds, attempt, retries))
            time.sleep(wait_seconds)
        except urllib.error.URLError as exc:
            if attempt == retries:
                raise TranscriptionAPIError(0, "无法连接{} API：{}".format(provider_name, exc.reason))
            time.sleep(float(2 ** (attempt - 1)))
    raise RuntimeError("{} API调用失败".format(provider_name))


def groq_transcribe(
    audio: Path,
    api_key: str,
    api_url: str,
    model: str,
    language: str,
    prompt: str,
    retries: int = 3,
) -> Dict[str, Any]:
    """Compatibility wrapper retained for the original Groq MVP."""
    return transcribe_audio(audio, api_key, api_url, model, language, prompt, "Groq", retries)


def normalized_segments(payload: Dict[str, Any], offset: float) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for item in payload.get("segments") or []:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        result.append(
            {
                "start": round(float(item.get("start", 0.0)) + offset, 3),
                "end": round(float(item.get("end", 0.0)) + offset, 3),
                "text": text,
            }
        )
    if not result and payload.get("text"):
        result.append({"start": offset, "end": offset, "text": str(payload["text"]).strip()})
    return result


def merge_by_overlap_midpoint(
    chunk_segments: List[List[Dict[str, Any]]],
    offsets: List[float],
    chunk_seconds: int,
    overlap_seconds: int,
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for index, segments in enumerate(chunk_segments):
        start_boundary = -math.inf
        end_boundary = math.inf
        if index > 0:
            start_boundary = offsets[index] + overlap_seconds / 2.0
        if index + 1 < len(offsets):
            end_boundary = offsets[index + 1] + overlap_seconds / 2.0
        for segment in segments:
            midpoint = (float(segment["start"]) + float(segment["end"])) / 2.0
            if start_boundary <= midpoint < end_boundary:
                merged.append(segment)
    merged.sort(key=lambda item: (float(item["start"]), float(item["end"])))
    return merged


def srt_time(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, ms = divmod(remainder, 1000)
    return "{:02d}:{:02d}:{:02d},{:03d}".format(hours, minutes, secs, ms)


def vtt_time(seconds: float) -> str:
    return srt_time(seconds).replace(",", ".")


def write_outputs(
    output_dir: Path, segments: List[Dict[str, Any]], report: Dict[str, Any]
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "groq_segments.json").write_text(
        json.dumps({"segments": segments}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    srt_lines: List[str] = []
    vtt_lines: List[str] = ["WEBVTT", ""]
    text_lines: List[str] = []
    for index, segment in enumerate(segments, 1):
        start = float(segment["start"])
        end = float(segment["end"])
        text = str(segment["text"])
        srt_lines.extend(
            [str(index), "{} --> {}".format(srt_time(start), srt_time(end)), text, ""]
        )
        vtt_lines.extend(["{} --> {}".format(vtt_time(start), vtt_time(end)), text, ""])
        text_lines.append(text)
    (output_dir / "groq_transcript.srt").write_text("\n".join(srt_lines), encoding="utf-8")
    (output_dir / "groq_transcript.vtt").write_text("\n".join(vtt_lines), encoding="utf-8")
    (output_dir / "groq_transcript.txt").write_text("\n".join(text_lines), encoding="utf-8")
    (output_dir / "groq_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Groq云端字幕MVP（不会修改正式档案）")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--video-url", help="YouTube单条视频链接")
    source.add_argument("--audio-file", type=Path, help="已有本地音频，仅用于受控测试")
    parser.add_argument("--output", type=Path, default=Path("mvp/results"))
    parser.add_argument("--api-key-env", default="GROQ_API_KEY")
    parser.add_argument("--keychain-service", default=DEFAULT_KEYCHAIN_SERVICE)
    parser.add_argument("--keychain-account", default=getpass.getuser())
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--prompt", default="以下内容以中文为主，可能包含英文公司名、人物名、股票代码和科技术语。")
    parser.add_argument("--chunk-seconds", type=int, default=DEFAULT_CHUNK_SECONDS)
    parser.add_argument("--overlap-seconds", type=int, default=DEFAULT_OVERLAP_SECONDS)
    parser.add_argument("--audio-kbps", type=int, default=DEFAULT_AUDIO_KBPS)
    parser.add_argument("--keep-audio", action="store_true", help="仅调试时保留转码后的音频")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    api_key = os.environ.get(args.api_key_env, "").strip()
    key_source = "环境变量"
    if not api_key:
        api_key = load_keychain_secret(args.keychain_service, args.keychain_account)
        key_source = "macOS钥匙串"
    if not api_key:
        log("尚未找到Groq API Key，未下载或上传任何音频。")
        log("请使用MVP目录中的“保存 Groq API Key.app”，或设置环境变量 {}。".format(args.api_key_env))
        return 2
    log("已从{}安全读取Groq API Key。".format(key_source))

    source_id = video_key(args.video_url) if args.video_url else safe_name(args.audio_file.stem)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    result_dir = args.output.expanduser().resolve() / source_id / run_id
    result_dir.mkdir(parents=True, exist_ok=False)
    work_parent = Path(tempfile.mkdtemp(prefix="yca-groq-mvp-"))
    started = time.monotonic()
    audio: Optional[Path] = None

    try:
        if args.video_url:
            source = download_audio(args.video_url, work_parent)
        else:
            source = args.audio_file.expanduser().resolve()
            if not source.is_file():
                raise RuntimeError("找不到音频文件：{}".format(source))
        audio = transcode_audio(source, work_parent, args.audio_kbps)
        duration = audio_duration(audio)
        size_mb = audio.stat().st_size / (1024 * 1024)
        log("临时音频：{:.1f}分钟，{:.2f}MB".format(duration / 60.0, size_mb))
        chunks = split_audio(
            audio,
            work_parent,
            duration,
            args.chunk_seconds,
            args.overlap_seconds,
            args.audio_kbps,
        )
        log("[3/5] 共{}个音频片段，开始Groq转录……".format(len(chunks)))

        raw_payloads: List[Dict[str, Any]] = []
        all_chunk_segments: List[List[Dict[str, Any]]] = []
        offsets: List[float] = []
        for index, (chunk_path, offset) in enumerate(chunks, 1):
            log("  正在识别 {}/{}……".format(index, len(chunks)))
            payload = groq_transcribe(
                chunk_path,
                api_key,
                args.api_url,
                args.model,
                args.language,
                args.prompt,
            )
            raw_payloads.append(payload)
            offsets.append(offset)
            all_chunk_segments.append(normalized_segments(payload, offset))

        log("[4/5] 正在合并时间轴并生成SRT/VTT/JSON……")
        segments = merge_by_overlap_midpoint(
            all_chunk_segments,
            offsets,
            args.chunk_seconds,
            args.overlap_seconds,
        )
        elapsed = time.monotonic() - started
        report: Dict[str, Any] = {
            "mvp_version": "0.1",
            "provider": "groq",
            "model": args.model,
            "source": args.video_url or str(args.audio_file),
            "duration_seconds": round(duration, 3),
            "temporary_audio_megabytes": round(size_mb, 3),
            "chunk_count": len(chunks),
            "chunk_seconds": args.chunk_seconds,
            "overlap_seconds": args.overlap_seconds,
            "segment_count": len(segments),
            "elapsed_seconds": round(elapsed, 3),
            "estimated_usd": round(duration / 3600.0 * GROQ_TURBO_USD_PER_AUDIO_HOUR, 6)
            if args.model == DEFAULT_MODEL
            else None,
            "temporary_audio_retained": bool(args.keep_audio),
        }
        (result_dir / "groq_raw_chunks.json").write_text(
            json.dumps(raw_payloads, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        write_outputs(result_dir, segments, report)
        if args.keep_audio and audio:
            shutil.copy2(audio, result_dir / "comparison_audio.mp3")
        log("[5/5] MVP完成：{}".format(result_dir))
        log("临时音频将在退出前清理。")
        return 0
    except KeyboardInterrupt:
        log("测试已取消，正在清理临时音频。")
        return 130
    except Exception as exc:
        log("MVP失败：{}".format(exc))
        return 1
    finally:
        shutil.rmtree(str(work_parent), ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
