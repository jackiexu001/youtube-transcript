#!/usr/bin/env python3
"""Independent high-speed Groq MVP for YouTube Caption Archive 2.0.

No VideoCaptioner modules are imported. The script keeps the useful architecture
validated by the comparison: overlapping chunks, bounded parallel requests,
fine-grained timestamps, deterministic merge, and automatic audio cleanup.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from . import cloud_asr_mvp as base
except ImportError:  # Allow direct execution from the mvp directory.
    import cloud_asr_mvp as base


DEFAULT_TEST_URL = "https://www.youtube.com/watch?v=jpoMabs9t4s"


def log(message: str) -> None:
    print(message, flush=True)


def download_speech_audio(video_url: str, work_dir: Path) -> Path:
    """Prefer YouTube's smallest speech-suitable audio stream."""
    yt_dlp = base.require_tool("yt-dlp")
    template = str(work_dir / "youtube_source.%(ext)s")
    log("[1/5] 正在获取低码率纯音频（不会下载视频画面）……")
    base.run(
        [
            yt_dlp,
            "--no-playlist",
            "--no-warnings",
            "--format",
            "bestaudio[abr<=64][acodec^=opus]/bestaudio[abr<=64]/worstaudio/bestaudio",
            "--output",
            template,
            video_url,
        ]
    )
    candidates = sorted(work_dir.glob("youtube_source.*"))
    if not candidates:
        raise RuntimeError("yt-dlp 没有生成临时音频文件")
    return candidates[0]


def transcribe_fine(
    audio: Path,
    api_key: str,
    api_url: str,
    model: str,
    language: str,
    prompt: str,
    retries: int = 3,
) -> Dict[str, Any]:
    fields = [
        ("model", model),
        ("response_format", "verbose_json"),
        ("temperature", "0"),
        ("timestamp_granularities[]", "word"),
        ("timestamp_granularities[]", "segment"),
    ]
    if language and language != "auto":
        fields.append(("language", language))
    if prompt:
        fields.append(("prompt", prompt))
    body, boundary = base.multipart_body(fields, "file", audio)

    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            api_url,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer {}".format(api_key),
                "Content-Type": "multipart/form-data; boundary={}".format(boundary),
                "Accept": "application/json",
                "User-Agent": "YouTube-Caption-Archive-Fast-MVP/0.2",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            if exc.code not in (429, 500, 502, 503, 504) or attempt == retries:
                raise RuntimeError("Groq API 错误 HTTP {}：{}".format(exc.code, message[:600]))
            retry_after = exc.headers.get("Retry-After", "")
            try:
                wait_seconds = min(15.0, max(1.0, float(retry_after)))
            except ValueError:
                wait_seconds = float(2 ** (attempt - 1))
            log("API 暂时不可用，{:.0f} 秒后重试（{}/{}）……".format(
                wait_seconds, attempt, retries
            ))
            time.sleep(wait_seconds)
        except urllib.error.URLError as exc:
            if attempt == retries:
                raise RuntimeError("无法连接 Groq API：{}".format(exc.reason))
            time.sleep(float(2 ** (attempt - 1)))
    raise RuntimeError("Groq API 调用失败")


def merge_fine_segments(
    chunk_segments: List[List[Dict[str, Any]]],
    offsets: List[float],
    overlap_seconds: int,
) -> List[Dict[str, Any]]:
    """Merge by ownership of each overlap half, then enforce a continuous timeline."""
    selected: List[Dict[str, Any]] = []
    for index, segments in enumerate(chunk_segments):
        start_boundary = -math.inf if index == 0 else offsets[index] + overlap_seconds / 2.0
        end_boundary = (
            math.inf
            if index + 1 == len(offsets)
            else offsets[index + 1] + overlap_seconds / 2.0
        )
        for segment in segments:
            midpoint = (float(segment["start"]) + float(segment["end"])) / 2.0
            if start_boundary <= midpoint < end_boundary:
                selected.append(dict(segment))

    selected.sort(key=lambda item: (float(item["start"]), float(item["end"])))
    merged: List[Dict[str, Any]] = []
    for item in selected:
        start = max(0.0, float(item["start"]))
        end = max(start + 0.001, float(item["end"]))
        text = str(item["text"]).strip()
        if not text:
            continue
        if merged and start < float(merged[-1]["end"]):
            boundary = max(float(merged[-1]["start"]) + 0.001, start)
            merged[-1]["end"] = round(boundary, 3)
            start = boundary
            end = max(start + 0.001, end)
        merged.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return merged


def join_recognized_words(words: List[str]) -> str:
    result = ""
    previous = ""
    for raw in words:
        token = str(raw).strip()
        if not token:
            continue
        separator = ""
        if result and previous:
            prev_ascii = bool(re.search(r"[A-Za-z0-9]$", previous))
            next_ascii = bool(re.match(r"^[A-Za-z0-9]", token))
            is_fragment = (
                len(previous.rstrip(".,!?;:")) == 1
                and previous.rstrip(".,!?;:").isupper()
                and token[:1].islower()
            )
            if prev_ascii and next_ascii and not is_fragment:
                separator = " "
        result += separator + token
        previous = token
    return result


