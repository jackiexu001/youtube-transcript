#!/usr/bin/env python3
"""Windows desktop UI for YouTube Transcript."""

from __future__ import annotations

import json
import os
import base64
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    import psutil
except ImportError:  # Development fallback; bundled builds always include it.
    psutil = None

APP_NAME = "YouTube Transcript"
CREATE_NO_WINDOW = 0x08000000


def enable_windows_dpi_awareness() -> None:
    """Keep text and controls crisp on 125%-200% Windows displays."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def protect_secret(value: str) -> str:
    """Encrypt a secret for the current Windows user with DPAPI."""
    if os.name != "nt" or not value:
        return ""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    raw = value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = DATA_BLOB(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = DATA_BLOB()
    crypt_protect = ctypes.windll.crypt32.CryptProtectData
    crypt_protect.argtypes = [
        ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    crypt_protect.restype = wintypes.BOOL
    if not crypt_protect(
        ctypes.byref(source), "YouTube Caption Archive", None, None, None, 0x1,
        ctypes.byref(destination),
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(destination.pbData, destination.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(ctypes.cast(destination.pbData, ctypes.c_void_p))


def unprotect_secret(value: str) -> str:
    """Decrypt a DPAPI value; damaged or foreign-user data safely becomes empty."""
    if os.name != "nt" or not value:
        return ""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    try:
        raw = base64.b64decode(value, validate=True)
        buffer = ctypes.create_string_buffer(raw)
        source = DATA_BLOB(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        destination = DATA_BLOB()
        crypt_unprotect = ctypes.windll.crypt32.CryptUnprotectData
        crypt_unprotect.argtypes = [
            ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
        ]
        crypt_unprotect.restype = wintypes.BOOL
        if not crypt_unprotect(
            ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(destination)
        ):
            return ""
        try:
            return ctypes.string_at(destination.pbData, destination.cbData).decode("utf-8")
        finally:
            local_free = ctypes.windll.kernel32.LocalFree
            local_free.argtypes = [ctypes.c_void_p]
            local_free.restype = ctypes.c_void_p
            local_free(ctypes.cast(destination.pbData, ctypes.c_void_p))
    except Exception:
        return ""


def app_directory() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def settings_path() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return base / "YouTube Caption Archive" / "settings.json"


def default_archive() -> Path:
    documents = Path(os.environ.get("USERPROFILE", Path.home())) / "Documents"
    if os.name == "nt":
        try:
            import ctypes
            buffer = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0:
                documents = Path(buffer.value)
        except Exception:
            pass
    return documents / "YouTube 字幕学习档案"


class CaptionApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.process: subprocess.Popen[str] | None = None
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.paused = False
        self.started_at = 0.0
        self.last_page = "index.html"
        self.channel_file: Path | None = None
        self.processed = 0
        self.total = 0
        self.had_warnings = False
        self.settings = self.load_settings()
        self.archive = Path(self.settings.get("archive", str(default_archive())))
        self.ai_provider_value = str(self.settings.get("ai_provider", "groq"))
        if self.ai_provider_value not in {"groq", "openai"}:
            self.ai_provider_value = "groq"
        self.build_ui()
        self.root.after(100, self.poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self.close_app)

    def load_settings(self) -> dict:
        try:
            return json.loads(settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_settings(self) -> None:
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        proxy = self.proxy.get().strip() if hasattr(self, "proxy") else ""
        payload = {
            "archive": str(self.archive),
            "ai_enabled": bool(self.ai_enabled.get()) if hasattr(self, "ai_enabled") else True,
            "ai_provider": self.ai_provider.get() if hasattr(self, "ai_provider") else "groq",
        }
        if "@" not in proxy:
            payload["proxy"] = proxy
        if hasattr(self, "ai_key") and self.ai_key.get().strip():
            provider = self.ai_provider.get()
            payload[f"{provider}_key_encrypted"] = protect_secret(self.ai_key.get().strip())
        for provider in ("groq", "openai"):
            key_name = f"{provider}_key_encrypted"
            if key_name in self.settings and key_name not in payload:
                payload[key_name] = self.settings[key_name]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def build_ui(self) -> None:
        self.root.title(APP_NAME)
        self.root.geometry("1120x860")
        self.root.minsize(900, 720)
        self.root.configure(bg="#F2F3F5")
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("App.TButton", font=("Microsoft YaHei UI", 10), padding=(14, 8))
        style.configure("App.TCombobox", font=("Microsoft YaHei UI", 10), padding=5)
        style.configure("Accent.Horizontal.TProgressbar", troughcolor="#E3E6EA", background="#F07C54")

        shell = tk.Frame(self.root, bg="#F2F3F5", padx=36, pady=24)
        shell.pack(fill="both", expand=True)
        self.shell = shell
        self.root.bind("<Configure>", self.update_responsive_padding)
        self.build_menu()

        header = tk.Frame(shell, bg="#F2F3F5")
        header.pack(fill="x", pady=(0, 18))
        logo = tk.Canvas(header, width=66, height=66, bg="#F2F3F5", highlightthickness=0)
        logo.pack(side="left")
        logo.create_polygon(
            3, 15, 3, 51, 15, 63, 51, 63, 63, 51, 63, 15, 51, 3, 15, 3,
            fill="#F07C54", outline="#F07C54", smooth=True,
        )
        logo.create_polygon(25, 18, 25, 48, 49, 33, fill="white", outline="white")
        heading = tk.Frame(header, bg="#F2F3F5")
        heading.pack(side="left", padx=(16, 0))
        tk.Label(heading, text=APP_NAME, bg="#F2F3F5", fg="#18191B",
                 font=("Microsoft YaHei UI", 27, "bold")).pack(anchor="w")
        tk.Label(heading, text="把公开视频与字幕整理成本地学习档案", bg="#F2F3F5", fg="#74777C",
                 font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(3, 0))

        card = tk.Frame(shell, bg="white", highlightbackground="#E0E1E4", highlightthickness=1, padx=24, pady=18)
        card.pack(fill="x")
        card.columnconfigure(1, weight=1)
        self.channel = tk.StringVar()
        self.batch = tk.StringVar(value="100")
        self.archive_text = tk.StringVar(value=str(self.archive))
        self.proxy = tk.StringVar(value=str(self.settings.get("proxy", "")))
        self.ai_provider = tk.StringVar(value=self.ai_provider_value)
        encrypted = str(self.settings.get(f"{self.ai_provider_value}_key_encrypted", ""))
        self.ai_key = tk.StringVar(value=unprotect_secret(encrypted))
        self.ai_enabled = tk.BooleanVar(value=bool(self.settings.get("ai_enabled", True)))
        self.add_row(card, 0, "频道主页", self.channel, "例如：https://www.youtube.com/@xxxx")
        self.add_row(card, 1, "本次数量", self.batch, "100（0 表示全部）", width=16)
        tk.Label(card, text="保存地址", bg="white", fg="#273247", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=2, column=0, sticky="w", pady=(11, 0), padx=(0, 18))
        tk.Entry(card, textvariable=self.archive_text, state="readonly", relief="solid", bd=1,
                 readonlybackground="#F8FAFC", font=("Microsoft YaHei UI", 10)).grid(
            row=2, column=1, sticky="ew", ipady=7, pady=(11, 0))
        ttk.Button(card, text="选择…", style="App.TButton", command=self.choose_archive).grid(
            row=2, column=2, padx=(10, 0), pady=(11, 0))
        tk.Label(card, text="AI 服务", bg="white", fg="#273247", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=3, column=0, sticky="w", pady=(11, 0), padx=(0, 18))
        self.ai_provider_box = ttk.Combobox(card, textvariable=self.ai_provider, state="readonly",
                                            values=("groq", "openai"), width=20, style="App.TCombobox")
        self.ai_provider_box.grid(row=3, column=1, sticky="w", pady=(11, 0), ipady=4)
        self.ai_provider_box.bind("<<ComboboxSelected>>", self.provider_changed)
        tk.Label(card, text="API Key", bg="white", fg="#273247", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=4, column=0, sticky="w", pady=(11, 0), padx=(0, 18))
        self.ai_key_entry = tk.Entry(card, textvariable=self.ai_key, relief="solid", bd=1,
                                     font=("Microsoft YaHei UI", 10), show="•")
        self.ai_key_entry.grid(row=4, column=1, sticky="ew", ipady=7, pady=(11, 0))
        self.ai_key_actions = tk.Frame(card, bg="white")
        self.ai_key_actions.grid(row=4, column=2, padx=(10, 0), pady=(11, 0))
        self.save_ai_key_btn = ttk.Button(
            self.ai_key_actions, text="保存 Key", style="App.TButton", command=self.save_ai_key
        )
        self.save_ai_key_btn.pack(side="left")
        self.get_ai_key_btn = ttk.Button(
            self.ai_key_actions, text="获取 API Key", style="App.TButton", command=self.open_api_key_page
        )
        self.get_ai_key_btn.pack(side="left", padx=(7, 0))
        tk.Checkbutton(
            card,
            text="勾选后若 YouTube 无字幕，系统将自动生成 AI 字幕（支持中文、英语等 99+ 种语言）",
            variable=self.ai_enabled,
            command=self.toggle_ai,
            bg="white",
            fg="#273247",
            activebackground="white",
            font=("Microsoft YaHei UI", 9),
        ).grid(row=5, column=1, columnspan=2, sticky="w", pady=(5, 0))
        self.toggle_ai()

        buttons = tk.Frame(shell, bg="#F2F3F5")
        buttons.pack(fill="x", pady=(14, 12))
        self.start_btn = tk.Button(buttons, text="▶  开始抓取", command=self.start, bg="#18191B", fg="white",
                                   activebackground="#303236", activeforeground="white", relief="flat",
                                   font=("Microsoft YaHei UI", 10, "bold"), padx=18, pady=9, cursor="hand2")
        self.start_btn.pack(side="left")
        self.open_btn = ttk.Button(buttons, text="打开结果", style="App.TButton", command=self.open_results)
        self.open_btn.pack(side="left", padx=8)
        self.pause_btn = ttk.Button(buttons, text="暂停", style="App.TButton", command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="left")
        self.stop_btn = ttk.Button(buttons, text="停止", style="App.TButton", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(buttons, text="关闭应用", style="App.TButton", command=self.close_app).pack(side="right")

        metrics_row = tk.Frame(shell, bg="#F2F3F5")
        metrics_row.pack(fill="x", pady=(0, 14))
        self.processed_metric = tk.StringVar(value="0 / —")
        self.position_metric = tk.StringVar(value="—")
        self.elapsed_metric = tk.StringVar(value="00:00")
        self.metric_card(metrics_row, "✓", "本次已处理", self.processed_metric, "#35B66A").pack(
            side="left", fill="x", expand=True, padx=(0, 7))
        self.metric_card(metrics_row, "☷", "频道位置", self.position_metric, "#2589F5").pack(
            side="left", fill="x", expand=True, padx=7)
        self.metric_card(metrics_row, "◷", "运行时间", self.elapsed_metric, "#F08A24").pack(
            side="left", fill="x", expand=True, padx=(7, 0))

        status_row = tk.Frame(shell, bg="#F2F3F5")
        status_row.pack(fill="x")
        tk.Label(status_row, text="抓取进度", bg="#F2F3F5", fg="#242629",
                 font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        self.status = tk.StringVar(value="准备就绪")
        tk.Label(status_row, textvariable=self.status, bg="#F2F3F5", fg="#74777C",
                 font=("Microsoft YaHei UI", 10)).pack(side="right")
        self.progress = ttk.Progressbar(shell, mode="determinate", maximum=100,
                                        style="Accent.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(8, 14))

        tk.Label(shell, text="运行日志", bg="#F2F3F5", fg="#242629",
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        log_frame = tk.Frame(shell, bg="white", highlightbackground="#DFE5EE", highlightthickness=1)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        scroll = ttk.Scrollbar(log_frame)
        scroll.pack(side="right", fill="y")
        self.log = tk.Text(log_frame, wrap="word", state="disabled", bg="white", fg="#263247", relief="flat",
                           padx=14, pady=12, font=("Microsoft YaHei UI", 9), yscrollcommand=scroll.set)
        self.log.pack(fill="both", expand=True)
        scroll.config(command=self.log.yview)

    def update_responsive_padding(self, event):
        if event.widget is self.root and hasattr(self, "shell"):
            side = max(28, (event.width - 1180) // 2)
            self.shell.configure(padx=side)

    def metric_card(self, parent, icon, label, value, accent):
        card = tk.Frame(parent, bg="white", highlightbackground="#E0E1E4", highlightthickness=1,
                        padx=18, pady=12)
        tk.Label(card, text=f"{icon}  {label}", bg="white", fg=accent,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        tk.Label(card, textvariable=value, bg="white", fg="#242629",
                 font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", pady=(5, 0))
        return card

    def add_row(self, card, row, label, variable, placeholder, width=None, show=None):
        tk.Label(card, text=label, bg="white", fg="#273247", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=row, column=0, sticky="w", pady=(0 if row == 0 else 11, 0), padx=(0, 18))
        entry = tk.Entry(card, textvariable=variable, relief="solid", bd=1, font=("Microsoft YaHei UI", 10), width=width, show=show)
        entry.grid(row=row, column=1, columnspan=2, sticky="ew" if width is None else "w", ipady=7,
                   pady=(0 if row == 0 else 11, 0))
        entry.insert(0, "")
        return entry

    def toggle_ai(self):
        if hasattr(self, "ai_key_entry"):
            self.ai_key_entry.configure(state="normal" if self.ai_enabled.get() else "disabled")
            self.ai_provider_box.configure(state="readonly" if self.ai_enabled.get() else "disabled")
            state = "normal" if self.ai_enabled.get() else "disabled"
            self.save_ai_key_btn.configure(state=state)
            self.get_ai_key_btn.configure(state=state)

    def provider_changed(self, _event=None):
        previous = self.ai_provider_value
        if self.ai_key.get().strip():
            self.settings[f"{previous}_key_encrypted"] = protect_secret(self.ai_key.get().strip())
        self.ai_provider_value = self.ai_provider.get()
        self.ai_key.set(unprotect_secret(str(self.settings.get(f"{self.ai_provider_value}_key_encrypted", ""))))

    def open_api_key_page(self):
        url = "https://platform.openai.com/api-keys" if self.ai_provider.get() == "openai" else "https://console.groq.com/keys"
        webbrowser.open(url)

    def save_ai_key(self):
        key = self.ai_key.get().strip()
        provider = self.ai_provider.get()
        provider_name = "OpenAI" if provider == "openai" else "Groq"
        prefix = "sk-" if provider == "openai" else "gsk_"
        if not key.startswith(prefix) or len(key) < 20:
            messagebox.showwarning(
                APP_NAME,
                f"请粘贴完整的、以 {prefix} 开头的 {provider_name} API Key。",
            )
            self.ai_key_entry.focus_set()
            return
        try:
            self.settings[f"{provider}_key_encrypted"] = protect_secret(key)
            self.save_settings()
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法安全保存 API Key：\n{exc}")
            return
        messagebox.showinfo(APP_NAME, f"{provider_name} API Key 已安全保存。")

    def build_menu(self):
        menu = tk.Menu(self.root)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="选择档案文件夹…", command=self.choose_archive)
        file_menu.add_command(label="打开字幕档案", command=self.open_results)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.close_app)
        menu.add_cascade(label="文件", menu=file_menu)
        task_menu = tk.Menu(menu, tearoff=False)
        task_menu.add_command(label="开始抓取", command=self.start)
        task_menu.add_command(label="暂停或继续", command=self.toggle_pause)
        task_menu.add_command(label="停止当前任务", command=self.stop)
        menu.add_cascade(label="任务", menu=task_menu)
        settings_menu = tk.Menu(menu, tearoff=False)
        settings_menu.add_command(label="网络代理…", command=self.configure_proxy)
        settings_menu.add_command(label="保存当前 API Key", command=self.save_ai_key)
        menu.add_cascade(label="设置", menu=settings_menu)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="获取 Groq API Key", command=lambda: webbrowser.open("https://console.groq.com/keys"))
        help_menu.add_command(label="获取 OpenAI API Key", command=lambda: webbrowser.open("https://platform.openai.com/api-keys"))
        help_menu.add_separator()
        help_menu.add_command(label=f"关于 {APP_NAME}", command=lambda: messagebox.showinfo(APP_NAME, APP_NAME))
        menu.add_cascade(label="帮助", menu=help_menu)
        self.root.config(menu=menu)

    def configure_proxy(self):
        value = simpledialog.askstring(
            "网络代理",
            "通常保持留空，应用会自动读取系统代理。\n只有需要手动指定时才填写，例如 http://127.0.0.1:7890：",
            initialvalue=self.proxy.get(),
            parent=self.root,
        )
        if value is None:
            return
        self.proxy.set(value.strip())
        self.save_settings()

    def choose_archive(self):
        folder = filedialog.askdirectory(initialdir=str(self.archive), title="选择字幕档案文件夹")
        if folder:
            self.archive = Path(folder)
            self.archive_text.set(folder)
            self.save_settings()

    def backend_command(self) -> list[str]:
        if getattr(sys, "frozen", False):
            return [str(app_directory() / "youtube-caption-backend.exe")]
        return [sys.executable, str(app_directory().parent / "youtube_caption.py")]

    def hidden_options(self) -> dict:
        startup = subprocess.STARTUPINFO() if os.name == "nt" else None
        if startup:
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return {"startupinfo": startup, "creationflags": CREATE_NO_WINDOW if os.name == "nt" else 0}

    def start(self):
        url = self.channel.get().strip()
        if not url:
            messagebox.showwarning(APP_NAME, "请先输入 YouTube 博主主页链接。")
            return
        try:
            count = int(self.batch.get().strip() or "100")
            if count < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning(APP_NAME, "本次数量必须是 0 或正整数。")
            return
        ai_enabled = bool(self.ai_enabled.get())
        ai_key = self.ai_key.get().strip()
        provider = self.ai_provider.get()
        provider_name = "OpenAI" if provider == "openai" else "Groq"
        prefix = "sk-" if provider == "openai" else "gsk_"
        if ai_enabled and (not ai_key.startswith(prefix) or len(ai_key) < 20):
            messagebox.showwarning(
                APP_NAME,
                f"要为没有 YouTube 字幕的视频生成 AI 字幕，请输入完整的、以 {prefix} 开头的 {provider_name} API Key；或者取消勾选 AI 字幕功能。",
            )
            self.ai_key_entry.focus_set()
            return
        self.archive = Path(self.archive_text.get()).expanduser()
        self.archive.mkdir(parents=True, exist_ok=True)
        try:
            self.save_settings()
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法安全保存 AI Key：\n{exc}")
            return
        channel_file = Path(tempfile.gettempdir()) / f"youtube-caption-channel-{os.getpid()}.txt"
        channel_file.write_text(url, encoding="utf-8")
        self.channel_file = channel_file
        command = self.backend_command() + ["--channel-file", str(channel_file), "--output", str(self.archive),
                                            "--batch-size", str(count), "--event-format", "jsonl"]
        if ai_enabled:
            command.extend(["--ai-transcribe-missing", "--ai-provider", provider, "--ai-language", "auto"])
        manual_proxy = self.proxy.get().strip()
        environment = os.environ.copy()
        if manual_proxy:
            environment["YCA_PROXY"] = manual_proxy
            environment["HTTPS_PROXY"] = manual_proxy
            environment["HTTP_PROXY"] = manual_proxy
        if ai_enabled:
            environment["YCA_AI_API_KEY"] = ai_key
        try:
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                            bufsize=1, env=environment, close_fds=True, **self.hidden_options())
        except OSError as exc:
            self.remove_channel_file()
            messagebox.showerror(APP_NAME, f"无法启动抓取程序：\n{exc}")
            return
        self.started_at, self.processed, self.total, self.paused = time.time(), 0, 0, False
        self.had_warnings = False
        self.clear_log()
        self.set_running(True)
        self.status.set("正在读取频道…")
        self.progress.configure(value=0, mode="indeterminate")
        self.progress.start(12)
        threading.Thread(target=self.read_output, daemon=True).start()

    def read_output(self):
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            self.events.put(("line", line.rstrip()))
        self.events.put(("done", self.process.wait()))

    def poll_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "line":
                    self.handle_line(str(value))
                else:
                    self.finished(int(value))
        except queue.Empty:
            pass
        if self.process and self.process.poll() is None:
            elapsed = int(time.time() - self.started_at)
            self.processed_metric.set(f"{self.processed} / {self.total or '—'}")
            self.position_metric.set(f"{self.processed}/{self.total or '—'}")
            self.elapsed_metric.set(f"{elapsed // 60:02d}:{elapsed % 60:02d}")
        self.root.after(100, self.poll_events)

    def handle_line(self, line):
        try:
            event = json.loads(line)
        except ValueError:
            self.append_log(line)
            return
        message = event.get("message", "")
        if message:
            self.append_log(message)
        name = event.get("event")
        if name == "network_check":
            self.status.set("正在检查 YouTube 连接…")
        elif name == "network_ok":
            self.status.set("网络连接正常")
        elif name in {"network_warning", "network_retry"}:
            self.status.set("网络不稳定，正在尝试恢复…")
        elif name == "retry_queue_loaded":
            self.status.set("正在优先重试上次未完成内容")
        elif name in {"ai_started", "ai_progress"}:
            self.status.set("正在生成 AI 字幕…")
        elif name == "ai_completed":
            self.status.set("AI 字幕生成完成")
        elif name == "ai_key_missing":
            self.had_warnings = True
            self.status.set("需要 AI API Key")
        elif name == "channel_loaded":
            self.total = int(event.get("total", 0) or 0)
            self.progress.stop(); self.progress.configure(mode="determinate", maximum=max(self.total, 1), value=0)
            self.status.set("正在抓取字幕…")
        elif name == "video_progress":
            self.processed = int(event.get("processed", self.processed + 1) or self.processed + 1)
            self.total = int(event.get("total", self.total) or self.total)
            requested = int(self.batch.get() or "0")
            progress_total = min(self.total, requested) if requested > 0 else self.total
            self.progress.configure(maximum=max(progress_total, 1), value=self.processed)
            self.processed_metric.set(f"{self.processed} / {progress_total or '—'}")
            self.position_metric.set(f"{self.processed}/{self.total or '—'}")
        elif name == "channel_completed":
            folder = str(event.get("folder", "")).strip()
            if folder:
                self.last_page = f"{folder}/index.html"
        elif name == "open_page":
            self.last_page = event.get("page", "index.html")
        elif name == "retry_queued":
            self.had_warnings = True
            self.status.set("单条失败，已加入待重试队列")
        elif name in {"rate_limited", "channel_paused", "metadata_paused"}:
            self.had_warnings = True
            self.status.set("YouTube 暂时不可用，当前进度已保存")
        elif name == "channel_error":
            self.had_warnings = True
            self.status.set("无法连接或读取该频道")

    def append_log(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}]  {message}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal"); self.log.delete("1.0", "end"); self.log.configure(state="disabled")

    def set_running(self, running):
        self.start_btn.configure(state="disabled" if running else "normal")
        self.pause_btn.configure(state="normal" if running else "disabled")
        self.stop_btn.configure(state="normal" if running else "disabled")

    def process_tree(self):
        if not self.process or not psutil:
            return []
        try:
            root = psutil.Process(self.process.pid)
            return root.children(recursive=True) + [root]
        except psutil.Error:
            return []

    def toggle_pause(self):
        if not psutil:
            messagebox.showerror(APP_NAME, "暂停组件不可用，请重新安装完整版本。")
            return
        try:
            for item in reversed(self.process_tree()) if self.paused else self.process_tree():
                item.resume() if self.paused else item.suspend()
            self.paused = not self.paused
            self.pause_btn.configure(text="继续" if self.paused else "暂停")
            self.status.set("已暂停" if self.paused else "正在抓取字幕…")
        except psutil.Error as exc:
            messagebox.showerror(APP_NAME, f"无法切换暂停状态：{exc}")

    def stop(self):
        if not self.process:
            return
        if not messagebox.askyesno(APP_NAME, "确定停止本次任务吗？已保存的视频不会丢失。"):
            return
        for item in self.process_tree():
            try: item.terminate()
            except Exception: pass

    def finished(self, code):
        self.progress.stop()
        self.set_running(False)
        self.pause_btn.configure(text="暂停")
        if code == 0 and not self.had_warnings:
            self.status.set("任务已完成")
        elif self.had_warnings:
            self.status.set("任务已保存，部分内容待重试")
            self.append_log("网络恢复后再次运行同一频道，将自动优先重试失败内容。")
        else:
            self.status.set("任务已停止或出现错误")
        if code == 0 and self.total and not self.had_warnings:
            self.progress.configure(mode="determinate", maximum=max(self.total, 1), value=max(self.processed, self.total))
        self.process = None
        self.remove_channel_file()
        if code == 0:
            self.open_results()

    def remove_channel_file(self):
        if not self.channel_file:
            return
        try:
            self.channel_file.unlink(missing_ok=True)
        except OSError:
            pass
        self.channel_file = None

    def open_results(self):
        index = self.archive / "index.html"
        if not index.exists():
            messagebox.showinfo(APP_NAME, "还没有可打开的字幕档案。")
            return
        command = self.backend_command() + ["--open", "--output", str(self.archive), "--page", self.last_page]
        try:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True, **self.hidden_options())
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法打开结果：{exc}")

    def close_app(self):
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno(APP_NAME, "任务仍在运行。停止任务并关闭应用吗？"):
                return
            for item in self.process_tree():
                try: item.terminate()
                except Exception: pass
        self.remove_channel_file()
        self.root.destroy()


def main() -> None:
    enable_windows_dpi_awareness()
    root = tk.Tk()
    CaptionApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
