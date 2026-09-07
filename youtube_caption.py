#!/usr/bin/env python3
"""Archive YouTube channel captions into local playable HTML pages."""

import argparse
import os
import html
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
import csv
import functools
import http.server
import socket
import socketserver
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_BATCH_SIZE = 100
DEFAULT_DELAY_SECONDS = 2
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_LANGUAGES = ["zh-Hans", "zh-Hant", "zh.*", "en.*", "en"]
SERVER_STATE_FILE = ".youtube-caption-server.json"
EVENT_FORMAT = "text"
YTDLP_SOCKET_TIMEOUT_SECONDS = 15
YTDLP_TOTAL_TIMEOUT_SECONDS = 75
YTDLP_ATTEMPTS = 2
ACTIVE_PROXY = None


class ArchiveRequestError(RuntimeError):
    def __init__(self, code, message, detail="", retryable=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.retryable = retryable

# Windows console and pipe encodings depend on the machine locale.  Desktop
# builds exchange Chinese JSONL messages with the GUI, so make that protocol
# explicitly UTF-8 instead of relying on cp1252/GBK defaults.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def emit_event(event, message="", level="info", **data):
    """Emit stable JSONL for desktop apps while preserving readable CLI logs."""
    if EVENT_FORMAT == "jsonl":
        payload = {"event": event, "level": level, "message": message, **data}
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)
        return
    stream = sys.stderr if level == "error" else sys.stdout
    print(message, file=stream, flush=True)


def redact_secrets(value):
    text = str(value or "")
    text = re.sub(r"(https?://)[^/@\s:]+:[^/@\s]+@", r"\1***:***@", text)
    text = re.sub(r"gsk_[A-Za-z0-9_-]+", "gsk_***", text)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{16,}", "sk-***", text)


def classify_request_error(detail):
    raw = redact_secrets(detail).strip()
    text = raw.lower()
    cases = (
        (("429", "too many requests"), "rate_limited", "YouTube 暂时限流，当前进度已保存。请等待 30–60 分钟后重试。", False),
        (("sign in to confirm", "confirm you’re not a bot", "confirm you're not a bot", "login required", "cookies-from-browser"), "verification_required", "YouTube 要求登录或进行机器人验证。请先在浏览器确认 YouTube 可以正常访问。", False),
        (("not available in your country", "geo-restricted", "geographic restriction"), "region_restricted", "这个视频受地区限制，当前网络位置无法访问。", False),
        (("private video", "members-only", "members only"), "private_video", "这是私密或会员视频，公开模式无法归档。", False),
        (("video unavailable", "this video is unavailable", "has been removed"), "video_unavailable", "视频已删除、不可用或频道暂时不允许访问。", False),
        (("timed out", "timeout", "read operation timed out"), "timeout", "连接 YouTube 超时。请检查网络、代理或 VPN 后重试。", True),
        (("name or service not known", "temporary failure in name resolution", "nodename nor servname", "getaddrinfo failed", "dns"), "dns_error", "无法解析 YouTube 地址，请检查 DNS 或网络连接。", True),
        (("ssl", "certificate verify", "unexpected_eof", "tls"), "tls_error", "与 YouTube 建立安全连接失败，请检查代理、VPN 或系统时间。", True),
        (("proxy", "tunnel connection failed"), "proxy_error", "代理连接失败，请检查系统或手动代理设置。", True),
        (("connection reset", "connection refused", "network is unreachable", "remote end closed", "failed to establish a new connection"), "connection_error", "无法连接 YouTube，请检查网络、代理或 VPN。", True),
    )
    for needles, code, message, retryable in cases:
        if any(needle in text for needle in needles):
            return code, message, retryable, raw
    return "youtube_error", "YouTube 返回了无法处理的错误，已保留现有结果。", False, raw


def configured_proxy(explicit=None):
    value = str(explicit or os.environ.get("YCA_PROXY") or "").strip()
    if value.lower() in {"none", "off", "direct"}:
        return None
    if value:
        return value
    proxies = urllib.request.getproxies()
    return proxies.get("https") or proxies.get("http") or proxies.get("all")


