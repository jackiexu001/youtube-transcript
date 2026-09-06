# YouTube Caption Archive

把 YouTube 频道的视频列表、字幕和基础互动数据整理成本地 HTML 档案：左边播放 YouTube 视频，右边阅读逐字字幕；没有 YouTube 字幕时，可使用 Groq 或 OpenAI 的 Whisper 服务自动生成带时间轴的 AI 字幕，支持中文、英语等 99+ 种语言。

这个工具不会下载视频文件，也不会长期保存音频、SRT、VTT、TXT 等中间文件。它只保存本地 HTML、一个很小的增量状态文件和可选 CSV 列表。

## 功能

- 抓取 YouTube 频道历史视频列表。
- 优先使用人工字幕，其次使用 YouTube 自动字幕。
- 没有 YouTube 字幕时，可自动提取低码率纯音频并调用 Groq 或 OpenAI Whisper 生成 AI 字幕，支持中文、英语等 99+ 种语言并自动识别语种。
- 长视频采用自适应切片、并行识别、音频源自动切换和断点续传；临时音频完成后自动删除。
- 每个频道生成一个 `index.html` 列表页。
- 每个视频生成一个单独 HTML 页面。
- 点击字幕时间戳，视频跳转到对应时间。
- 无字幕视频也生成页面，左侧仍可播放视频。
- 显示发布时间、观看数、点赞数、评论数。
- 生成 `videos.csv`，方便在 Excel、Numbers、Google Sheets 中打开。
- 增量更新：已归档视频自动跳过。
- 启动任务前检查 YouTube 连接，并区分超时、DNS、TLS、代理、限流、登录验证、地区限制、私密视频和视频失效。
- 临时网络错误自动限次重试；不会无限停在“正在读取”。
- 失败视频写入待重试队列，下次运行同一频道时优先处理，不重复抓取已成功视频。
- 检测到限流、登录验证或连续网络失败时自动暂停频道，避免继续增加请求压力。
- 默认读取 macOS/Windows 系统代理，也支持在应用内填写手动 HTTP/HTTPS/SOCKS 代理地址。
- macOS 原生 `.app` 和 Windows 图形应用：直接输入频道、数量并查看实时进度与左对齐运行日志，不打开命令窗口；任务支持暂停、继续和停止。
- 支持中文频道名以及浏览器复制出来的 `%E6...` 编码链接。
- 可选择并记住档案保存位置；升级应用不会覆盖已有数据。
- 新版不再为每个频道生成 `.command`、`.bat` 或小型查看器，统一由主应用打开结果。
- 支持直接传频道 URL，不强制手动编辑配置文件。

## 安装

macOS 普通用户下载 DMG 后，把“YouTube Transcript”拖入“应用程序”即可。发布版已内置独立后端、yt-dlp 和 FFmpeg，不需要安装 Python、Homebrew 或使用 Terminal。

DMG 使用传统 Mac 安装布局：左侧是应用，右侧 `Applications` 是系统“应用程序”目录的快捷方式。将左侧应用拖到右侧即完成安装。首次从“应用程序”启动后，应用会提醒用户推出安装磁盘并删除下载的 DMG；不会自动删除用户文件。

因为当前免费版本没有 Apple Developer 公证，首次启动可能被 Gatekeeper 阻止。请在 Finder 中按住 Control 点击应用并选择“打开”；如果仍被阻止，进入“系统设置 → 隐私与安全性”，在底部选择“仍要打开”。不需要输入任何终端命令。

直接运行源码的开发者才需要 Python 3、`yt-dlp` 和 FFmpeg。应用不会下载或保存视频文件；AI 字幕只临时处理纯音频，完成或失败后都会自动清理。

## 最快用法

如果只想看小白三步说明，先看 [QUICKSTART.md](QUICKSTART.md)。

如果你不想输入命令：

- macOS：双击 `YouTube Transcript.app`。它是原生 macOS 界面，让你粘贴频道地址、设置本次数量、选择档案位置，并实时显示抓取进度；抓取过程中可以暂停、继续或停止，不会打开 Terminal。
- Windows：从网站下载 `YouTube-Transcript-2.3.0-Windows-x64-Setup.exe`，按安装向导完成安装，再从开始菜单打开“YouTube Transcript”。不需要另装 Python、yt-dlp 或 FFmpeg。