def fine_segments_from_words(
    payload: Dict[str, Any],
    offset: float,
    target_seconds: float = 3.2,
    max_seconds: float = 5.0,
) -> List[Dict[str, Any]]:
    """Build short learning-friendly cues from Groq word timestamps."""
    words = payload.get("words") or []
    if not words:
        return base.normalized_segments(payload, offset)

    result: List[Dict[str, Any]] = []
    group: List[Dict[str, Any]] = []
    for word in words:
        text = str(word.get("word") or "").strip()
        if not text:
            continue
        group.append(word)
        duration = float(group[-1].get("end", 0.0)) - float(group[0].get("start", 0.0))
        sentence_end = bool(re.search(r"[。！？!?]$", text)) and duration >= 1.2
        phrase_end = bool(re.search(r"[，,；;：:]$", text)) and duration >= target_seconds
        hard_limit = duration >= max_seconds or sum(len(str(item.get("word") or "")) for item in group) >= 48
        if sentence_end or phrase_end or hard_limit:
            result.append(
                {
                    "start": round(float(group[0].get("start", 0.0)) + offset, 3),
                    "end": round(float(group[-1].get("end", 0.0)) + offset, 3),
                    "text": join_recognized_words([str(item.get("word") or "") for item in group]),
                }
            )
            group = []
    if group:
        result.append(
            {
                "start": round(float(group[0].get("start", 0.0)) + offset, 3),
                "end": round(float(group[-1].get("end", 0.0)) + offset, 3),
                "text": join_recognized_words([str(item.get("word") or "") for item in group]),
            }
        )
    return result


def split_display_text(text: str, target_chars: int = 30, max_chars: int = 46) -> List[str]:
    """Split exact segment text without breaking words when a delimiter is available."""
    remaining = text.strip()
    chunks: List[str] = []
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = 0
        for match in re.finditer(r"[。！？!?；;]", window):
            if match.end() >= 8:
                cut = match.end()
                break
        if not cut:
            for match in re.finditer(r"[，,：:]", window):
                if match.end() >= target_chars:
                    cut = match.end()
                    break
        if not cut:
            delimiter_positions = [
                index + 1
                for index, char in enumerate(window)
                if char.isspace() or char in "，,：:"
            ]
            eligible = [position for position in delimiter_positions if position >= 8]
            cut = eligible[-1] if eligible else 0
        if not cut:
            extended = remaining[: min(len(remaining), max_chars + 12)]
            later = re.search(r"[。！？!?；;，,：:\s]", extended[max_chars:])
            cut = max_chars + later.end() if later else max_chars
        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    cleaned: List[str] = []
    for chunk in chunks:
        if cleaned and re.fullmatch(r"[。！？!?；;，,：:\s]+", chunk):
            cleaned[-1] += chunk.strip()
        else:
            cleaned.append(chunk)
    return cleaned


