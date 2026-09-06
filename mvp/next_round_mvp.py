#!/usr/bin/env python3
"""Next-round Groq MVP: direct URL versus a true download/transcribe pipeline.

This experiment is deliberately isolated from the production archive.  It first
asks Groq to fetch YouTube's temporary audio URL directly.  It then streams the
same low-bitrate audio through ffmpeg, starts transcription as soon as a segment
closes, and merges the ordered results.  Temporary media is always removed.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from . import cloud_asr_mvp as base
    from . import fast_groq_mvp as fast
except ImportError:  # Allow direct execution from the mvp directory.
    import cloud_asr_mvp as base
    import fast_groq_mvp as fast


DEFAULT_TEST_URL = "https://www.youtube.com/watch?v=jpoMabs9t4s"
FORMAT_SELECTOR = "bestaudio[abr<=64][acodec^=opus]/bestaudio[abr<=64]/worstaudio/bestaudio"
PROBE_BYTES = 256 * 1024
METADATA_SOCKET_TIMEOUT_SECONDS = 15
METADATA_TOTAL_TIMEOUT_SECONDS = 75
DEFAULT_PROMPT = "以下内容可能包含中文、英文公司名、人物名、股票代码和科技术语。请按原语言准确转录。"
_LOG_CALLBACK = None


def log(message: str) -> None:
    if _LOG_CALLBACK:
        _LOG_CALLBACK(message)
    else:
        print(message, flush=True)


def set_log_callback(callback) -> None:
    global _LOG_CALLBACK
    _LOG_CALLBACK = callback
    base.set_log_callback(callback)


def media_subprocess_environment() -> Dict[str, str]:
    """Do not expose transcription secrets to yt-dlp or FFmpeg child processes."""
    environment = os.environ.copy()
    environment.pop("GROQ_API_KEY", None)
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("YCA_AI_API_KEY", None)
    return environment


def load_api_key() -> str:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        key = base.load_keychain_secret(base.DEFAULT_KEYCHAIN_SERVICE, getpass.getuser())
    if not key.startswith("gsk_") or len(key) < 20:
        raise RuntimeError("没有找到完整的 Groq API Key")
    return key


def audio_info(video_url: str) -> Dict[str, Any]:
    yt_dlp = base.require_tool("yt-dlp")
    try:
        completed = subprocess.run(
            [
                yt_dlp,
                "--no-playlist",
                "--no-warnings",
                "--no-progress",
                "--socket-timeout",
                str(METADATA_SOCKET_TIMEOUT_SECONDS),
                "--extractor-retries",
                "2",
                "--retries",
                "2",
                "--skip-download",
                "--dump-single-json",
                video_url,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=METADATA_TOTAL_TIMEOUT_SECONDS,
            env=media_subprocess_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "读取 YouTube 视频信息超过 {} 秒。请检查网络后重试；尚未下载音频，也未调用 Groq。".format(
                METADATA_TOTAL_TIMEOUT_SECONDS
            )
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError("yt-dlp 无法读取音频信息：{}".format(detail[-500:]))
    payload = json.loads(completed.stdout.decode("utf-8"))
    candidates: List[Dict[str, Any]] = []
    for item in payload.get("formats") or []:
        url = str(item.get("url") or "")
        acodec = str(item.get("acodec") or "none")
        vcodec = str(item.get("vcodec") or "none")
        if not url.startswith(("http://", "https://")) or acodec == "none" or vcodec != "none":
            continue
        abr_value = item.get("abr") or item.get("tbr") or 9999
        try:
            abr = float(abr_value)
        except (TypeError, ValueError):
            abr = 9999.0
        candidates.append(
            {
                "format_id": str(item.get("format_id") or ""),
                "url": url,
                "ext": str(item.get("ext") or ""),
                "acodec": acodec,
                "abr": abr,
                "filesize": item.get("filesize") or item.get("filesize_approx"),
                "http_headers": item.get("http_headers") or {},
            }
        )
    if not candidates:
        raise RuntimeError("yt-dlp 没有返回可用的纯音频格式")
    # Probe only the smallest practical streams.  This avoids testing numerous
    # near-identical language/client variants while retaining fallbacks.
    candidates.sort(key=lambda item: (item["abr"], 0 if item["acodec"].startswith("opus") else 1))
    probe_pool: List[Dict[str, Any]] = []
    seen = set()
    for candidate in candidates:
        identity = (candidate["format_id"], candidate["ext"], round(candidate["abr"], 1))
        if identity in seen:
            continue
        seen.add(identity)
        probe_pool.append(candidate)
        if len(probe_pool) >= 4:
            break
    for candidate in probe_pool:
        candidate.update(probe_audio_candidate(candidate))
    probe_pool.sort(
        key=lambda item: (
            0 if item.get("probe_ok") else 1,
            -float(item.get("media_speed_ratio") or 0.0),
            item["abr"],
        )
    )
    selected = probe_pool[0]
    return {
        "url": selected["url"],
        "duration": float(payload.get("duration") or 0.0),
        "format_id": selected["format_id"],
        "ext": selected["ext"],
        "abr": selected["abr"],
        "media_speed_ratio": selected.get("media_speed_ratio"),
        "candidates": probe_pool,
    }


def probe_audio_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    headers = {
        str(key): str(value)
        for key, value in (candidate.get("http_headers") or {}).items()
        if str(key).lower() not in {"range", "accept-encoding"}
    }
    headers["Range"] = "bytes=0-{}".format(PROBE_BYTES - 1)
    started = time.monotonic()
    received = 0
    try:
        request = urllib.request.Request(str(candidate["url"]), headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            while received < PROBE_BYTES:
                block = response.read(min(64 * 1024, PROBE_BYTES - received))
                if not block:
                    break
                received += len(block)
        elapsed = max(0.001, time.monotonic() - started)
        throughput_kbps = received * 8.0 / 1000.0 / elapsed
        abr = max(1.0, float(candidate.get("abr") or 64.0))
        return {
            "probe_ok": received > 0,
            "probe_seconds": round(elapsed, 3),
            "probe_kbps": round(throughput_kbps, 1),
            "media_speed_ratio": round(throughput_kbps / abr, 2),
        }
    except Exception as exc:
        return {
            "probe_ok": False,
            "probe_seconds": round(time.monotonic() - started, 3),
            "probe_error": type(exc).__name__,
            "media_speed_ratio": 0.0,
        }


def multipart_fields(fields: Sequence[Tuple[str, str]]) -> Tuple[bytes, str]:
    boundary = "----yca{}".format(uuid.uuid4().hex)
    body = bytearray()
    for name, value in fields:
        body.extend(("--{}\r\n".format(boundary)).encode("ascii"))
        body.extend(
            ('Content-Disposition: form-data; name="{}"\r\n\r\n'.format(name)).encode(
                "utf-8"
            )
        )
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")
    body.extend(("--{}--\r\n".format(boundary)).encode("ascii"))
    return bytes(body), boundary


def transcribe_url(
    direct_url: str,
    api_key: str,
    model: str,
    language: str,
    prompt: str,
) -> Dict[str, Any]:
    fields = [
        ("model", model),
        ("url", direct_url),
        ("response_format", "verbose_json"),
        ("temperature", "0"),
        ("timestamp_granularities[]", "segment"),
    ]
    if language and language != "auto":
        fields.append(("language", language))
    if prompt:
        fields.append(("prompt", prompt))
    body, boundary = multipart_fields(fields)
    request = urllib.request.Request(
        base.DEFAULT_API_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer {}".format(api_key),
            "Content-Type": "multipart/form-data; boundary={}".format(boundary),
            "Accept": "application/json",
            "User-Agent": "YouTube-Caption-Archive-Next-MVP/0.3",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("Groq URL 模式返回 HTTP {}：{}".format(exc.code, detail[:500]))
    except urllib.error.URLError as exc:
        raise RuntimeError("Groq URL 模式连接失败：{}".format(exc.reason))


def resolve_media_redirect(direct_url: str) -> str:
    """Resolve YouTube's first redirect locally without downloading the media."""
    request = urllib.request.Request(
        direct_url,
        method="GET",
        headers={
            "Range": "bytes=0-0",
            "User-Agent": "Mozilla/5.0 YouTube-Caption-Archive-Next-MVP/0.3",
        },
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        resolved = response.geturl()
    if not resolved.startswith(("http://", "https://")):
        raise RuntimeError("YouTube 重定向后没有返回可用地址")
    return resolved


def write_direct_result(
    result_dir: Path,
    video_url: str,
    info: Dict[str, Any],
    payload: Dict[str, Any],
    elapsed: float,
) -> Dict[str, Any]:
    segments = base.normalized_segments(payload, 0.0)
    report = {
        "mvp_version": "0.3-direct-url",
        "source": video_url,
        "provider": "groq",
        "model": base.DEFAULT_MODEL,
        "duration_seconds": round(float(info["duration"]), 3),
        "source_format_id": info["format_id"],
        "source_ext": info["ext"],
        "source_abr_kbps": info["abr"],
        "segment_count": len(segments),
        "total_seconds": round(elapsed, 3),
        "local_audio_downloaded": False,
        "temporary_audio_url_saved": False,
    }
    result_dir.mkdir(parents=True, exist_ok=False)
    (result_dir / "groq_raw.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    base.write_outputs(result_dir, segments, report)
    return report


def choose_first_chunk_seconds(duration: float, media_speed_ratio: Optional[float]) -> int:
    """Aim for the first closed piece in roughly 10–15 wall-clock seconds."""
    if duration <= 300:
        return max(60, int(duration))
    ratio = max(0.0, float(media_speed_ratio or 0.0))
    raw = ratio * 12.0
    snapped = int(round(raw / 60.0) * 60)
    return min(300, max(120, snapped))


def adaptive_cut_times(duration: float, first_chunk_seconds: int = 300) -> List[int]:
    """First result after 5 minutes; later pieces are 10 minutes long."""
    if duration <= first_chunk_seconds:
        return []
    cuts = [first_chunk_seconds]
    point = first_chunk_seconds + 600
    while point < duration:
        cuts.append(point)
        point += 600
    return cuts


def segment_offset(index: int, cuts: Sequence[int]) -> float:
    return 0.0 if index == 0 else float(cuts[index - 1])


def upload_offset(index: int, cuts: Sequence[int], overlap_seconds: int) -> float:
    return max(0.0, segment_offset(index, cuts) - (overlap_seconds if index else 0))


def plan_signature(
    video_url: str,
    duration: float,
    cuts: Sequence[int],
    overlap_seconds: int,
    model: str,
    language: str,
    prompt: str = "",
) -> str:
    value = json.dumps(
        {
            "video": base.video_key(video_url),
            "duration": round(duration, 1),
            "cuts": list(cuts),
            "overlap": overlap_seconds,
            "model": model,
            "language": language,
            "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class CaptionCheckpoint:
    """Persist only successful API responses; never persist audio or secrets."""

    def __init__(self, directory: Path):
        self.directory = directory

    def load(self) -> Dict[int, Dict[str, Any]]:
        loaded: Dict[int, Dict[str, Any]] = {}
        if not self.directory.is_dir():
            return loaded
        for path in sorted(self.directory.glob("chunk_*.json")):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                index = int(item["index"])
                if isinstance(item.get("payload"), dict):
                    loaded[index] = item
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
        return loaded

    def save(self, index: int, offset: float, payload: Dict[str, Any], api_seconds: float) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / "chunk_{:03d}.json".format(index)
        temporary = self.directory / ".chunk_{:03d}.{}.tmp".format(index, os.getpid())
        temporary.write_text(
            json.dumps(
                {
                    "index": index,
                    "offset": round(offset, 3),
                    "api_seconds": round(api_seconds, 3),
                    "payload": payload,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(destination)

    def cleanup(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def prepare_overlapped_upload(
    index: int,
    source: Path,
    cuts: Sequence[int],
    overlap_seconds: int,
    ffmpeg: str,
    work_dir: Path,
) -> Tuple[Path, float]:
    """Save this piece's tail and prepend the previous tail to later pieces."""
    tail = work_dir / "tail_{:03d}.m4a".format(index)
    tail_command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-sseof",
        "-{}".format(overlap_seconds),
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "32k",
        "-c:a",
        "aac",
        "-f",
        "mov",
        str(tail),
    ]
    subprocess.run(tail_command, check=True, env=media_subprocess_environment())
    if index == 0:
        return source, 0.0

    previous_tail = work_dir / "tail_{:03d}.m4a".format(index - 1)
    if not previous_tail.is_file():
        raise RuntimeError("找不到上一切片的交界上下文")
    upload = work_dir / "upload_{:03d}.m4a".format(index)
    join_command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(previous_tail),
        "-i",
        str(source),
        "-filter_complex",
        "[0:a][1:a]concat=n=2:v=0:a=1[out]",
        "-map",
        "[out]",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "32k",
        "-c:a",
        "aac",
        "-f",
        "mov",
        str(upload),
    ]
    subprocess.run(join_command, check=True, env=media_subprocess_environment())
    previous_tail.unlink(missing_ok=True)
    source.unlink(missing_ok=True)
    return upload, upload_offset(index, cuts, overlap_seconds)


def transcribe_segment(
    path: Path,
    offset: float,
    api_key: str,
    language: str,
    prompt: str,
    provider: str,
    api_url: str,
    model: str,
    provider_name: str,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], float]:
    started = time.monotonic()
    payload = base.transcribe_audio(
        path,
        api_key,
        api_url,
        model,
        language,
        prompt,
        provider_name,
    )
    elapsed = time.monotonic() - started
    # Segment timestamps are faster than word timestamps.  Split long display
    # cues locally while preserving Groq's exact recognized text.
    return payload, fast.fine_segments_from_payload(payload, offset), elapsed


def stream_pipeline(
    video_url: str,
    duration: float,
    api_key: str,
    result_dir: Path,
    language: str,
    prompt: str,
    concurrency: int,
    format_id: str,
    first_chunk_seconds: int,
    checkpoint_dir: Optional[Path] = None,
    provider: str = base.DEFAULT_PROVIDER,
) -> Dict[str, Any]:
    provider_settings = base.provider_config(provider)
    provider_name = provider_settings["name"]
    api_url = provider_settings["api_url"]
    model = provider_settings["model"]
    yt_dlp = base.require_tool("yt-dlp")
    ffmpeg = base.require_tool("ffmpeg")
    work_dir = Path(tempfile.mkdtemp(prefix="yca-stream-mvp-"))
    cuts = adaptive_cut_times(duration, first_chunk_seconds)
    overlap_seconds = 10
    signature = plan_signature(
        video_url, duration, cuts, overlap_seconds, "{}:{}".format(provider, model), language, prompt
    )
    checkpoint = CaptionCheckpoint((checkpoint_dir or result_dir / "checkpoint") / signature)
    pattern = work_dir / "chunk_%03d.m4a"
    yt_command = [
        yt_dlp,
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--format",
        format_id or FORMAT_SELECTOR,
        "--output",
        "-",
        video_url,
    ]
    ffmpeg_command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-vn",
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "32k",
        "-c:a",
        "aac",
        "-f",
        "segment",
        "-segment_format",
        "mov",
        "-reset_timestamps",
        "1",
    ]
    if cuts:
        ffmpeg_command.extend(["-segment_times", ",".join(str(value) for value in cuts)])
    else:
        ffmpeg_command.extend(["-segment_time", "3600"])
    ffmpeg_command.append(str(pattern))

    started = time.monotonic()
    first_result_seconds: Optional[float] = None
    producer_seconds = 0.0
    submitted: Dict[int, Future[Tuple[Dict[str, Any], List[Dict[str, Any]], float]]] = {}
    submitted_paths: Dict[int, Path] = {}
    input_offsets: Dict[int, float] = {}
    raw_payloads: Dict[int, Dict[str, Any]] = {}
    chunk_segments: Dict[int, List[Dict[str, Any]]] = {}
    api_seconds: Dict[int, float] = {}
    lock = threading.Lock()
    restored = checkpoint.load()
    for index, item in restored.items():
        offset = float(item.get("offset") or 0.0)
        payload = item["payload"]
        raw_payloads[index] = payload
        input_offsets[index] = offset
        chunk_segments[index] = fast.fine_segments_from_payload(payload, offset)
        api_seconds[index] = float(item.get("api_seconds") or 0.0)
    if restored:
        log("已恢复 {} 个完成片段；这些片段不会再次调用 {}。".format(len(restored), provider_name))

    def submit_closed(executor: ThreadPoolExecutor, include_last: bool) -> None:
        paths = sorted(work_dir.glob("chunk_*.m4a"))
        eligible = paths if include_last else paths[:-1]
        for path in eligible:
            index = int(path.stem.rsplit("_", 1)[1])
            if index in submitted or path.stat().st_size == 0:
                continue
            upload, offset = prepare_overlapped_upload(
                index, path, cuts, overlap_seconds, ffmpeg, work_dir
            )
            if index in raw_payloads:
                input_offsets[index] = offset
                upload.unlink(missing_ok=True)
                log("  切片 {} 已从断点恢复，跳过 {}。".format(index + 1, provider_name))
                continue
            log("  切片 {} 已就绪，立即提交 {}（含前段 {} 秒上下文）……".format(
                index + 1, provider_name, overlap_seconds if index else 0
            ))
            submitted_paths[index] = upload
            input_offsets[index] = offset
            submitted[index] = executor.submit(
                transcribe_segment, upload, offset, api_key, language, prompt,
                provider, api_url, model, provider_name
            )

    def collect_finished() -> None:
        nonlocal first_result_seconds
        for index, future in list(submitted.items()):
            if index in raw_payloads or not future.done():
                continue
            payload, segments, elapsed = future.result()
            with lock:
                raw_payloads[index] = payload
                chunk_segments[index] = segments
                api_seconds[index] = elapsed
                if first_result_seconds is None:
                    first_result_seconds = time.monotonic() - started
                checkpoint.save(index, input_offsets[index], payload, elapsed)
            submitted_paths[index].unlink(missing_ok=True)
            log("  切片 {} 识别完成，用时 {:.1f} 秒。".format(index + 1, elapsed))

    yt_process: Optional[subprocess.Popen[bytes]] = None
    ffmpeg_process: Optional[subprocess.Popen[bytes]] = None
    try:
        log("开始流水线：YouTube 下载、FFmpeg 切片和 {} 识别将重叠执行。".format(provider_name))
        yt_process = subprocess.Popen(
            yt_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=media_subprocess_environment(),
        )
        if yt_process.stdout is None:
            raise RuntimeError("无法创建 yt-dlp 输出流")
        ffmpeg_process = subprocess.Popen(
            ffmpeg_command,
            stdin=yt_process.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=media_subprocess_environment(),
        )
        yt_process.stdout.close()

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            while ffmpeg_process.poll() is None:
                submit_closed(executor, include_last=False)
                collect_finished()
                time.sleep(0.15)

            producer_seconds = time.monotonic() - started
            yt_code = yt_process.wait()
            yt_error = (
                yt_process.stderr.read().decode("utf-8", errors="replace")
                if yt_process.stderr
                else ""
            )
            ff_error = (
                ffmpeg_process.stderr.read().decode("utf-8", errors="replace")
                if ffmpeg_process.stderr
                else ""
            )
            if yt_code != 0 or ffmpeg_process.returncode != 0:
                raise RuntimeError(
                    "流式音频处理失败：{} {}".format(yt_error[-400:], ff_error[-400:]).strip()
                )
            submit_closed(executor, include_last=True)
            while any(index not in raw_payloads for index in submitted):
                collect_finished()
                time.sleep(0.05)

        indexes = sorted(raw_payloads)
        offsets = [input_offsets[index] for index in indexes]
        ordered_segments = [chunk_segments[index] for index in indexes]
        merged = fast.merge_fine_segments(ordered_segments, offsets, overlap_seconds)
        total_seconds = time.monotonic() - started
        report = {
            "mvp_version": "0.3-stream-pipeline",
            "source": video_url,
            "provider": provider,
            "provider_name": provider_name,
            "model": model,
            "duration_seconds": round(duration, 3),
            "cut_strategy_seconds": [0] + cuts,
            "selected_format_id": format_id,
            "first_chunk_seconds": first_chunk_seconds,
            "overlap_seconds": overlap_seconds,
            "chunk_count": len(indexes),
            "concurrency": concurrency,
            "timestamp_granularity": "segment",
            "first_transcript_ready_seconds": round(first_result_seconds or total_seconds, 3),
            "download_and_transcode_pipeline_seconds": round(producer_seconds, 3),
            "individual_api_seconds": [round(api_seconds[index], 3) for index in indexes],
            "total_seconds": round(total_seconds, 3),
            "segment_count": len(merged),
            "temporary_audio_retained": False,
            "restored_chunk_count": len(restored),
            "checkpoint_signature": signature,
        }
        result_dir.mkdir(parents=True, exist_ok=False)
        (result_dir / "groq_raw_chunks.json").write_text(
            json.dumps([raw_payloads[index] for index in indexes], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        base.write_outputs(result_dir, merged, report)
        checkpoint.cleanup()
        return report
    finally:
        for process in (ffmpeg_process, yt_process):
            if process is not None and process.poll() is None:
                process.terminate()
        shutil.rmtree(work_dir, ignore_errors=True)


def transcribe_video(
    video_url: str,
    api_key: str,
    checkpoint_root: Path,
    language: str = "auto",
    prompt: str = DEFAULT_PROMPT,
    concurrency: int = 2,
    logger=None,
    provider: str = base.DEFAULT_PROVIDER,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Production entry point: return timestamped segments without retaining audio."""
    key = str(api_key or "").strip()
    provider_settings = base.provider_config(provider)
    provider_name = provider_settings["name"]
    key_prefix = provider_settings["key_prefix"]
    if not key.startswith(key_prefix) or len(key) < 20:
        raise base.TranscriptionAPIError(401, "{} API Key为空或格式不完整。".format(provider_name))
    if concurrency not in (1, 2, 3):
        raise ValueError("并发数必须为 1、2 或 3")

    set_log_callback(logger)
    try:
        # Fail once, before source probing, if the installed application is incomplete.
        base.require_tool("yt-dlp")
        base.require_tool("ffmpeg")
        log("正在解析低码率纯音频地址……")
        info = audio_info(video_url)
        duration = float(info["duration"])
        first_chunk_seconds = choose_first_chunk_seconds(duration, info.get("media_speed_ratio"))
        log(
            "视频时长 {:.1f} 分钟；选择音频格式 {}；首段 {} 分钟。".format(
                duration / 60.0,
                info["format_id"],
                max(1, round(first_chunk_seconds / 60)),
            )
        )

        candidate_ids = [str(item["format_id"]) for item in info.get("candidates") or []]
        if info["format_id"] not in candidate_ids:
            candidate_ids.insert(0, info["format_id"])
        source_errors: List[str] = []
        report: Optional[Dict[str, Any]] = None
        segments: List[Dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="yca-ai-result-") as temporary:
            temporary_root = Path(temporary)
            for attempt, format_id in enumerate(candidate_ids, 1):
                try:
                    log("音频源尝试 {}/{}：格式 {}。".format(attempt, len(candidate_ids), format_id))
                    result_dir = temporary_root / "result-{:02d}".format(attempt)
                    report = stream_pipeline(
                        video_url,
                        duration,
                        key,
                        result_dir,
                        language,
                        prompt,
                        concurrency,
                        format_id,
                        first_chunk_seconds,
                        Path(checkpoint_root),
                        provider,
                    )
                    report["audio_source_attempt"] = attempt
                    report["audio_source_fallbacks"] = attempt - 1
                    payload = json.loads((result_dir / "groq_segments.json").read_text(encoding="utf-8"))
                    segments = payload.get("segments") or []
                    break
                except base.TranscriptionAPIError:
                    # Changing the YouTube format cannot fix an API key, quota, or provider outage.
                    raise
                except Exception as exc:
                    source_errors.append("{}: {}".format(format_id, exc))
                    log("音频源 {} 失败，自动尝试下一个来源。".format(format_id))
            if report is None:
                raise RuntimeError("所有候选音频源均失败：{}".format(" | ".join(source_errors)))
        try:
            checkpoint_parent = Path(checkpoint_root)
            if checkpoint_parent.is_dir() and not any(checkpoint_parent.iterdir()):
                checkpoint_parent.rmdir()
        except OSError:
            pass
        return segments, report
    finally:
        set_log_callback(None)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Groq 下一轮端到端提速实验")
    parser.add_argument("--video-url", default=DEFAULT_TEST_URL)
    parser.add_argument("--output", type=Path, default=Path("mvp/results"))
    parser.add_argument("--language", default="zh")
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
    )
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--try-direct-url", action="store_true", help="诊断性测试；YouTube目前通常返回302")
    parser.add_argument("--skip-direct-url", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--direct-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.concurrency not in (1, 2, 3):
        raise ValueError("并发数必须为 1、2 或 3")
    api_key = load_api_key()
    source_id = base.video_key(args.video_url)
    run_root = (
        args.output.expanduser().resolve()
        / source_id
        / ("next-round-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    )
    run_root.mkdir(parents=True, exist_ok=False)
    log("[准备] 正在解析低码率纯音频地址……")
    info = audio_info(args.video_url)
    duration = float(info["duration"])
    log("视频时长 {:.1f} 分钟；音频格式 {}，约 {} kbps。".format(
        duration / 60.0, info["ext"] or "未知", info["abr"] or "未知"
    ))
    first_chunk_seconds = choose_first_chunk_seconds(duration, info.get("media_speed_ratio"))
    log("自动测速后选择音频格式 {}；首段 {} 分钟。".format(
        info["format_id"], max(1, round(first_chunk_seconds / 60))
    ))

    summary: Dict[str, Any] = {
        "source": args.video_url,
        "duration_seconds": duration,
        "audio_source_candidates": [
            {
                key: value
                for key, value in candidate.items()
                if key not in {"url", "http_headers"}
            }
            for candidate in info.get("candidates") or []
        ],
    }
    if args.direct_only or (args.try_direct_url and not args.skip_direct_url):
        log("\n[实验 A] Groq 直接读取 YouTube 临时音频地址……")
        direct_started = time.monotonic()
        try:
            resolved_url = resolve_media_redirect(str(info["url"]))
            payload = transcribe_url(
                resolved_url, api_key, base.DEFAULT_MODEL, args.language, args.prompt
            )
            direct_report = write_direct_result(
                run_root / "direct-url",
                args.video_url,
                info,
                payload,
                time.monotonic() - direct_started,
            )
            summary["direct_url"] = {"status": "success", **direct_report}
            log("直接 URL 成功：{:.1f} 秒。".format(direct_report["total_seconds"]))
        except Exception as exc:
            summary["direct_url"] = {"status": "failed", "error": str(exc)}
            log("直接 URL 不可用，将继续流水线实验：{}".format(exc))

    if args.direct_only:
        (run_root / "comparison_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log("\n直接 URL 实验完成：{}".format(run_root))
        return 0

    log("\n[实验 B] 真正的下载/切片/Groq 流水线……")
    candidate_ids = [str(item["format_id"]) for item in info.get("candidates") or []]
    if info["format_id"] not in candidate_ids:
        candidate_ids.insert(0, info["format_id"])
    pipeline_report: Optional[Dict[str, Any]] = None
    source_errors: List[str] = []
    for attempt, format_id in enumerate(candidate_ids, 1):
        try:
            log("音频源尝试 {}/{}：格式 {}。".format(attempt, len(candidate_ids), format_id))
            pipeline_report = stream_pipeline(
                args.video_url,
                duration,
                api_key,
                run_root / "stream-pipeline",
                args.language,
                args.prompt,
                args.concurrency,
                format_id,
                first_chunk_seconds,
                args.output.expanduser().resolve() / source_id / "resume-checkpoints",
            )
            pipeline_report["audio_source_attempt"] = attempt
            pipeline_report["audio_source_fallbacks"] = attempt - 1
            break
        except Exception as exc:
            source_errors.append("{}: {}".format(format_id, exc))
            log("音频源 {} 失败，自动尝试下一个来源：{}".format(format_id, exc))
    if pipeline_report is None:
        raise RuntimeError("所有候选音频源均失败：{}".format(" | ".join(source_errors)))
    summary["stream_pipeline"] = {"status": "success", **pipeline_report}
    (run_root / "comparison_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log("\n全部实验完成：{}".format(run_root))
    log("流水线首段字幕：{:.1f} 秒；完整完成：{:.1f} 秒。".format(
        pipeline_report["first_transcript_ready_seconds"], pipeline_report["total_seconds"]
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
