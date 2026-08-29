#!/usr/bin/env python3
"""Windows desktop UI for YouTube Caption Archive."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import psutil
except ImportError:  # Development fallback; bundled builds always include it.
    psutil = None

APP_NAME = "YouTube 字幕抓取"
CREATE_NO_WINDOW = 0x08000000


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
        self.processed = 0
        self.total = 0
        self.settings = self.load_settings()
        self.archive = Path(self.settings.get("archive", str(default_archive())))
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
        path.write_text(json.dumps({"archive": str(self.archive)}, ensure_ascii=False, indent=2), encoding="utf-8")

    def build_ui(self) -> None:
        self.root.title(APP_NAME)
        self.root.geometry("980x760")
        self.root.minsize(820, 650)
        self.root.configure(bg="#F3F6FA")
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=(18, 9))
        style.configure("App.TButton", font=("Microsoft YaHei UI", 10), padding=(14, 8))
        style.configure("App.TLabel", background="#F3F6FA", font=("Microsoft YaHei UI", 10))

        shell = tk.Frame(self.root, bg="#F3F6FA", padx=34, pady=26)
        shell.pack(fill="both", expand=True)
        title = tk.Label(shell, text="YouTube 字幕抓取", bg="#F3F6FA", fg="#172033",
                         font=("Microsoft YaHei UI", 25, "bold"))
        title.pack(anchor="w")
        tk.Label(shell, text="把频道视频、字幕和公开数据整理成本地学习档案", bg="#F3F6FA", fg="#687386",
                 font=("Microsoft YaHei UI", 11)).pack(anchor="w", pady=(4, 20))

        card = tk.Frame(shell, bg="white", highlightbackground="#DFE5EE", highlightthickness=1, padx=22, pady=18)
        card.pack(fill="x")
        card.columnconfigure(1, weight=1)
        self.channel = tk.StringVar()
        self.batch = tk.StringVar(value="100")
        self.archive_text = tk.StringVar(value=str(self.archive))
        self.add_row(card, 0, "频道主页", self.channel, "例如：https://www.youtube.com/@xxxx")
        self.add_row(card, 1, "本次数量", self.batch, "100（0 表示全部）", width=16)
        tk.Label(card, text="档案位置", bg="white", fg="#273247", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=2, column=0, sticky="w", pady=(11, 0), padx=(0, 18))
        tk.Entry(card, textvariable=self.archive_text, state="readonly", relief="solid", bd=1,
                 readonlybackground="#F8FAFC", font=("Microsoft YaHei UI", 10)).grid(
            row=2, column=1, sticky="ew", ipady=7, pady=(11, 0))
        ttk.Button(card, text="选择…", style="App.TButton", command=self.choose_archive).grid(
            row=2, column=2, padx=(10, 0), pady=(11, 0))

        buttons = tk.Frame(shell, bg="#F3F6FA")
        buttons.pack(fill="x", pady=16)
        self.start_btn = ttk.Button(buttons, text="开始抓取", style="Primary.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.open_btn = ttk.Button(buttons, text="打开结果", style="App.TButton", command=self.open_results)
        self.open_btn.pack(side="left", padx=8)
        self.pause_btn = ttk.Button(buttons, text="暂停", style="App.TButton", command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="left")
        self.stop_btn = ttk.Button(buttons, text="停止", style="App.TButton", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(buttons, text="关闭应用", style="App.TButton", command=self.close_app).pack(side="right")

        status_row = tk.Frame(shell, bg="#F3F6FA")
        status_row.pack(fill="x")
        tk.Label(status_row, text="抓取进度", bg="#F3F6FA", fg="#273247",
                 font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        self.status = tk.StringVar(value="准备就绪")
        tk.Label(status_row, textvariable=self.status, bg="#F3F6FA", fg="#657087",
                 font=("Microsoft YaHei UI", 10)).pack(side="right")
        self.progress = ttk.Progressbar(shell, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(8, 10))
        self.metrics = tk.StringVar(value="已处理 0 条   ·   当前位置 —   ·   已用时 00:00")
        tk.Label(shell, textvariable=self.metrics, bg="#F3F6FA", fg="#687386",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 10))

        tk.Label(shell, text="运行日志", bg="#F3F6FA", fg="#273247",
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        log_frame = tk.Frame(shell, bg="white", highlightbackground="#DFE5EE", highlightthickness=1)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        scroll = ttk.Scrollbar(log_frame)
        scroll.pack(side="right", fill="y")
        self.log = tk.Text(log_frame, wrap="word", state="disabled", bg="white", fg="#263247", relief="flat",
                           padx=14, pady=12, font=("Microsoft YaHei UI", 9), yscrollcommand=scroll.set)
        self.log.pack(fill="both", expand=True)
        scroll.config(command=self.log.yview)

    def add_row(self, card, row, label, variable, placeholder, width=None):
        tk.Label(card, text=label, bg="white", fg="#273247", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=row, column=0, sticky="w", pady=(0 if row == 0 else 11, 0), padx=(0, 18))
        entry = tk.Entry(card, textvariable=variable, relief="solid", bd=1, font=("Microsoft YaHei UI", 10), width=width)
        entry.grid(row=row, column=1, columnspan=2, sticky="ew" if width is None else "w", ipady=7,
                   pady=(0 if row == 0 else 11, 0))
        entry.insert(0, "")

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
        self.archive = Path(self.archive_text.get()).expanduser()
        self.archive.mkdir(parents=True, exist_ok=True)
        self.save_settings()
        channel_file = Path(tempfile.gettempdir()) / "youtube-caption-channel-url.txt"
        channel_file.write_text(url, encoding="utf-8")
        command = self.backend_command() + ["--channel-file", str(channel_file), "--output", str(self.archive),
                                            "--batch-size", str(count), "--event-format", "jsonl", "--open-after"]
        try:
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                            bufsize=1, **self.hidden_options())
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法启动抓取程序：\n{exc}")
            return
        self.started_at, self.processed, self.total, self.paused = time.time(), 0, 0, False
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
            self.metrics.set(f"已处理 {self.processed} 条   ·   当前位置 {self.processed}/{self.total or '—'}   ·   已用时 {elapsed // 60:02d}:{elapsed % 60:02d}")
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
        if name == "channel_loaded":
            self.total = int(event.get("total", 0) or 0)
            self.progress.stop(); self.progress.configure(mode="determinate", maximum=max(self.total, 1), value=0)
            self.status.set("正在抓取字幕…")
        elif name == "video_progress":
            self.processed = int(event.get("processed", self.processed + 1) or self.processed + 1)
            self.total = int(event.get("total", self.total) or self.total)
            requested = int(self.batch.get() or "0")
            progress_total = min(self.total, requested) if requested > 0 else self.total
            self.progress.configure(maximum=max(progress_total, 1), value=self.processed)
        elif name == "open_page":
            self.last_page = event.get("page", "index.html")
        elif name == "rate_limited":
            self.status.set("YouTube 暂时限流，已保存当前进度")

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
        self.status.set("任务已完成" if code == 0 else "任务已停止或出现错误")
        if code == 0 and self.total:
            self.progress.configure(mode="determinate", maximum=max(self.total, 1), value=max(self.processed, self.total))
        self.process = None

    def open_results(self):
        index = self.archive / "index.html"
        if not index.exists():
            messagebox.showinfo(APP_NAME, "还没有可打开的字幕档案。")
            return
        command = self.backend_command() + ["--open", "--output", str(self.archive), "--page", self.last_page]
        try:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             **self.hidden_options())
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法打开结果：{exc}")

    def close_app(self):
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno(APP_NAME, "任务仍在运行。停止任务并关闭应用吗？"):
                return
            for item in self.process_tree():
                try: item.terminate()
                except Exception: pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    CaptionApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