def fine_segments_from_payload(payload: Dict[str, Any], offset: float) -> List[Dict[str, Any]]:
    """Preserve recognized wording and use word timings only to place short cue boundaries."""
    api_segments = payload.get("segments") or []
    words = payload.get("words") or []
    if not api_segments:
        return fine_segments_from_words(payload, offset)

    result: List[Dict[str, Any]] = []
    for segment in api_segments:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        seg_start = float(segment.get("start", 0.0))
        seg_end = max(seg_start + 0.001, float(segment.get("end", seg_start)))
        chunks = split_display_text(text)
        if len(chunks) == 1:
            result.append({"start": round(seg_start + offset, 3), "end": round(seg_end + offset, 3), "text": text})
            continue

        relevant_words = [
            word
            for word in words
            if seg_start - 0.25
            <= (float(word.get("start", 0.0)) + float(word.get("end", 0.0))) / 2.0
            <= seg_end + 0.25
        ]
        text_units = [max(1, len(re.sub(r"\s+", "", chunk))) for chunk in chunks]
        total_text_units = sum(text_units)
        word_units = [
            max(1, len(re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", str(word.get("word") or ""))))
            for word in relevant_words
        ]
        total_word_units = sum(word_units)

        cue_start = seg_start
        cumulative_text = 0
        word_index = 0
        cumulative_words = 0
        for index, chunk in enumerate(chunks):
            cumulative_text += text_units[index]
            if index + 1 == len(chunks):
                cue_end = seg_end
            elif relevant_words and total_word_units:
                target_word_units = total_word_units * cumulative_text / total_text_units
                while word_index < len(relevant_words) - 1 and cumulative_words < target_word_units:
                    cumulative_words += word_units[word_index]
                    word_index += 1
                cue_end = float(relevant_words[max(0, word_index - 1)].get("end", cue_start))
                cue_end = min(seg_end, max(cue_start + 0.001, cue_end))
            else:
                cue_end = seg_start + (seg_end - seg_start) * cumulative_text / total_text_units
            result.append({"start": round(cue_start + offset, 3), "end": round(cue_end + offset, 3), "text": chunk})
            cue_start = cue_end
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="自研高速 Groq 字幕实验")
    parser.add_argument("--video-url", default=DEFAULT_TEST_URL)
    parser.add_argument("--output", type=Path, default=Path("mvp/results"))
    parser.add_argument("--api-url", default=base.DEFAULT_API_URL)
    parser.add_argument("--model", default=base.DEFAULT_MODEL)
    parser.add_argument("--language", default="zh")
    parser.add_argument(
        "--prompt",
        default="你好，我们需要使用简体中文。内容可能包含英文公司名、人物名、股票代码和科技术语。",
    )
    parser.add_argument("--chunk-seconds", type=int, default=600)
    parser.add_argument("--overlap-seconds", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--audio-kbps", type=int, default=48)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key.startswith("gsk_") or len(api_key) < 20:
        log("Groq API Key 缺失或不完整。")
        return 2
    if args.concurrency < 1 or args.concurrency > 3:
        raise ValueError("并发数量必须为 1–3。")

    source_id = base.video_key(args.video_url)
    run_dir = (
        args.output.expanduser().resolve()
        / source_id
        / ("own-fast-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    work_dir = Path(tempfile.mkdtemp(prefix="yca-own-fast-"))
    timings: Dict[str, float] = {}
    total_started = time.monotonic()

    try:
        phase = time.monotonic()
        source = download_speech_audio(args.video_url, work_dir)
        timings["youtube_audio_download_seconds"] = time.monotonic() - phase

        phase = time.monotonic()
        audio = base.transcode_audio(source, work_dir, args.audio_kbps)
        duration = base.audio_duration(audio)
        timings["audio_transcode_seconds"] = time.monotonic() - phase

        phase = time.monotonic()
        chunks = base.split_audio(
            audio,
            work_dir,
            duration,
            args.chunk_seconds,
            args.overlap_seconds,
            args.audio_kbps,
        )
        timings["audio_split_seconds"] = time.monotonic() - phase
        log("临时音频：{:.1f} 分钟，{:.2f} MB；共 {} 个片段。".format(
            duration / 60.0, audio.stat().st_size / 1024 / 1024, len(chunks)
        ))

        phase = time.monotonic()
        payloads: List[Optional[Dict[str, Any]]] = [None] * len(chunks)
        workers = min(args.concurrency, len(chunks))
        log("开始自研并发识别：同时处理 {} 个片段……".format(workers))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    transcribe_fine,
                    chunk_path,
                    api_key,
                    args.api_url,
                    args.model,
                    args.language,
                    args.prompt,
                ): index
                for index, (chunk_path, _offset) in enumerate(chunks)
            }
            for future in as_completed(futures):
                index = futures[future]
                payloads[index] = future.result()
                log("  音频片段 {}/{} 已完成。".format(index + 1, len(chunks)))
        timings["groq_parallel_asr_seconds"] = time.monotonic() - phase

        phase = time.monotonic()
        complete_payloads = [payload for payload in payloads if payload is not None]
        offsets = [offset for _path, offset in chunks]
        chunk_segments = [
            fine_segments_from_payload(payload, offsets[index])
            for index, payload in enumerate(complete_payloads)
        ]
        segments = merge_fine_segments(chunk_segments, offsets, args.overlap_seconds)
        timings["merge_and_write_seconds"] = time.monotonic() - phase
        timings["total_seconds"] = time.monotonic() - total_started

        report: Dict[str, Any] = {
            "mvp_version": "0.2-own-fast",
            "implementation": "independent; no VideoCaptioner imports",
            "provider": "groq",
            "model": args.model,
            "source": args.video_url,
            "duration_seconds": round(duration, 3),
            "temporary_audio_megabytes": round(audio.stat().st_size / 1024 / 1024, 3),
            "chunk_count": len(chunks),
            "chunk_seconds": args.chunk_seconds,
            "overlap_seconds": args.overlap_seconds,
            "concurrency": workers,
            "segment_count": len(segments),
            "timings": {key: round(value, 3) for key, value in timings.items()},
            "estimated_usd": round(duration / 3600.0 * 0.04, 6),
            "temporary_audio_retained": False,
        }
        (run_dir / "groq_raw_chunks.json").write_text(
            json.dumps(complete_payloads, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        base.write_outputs(run_dir, segments, report)
        log("AI 识别耗时：{:.1f} 秒。".format(timings["groq_parallel_asr_seconds"]))
        log("端到端总耗时：{:.1f} 秒。".format(timings["total_seconds"]))
        log("自研高速实验完成：{}".format(run_dir))
        log("临时音频正在自动删除。")
        return 0
    except KeyboardInterrupt:
        log("实验已取消，正在清理临时音频。")
        return 130
    except Exception as exc:
        log("实验失败：{}: {}".format(type(exc).__name__, exc))
        return 1
    finally:
        shutil.rmtree(str(work_dir), ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
