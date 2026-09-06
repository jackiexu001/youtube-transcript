import plistlib
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class DesktopReleaseTests(unittest.TestCase):
    def test_macos_unsigned_build_uses_compatible_keychain(self):
        source = (ROOT / "macos" / "YouTubeCaptionApp.swift").read_text(encoding="utf-8")
        self.assertNotIn("kSecUseDataProtectionKeychain", source)
        self.assertIn('NSButton(title: storedAPIKey.isEmpty ? "保存 Key" : "已保存"', source)
        self.assertIn("saveSelectedAPIKey", source)
        self.assertIn("YouTubeCaptionDidShowInstallerCleanupNotice-\\(noticeVersion)", source)
        self.assertIn("为什么搜索到多个应用？", source)
        self.assertIn('"跟随系统（System）"', source)
        self.assertIn('"浅色（Light）"', source)
        self.assertIn('"深色（Dark）"', source)
        self.assertIn("NSApp.appearance = nil", source)
        self.assertIn("NSAppearance(named: .aqua)", source)
        self.assertIn("NSAppearance(named: .darkAqua)", source)
        self.assertIn('UserDefaults(suiteName: "com.youtube-caption.archive")', source)

        with (ROOT / "macos" / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
        self.assertEqual(info["CFBundleIdentifier"], "com.jackiexu.youtube-transcript")

    def test_windows_has_explicit_save_key_action(self):
        source = (ROOT / "windows" / "YouTubeCaptionApp.py").read_text(encoding="utf-8")
        self.assertIn('text="保存 Key"', source)
        self.assertIn("def save_ai_key(self):", source)
        self.assertIn('f"youtube-caption-channel-{os.getpid()}.txt"', source)
        self.assertIn("def remove_channel_file(self):", source)

    def test_release_versions_stay_in_sync(self):
        with (ROOT / "macos" / "Info.plist").open("rb") as handle:
            mac_version = plistlib.load(handle)["CFBundleShortVersionString"]
        ps1 = (ROOT / "build_windows_exe.ps1").read_text(encoding="utf-8")
        iss = (ROOT / "windows" / "installer.iss").read_text(encoding="utf-8")
        ps1_version = re.search(r'\$Version = "([^"]+)"', ps1).group(1)
        iss_version = re.search(r'#define AppVersion "([^"]+)"', iss).group(1)
        self.assertEqual(mac_version, ps1_version)
        self.assertEqual(mac_version, iss_version)

    def test_dmg_contains_one_application_and_applications_shortcut(self):
        settings = (ROOT / "macos" / "dmg_settings.py").read_text(encoding="utf-8")
        self.assertIn("files = [application]", settings)
        self.assertIn('symlinks = {"Applications": "/Applications"}', settings)

    def test_macos_checksum_is_portable(self):
        script = (ROOT / "macos" / "build_release_macos.sh").read_text(encoding="utf-8")
        self.assertIn('cd "$RELEASE_DIR"', script)
        self.assertIn('shasum -a 256 "$DMG_NAME"', script)

    def test_macos_debug_app_is_not_left_in_spotlight_visible_project_root(self):
        script = (ROOT / "macos" / "build_macos_app.sh").read_text(encoding="utf-8")
        self.assertIn(".build/Products.noindex/YouTube Transcript.app", script)
        self.assertNotIn('YCA_OUTPUT_APP:-$PROJECT_ROOT/YouTube Transcript.app', script)

    def test_legacy_launchers_are_only_created_when_explicitly_requested(self):
        source = (ROOT / "youtube_caption.py").read_text(encoding="utf-8")
        self.assertIn('if args.install_launchers and (root / "channels.json").exists():', source)

    def test_windows_guide_matches_current_release_version(self):
        with (ROOT / "macos" / "Info.plist").open("rb") as handle:
            version = plistlib.load(handle)["CFBundleShortVersionString"]
        guide = (ROOT / "WINDOWS_DOWNLOAD_GUIDE.txt").read_text(encoding="utf-8")
        self.assertIn(f"YouTube-Transcript-{version}-Windows-x64-Setup.exe", guide)


if __name__ == "__main__":
    unittest.main()