然后粘贴 YouTube 博主主页链接，例如 `https://www.youtube.com/@handle`。首次运行默认保存到“文稿/YouTube 字幕学习档案”，也可以在应用里选择已有档案目录；抓完后会打开刚处理的博主页面。

如果启用“无 YouTube 字幕时自动生成 AI 字幕”，可选择 Groq（默认、速度优先）或 OpenAI Whisper，并粘贴对应 API Key。两者均支持中文、英语等 99+ 种语言并自动识别语种。界面的“获取 API Key”会打开服务商官方平台。不同服务的 Key 分开加密保存，不会写入字幕档案或运行日志。若只想保存 YouTube 自带字幕和无字幕播放页，可以取消勾选该功能。

“网络代理”通常留空即可，应用会自动读取系统设置。如果浏览器可以打开 YouTube、应用却无法读取频道，可以填写代理软件显示的本机代理地址，例如 `http://127.0.0.1:端口`。包含账号密码的代理不会被应用记住，日志也会隐藏认证信息。

如果这个博主以前已经抓过，再次输入同一个博主主页即可继续。程序会自动跳过已完成的视频，继续抓更早的视频。

数量填 `50` 或 `100` 适合日常使用；填 `0` 表示本次尝试抓取全部未归档视频。大频道仍建议分批运行，减少 YouTube 临时限流。

如果你习惯命令行，也可以只用一个 Python 文件运行：

```bash
python3 youtube_caption.py --interactive
```

抓取完成后打开结果：

```bash
python3 youtube_caption.py --open --output archive
```

也可以直接传频道链接：

```bash
python3 youtube_caption.py --channel "https://www.youtube.com/@handle" --output archive
```

Windows 可把 `python3` 换成：

```powershell
py -3 youtube_caption.py --channel "https://www.youtube.com/@handle" --output archive
```

## 构建 macOS 原生应用

普通用户不需要构建。开发者修改界面或后端后，日常调试可运行：

```bash
./macos/build_macos_app.sh
```

生成网站分发用的通用 DMG：

```bash
./macos/build_release_macos.sh
```

发布构建同时支持 Apple Silicon 与 Intel Mac，并内置后端、yt-dlp 与从 FFmpeg 官方固定版本源码构建的 LGPL 通用音频组件。产物位于 `release/`，同时生成 SHA-256 校验文件。构建机需要一次性准备项目内的 PyInstaller、dmgbuild 环境和官方 `yt-dlp_macos`；缺少 FFmpeg 时脚本会校验官方源码后自动构建。普通用户不需要这些工具。

查看档案时直接打开主应用并点击“打开结果”。

## 批量配置多个频道

复制示例配置：

```bash
cp channels.example.json channels.json
```

编辑 `channels.json`：

```json
{
  "channels": [
    {
      "url": "https://www.youtube.com/@handle",
      "batch_size": 50,
      "delay_seconds": 2,
      "timezone": "Asia/Shanghai",
      "languages": ["zh-Hans", "zh-Hant", "zh.*", "en.*", "en"]
    }
  ]
}
```

然后运行：

```bash
python3 youtube_caption.py --config channels.json --output archive
```

频道 URL 可以直接粘贴主页地址，例如 `https://www.youtube.com/@handle`；程序会自动转换到 `/videos`。`name` 是可选字段，只有你想自定义本地文件夹名称时才需要。

## 常用命令

## 构建 Windows 安装程序

普通用户不需要构建。开发者可在 Windows 10/11 x64 上安装 Python 3.11 与 Inno Setup 6，然后运行：

```powershell
.\build_windows_exe.ps1
```

脚本会创建隔离环境、下载官方 `yt-dlp.exe`，分别打包无窗口图形应用和后端，并生成标准安装程序及 SHA-256：

```text
release\windows\YouTube-Transcript-2.3.0-Windows-x64-Setup.exe
release\windows\YouTube-Transcript-2.3.0-Windows-x64-Setup.exe.sha256
```