def run_json(args, attempts=YTDLP_ATTEMPTS):
    bundled = os.environ.get("YCA_YTDLP")
    if not bundled and getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates = (
            executable_dir / "yt-dlp.exe",
            executable_dir / "yt-dlp",
            executable_dir.parent / "Resources" / "yt-dlp_macos",
        )
        bundled = next((str(candidate) for candidate in candidates if candidate.is_file()), None)
    base_command = [
        bundled or "yt-dlp",
        "--quiet",
        "--no-warnings",
        "--socket-timeout",
        str(YTDLP_SOCKET_TIMEOUT_SECONDS),
        "--retries",
        "2",
        "--extractor-retries",
        "2",
    ]
    if ACTIVE_PROXY:
        base_command.extend(["--proxy", ACTIVE_PROXY])
    env = os.environ.copy()
    env.pop("GROQ_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    env.pop("YCA_AI_API_KEY", None)
    extra_paths = ["/opt/homebrew/bin", "/usr/local/bin"]
    env["PATH"] = os.pathsep.join(extra_paths + [env.get("PATH", "")])
    def execute(command_args, command_attempts):
        command = base_command + list(command_args)
        last_error = None
        for attempt in range(1, max(1, command_attempts) + 1):
            try:
                result = subprocess.run(
                    command,
                    check=False,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    env=env,
                    timeout=YTDLP_TOTAL_TIMEOUT_SECONDS,
                )
                if result.returncode == 0:
                    try:
                        return json.loads(result.stdout), None
                    except json.JSONDecodeError as exc:
                        detail = "yt-dlp 没有返回有效数据：{}".format(exc)
                else:
                    detail = result.stderr or result.stdout or "yt-dlp 错误码 {}".format(result.returncode)
            except FileNotFoundError:
                raise SystemExit("找不到 yt-dlp。请重新安装完整版本，或在命令行环境中安装 yt-dlp。")
            except subprocess.TimeoutExpired:
                detail = "request timed out after {} seconds".format(YTDLP_TOTAL_TIMEOUT_SECONDS)
            code, message, retryable, safe_detail = classify_request_error(detail)
            last_error = ArchiveRequestError(code, message, safe_detail[-600:], retryable)
            if not retryable or code == "rate_limited" or attempt >= command_attempts:
                break
            wait_seconds = min(4, attempt * 2)
            emit_event(
                "network_retry",
                "{} 正在进行第 {}/{} 次重试……".format(message, attempt + 1, command_attempts),
                level="warning",
                error_type=code,
                attempt=attempt + 1,
                attempts=command_attempts,
                wait_seconds=wait_seconds,
            )
            time.sleep(wait_seconds)
        return None, last_error

    payload, last_error = execute(args, attempts)
    if payload is not None:
        return payload

    # YouTube increasingly challenges the normal web client on some Windows/IP
    # combinations. These public player clients can still expose public metadata
    # and captions without using browser cookies or a user's signed-in session.
    if (
        last_error
        and last_error.code == "verification_required"
        and "--extractor-args" not in args
    ):
        emit_event(
            "youtube_client_fallback",
            "YouTube 暂时拒绝默认访问方式，正在切换公开客户端重新读取字幕……",
            level="warning",
        )
        fallback_args = [
            "--extractor-args",
            "youtube:player_client=android_vr,web_embedded,tv_simply",
            *args,
        ]
        payload, fallback_error = execute(fallback_args, 1)
        if payload is not None:
            emit_event("youtube_client_recovered", "已切换公开客户端并恢复读取。")
            return payload
        last_error = fallback_error or last_error

    raise last_error


def network_preflight():
    emit_event("network_check", "正在检查 YouTube 网络连接……", proxy=bool(ACTIVE_PROXY))
    try:
        socket.getaddrinfo("www.youtube.com", 443)
    except OSError as exc:
        code, message, retryable, detail = classify_request_error(exc)
        emit_event("network_warning", message, level="warning", error_type=code, retryable=retryable, detail=detail[-300:])
        return False
    try:
        handlers = []
        if ACTIVE_PROXY and ACTIVE_PROXY.startswith(("http://", "https://")):
            handlers.append(urllib.request.ProxyHandler({"http": ACTIVE_PROXY, "https": ACTIVE_PROXY}))
        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(
            "https://www.youtube.com/generate_204",
            headers={"User-Agent": "Mozilla/5.0 YouTube-Caption-Archive/2"},
        )
        with opener.open(request, timeout=8) as response:
            if response.status >= 500:
                raise RuntimeError("YouTube HTTP {}".format(response.status))
        emit_event("network_ok", "YouTube 网络连接正常。", proxy=bool(ACTIVE_PROXY))
        return True
    except Exception as exc:
        code, message, retryable, detail = classify_request_error(exc)
        emit_event(
            "network_warning",
            "{} 程序仍会尝试读取频道。".format(message),
            level="warning",
            error_type=code,
            retryable=retryable,
            detail=detail[-300:],
        )
        return False


def safe_name(value):
    value = re.sub(r"[/:*?\"<>|]", "_", value).strip(" .")
    return value[:100] or "未命名频道"


def normalize_channel_url(url):
    """Turn a YouTube channel homepage URL into its Videos-tab URL."""
    value = str(url).strip().strip("<>")
    markdown_match = re.match(r"\[[^\]]+\]\((https?://[^)]+)\)", value)
    if markdown_match:
        value = markdown_match.group(1).strip()
    parsed = urllib.parse.urlsplit(value)
    host = parsed.netloc.lower().split(":", 1)[0]
    path = urllib.parse.unquote(parsed.path).rstrip("/")
    parts = [part for part in path.split("/") if part]
    is_youtube = host in {"youtube.com", "www.youtube.com", "m.youtube.com"}
    if not is_youtube or not parts:
        raise ArchiveRequestError(
            "invalid_channel_url",
            "请输入完整的 YouTube 频道主页链接，例如 https://www.youtube.com/@xxxx。",
            retryable=False,
        )
    is_channel_root = is_youtube and (
        (len(parts) == 1 and parts[0].startswith("@")) or
        (len(parts) == 2 and parts[0] in {"channel", "c", "user"})
    )
    if is_channel_root:
        path += "/videos"
    encoded_path = urllib.parse.quote(path, safe="/@")
    return urllib.parse.urlunsplit((parsed.scheme or "https", parsed.netloc, encoded_path, parsed.query, ""))


def decode_user_url(url):
    try:
        return urllib.parse.unquote(str(url).strip())
    except Exception:
        return str(url).strip()


def timestamp(seconds):
    seconds = int(float(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def published_at(info, timezone_name="Asia/Shanghai"):
    value = info.get("release_timestamp") or info.get("timestamp")
    if value is not None:
        try:
            return datetime.fromtimestamp(float(value), ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError, OverflowError):
            pass
    date = info.get("upload_date") or info.get("release_date") or ""
    if len(date) == 8 and date.isdigit():
        date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    return date or "未知"


def format_count(value):
    if value is None or value == "":
        return "—"
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return "—"
    if number >= 100_000_000:
        text = f"{number / 100_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{text}亿"
    if number >= 10_000:
        text = f"{number / 10_000:.1f}".rstrip("0").rstrip(".")
        return f"{text}万"
    return f"{number:,}"


def video_stats(info):
    return {
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
    }


def stats_text(record):
    return " · ".join([
        f"👁 {format_count(record.get('view_count'))}",
        f"👍 {format_count(record.get('like_count'))}",
        f"💬 {format_count(record.get('comment_count'))}",
    ])


def meta_html(channel, record):
    video_id = record.get("id") or ""
    publish_time = record.get("published_at") or record.get("date") or "未知"
    url = f"https://www.youtube.com/watch?v={video_id}"
    return f'<div class="meta">{html.escape(channel)} · {html.escape(publish_time)} · {html.escape(stats_text(record))} · <a href="{url}" target="_blank" rel="noopener">YouTube 原始链接</a></div>'


def choose_track(info, languages):
    # Human subtitles always win over automatic captions.
    for source_name, source in (("YouTube 人工字幕", info.get("subtitles") or {}),
                                ("YouTube 自动字幕", info.get("automatic_captions") or {})):
        for wanted in languages:
            candidates = []
            if wanted.endswith(".*"):
                prefix = wanted[:-2].lower()
                candidates = [key for key in source if key.lower().startswith(prefix)]
            elif wanted in source:
                candidates = [wanted]
            for language in candidates:
                formats = source[language]
                track = next((item for item in formats if item.get("ext") == "json3"), None)
                if track and track.get("url"):
                    return source_name, language, track["url"]
    return None


def fetch_segments(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    handlers = []
    if ACTIVE_PROXY and ACTIVE_PROXY.startswith(("http://", "https://")):
        handlers.append(urllib.request.ProxyHandler({"http": ACTIVE_PROXY, "https": ACTIVE_PROXY}))
    opener = urllib.request.build_opener(*handlers)
    for attempt in range(1, YTDLP_ATTEMPTS + 1):
        try:
            with opener.open(request, timeout=60) as response:
                data = json.load(response)
            break
        except Exception as exc:
            code, message, retryable, detail = classify_request_error(exc)
            error = ArchiveRequestError(code, message, detail[-600:], retryable)
            if not retryable or attempt >= YTDLP_ATTEMPTS:
                raise error from exc
            emit_event(
                "network_retry",
                "字幕文件读取失败，正在进行第 {}/{} 次重试……".format(attempt + 1, YTDLP_ATTEMPTS),
                level="warning",
                error_type=code,
                attempt=attempt + 1,
                attempts=YTDLP_ATTEMPTS,
            )
            time.sleep(attempt * 2)
    segments = []
    for event in data.get("events", []):
        parts = event.get("segs") or []
        text = "".join(part.get("utf8", "") for part in parts).replace("\n", " ").strip()
        if text and text != "[音乐]":
            segments.append((event.get("tStartMs", 0) / 1000, text))
    return segments


STYLE = """
:root{color-scheme:light dark;--bg:#f5f5f7;--card:#fff;--text:#171717;--muted:#666;--line:#ddd;--accent:#c00}
@media(prefers-color-scheme:dark){:root{--bg:#111;--card:#1c1c1e;--text:#f5f5f7;--muted:#aaa;--line:#333}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1500px;margin:auto;padding:24px}.meta{color:var(--muted);margin-bottom:20px}.meta a{color:inherit}.layout{display:grid;grid-template-columns:minmax(360px,1.25fr) minmax(320px,1fr);gap:20px;align-items:start}
.video{position:sticky;top:20px}.player{position:relative;padding-top:56.25%;background:#000}.player iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
.transcript,.list{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 18px}.transcript{max-height:calc(100vh - 40px);overflow:auto}.line{display:grid;grid-template-columns:68px 1fr;gap:10px;padding:9px 6px;border-bottom:1px solid var(--line);border-radius:7px;cursor:pointer;transition:background .15s ease,box-shadow .15s ease}.line:hover{background:color-mix(in srgb,var(--accent) 7%,transparent)}.line:focus-visible{outline:2px solid var(--accent);outline-offset:1px}.line.active{background:color-mix(in srgb,var(--accent) 11%,transparent);box-shadow:inset 3px 0 var(--accent)}
.time{border:0;background:none;color:var(--accent);font:inherit;font-variant-numeric:tabular-nums;cursor:pointer;text-align:left;padding:0}.source{color:var(--muted);font-size:14px}.empty{color:var(--muted);padding:28px 0}.list a{color:var(--text);text-decoration:none}.list li{padding:10px 0;border-bottom:1px solid var(--line)}
@media(max-width:850px){main{padding:14px}.layout{grid-template-columns:1fr}.video{position:static}.transcript{max-height:none}}
"""


def video_html(info, channel, source, language, segments):
    video_id = info["id"]
    title = html.escape(info.get("title") or video_id)
    publish_time = info.get("published_at") or published_at(info)
    meta_record = {"id": video_id, "published_at": publish_time, **video_stats(info)}
    rows = "\n".join(
        '<div class="line" role="button" tabindex="0" data-start="{seconds}" '
        'onclick="seek({seconds},this)" onkeydown="captionKey(event,{seconds},this)">'
        '<button class="time" type="button" tabindex="-1" '
        'onclick="event.stopPropagation();seek({seconds},this.closest(\'.line\'))">{stamp}</button>'
        '<div>{text}</div></div>'.format(
            seconds=("{:.3f}".format(max(0.0, float(sec))).rstrip("0").rstrip(".")),
            stamp=timestamp(sec),
            text=html.escape(text),
        )
        for sec, text in segments
    )
    if not rows:
        rows = '<p class="empty">此视频目前没有 YouTube 字幕，但仍可在左侧正常观看。</p>'
    language_meta = f" · 语言：{html.escape(language)}" if language else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>{STYLE}</style></head>
<body><main><h1>{title}</h1>{meta_html(channel, meta_record)}
<div class="layout"><section class="video"><div class="player"><div id="player"></div></div></section>
<section class="transcript"><div class="source">字幕来源：{html.escape(source)}{language_meta}</div>{rows}</section></div></main>
<script src="https://www.youtube.com/iframe_api"></script><script>
let player, pendingSeek=null;
function onYouTubeIframeAPIReady(){{
  const vars={{playsinline:1,enablejsapi:1}};
  if(location.protocol==='http:'||location.protocol==='https:') vars.origin=location.origin;
  player=new YT.Player('player',{{videoId:'{video_id}',playerVars:vars,events:{{onReady(){{
    if(pendingSeek!==null){{const s=pendingSeek;pendingSeek=null;seek(s);}}
  }}}}}});
}}
function markCaption(row){{
  document.querySelectorAll('.line.active').forEach(item=>item.classList.remove('active'));
  if(row) row.classList.add('active');
}}
function commandPlayer(func,args){{
  const frame=document.querySelector('#player iframe');
  if(!frame||!frame.contentWindow) return false;
  frame.contentWindow.postMessage(JSON.stringify({{event:'command',func:func,args:args||[]}}),'*');
  return true;
}}
function seek(s,row){{
  s=Number(s);
  markCaption(row);
  if(player&&typeof player.seekTo==='function'){{player.seekTo(s,true);player.playVideo();}}
  else {{pendingSeek=s;commandPlayer('seekTo',[s,true]);commandPlayer('playVideo',[]);}}
  document.querySelector('.video').scrollIntoView({{behavior:'smooth',block:'start'}});
}}
function captionKey(event,s,row){{
  if(event.key==='Enter'||event.key===' '){{event.preventDefault();seek(s,row);}}
}}
</script></body></html>"""


def upgrade_caption_page_interactions(folder):
    """Make caption text clickable in pages created by older versions."""
    changed = 0
    pattern = re.compile(
        r'<div class="line"><button class="time" onclick="seek\(([-+0-9.]+)\)">'
    )
    interaction_style = (
        ".line[role=button]{cursor:pointer;border-radius:7px;padding-left:6px;padding-right:6px}"
        ".line[role=button]:hover{background:rgba(217,119,87,.08)}"
        ".line[role=button]:focus-visible{outline:2px solid var(--accent);outline-offset:1px}"
    )
    for path in folder.glob("*.html"):
        try:
            original = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if '<section class="transcript">' not in original or 'class="line"' not in original:
            continue

        def replacement(match):
            seconds = match.group(1)
            return (
                '<div class="line" role="button" tabindex="0" data-start="{0}" '
                'onclick="seek({0},this)" onkeydown="captionKey(event,{0},this)">'
                '<button class="time" type="button" tabindex="-1" '
                'onclick="event.stopPropagation();seek({0},this.closest(\'.line\'))">'
            ).format(seconds)

        updated = pattern.sub(replacement, original)
        if updated == original:
            continue
        if interaction_style not in updated:
            updated = updated.replace("</style>", interaction_style + "</style>", 1)
        if "function captionKey(" not in updated:
            updated = updated.replace(
                "</script></body></html>",
                "function captionKey(event,s,row){if(event.key==='Enter'||event.key===' '){event.preventDefault();seek(s,row);}}</script></body></html>",
                1,
            )
        path.write_text(updated, encoding="utf-8")
        changed += 1
    return changed


def write_index(folder, channel, records):
    items = []
    for record in sorted(records, key=lambda row: row.get("date", ""), reverse=True):
        if record.get("file"):
            publish_time = record.get("published_at") or record.get("date") or "未知"
            items.append(f'<li><a href="{html.escape(record["file"])}"><strong>{html.escape(record["title"])}</strong></a><br><span class="source">{html.escape(publish_time)} · {html.escape(stats_text(record))} · {html.escape(record.get("source", ""))}</span></li>')
    body = "\n".join(items) or "<p>尚无已生成字幕页面的视频。</p>"
    page = f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(channel)}</title><style>{STYLE}</style></head><body><main><h1>{html.escape(channel)}</h1><div class="list"><ol>{body}</ol></div></main></body></html>'
    (folder / "index.html").write_text(page, encoding="utf-8")


def write_video_list(folder, records):
    """Write a portable CSV list for spreadsheet use on macOS and Windows."""
    fields = [
        "title",
        "video_id",
        "url",
        "published_at",
        "views",
        "likes",
        "comments",
        "status",
        "caption_source",
        "html_file",
    ]
    path = folder / "videos.csv"
    rows = sorted(records, key=lambda row: row.get("date", ""), reverse=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in rows:
            video_id = record.get("id") or ""
            writer.writerow({
                "title": record.get("title", ""),
                "video_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else "",
                "published_at": record.get("published_at") or record.get("date") or "",
                "views": record.get("view_count") if record.get("view_count") is not None else "",
                "likes": record.get("like_count") if record.get("like_count") is not None else "",
                "comments": record.get("comment_count") if record.get("comment_count") is not None else "",
                "status": record.get("status", ""),
                "caption_source": record.get("source", ""),
                "html_file": record.get("file", ""),
            })


def discover_channels(root):
    channels = []
    for archive_path in sorted(root.glob("*/.caption-archive.json")):
        folder = archive_path.parent
        try:
            archive = json.loads(archive_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        records = list(archive.values())
        channels.append({
            "name": folder.name,
            "folder": folder.name,
            "count": len(records),
            "done": sum(1 for record in records if record.get("status") == "done"),
            "no_subtitles": sum(1 for record in records if record.get("status") == "no_subtitles"),
        })
    return channels


def write_root_index(root):
    channels = discover_channels(root)
    items = []
    for channel in channels:
        url = urllib.parse.quote(channel["folder"]) + "/index.html"
        items.append(
            f'<li><a href="{url}"><strong>{html.escape(channel["name"])}</strong></a>'
            f'<br><span class="source">{channel["count"]} 条视频 · {channel["done"]} 条有字幕 · {channel["no_subtitles"]} 条无字幕</span></li>'
        )
    body = "\n".join(items) or "<p>还没有生成频道档案。先运行更新命令抓取一个 YouTube 频道。</p>"
    page = f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>YouTube 字幕档案</title><style>{STYLE}</style></head><body><main><h1>YouTube 字幕档案</h1><div class="list"><ol>{body}</ol></div></main></body></html>'
    (root / "index.html").write_text(page, encoding="utf-8")


def write_update_launcher(root):
    mac_launcher = root / "更新频道字幕.command"
    mac_content = '''#!/bin/zsh
set -u
ROOT_DIR="${0:A:h}"

cd "$ROOT_DIR" || exit 1
echo "正在读取 channels.json 并增量更新频道字幕……"
echo
python3 "$ROOT_DIR/youtube_caption.py" --config "$ROOT_DIR/channels.json" --output "$ROOT_DIR"
echo
echo "更新完成。已处理的视频会自动跳过。"
echo "按任意键关闭窗口。"
read -k 1
'''
    mac_launcher.write_text(mac_content, encoding="utf-8")
    mac_launcher.chmod(0o755)

    win_launcher = root / "更新频道字幕.bat"
    win_content = '''@echo off
setlocal
set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%" || exit /b 1
echo 正在读取 channels.json 并增量更新频道字幕……
echo.
py -3 "%ROOT_DIR%\\youtube_caption.py" --config "%ROOT_DIR%\\channels.json" --output "%ROOT_DIR%"
if errorlevel 1 python "%ROOT_DIR%\\youtube_caption.py" --config "%ROOT_DIR%\\channels.json" --output "%ROOT_DIR%"
echo.
echo 更新完成。已处理的视频会自动跳过。
pause
'''
    with win_launcher.open("w", encoding="utf-8", newline="\r\n") as handle:
        handle.write(win_content)


def write_beginner_launchers(root):
    mac_launcher = root / "开始抓取 YouTube 字幕.command"
    mac_content = '''#!/bin/zsh
set -u
ROOT_DIR="${0:A:h}"
DONE_FILE="$ROOT_DIR/.youtube-caption-terminal-done"
WINDOW_TITLE="YouTube Caption Archive - Done"

if [ -f "$DONE_FILE" ]; then
  osascript -e 'tell application "Terminal" to close (windows whose name contains "YouTube Caption Archive")' >/dev/null 2>&1 || true
  rm -f "$DONE_FILE"
  exit 0
fi

cd "$ROOT_DIR" || exit 1
rm -f "$DONE_FILE"
export LANG="${LANG:-en_US.UTF-8}"
export LC_ALL="${LC_ALL:-en_US.UTF-8}"
printf '\\033]0;YouTube Caption Archive - Running\\007'
python3 "$ROOT_DIR/youtube_caption.py" --interactive --output "$ROOT_DIR/archive" --open-after
STATUS=$?
echo
printf '\\033]0;%s\\007' "$WINDOW_TITLE"
touch "$DONE_FILE"
echo "任务已结束。你可以查看上面的结果；再次双击“开始抓取 YouTube 字幕.command”会关闭这个窗口。"
exit "$STATUS"
'''
    mac_launcher.write_text(mac_content, encoding="utf-8")
    mac_launcher.chmod(0o755)

    win_launcher = root / "开始抓取 YouTube 字幕.bat"
    win_content = '''@echo off
setlocal
set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%" || exit /b 1
py -3 "%ROOT_DIR%\\youtube_caption.py" --interactive --output "%ROOT_DIR%\\archive" --open-after
if errorlevel 1 python "%ROOT_DIR%\\youtube_caption.py" --interactive --output "%ROOT_DIR%\\archive" --open-after
echo.
echo 任务已结束。请查看上面的结果。
pause
'''
    with win_launcher.open("w", encoding="utf-8", newline="\r\n") as handle:
        handle.write(win_content)

    for old_launcher in (
        root / "继续抓取上次频道.app",
        root / "继续抓取上次频道.command",
        root / "继续抓取上次频道.bat",
    ):
        if old_launcher.exists():
            if old_launcher.is_dir():
                shutil.rmtree(old_launcher)
            else:
                old_launcher.unlink()


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def open_archive(root, relative_page="index.html"):
    """Open generated archive through localhost so YouTube embeds work."""
    root = Path(root).expanduser().resolve()
    page = str(relative_page).replace("\\", "/").lstrip("/")
    target = root / urllib.parse.unquote(page.split("?", 1)[0])
    if not target.exists():
        raise SystemExit(f"找不到页面：{target}")

    state_path = root / SERVER_STATE_FILE
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            port = int(state.get("port"))
            existing_url = f"http://127.0.0.1:{port}/{urllib.parse.quote(page, safe='/?=&%')}"
            urllib.request.urlopen(existing_url, timeout=1).close()
            separator = "&" if "?" in existing_url else "?"
            webbrowser.open(f"{existing_url}{separator}t={int(time.time())}")
            print(f"已复用正在运行的本地服务：{existing_url}")
            return
        except Exception:
            try:
                state_path.unlink()
            except OSError:
                pass

    port = free_port()
    handler = functools.partial(QuietHandler, directory=str(root))
    with socketserver.TCPServer(("127.0.0.1", port), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state_path.write_text(json.dumps({"port": port, "started_at": int(time.time())}, ensure_ascii=False, indent=2), encoding="utf-8")
        url = f"http://127.0.0.1:{port}/{urllib.parse.quote(page, safe='/?=&%')}"
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}t={int(time.time())}"
        print(f"正在打开：{url}")
        print("观看期间请保留此窗口；结束时按 Control + C。")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\n已关闭。")
        finally:
            try:
                state_path.unlink()
            except OSError:
                pass


def open_archive_detached(root, relative_page="index.html"):
    """Open generated archive in a background local server and return immediately."""
    root = Path(root).expanduser().resolve()
    page = str(relative_page).replace("\\", "/").lstrip("/")
    target = root / urllib.parse.unquote(page.split("?", 1)[0])
    if not target.exists():
        raise SystemExit(f"找不到页面：{target}")
    log_path = root / ".youtube-caption-open.log"
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--open", "--output", str(root), "--page", page]
    else:
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--open",
            "--output",
            str(root),
            "--page",
            page,
        ]
    with log_path.open("a", encoding="utf-8") as log:
        detached_environment = os.environ.copy()
        detached_environment.pop("GROQ_API_KEY", None)
        detached_environment.pop("OPENAI_API_KEY", None)
        detached_environment.pop("YCA_AI_API_KEY", None)
        subprocess.Popen(
            command,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=detached_environment,
        )
    emit_event(
        "open_page",
        f"正在后台打开字幕页面：{urllib.parse.unquote(page)}",
        page=urllib.parse.unquote(page),
    )


def interactive_config(args):
    print("YouTube Caption Archive")
    print()
    channel_url = decode_user_url(input("请粘贴 YouTube 博主主页链接"))
    if not channel_url:
        raise SystemExit("没有输入频道链接，已取消。")
    batch_text = input(f"每次最多处理多少条新视频？直接回车默认 {DEFAULT_BATCH_SIZE}：").strip()
    try:
        batch_size = int(batch_text) if batch_text else DEFAULT_BATCH_SIZE
    except ValueError:
        raise SystemExit("本次数量必须是数字。")
    args.channel = channel_url
    args.batch_size = batch_size
    if args.output == ".":
        args.output = "archive"
    return args


def is_rate_limit_error(exc):
    text = str(exc).lower()
    return getattr(exc, "code", "") == "rate_limited" or "429" in text or "too many requests" in text


def retry_queue_path(folder):
    return folder / ".caption-retry.json"


def load_retry_queue(folder):
    try:
        payload = json.loads(retry_queue_path(folder).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def save_retry_queue(folder, queue):
    path = retry_queue_path(folder)
    if not queue:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def queue_video_retry(queue, video_id, title, exc):
    previous = queue.get(video_id) if isinstance(queue.get(video_id), dict) else {}
    queue[video_id] = {
        "id": video_id,
        "title": title,
        "error_type": getattr(exc, "code", "youtube_error"),
        "message": getattr(exc, "message", str(exc)),
        "last_detail": redact_secrets(getattr(exc, "detail", ""))[-300:],
        "attempts": int(previous.get("attempts") or 0) + 1,
        "last_attempt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def should_stop_channel(exc, consecutive_network_errors):
    code = getattr(exc, "code", "")
    if code in {
        "rate_limited",
        "verification_required",
        "proxy_error",
        "ai_auth_error",
        "ai_rate_limited",
        "ai_connection_error",
    }:
        return True
    return code in {"timeout", "dns_error", "tls_error", "connection_error"} and consecutive_network_errors >= 3


def normalized_request_error(exc):
    if isinstance(exc, ArchiveRequestError):
        return exc
    code, message, retryable, detail = classify_request_error(exc)
    return ArchiveRequestError(code, message, detail[-600:], retryable)


def ai_provider_name(provider):
    return {"groq": "Groq", "openai": "OpenAI"}.get(str(provider).lower(), "AI 字幕服务")


def ai_source_name(provider):
    return {"groq": "Groq AI 字幕", "openai": "OpenAI Whisper 字幕"}.get(
        str(provider).lower(), "AI 字幕"
    )


def normalized_ai_error(exc, provider="groq"):
    """Turn transcription failures into stable user-facing errors without leaking secrets."""
    if isinstance(exc, ArchiveRequestError):
        return exc
    detail = redact_secrets(exc)
    status_code = int(getattr(exc, "status_code", 0) or 0)
    lowered = detail.lower()
    provider_name = ai_provider_name(provider)
    if status_code in {401, 403} or "api key" in lowered:
        return ArchiveRequestError(
            "ai_auth_error",
            "{} API Key无效或没有访问权限。请在应用中重新输入正确的 Key。".format(provider_name),
            detail[-600:],
            False,
        )
    if status_code == 429:
        return ArchiveRequestError(
            "ai_rate_limited",
            "{} 字幕额度或速率已达到限制。当前进度已保存，请稍后继续。".format(provider_name),
            detail[-600:],
            True,
        )
    if status_code in {500, 502, 503, 504}:
        return ArchiveRequestError(
            "ai_connection_error",
            "{} 字幕服务暂时不可用。当前进度已保存，请稍后继续。".format(provider_name),
            detail[-600:],
            True,
        )
    if status_code == 0 and ("无法连接" in detail or "timed out" in lowered or "timeout" in lowered):
        return ArchiveRequestError(
            "ai_connection_error",
            "暂时无法连接 {} 字幕服务。当前进度已保存，请检查网络后重试。".format(provider_name),
            detail[-600:],
            True,
        )
    return ArchiveRequestError(
        "ai_transcription_error",
        "AI 字幕生成失败，已保留无字幕页面，下次运行会自动重试。",
        detail[-600:],
        True,
    )


def transcribe_missing_video(video_id, folder, api_key, language="auto", provider="groq"):
    """Generate timestamped AI captions while retaining no temporary media."""
    try:
        from mvp import next_round_mvp as ai_engine
    except ImportError as exc:
        raise ArchiveRequestError(
            "ai_component_missing",
            "应用缺少 AI 字幕组件，请重新安装完整版本。",
            str(exc),
            False,
        ) from exc

    video_url = "https://www.youtube.com/watch?v={}".format(video_id)
    checkpoint_root = folder / ".ai-checkpoints" / video_id

    def progress(message):
        emit_event("ai_progress", str(message), video_id=video_id)

    emit_event(
        "ai_started",
        "没有找到 YouTube 字幕，正在生成 AI 字幕……",
        video_id=video_id,
    )
    try:
        raw_segments, report = ai_engine.transcribe_video(
            video_url,
            api_key,
            checkpoint_root,
            language=language,
            concurrency=2,
            logger=progress,
            provider=provider,
        )
    except Exception as exc:
        raise normalized_ai_error(exc, provider) from exc

    segments = [
        (float(item.get("start") or 0.0), str(item.get("text") or "").strip())
        for item in raw_segments
        if str(item.get("text") or "").strip()
    ]
    if not segments:
        raise ArchiveRequestError(
            "ai_transcription_error",
            "{} 没有返回可用字幕，已保留无字幕页面，下次运行会自动重试。".format(ai_provider_name(provider)),
            "empty AI transcript",
            True,
        )

    report_dir = folder / ".ai-reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "{}.json".format(video_id)
    temporary = report_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(report_path)
    emit_event(
        "ai_completed",
        "AI 字幕生成完成：{} 段，用时 {:.1f} 秒。".format(
            len(segments), float(report.get("total_seconds") or 0.0)
        ),
        video_id=video_id,
        segments=len(segments),
        seconds=float(report.get("total_seconds") or 0.0),
    )
    return segments, report


def print_rate_limit_hint():
    emit_event(
        "rate_limited",
        "检测到 YouTube 临时限流。已保存当前结果，建议等待 30–60 分钟后重试。",
        level="warning",
        retry_after_minutes=30,
    )


def update_existing_page_meta(folder, channel, video_id, record):
    filename = record.get("file")
    if not filename:
        return False
    path = folder / filename
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    enriched = {"id": video_id, **record}
    new_text, count = re.subn(r'<div class="meta">.*?</div>', meta_html(channel, enriched), text, count=1)
    if count and new_text != text:
        path.write_text(new_text, encoding="utf-8")
        return True
    return False


def write_launcher(root, folder, channel):
    """Create platform launchers that open this channel over localhost."""
    relative_url = urllib.parse.quote(folder.name) + "/index.html"
    mac_launcher = folder / f"打开 {safe_name(channel)} 字幕.command"
    mac_content = f'''#!/bin/zsh
set -u
CHANNEL_DIR="${{0:A:h}}"
ARCHIVE_ROOT="${{CHANNEL_DIR:h}}"
PROJECT_ROOT="$ARCHIVE_ROOT"
if [ ! -f "$PROJECT_ROOT/youtube_caption.py" ]; then
  PROJECT_ROOT="${{ARCHIVE_ROOT:h}}"
fi

cd "$PROJECT_ROOT" || exit 1
python3 "$PROJECT_ROOT/youtube_caption.py" --open --output "$ARCHIVE_ROOT" --page "{relative_url}"
'''
    mac_launcher.write_text(mac_content, encoding="utf-8")
    mac_launcher.chmod(0o755)

    win_launcher = folder / f"打开 {safe_name(channel)} 字幕.bat"
    win_content = f'''@echo off
setlocal
set "CHANNEL_DIR=%~dp0"
for %%I in ("%CHANNEL_DIR%..") do set "ARCHIVE_ROOT=%%~fI"
set "PROJECT_ROOT=%ARCHIVE_ROOT%"
if not exist "%PROJECT_ROOT%\\youtube_caption.py" for %%I in ("%ARCHIVE_ROOT%..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%" || exit /b 1
py -3 "%PROJECT_ROOT%\\youtube_caption.py" --open --output "%ARCHIVE_ROOT%" --page "{relative_url}"
if errorlevel 1 python "%PROJECT_ROOT%\\youtube_caption.py" --open --output "%ARCHIVE_ROOT%" --page "{relative_url}"
'''
    with win_launcher.open("w", encoding="utf-8", newline="\r\n") as handle:
        handle.write(win_content)

    # A Finder-launched shell executable runs without opening Terminal.  This
    # viewer is deliberately tiny and does not use AppleScript/JXA, which made
    # older generated apps fragile across macOS releases and non-ASCII paths.
    app_dir = folder / f"打开 {safe_name(channel)} 字幕.app"
    if app_dir.exists():
        shutil.rmtree(app_dir)
    macos_dir = app_dir / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True)
    viewer = macos_dir / "open-caption-archive"
    viewer.write_text(f'''#!/bin/zsh
set -u
APP_BUNDLE="${{0:A:h:h:h}}"
CHANNEL_DIR="${{APP_BUNDLE:h}}"
ARCHIVE_ROOT="${{CHANNEL_DIR:h}}"
PROJECT_ROOT="${{ARCHIVE_ROOT:h}}"
PYTHON_SCRIPT="$PROJECT_ROOT/youtube_caption.py"
if [ ! -f "$PYTHON_SCRIPT" ]; then
  PYTHON_SCRIPT="$ARCHIVE_ROOT/youtube_caption.py"
fi
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
nohup python3 "$PYTHON_SCRIPT" --open --output "$ARCHIVE_ROOT" --page "{relative_url}" \
  >>"$ARCHIVE_ROOT/.youtube-caption-open.log" 2>&1 </dev/null &
''', encoding="utf-8")
    viewer.chmod(0o755)
    (app_dir / "Contents" / "Info.plist").write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>open-caption-archive</string>
  <key>CFBundleIdentifier</key><string>local.youtube-caption.viewer.{re.sub(r'[^A-Za-z0-9.-]', '-', folder.name)[:60]}</string>
  <key>CFBundleName</key><string>打开 {html.escape(safe_name(channel))} 字幕</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict></plist>
''', encoding="utf-8")


def ensure_no_subtitle_pages(folder, channel, archive):
    """Backfill playable HTML pages for archived videos that have no captions."""
    changed = False
    for video_id, record in archive.items():
        if record.get("status") != "no_subtitles" or record.get("file"):
            continue
        date = record.get("date", "")
        filename = f"{date + '_' if date else ''}{video_id}.html"
        info = {
            "id": video_id,
            "title": record.get("title", video_id),
            "upload_date": date.replace("-", ""),
            "published_at": record.get("published_at"),
            **video_stats(record),
        }
        (folder / filename).write_text(video_html(info, channel, "无字幕", "", []), encoding="utf-8")
        record.update({"source": "无字幕", "file": filename})
        changed = True
    return changed


def needs_publish_metadata(record):
    publish_time = record.get("published_at") or ""
    return (
        record.get("status") == "no_subtitles"
        and (not publish_time or publish_time == "未知" or "具体时间暂不可用" in publish_time)
    )


def needs_stats_metadata(record):
    return any(record.get(key) is None for key in ("view_count", "like_count", "comment_count"))


def refresh_no_subtitle_metadata(folder, channel, archive, archive_path, timezone_name, delay_seconds=0, metadata_limit=None):
    """Fetch precise publish timestamps and stats for existing archived pages."""
    changed = False
    targets = [
        (video_id, record)
        for video_id, record in archive.items()
        if needs_stats_metadata(record) or needs_publish_metadata(record)
    ]
    if metadata_limit is not None:
        targets = targets[:max(0, metadata_limit)]
    consecutive_network_errors = 0
    for index, (video_id, record) in enumerate(targets, 1):
        emit_event(
            "metadata_progress",
            f"[{channel}] 回填发布时间/播放数据 {index}/{len(targets)} {record.get('title', video_id)}",
            channel=channel,
            position=index,
            total=len(targets),
            title=record.get("title", video_id),
        )
        try:
            info = run_json(["--skip-download", "--dump-single-json", f"https://www.youtube.com/watch?v={video_id}"])
        except Exception as exc:
            error = normalized_request_error(exc)
            if error.code in {"timeout", "dns_error", "tls_error", "connection_error", "proxy_error"}:
                consecutive_network_errors += 1
            else:
                consecutive_network_errors = 0
            emit_event(
                "metadata_error",
                "元数据回填失败（稍后自动重试）：{}".format(error.message),
                level="error",
                channel=channel,
                video_id=video_id,
                error_type=error.code,
                retryable=error.retryable,
            )
            if is_rate_limit_error(error):
                print_rate_limit_hint()
            if should_stop_channel(error, consecutive_network_errors):
                emit_event(
                    "metadata_paused",
                    "为避免连续请求，元数据回填已暂停；下次运行会继续。",
                    level="warning",
                    channel=channel,
                    error_type=error.code,
                )
                break
            if delay_seconds:
                time.sleep(delay_seconds)
            continue
        consecutive_network_errors = 0
        publish_time = published_at(info, timezone_name)
        date = info.get("upload_date") or record.get("date", "")
        display_date = f"{date[:4]}-{date[4:6]}-{date[6:]}" if len(date) == 8 and date.isdigit() else date
        old_file = record.get("file") or ""
        filename = old_file or f"{display_date + '_' if display_date else ''}{video_id}.html"
        page_info = {
            "id": video_id,
            "title": info.get("title") or record.get("title", video_id),
            "upload_date": (display_date or "").replace("-", ""),
            "published_at": publish_time,
            **video_stats(info),
        }
        record.update({
            "status": record.get("status") or "no_subtitles",
            "title": page_info["title"],
            "date": display_date,
            "published_at": publish_time,
            **video_stats(info),
            "source": record.get("source") or "无字幕",
            "file": filename,
        })
        if record.get("status") == "no_subtitles":
            (folder / filename).write_text(video_html(page_info, channel, "无字幕", "", []), encoding="utf-8")
        else:
            update_existing_page_meta(folder, channel, video_id, record)
        changed = True
        archive_path.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
        if delay_seconds:
            time.sleep(delay_seconds)
    return changed


def process_channel(
    root,
    channel_cfg,
    retry_missing=False,
    limit=None,
    metadata_limit=None,
    install_channel_launchers=False,
    ai_transcribe_missing=False,
    groq_api_key="",
    ai_language="auto",
    ai_provider="groq",
):
    # Keep the historical keyword for third-party scripts; it now carries the
    # selected provider's key, not only a Groq key.
    ai_api_key = groq_api_key
    channel_url = normalize_channel_url(channel_cfg["url"])
    playlist = run_json(["--flat-playlist", "--dump-single-json", channel_url])
    channel = (channel_cfg.get("name") or playlist.get("channel") or
               playlist.get("uploader") or playlist.get("title") or "未命名频道")
    folder = root / safe_name(channel)
    folder.mkdir(parents=True, exist_ok=True)
    archive_path = folder / ".caption-archive.json"
    archive = json.loads(archive_path.read_text("utf-8")) if archive_path.exists() else {}
    retry_queue = load_retry_queue(folder)
    for completed_id in set(retry_queue).intersection(archive):
        if archive[completed_id].get("status") == "done":
            retry_queue.pop(completed_id, None)
    save_retry_queue(folder, retry_queue)
    if ensure_no_subtitle_pages(folder, channel, archive):
        archive_path.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    upgraded_pages = upgrade_caption_page_interactions(folder)
    if upgraded_pages:
        emit_event(
            "caption_pages_upgraded",
            "已升级 {} 个旧字幕页面，现在可点击整行字幕跳转视频。".format(upgraded_pages),
            channel=channel,
            pages=upgraded_pages,
        )
    languages = channel_cfg.get("languages", DEFAULT_LANGUAGES)
    entries = playlist.get("entries") or []
    if retry_queue:
        queued_ids = set(retry_queue)
        entries = sorted(entries, key=lambda item: 0 if item.get("id") in queued_ids else 1)
        emit_event(
            "retry_queue_loaded",
            "发现 {} 条上次未完成的视频，将优先重试。".format(len(retry_queue)),
            level="warning",
            queued=len(retry_queue),
        )
    emit_event(
        "channel_loaded",
        f"已读取频道：{channel}，共 {len(entries)} 条公开视频。",
        channel=channel,
        total=len(entries),
        folder=folder.name,
    )
    batch_size = limit if limit is not None else channel_cfg.get("batch_size", DEFAULT_BATCH_SIZE)
    delay_seconds = max(0, float(channel_cfg.get("delay_seconds", DEFAULT_DELAY_SECONDS)))
    if metadata_limit is not None and metadata_limit > 0 and refresh_no_subtitle_metadata(folder, channel, archive, archive_path, channel_cfg.get("timezone", "Asia/Shanghai"), delay_seconds, metadata_limit):
        archive_path.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    attempted = 0
    consecutive_network_errors = 0
    stopped_for_network = False
    for position, entry in enumerate(entries, 1):
        video_id = entry.get("id")
        retry_old_missing = (
            video_id in archive
            and archive[video_id].get("status") == "no_subtitles"
            and (retry_missing or ai_transcribe_missing)
        )
        if not video_id or (video_id in archive and not retry_old_missing):
            continue
        # In direct/GUI mode a batch size of 0 means "no limit".  The explicit
        # --limit 0 form keeps its older meaning (process no new videos), which
        # is required by metadata-only backfill runs.
        limit_reached = (
            batch_size is not None
            and (limit is not None or batch_size > 0)
            and attempted >= batch_size
        )
        if limit_reached:
            emit_event(
                "batch_completed",
                f"[{channel}] 本批已处理 {attempted} 条未归档视频，下次将继续更早的视频。",
                channel=channel,
                processed=attempted,
                total=len(entries),
            )
            break
        attempted += 1
        title = entry.get("title", video_id)
        emit_event(
            "video_progress",
            f"[{channel}] {position}/{len(entries)} {title}",
            channel=channel,
            position=position,
            total=len(entries),
            processed=attempted,
            title=title,
            video_id=video_id,
        )
        try:
            info = run_json(["--skip-download", "--dump-single-json", f"https://www.youtube.com/watch?v={video_id}"])
            publish_time = published_at(info, channel_cfg.get("timezone", "Asia/Shanghai"))
            stats = video_stats(info)
            selected = choose_track(info, languages)
            if not selected:
                date = info.get("upload_date") or ""
                display_date = f"{date[:4]}-{date[4:6]}-{date[6:]}" if len(date) == 8 else date
                filename = f"{display_date + '_' if display_date else ''}{video_id}.html"
                if ai_transcribe_missing:
                    try:
                        segments, ai_report = transcribe_missing_video(
                            video_id, folder, ai_api_key, ai_language, ai_provider
                        )
                    except Exception:
                        # Preserve a playable page and metadata even if the AI service fails.
                        (folder / filename).write_text(video_html(info, channel, "无字幕", "", []), encoding="utf-8")
                        archive[video_id] = {"status": "no_subtitles", "title": info.get("title", video_id), "date": display_date, "published_at": publish_time, **stats, "source": "无字幕", "file": filename}
                        archive_path.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
                        raise
                    source_name = ai_source_name(ai_provider)
                    (folder / filename).write_text(video_html(info, channel, source_name, ai_language, segments), encoding="utf-8")
                    archive[video_id] = {
                        "status": "done",
                        "title": info.get("title", video_id),
                        "date": display_date,
                        "published_at": publish_time,
                        **stats,
                        "source": source_name,
                        "language": ai_language,
                        "file": filename,
                        "ai_model": ai_report.get("model"),
                    }
                else:
                    (folder / filename).write_text(video_html(info, channel, "无字幕", "", []), encoding="utf-8")
                    archive[video_id] = {"status": "no_subtitles", "title": info.get("title", video_id), "date": display_date, "published_at": publish_time, **stats, "source": "无字幕", "file": filename}
            else:
                source, language, subtitle_url = selected
                segments = fetch_segments(subtitle_url)
                date = info.get("upload_date") or ""
                display_date = f"{date[:4]}-{date[4:6]}-{date[6:]}" if len(date) == 8 else date
                filename = f"{display_date + '_' if display_date else ''}{video_id}.html"
                (folder / filename).write_text(video_html(info, channel, source, language, segments), encoding="utf-8")
                archive[video_id] = {"status": "done", "title": info.get("title", video_id), "date": display_date, "published_at": publish_time, **stats, "source": source, "file": filename}
            consecutive_network_errors = 0
            retry_queue.pop(video_id, None)
            save_retry_queue(folder, retry_queue)
        except Exception as exc:
            error = normalized_request_error(exc)
            queue_video_retry(retry_queue, video_id, title, error)
            save_retry_queue(folder, retry_queue)
            if error.code in {"timeout", "dns_error", "tls_error", "connection_error", "proxy_error"}:
                consecutive_network_errors += 1
            else:
                consecutive_network_errors = 0
            emit_event(
                "retry_queued",
                "{} 已加入待重试队列：{}".format(title, error.message),
                level="error",
                channel=channel,
                position=position,
                total=len(entries),
                title=title,
                video_id=video_id,
                error_type=error.code,
                retryable=error.retryable,
                queued=len(retry_queue),
            )
            if is_rate_limit_error(error):
                print_rate_limit_hint()
            if should_stop_channel(error, consecutive_network_errors):
                stopped_for_network = True
                emit_event(
                    "channel_paused",
                    "为避免连续请求，当前频道已暂停。已完成内容和待重试队列都已保存。",
                    level="warning",
                    channel=channel,
                    error_type=error.code,
                    queued=len(retry_queue),
                )
                break
            if delay_seconds:
                time.sleep(delay_seconds)
            continue
        archive_path.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
        if delay_seconds:
            time.sleep(delay_seconds)
    for video_id, record in archive.items():
        record.setdefault("id", video_id)
    archive_path.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    records = list(archive.values())
    write_index(folder, channel, records)
    write_video_list(folder, records)
    if install_channel_launchers:
        write_launcher(root, folder, channel)
    emit_event(
        "channel_completed",
        f"频道档案已更新：{channel}",
        channel=channel,
        folder=folder.name,
        processed=attempted,
        archived=len(records),
        queued=len(retry_queue),
        paused=stopped_for_network,
    )
    return channel, folder


def config_from_args(args):
    if args.channel_file:
        args.channel = Path(args.channel_file).read_text(encoding="utf-8").strip()
    if args.channel:
        channel = {
            "url": decode_user_url(args.channel),
            "batch_size": args.batch_size,
            "delay_seconds": args.delay_seconds,
            "timezone": args.timezone,
            "languages": args.languages.split(",") if args.languages else DEFAULT_LANGUAGES,
        }
        if args.name:
            channel["name"] = args.name
        return {"channels": [channel]}
    return json.loads(Path(args.config).read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description="把 YouTube 频道字幕和视频列表整理为本地可播放 HTML 档案")
    parser.add_argument("--config", default="channels.json", help="频道配置文件；未传 --channel 时使用")
    parser.add_argument("--channel", help="单个 YouTube 频道 URL；可直接传 https://www.youtube.com/@handle")
    parser.add_argument("--channel-file", help="从 UTF-8 文本文件读取单个 YouTube 频道 URL，供图形启动器使用")
    parser.add_argument("--name", help="单频道模式下自定义本地文件夹名称")
    parser.add_argument("--output", default=".", help="输出目录")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="单频道模式下每次最多处理多少条未归档视频")
    parser.add_argument("--delay-seconds", type=float, default=DEFAULT_DELAY_SECONDS, help="单频道模式下每条视频之间等待秒数")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="发布时间显示时区")
    parser.add_argument("--languages", help='字幕语言优先级，逗号分隔，例如 "zh-Hans,zh-Hant,zh.*,en.*,en"')
    parser.add_argument("--limit", type=int, help="本次每个频道最多处理 N 条未归档视频")
    parser.add_argument("--metadata-limit", type=int, help="本次每个频道最多回填 N 条旧视频的发布时间、观看数、点赞数和评论数")
    parser.add_argument("--retry-missing", action="store_true", help="重新检查之前无字幕的视频")
    parser.add_argument("--ai-transcribe-missing", action="store_true", help="没有 YouTube 字幕时使用云端语音识别生成 AI 字幕")
    parser.add_argument("--ai-provider", choices=("groq", "openai"), default="groq", help="AI 字幕服务；默认 Groq")
    parser.add_argument("--ai-language", default="auto", help="AI 字幕语言；默认自动检测")
    parser.add_argument("--proxy", help="手动代理地址；留空时自动读取系统代理，填 none 表示直连")
    parser.add_argument("--interactive", action="store_true", help="交互式输入频道链接和本次数量")
    parser.add_argument("--open", action="store_true", help="启动本地 HTTP 服务并打开已生成档案")
    parser.add_argument("--open-after", action="store_true", help="抓取完成后自动打开刚处理的频道页")
    parser.add_argument("--page", default="index.html", help="配合 --open 使用，要打开的相对 HTML 页面")
    parser.add_argument("--event-format", choices=("text", "jsonl"), default="text", help=argparse.SUPPRESS)
    parser.add_argument("--install-launchers", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    global EVENT_FORMAT, ACTIVE_PROXY
    EVENT_FORMAT = args.event_format
    if args.open:
        open_archive(args.output, args.page)
        return
    if args.interactive:
        args = interactive_config(args)
    root = Path(args.output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    provider_prefix = {"groq": "gsk_", "openai": "sk-"}[args.ai_provider]
    provider_env = {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY"}[args.ai_provider]
    ai_api_key = (os.environ.get("YCA_AI_API_KEY") or os.environ.get(provider_env) or "").strip()
    if args.ai_transcribe_missing and (
        not ai_api_key.startswith(provider_prefix) or len(ai_api_key) < 20
    ):
        emit_event(
            "ai_key_missing",
            "需要先在应用中输入完整的 {} API Key，才能为无字幕视频生成 AI 字幕。".format(ai_provider_name(args.ai_provider)),
            level="error",
        )
        raise SystemExit(3)
    ACTIVE_PROXY = configured_proxy(args.proxy)
    if ACTIVE_PROXY and ACTIVE_PROXY.startswith(("http://", "https://")):
        os.environ["HTTPS_PROXY"] = ACTIVE_PROXY
        os.environ["HTTP_PROXY"] = ACTIVE_PROXY
    if ACTIVE_PROXY:
        emit_event("proxy_detected", "已启用网络代理（地址已隐藏）。", proxy=True)
    else:
        emit_event("proxy_detected", "未检测到代理，将使用网络直连。", proxy=False)
    network_preflight()
    config = config_from_args(args)
    processed = []
    channel_failures = []
    for channel in config.get("channels", []):
        try:
            channel_name, folder = process_channel(
                root,
                channel,
                args.retry_missing,
                args.limit,
                args.metadata_limit,
                args.install_launchers,
                args.ai_transcribe_missing,
                ai_api_key,
                args.ai_language,
                args.ai_provider,
            )
            processed.append((channel_name, folder))
        except Exception as exc:
            error = normalized_request_error(exc)
            channel_failures.append(error)
            emit_event(
                "channel_error",
                "无法读取频道：{}".format(error.message),
                level="error",
                channel_url=decode_user_url(channel.get("url", "")),
                error_type=error.code,
                retryable=error.retryable,
                detail=error.detail[-300:],
            )
    try:
        write_root_index(root)
    except PermissionError:
        emit_event(
            "archive_permission_error",
            "无法写入字幕档案文件夹。请在应用中重新选择档案位置后再试；已有结果不会丢失。",
            level="error",
            output=str(root),
        )
        raise SystemExit(4)
    if args.install_launchers and (root / "channels.json").exists():
        write_update_launcher(root)
    # Launcher installation is a packaging operation, not part of an archive
    # run.  Keeping it opt-in prevents a bundled app from rebuilding itself or
    # nesting stale launchers inside its Resources directory after every run.
    if args.install_launchers:
        write_beginner_launchers(Path(__file__).resolve().parent)
    emit_event(
        "archive_completed",
        "完成：{}{}".format(root, "（有 {} 个频道未完成）".format(len(channel_failures)) if channel_failures else ""),
        output=str(root),
        channels=len(processed),
        failed_channels=len(channel_failures),
    )
    if args.open_after and processed:
        _, folder = processed[-1]
        open_archive_detached(root, urllib.parse.quote(folder.name) + "/index.html")
    if channel_failures and not processed:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
