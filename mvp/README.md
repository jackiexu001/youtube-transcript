# 云端字幕MVP

这个目录只用于比较Groq与VideoCaptioner的转录流程，不会修改正式的`archive/`，也不会把临时音频保存到字幕档案。

## 测试对象

默认建议使用当前档案中确认没有YouTube字幕的视频：

```text
https://www.youtube.com/watch?v=jpoMabs9t4s
```

## 第一步：运行轻量Groq流程

macOS图形化MVP：双击`运行 Groq MVP.app`，粘贴完整Key后点击“开始MVP测试”。可以勾选“显示输入内容以便确认”。Key只保留在该次子进程内存中，不会保存到钥匙串或文件。

也可以在本机终端临时设置API Key。不要把Key写入脚本、截图、GitHub或聊天记录：

```bash
export GROQ_API_KEY='你的Groq API Key'
cd "/path/to/YouTube Transcript"
python3 mvp/cloud_asr_mvp.py \
  --video-url 'https://www.youtube.com/watch?v=jpoMabs9t4s'
```

输出写入`mvp/results/<视频ID>/<运行时间>/`：

- `groq_raw_chunks.json`：Groq原始响应，供审计；
- `groq_segments.json`：统一后的时间轴；
- `groq_transcript.srt`；
- `groq_transcript.vtt`；
- `groq_transcript.txt`；
- `groq_report.json`：时长、速度、切片数量和估算费用。

默认行为：

- 只获取YouTube音频流，不下载视频画面；
- 转为16kHz、单声道、48kbps MP3；
- 超过10分钟自动切片，相邻片段重叠10秒；
- 使用`whisper-large-v3-turbo`；
- 成功、失败或取消后都会删除临时音频。

只有为了让VideoCaptioner使用完全相同的输入音频做对照时，才可以增加`--keep-audio`。对比结束后应删除`comparison_audio.mp3`。

## 第二步：VideoCaptioner对照

VideoCaptioner固定使用本次审查的上游版本：

```text
commit 95842ecb5618c0b6a548a336bdfb0eb859bdb501
```

对照运行需要单独的Python 3.10–3.12隔离环境和较多依赖。先完成轻量Groq流程，确认API、音频和时间轴正常，再安装并运行VideoCaptioner，避免在尚未验证Key之前下载无关的大型依赖。

比较时，两条路线必须使用相同的`comparison_audio.mp3`、Groq模型、语言和提示词。

## 离线测试

```bash
cd "/path/to/YouTube Transcript/mvp"
python3 -m unittest -v test_cloud_asr_mvp.py
```

## 下一轮端到端提速实验

`next_round_mvp.py`会连续测试两条独立链路，不修改正式字幕档案：

1. 默认使用`yt-dlp → ffmpeg → Groq`流水线，第一片根据音频测速在2–5分钟间自适应，后续每片10分钟，最多2路并发；
2. `--try-direct-url`保留为诊断选项；实测Groq读取YouTube临时URL会收到302，因此正式路径不会浪费时间尝试；
3. 自动测速最多4个低码率纯音频来源，首选源失败后按顺序降级；
4. 每个Groq片段成功后立即原子保存文字断点。暂停、异常退出或网络失败后，下一次不会重复调用已经成功的片段；整条视频完成后自动删除断点。

它只请求段落时间戳，并记录首段字幕出现时间与完整端到端时间：

```bash
cd "/path/to/YouTube Transcript"
python3 mvp/next_round_mvp.py
```

API Key优先从环境变量读取；没有环境变量时，从前述macOS钥匙串项目读取。临时音频片段在Groq确认完成后立即删除，YouTube临时音频URL不会写入结果。

真实验证记录：

- 自动选源选择了约50.6kbps的`249-drc`，14分16秒视频首段字幕5.8秒出现、8.1秒全部完成；
- 断点恢复时第一片明确跳过Groq，只识别剩余第二片；
- 故意使用不存在的音频格式后，程序自动切换到可用来源并完成；
- 测试成功后断点和临时音频均自动删除。