也可以把代码推送到 GitHub 后，手动运行 `Build Windows installer` 工作流，在构建产物中下载相同的安装包。PyInstaller 不支持在 macOS 上直接生成 Windows 可执行文件，因此最后的 EXE 必须由 Windows 构建机或 Windows GitHub Actions 生成。

安装程序采用当前用户安装，不要求管理员权限；程序文件位于用户应用目录，字幕档案默认位于“文档\YouTube 字幕学习档案”，卸载或升级应用不会删除字幕档案。当前免费版本未做商业代码签名，其他电脑首次安装可能出现 Microsoft Defender SmartScreen 提醒。

小批量测试：

```bash
python3 youtube_caption.py --channel "https://www.youtube.com/@handle" --output archive --limit 3
```

每次最多处理 100 条新视频：

```bash
python3 youtube_caption.py --channel "https://www.youtube.com/@handle" --output archive --limit 100
```

重新检查之前无字幕的视频：

```bash
python3 youtube_caption.py --config channels.json --output archive --retry-missing
```

只回填旧视频的发布时间、观看数、点赞数、评论数，不新增视频：

```bash
python3 youtube_caption.py --config channels.json --output archive --limit 0 --metadata-limit 100
```

## 输出结构

```text
archive/
├── index.html
└── Channel Name/
    ├── index.html
    ├── videos.csv
    ├── .caption-archive.json
    ├── .caption-retry.json（仅在存在待重试视频时出现）
    ├── .ai-checkpoints/（AI 任务中断时保留，成功后自动清理）
    ├── .ai-reports/（AI 处理耗时与模型信息，不含音频和 Key）
    └── 2026-08-21_videoId.html
```

`.caption-archive.json` 是增量状态文件，请保留。它不是视频或字幕中间文件。

`.caption-retry.json` 保存失败视频的编号、错误类型和重试次数，不包含视频、音频、字幕或密码。网络恢复后再次运行同一频道，程序会优先重试；全部成功后该文件自动删除。

`.ai-checkpoints` 只保存已经成功返回的分段识别结果，用于断点续传；不保存音频或 API Key。整条视频完成后对应检查点会自动删除。

## 网络异常时会发生什么

- 浏览器也无法打开 YouTube：应用会限次重试并清楚提示，现有档案不受影响。
- 浏览器能打开但应用失败：先确认系统代理已启用；仍失败时在“网络代理”填写本机代理地址。
- YouTube 提示 429：应用立即停止当前频道并保存进度，建议等待 30–60 分钟。
- YouTube 要求登录或机器人验证：应用不会提高请求频率，也不会尝试绕过验证。
- 单个视频私密、删除或受地区限制：只把该条加入待重试队列，其余视频继续处理。
- 连续三条视频发生连接类错误：应用暂停频道，避免在断网状态下不断请求。

## 为什么需要启动器打开

直接双击本地 HTML 时，浏览器使用 `file://` 协议，YouTube 嵌入播放器经常不能正常播放。主应用的“打开结果”会启动一个仅限本机访问的 `127.0.0.1` HTTP 服务，因此浏览器可以正常嵌入 YouTube 播放器。

## 限流建议

大频道第一次抓取可能有几百条视频，建议：

- `batch_size` 或 `--limit` 设为 50 到 100。
- `delay_seconds` 保持 2 秒或更高。
- 如果 YouTube 提示登录、机器人验证或访问受限，先暂停一段时间，不要提高并发或缩短间隔。

## 给 AI 助手使用

你可以这样让 Codex、Claude、Cursor 等助手调用：

```text
请使用这个项目抓取 https://www.youtube.com/@handle 的全部视频列表和字幕。
默认不要下载视频，输出到 archive，生成 HTML 页面、频道列表、videos.csv，并使用增量更新。
```

如果你的助手支持 skill，可以把 `skills/youtube-caption-archive/SKILL.md` 安装到对应的 skills 目录中。

## 许可

MIT License。请遵守 YouTube 服务条款和内容创作者权益。本工具只整理公开页面可访问的字幕和元数据，不绕过付费、登录、版权或地区限制。
