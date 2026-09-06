# Third-party notices

The website-distributed macOS and Windows builds bundle the following third-party components:

- yt-dlp — <https://github.com/yt-dlp/yt-dlp> — The Unlicense.
- FFmpeg — <https://ffmpeg.org/> — LGPL 2.1 or later. The macOS build is compiled from the pinned official FFmpeg 9.0.1 source with only built-in codecs; the Windows build uses the BtbN LGPL static distribution. FFmpeg runs as a separate executable for temporary speech-audio conversion.
- PyInstaller bootloader/runtime — <https://pyinstaller.org/> — GPL with the PyInstaller bootloader exception.
- psutil (Windows GUI process controls) — <https://github.com/giampaolo/psutil> — BSD-3-Clause.
- Python and Tcl/Tk runtime (Windows build) — <https://www.python.org/> — Python Software Foundation License.

The optional AI transcription service is Groq Whisper. Users supply their own Groq API key; no key is included in the application. Audio is uploaded to Groq only when a video has no usable YouTube caption track and the AI-caption option is enabled.

These components are used only to make the desktop application self-contained. YouTube video files are not bundled or retained by this application. Temporary speech-only audio is deleted after transcription.
