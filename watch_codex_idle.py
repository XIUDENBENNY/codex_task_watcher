from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable


DEFAULT_POLL_SECONDS = 1.0
DEFAULT_LOOKBACK_BYTES = 1024 * 1024
APP_NAME = "Codex Task Watcher"
STARTUP_SHORTCUT_NAME = "Codex Task Watcher.lnk"
SETTINGS_DIR_NAME = "codex_task_watcher"
SETTINGS_FILE_NAME = "settings.json"
NOTIFY_SOUND_NAME = "notify.wav"
PRESET_SOUND_NAME = "ikunganma.aac"
SINGLE_INSTANCE_MUTEX_NAME = "Global\\CodexTaskWatcherTraySingleton"


@dataclass
class TaskWindow:
    turn_id: str | None
    started_at: float
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    @property
    def generated_tokens(self) -> int:
        return self.output_tokens + self.reasoning_output_tokens


@dataclass
class SessionState:
    path: Path
    offset: int
    remainder: str = ""
    workspace_dir: Path | None = None


@dataclass
class AppSettings:
    sound_enabled: bool = True
    sound_path: str | None = None
    sound_mode: str = "preset"


def debug(enabled: bool, message: str) -> None:
    if enabled:
        print(message, flush=True)


def get_app_root() -> Path:
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        return Path(bundle_dir)
    return Path(__file__).resolve().parent


def is_frozen_app() -> bool:
    return bool(getattr(sys, "frozen", False))


def get_settings_dir() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / SETTINGS_DIR_NAME


def get_settings_path() -> Path | None:
    settings_dir = get_settings_dir()
    if settings_dir is None:
        return None
    return settings_dir / SETTINGS_FILE_NAME


def load_settings() -> AppSettings:
    settings_path = get_settings_path()
    if settings_path is None or not settings_path.exists():
        return get_default_settings()

    try:
        with settings_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return get_default_settings()

    if not isinstance(payload, dict):
        return get_default_settings()

    sound_enabled = payload.get("sound_enabled", True)
    sound_path = payload.get("sound_path")
    sound_mode = payload.get("sound_mode")
    if not isinstance(sound_path, str) or not sound_path.strip():
        sound_path = None
    if sound_mode not in {"preset", "default", "custom"}:
        sound_mode = None

    if sound_mode is None:
        sound_mode = "custom" if sound_path else "preset"

    return AppSettings(
        sound_enabled=bool(sound_enabled),
        sound_path=sound_path,
        sound_mode=sound_mode,
    )


def save_settings(settings: AppSettings) -> tuple[bool, str]:
    settings_dir = get_settings_dir()
    settings_path = get_settings_path()
    if settings_dir is None or settings_path is None:
        return False, "\u627e\u4e0d\u5230\u914d\u7f6e\u76ee\u5f55"

    try:
        settings_dir.mkdir(parents=True, exist_ok=True)
        with settings_path.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "sound_enabled": settings.sound_enabled,
                    "sound_path": settings.sound_path,
                    "sound_mode": settings.sound_mode,
                },
                handle,
                ensure_ascii=True,
                indent=2,
            )
            handle.write("\n")
    except OSError:
        return False, "\u4fdd\u5b58\u63d0\u793a\u97f3\u8bbe\u7f6e\u5931\u8d25"

    return True, (
        "\u5df2\u5f00\u542f\u63d0\u793a\u97f3"
        if settings.sound_enabled
        else "\u5df2\u5173\u95ed\u63d0\u793a\u97f3"
    )


def get_default_settings() -> AppSettings:
    preset_sound = get_preset_sound_path()
    return AppSettings(
        sound_enabled=True,
        sound_path=str(preset_sound) if preset_sound.exists() else None,
        sound_mode="preset",
    )


def parse_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def find_latest_session_file() -> Path | None:
    user_profile = os.environ.get("USERPROFILE")
    if not user_profile:
        return None

    root = Path(user_profile) / ".codex" / "sessions"
    if not root.exists():
        return None

    matches = list(root.glob("*/*/*/rollout-*.jsonl"))
    if not matches:
        return None

    return max(matches, key=lambda item: item.stat().st_mtime)


def parse_workspace_dir(value: object) -> Path | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return Path(text)
    except Exception:
        return None


def read_session_workspace(path: Path) -> Path | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    break
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "session_meta":
                    continue
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    return None
                return parse_workspace_dir(payload.get("cwd"))
    except OSError:
        return None
    return None


def attach_session(
    current: SessionState | None,
    lookback_bytes: int,
) -> SessionState | None:
    path = find_latest_session_file()
    if path is None or not path.exists():
        return current

    if current and current.path == path:
        return current

    size = path.stat().st_size
    return SessionState(
        path=path,
        offset=max(0, size - lookback_bytes),
        workspace_dir=read_session_workspace(path),
    )


def read_new_entries(state: SessionState) -> list[dict]:
    try:
        size = state.path.stat().st_size
    except FileNotFoundError:
        return []

    if size < state.offset:
        state.offset = 0
        state.remainder = ""

    if size == state.offset:
        return []

    with state.path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(state.offset)
        chunk = handle.read()
        state.offset = handle.tell()

    data = state.remainder + chunk
    state.remainder = ""
    if not data:
        return []

    lines = data.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        state.remainder = lines.pop()

    entries: list[dict] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def apply_token_count(window: TaskWindow, payload: dict) -> None:
    info = payload.get("info")
    if not isinstance(info, dict):
        return
    last_usage = info.get("last_token_usage")
    if not isinstance(last_usage, dict):
        return

    output_tokens = last_usage.get("output_tokens", 0)
    reasoning_output_tokens = last_usage.get("reasoning_output_tokens", 0)

    if isinstance(output_tokens, int) and output_tokens > 0:
        window.output_tokens += output_tokens
    if isinstance(reasoning_output_tokens, int) and reasoning_output_tokens > 0:
        window.reasoning_output_tokens += reasoning_output_tokens


def seed_open_task(entries: list[dict]) -> TaskWindow | None:
    window: TaskWindow | None = None
    for entry in entries:
        if entry.get("type") != "event_msg":
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue

        event_type = payload.get("type")
        ts = parse_timestamp(entry.get("timestamp")) or time.time()

        if event_type == "task_started":
            turn_id = payload.get("turn_id")
            window = TaskWindow(
                turn_id=turn_id if isinstance(turn_id, str) else None,
                started_at=ts,
            )
        elif event_type == "token_count" and window is not None:
            apply_token_count(window, payload)
        elif event_type == "task_complete":
            window = None

    return window


def beep() -> None:
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONASTERISK)
        return
    except Exception:
        pass

    sys.stdout.write("\a")
    sys.stdout.flush()


def get_notify_sound_path() -> Path:
    return get_app_root() / "assets" / NOTIFY_SOUND_NAME


def get_preset_sound_path() -> Path:
    return get_app_root() / "assets" / PRESET_SOUND_NAME


def get_effective_sound_path(settings: AppSettings | None = None) -> Path:
    resolved_settings = settings or load_settings()
    if resolved_settings.sound_mode == "custom" and resolved_settings.sound_path:
        custom_path = Path(resolved_settings.sound_path).expanduser()
        if custom_path.exists() and custom_path.is_file():
            return custom_path
    if resolved_settings.sound_mode == "default":
        return get_notify_sound_path()

    preset_path = get_preset_sound_path()
    if preset_path.exists():
        return preset_path
    return get_notify_sound_path()


def run_hidden_powershell_with_env(command: str, env: dict[str, str], wait: bool = False) -> bool:
    kwargs = {
        "args": ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": {**os.environ, **env},
    }

    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        if wait:
            result = subprocess.run(timeout=15, check=False, **kwargs)
            return result.returncode == 0
        subprocess.Popen(**kwargs)
        return True
    except Exception:
        return False


def play_media_sound_via_powershell(sound_path: Path, wait_until_done: bool = False) -> bool:
    powershell_command = r"""
Add-Type -AssemblyName PresentationCore
$path = $env:CODEX_TASK_WATCHER_SOUND
$uri = New-Object System.Uri($path)
$player = New-Object System.Windows.Media.MediaPlayer
$player.Open($uri)
$deadline = [DateTime]::UtcNow.AddSeconds(3)
while (-not $player.NaturalDuration.HasTimeSpan -and [DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 100
}
$player.Play()
$waitMs = 3000
if ($player.NaturalDuration.HasTimeSpan) {
    $waitMs = [Math]::Max(400, [int]($player.NaturalDuration.TimeSpan.TotalMilliseconds + 150))
}
Start-Sleep -Milliseconds $waitMs
$player.Stop()
$player.Close()
"""
    return run_hidden_powershell_with_env(
        powershell_command,
        {"CODEX_TASK_WATCHER_SOUND": str(sound_path)},
        wait=wait_until_done,
    )


def play_notification_sound(
    wait_until_done: bool = False,
    settings: AppSettings | None = None,
) -> bool:
    sound_path = get_effective_sound_path(settings)
    if not sound_path.exists():
        return False

    if sound_path.suffix.lower() != ".wav":
        return play_media_sound_via_powershell(sound_path, wait_until_done=wait_until_done)

    try:
        import winsound

        flags = winsound.SND_FILENAME | winsound.SND_NODEFAULT
        if not wait_until_done:
            flags |= winsound.SND_ASYNC

        winsound.PlaySound(
            str(sound_path),
            flags,
        )
        return True
    except Exception:
        return False


def run_hidden_powershell(command: str) -> bool:
    return run_hidden_powershell_with_env(command, {}, wait=False)


def show_info_toast(message: str, duration_seconds: float = 3.5) -> bool:
    return show_custom_toast_process(APP_NAME, message, None, duration_seconds)


def acquire_single_instance_mutex() -> object | None:
    if os.name != "nt":
        return object()

    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
        if not handle:
            return None
        if kernel32.GetLastError() == 183:
            kernel32.CloseHandle(handle)
            return None
        return handle
    except Exception:
        return object()


def release_single_instance_mutex(handle: object | None) -> None:
    if handle is None or os.name != "nt" or not isinstance(handle, int):
        return
    try:
        ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass


def show_custom_toast_process(
    title: str,
    message: str,
    workspace_dir: Path | None = None,
    duration_seconds: float = 5.0,
) -> bool:
    executable = Path(sys.executable)
    candidates = [executable]
    if not is_frozen_app() and executable.name.lower() == "python.exe":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            candidates.insert(0, pythonw)

    for candidate in candidates:
        args = []
        if is_frozen_app():
            args.append(str(candidate))
        else:
            args.extend([str(candidate), str(Path(__file__).resolve())])
        kwargs = {
            "args": [
                *args,
                "--show-toast-title",
                title,
                "--show-toast-message",
                message,
                "--toast-seconds",
                str(duration_seconds),
            ],
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if workspace_dir is not None:
            kwargs["args"].extend(["--show-toast-workspace", str(workspace_dir)])
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            subprocess.Popen(**kwargs)
            return True
        except Exception:
            continue
    return False


def list_cursor_windows() -> list[tuple[int, str]]:
    if os.name != "nt":
        return []

    user32 = ctypes.windll.user32
    windows: list[tuple[int, str]] = []

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if title and "cursor" in title.lower():
            windows.append((hwnd, title))
        return True

    user32.EnumWindows(enum_proc(callback), 0)
    return windows


def pick_cursor_window(workspace_dir: Path | None = None) -> tuple[int, str] | None:
    windows = list_cursor_windows()
    if not windows:
        return None

    project_name = workspace_dir.name.lower() if workspace_dir else Path.cwd().name.lower()
    preferred: tuple[int, str] | None = None
    fallback: tuple[int, str] | None = None

    for hwnd, title in windows:
        lowered = title.lower()
        if fallback is None:
            fallback = (hwnd, title)
        if project_name and project_name in lowered:
            preferred = (hwnd, title)
            break

    return preferred or fallback


def is_cursor_foreground(workspace_dir: Path | None = None) -> bool:
    if os.name != "nt":
        return False

    try:
        foreground_hwnd = ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        return False

    if not foreground_hwnd:
        return False

    target = pick_cursor_window(workspace_dir)
    if target is not None and foreground_hwnd == target[0]:
        return True

    for hwnd, _title in list_cursor_windows():
        if hwnd == foreground_hwnd:
            return True
    return False


def find_cursor_command() -> list[str] | None:
    cursor_on_path = shutil.which("cursor")
    if cursor_on_path:
        suffix = Path(cursor_on_path).suffix.lower()
        if suffix in {".cmd", ".bat"}:
            return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", cursor_on_path]
        return [cursor_on_path]

    candidates = [
        Path("D:/cursor/resources/app/bin/cursor.cmd"),
        Path("D:/cursor/Cursor.exe"),
        Path.home() / "AppData/Local/Programs/Cursor/resources/app/bin/cursor.cmd",
        Path.home() / "AppData/Local/Programs/Cursor/Cursor.exe",
        Path("C:/Program Files/Cursor/Cursor.exe"),
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        if candidate.suffix.lower() in {".cmd", ".bat"}:
            return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(candidate)]
        return [str(candidate)]
    return None


def open_cursor_workspace(workspace_dir: Path | None) -> bool:
    if workspace_dir is None:
        return False

    command = find_cursor_command()
    if command is None:
        return False

    kwargs = {
        "args": [*command, "--reuse-window", str(workspace_dir)],
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if workspace_dir.exists():
        kwargs["cwd"] = str(workspace_dir)
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        subprocess.Popen(**kwargs)
        return True
    except Exception:
        return False


def focus_cursor(workspace_dir: Path | None = None) -> bool:
    target = pick_cursor_window(workspace_dir)
    if target is None:
        return False

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd, title = target
    sw_restore = 9

    def is_foreground() -> bool:
        try:
            return user32.GetForegroundWindow() == hwnd
        except Exception:
            return False

    def app_activate() -> bool:
        script = f"""
$shell = New-Object -ComObject WScript.Shell
$null = $shell.SendKeys('%')
Start-Sleep -Milliseconds 60
if ($shell.AppActivate('{title.replace("'", "''")}')) {{ exit 0 }}
exit 1
"""
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
            return result.returncode == 0
        except Exception:
            return False

    def direct_activate() -> bool:
        foreground_hwnd = user32.GetForegroundWindow()
        current_thread_id = kernel32.GetCurrentThreadId()
        foreground_thread_id = user32.GetWindowThreadProcessId(foreground_hwnd, None)
        target_thread_id = user32.GetWindowThreadProcessId(hwnd, None)
        attached_thread_ids: list[int] = []

        try:
            user32.ShowWindow(hwnd, sw_restore)
            for thread_id in {foreground_thread_id, target_thread_id}:
                if thread_id and thread_id != current_thread_id:
                    if user32.AttachThreadInput(current_thread_id, thread_id, True):
                        attached_thread_ids.append(thread_id)

            user32.BringWindowToTop(hwnd)
            user32.SetActiveWindow(hwnd)
            user32.SetFocus(hwnd)
            user32.SetForegroundWindow(hwnd)
            if hasattr(user32, "SwitchToThisWindow"):
                user32.SwitchToThisWindow(hwnd, True)
        finally:
            for thread_id in reversed(attached_thread_ids):
                try:
                    user32.AttachThreadInput(current_thread_id, thread_id, False)
                except Exception:
                    pass

        time.sleep(0.08)
        return is_foreground()

    try:
        if is_foreground():
            return True
        if direct_activate():
            return True
    except Exception:
        pass

    if app_activate():
        time.sleep(0.08)
        return is_foreground()

    return False


def render_custom_toast(
    title: str,
    message: str,
    duration_seconds: float,
    workspace_dir: Path | None = None,
) -> int:
    try:
        import tkinter as tk
    except Exception:
        return 1

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#d8dde6")

    width = 364
    height = 118
    margin = 16
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    x = screen_width - width - margin
    y = screen_height - height - 56
    root.geometry(f"{width}x{height}+{x}+{y}")

    container = tk.Frame(root, bg="#fbfbfd", highlightbackground="#d3d8e1", highlightthickness=1)
    container.pack(fill="both", expand=True, padx=1, pady=1)

    header = tk.Frame(container, bg="#fbfbfd", padx=14, pady=10)
    header.pack(fill="x")

    icon = tk.Canvas(header, width=18, height=18, bg="#fbfbfd", highlightthickness=0)
    icon.pack(side="left")
    icon.create_oval(1, 1, 17, 17, fill="#0078d4", outline="#0078d4")
    icon.create_text(9, 9, text="i", fill="#ffffff", font=("Segoe UI", 10, "bold"))

    title_label = tk.Label(
        header,
        text=title,
        bg="#fbfbfd",
        fg="#4b5563",
        anchor="w",
        font=("Segoe UI", 9),
        padx=8,
    )
    title_label.pack(side="left")

    body = tk.Frame(container, bg="#fbfbfd", padx=14, pady=0)
    body.pack(fill="both", expand=True)

    message_label = tk.Label(
        body,
        text=message,
        bg="#fbfbfd",
        fg="#111827",
        anchor="nw",
        justify="left",
        wraplength=320,
        font=("Segoe UI", 11),
    )
    message_label.pack(fill="both", expand=True)

    footer = tk.Label(
        container,
        text="\u70b9\u51fb\u8fd4\u56de Cursor",
        bg="#fbfbfd",
        fg="#6b7280",
        anchor="w",
        font=("Segoe UI", 9),
        padx=14,
        pady=8,
    )
    footer.pack(fill="x")

    def handle_click(_event=None) -> None:
        target_exists = pick_cursor_window(workspace_dir) is not None
        focused = focus_cursor(workspace_dir) if target_exists else False
        if not focused and not target_exists:
            launched = open_cursor_workspace(workspace_dir)
            if launched:
                time.sleep(0.6)
                focus_cursor(workspace_dir)
        root.destroy()

    for widget in (root, container, header, icon, title_label, body, message_label, footer):
        widget.bind("<Button-1>", handle_click)
        widget.configure(cursor="hand2")

    root.after(max(1000, int(duration_seconds * 1000)), root.destroy)
    root.mainloop()
    return 0


def choose_sound_file() -> Path | None:
    powershell_script = r"""
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = '选择提示音'
$dialog.Filter = '音频文件|*.wav;*.aac;*.mp3;*.m4a;*.wma|所有文件|*.*'
$dialog.Multiselect = $false
$dialog.CheckFileExists = $true
$dialog.RestoreDirectory = $true
$result = $dialog.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK -and $dialog.FileName) {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    Write-Output $dialog.FileName
}
"""
    kwargs = {
        "args": [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-STA",
            "-Command",
            powershell_script,
        ],
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 120,
        "check": False,
    }
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    dialog_failed = False
    try:
        result = subprocess.run(**kwargs)
        selected = (result.stdout or "").strip()
        if selected:
            return Path(selected)
        if result.returncode == 0:
            return None
        dialog_failed = True
    except Exception:
        dialog_failed = True

    if not dialog_failed:
        return None

    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None

    root = tk.Tk()
    root.withdraw()
    try:
        selected = filedialog.askopenfilename(
            title="\u9009\u62e9\u63d0\u793a\u97f3",
            filetypes=[
                ("\u97f3\u9891\u6587\u4ef6", "*.wav *.aac *.mp3 *.m4a *.wma"),
                ("\u6240\u6709\u6587\u4ef6", "*.*"),
            ],
        )
    finally:
        root.destroy()

    if not selected:
        return None
    return Path(selected)


def show_windows_popup(title: str, message: str) -> bool:
    command = rf"""
$shell = New-Object -ComObject WScript.Shell
$null = $shell.Popup('{message.replace("'", "''")}', 5, '{title.replace("'", "''")}', 64)
"""
    return run_hidden_powershell(command)


def notify(
    window: TaskWindow,
    completed_at: float,
    session_path: Path,
    workspace_dir: Path | None,
    sound_wait_until_done: bool = False,
) -> None:
    settings = load_settings()
    duration = max(0.0, completed_at - window.started_at)
    title = "OpenAI Codex"
    message = f"\u4efb\u52a1\u5df2\u5b8c\u6210\uff0c\u7528\u65f6 {duration:.0f} \u79d2"
    if window.generated_tokens > 0:
        message += f"  \u00b7  Tokens {window.generated_tokens}"

    suppress_notification = is_cursor_foreground(workspace_dir)

    if settings.sound_enabled and not suppress_notification:
        play_notification_sound(
            wait_until_done=sound_wait_until_done,
            settings=settings,
        )

    toast_state: str | bool = "suppressed" if suppress_notification else False
    if toast_state is False:
        shown = show_custom_toast_process(title, message, workspace_dir)
        if not shown:
            shown = show_windows_popup(title, message)
        toast_state = shown
    print(
        f"[notify] task_complete from {session_path} "
        f"(turn_id={window.turn_id or 'unknown'}, duration={duration:.1f}s, "
        f"tokens={window.generated_tokens}, toast={toast_state})",
        flush=True,
    )


def format_status_label(state: SessionState | None) -> str:
    if state is None:
        return "Codex"
    if state.workspace_dir is not None:
        return state.workspace_dir.name
    return state.path.name


def watch_sessions(
    poll_seconds: float,
    lookback_bytes: int,
    debug_enabled: bool,
    once: bool = False,
    stop_event: threading.Event | None = None,
    status_callback: Callable[[str], None] | None = None,
    output_enabled: bool = True,
) -> int:
    state: SessionState | None = None
    window: TaskWindow | None = None
    missing_session_reported = False

    def set_status(message: str) -> None:
        if status_callback is not None:
            status_callback(message)

    def log(message: str) -> None:
        if output_enabled:
            print(message, flush=True)

    def sleep_or_stop() -> bool:
        if stop_event is None:
            time.sleep(poll_seconds)
            return False
        return stop_event.wait(poll_seconds)

    log("Watching Codex session events...")
    set_status("\u6b63\u5728\u76d1\u542c Codex")

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                return 0

            previous_path = state.path if state else None
            state = attach_session(state, lookback_bytes)
            if state is None:
                set_status("\u7b49\u5f85 Codex \u4f1a\u8bdd")
                if not missing_session_reported:
                    log("No Codex session file found yet. Waiting...")
                    missing_session_reported = True
                if sleep_or_stop():
                    return 0
                continue

            if previous_path != state.path:
                log(f"Watching session: {state.path}")
                missing_session_reported = False
                set_status(f"\u76d1\u542c\u4e2d: {format_status_label(state)}")
                entries = read_new_entries(state)
                window = seed_open_task(entries)
                if window is not None:
                    log(
                        "Seeded active task from session history "
                        f"(turn_id={window.turn_id or 'unknown'}, tokens={window.generated_tokens})"
                    )
                    set_status(f"\u4efb\u52a1\u8fdb\u884c\u4e2d: {format_status_label(state)}")
                else:
                    debug(debug_enabled, "[debug] no open task found in recent session history")
                if sleep_or_stop():
                    return 0
                continue

            entries = read_new_entries(state)
            if debug_enabled and entries:
                debug(debug_enabled, f"[debug] parsed {len(entries)} new session events")

            for entry in entries:
                if entry.get("type") == "session_meta":
                    payload = entry.get("payload")
                    if isinstance(payload, dict):
                        state.workspace_dir = parse_workspace_dir(payload.get("cwd"))
                        set_status(f"\u76d1\u542c\u4e2d: {format_status_label(state)}")
                    continue

                if entry.get("type") != "event_msg":
                    continue
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    continue

                event_type = payload.get("type")
                event_ts = parse_timestamp(entry.get("timestamp")) or time.time()

                if event_type == "task_started":
                    turn_id = payload.get("turn_id")
                    window = TaskWindow(
                        turn_id=turn_id if isinstance(turn_id, str) else None,
                        started_at=event_ts,
                    )
                    log(f"Task started (turn_id={window.turn_id or 'unknown'})")
                    set_status(f"\u4efb\u52a1\u8fdb\u884c\u4e2d: {format_status_label(state)}")
                    continue

                if event_type == "token_count" and window is not None:
                    before = window.generated_tokens
                    apply_token_count(window, payload)
                    if debug_enabled and window.generated_tokens != before:
                        debug(
                            debug_enabled,
                            f"[debug] token_count total={window.generated_tokens}",
                        )
                    continue

                if event_type == "task_complete":
                    if window is None:
                        debug(debug_enabled, "[debug] saw task_complete without an open task")
                        continue
                    notify(window, event_ts, state.path, state.workspace_dir)
                    set_status(f"\u6700\u8fd1\u5b8c\u6210: {format_status_label(state)}")
                    window = None
                    if once:
                        return 0

            if sleep_or_stop():
                return 0
    except KeyboardInterrupt:
        log("Stopped.")
        return 0


class WatcherService:
    def __init__(self, poll_seconds: float, lookback_bytes: int, debug_enabled: bool) -> None:
        self.poll_seconds = poll_seconds
        self.lookback_bytes = lookback_bytes
        self.debug_enabled = debug_enabled
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = "\u672a\u542f\u52a8"

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._run,
                name="codex-task-watcher",
                daemon=True,
            )
            self._status = "\u542f\u52a8\u4e2d"
            self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        with self._lock:
            thread = self._thread
            self._stop_event.set()

        if thread is None:
            with self._lock:
                self._status = "\u5df2\u505c\u6b62"
            return True

        thread.join(timeout)
        stopped = not thread.is_alive()
        if stopped:
            with self._lock:
                if self._thread is thread:
                    self._thread = None
                if not self._status.startswith("\u9519\u8bef"):
                    self._status = "\u5df2\u505c\u6b62"
        return stopped

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def get_status_text(self) -> str:
        with self._lock:
            return self._status

    def _set_status(self, message: str) -> None:
        with self._lock:
            self._status = message

    def _run(self) -> None:
        try:
            watch_sessions(
                poll_seconds=self.poll_seconds,
                lookback_bytes=self.lookback_bytes,
                debug_enabled=self.debug_enabled,
                stop_event=self._stop_event,
                status_callback=self._set_status,
                output_enabled=self.debug_enabled,
            )
        except Exception as exc:
            self._set_status(f"\u9519\u8bef: {type(exc).__name__}")
            if self.debug_enabled:
                print(f"[tray] watcher crashed: {exc}", flush=True)
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
                if not self._status.startswith("\u9519\u8bef"):
                    self._status = "\u5df2\u505c\u6b62"


def get_startup_shortcut_path() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup" / STARTUP_SHORTCUT_NAME


def is_startup_enabled() -> bool:
    shortcut_path = get_startup_shortcut_path()
    return shortcut_path is not None and shortcut_path.exists()


def set_startup_enabled(enabled: bool) -> tuple[bool, str]:
    shortcut_path = get_startup_shortcut_path()
    if shortcut_path is None:
        return False, "\u627e\u4e0d\u5230\u542f\u52a8\u76ee\u5f55"

    launcher_path = Path(__file__).resolve().with_name("start_codex_watcher.vbs")
    if enabled:
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        wscript_path = system_root / "System32/wscript.exe"
        shortcut_path.parent.mkdir(parents=True, exist_ok=True)

        if is_frozen_app():
            target_path = str(Path(sys.executable))
            arguments = "--tray"
            working_directory = str(Path(sys.executable).resolve().parent)
        else:
            if not launcher_path.exists():
                return False, "\u627e\u4e0d\u5230 start_codex_watcher.vbs"
            target_path = str(wscript_path)
            arguments = f'"{str(launcher_path)}"'
            working_directory = str(launcher_path.parent)

        powershell_command = f"""
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut('{str(shortcut_path).replace("'", "''")}')
$shortcut.TargetPath = '{target_path.replace("'", "''")}'
$shortcut.Arguments = '{arguments.replace("'", "''")}'
$shortcut.WorkingDirectory = '{working_directory.replace("'", "''")}'
$shortcut.IconLocation = '{sys.executable.replace("'", "''")},0'
$shortcut.Description = '{APP_NAME}'
$shortcut.Save()
"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", powershell_command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return False, "\u521b\u5efa\u5f00\u673a\u542f\u52a8\u5feb\u6377\u65b9\u5f0f\u5931\u8d25"
        return True, "\u5df2\u542f\u7528\u5f00\u673a\u542f\u52a8"

    try:
        shortcut_path.unlink(missing_ok=True)
        return True, "\u5df2\u5173\u95ed\u5f00\u673a\u542f\u52a8"
    except OSError:
        return False, "\u5220\u9664\u5f00\u673a\u542f\u52a8\u5feb\u6377\u65b9\u5f0f\u5931\u8d25"


def create_tray_icon_image():
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((6, 6, 58, 58), radius=14, fill=(17, 24, 39, 255))
    draw.rounded_rectangle((14, 14, 50, 50), radius=10, fill=(0, 120, 212, 255))
    draw.arc((20, 20, 44, 44), start=40, end=320, fill=(255, 255, 255, 255), width=4)
    draw.rectangle((32, 16, 38, 28), fill=(0, 120, 212, 255))
    return image


def run_tray_app(args: argparse.Namespace) -> int:
    try:
        import pystray
    except Exception:
        print("pystray is required for --tray mode.", flush=True)
        return 1

    mutex_handle = acquire_single_instance_mutex()
    if mutex_handle is None:
        show_info_toast("\u5df2\u5728\u6258\u76d8\u8fd0\u884c")
        return 0

    service = WatcherService(args.poll_seconds, args.lookback_bytes, args.debug)
    service.start()

    def notify_tray(icon, message: str) -> None:
        try:
            icon.notify(message, APP_NAME)
        except Exception:
            pass

    def status_text(_item) -> str:
        return f"\u72b6\u6001: {service.get_status_text()}"

    def toggle_watch_text(_item) -> str:
        return "\u505c\u6b62\u76d1\u63a7" if service.is_running() else "\u5f00\u59cb\u76d1\u63a7"

    def toggle_watch(icon, _item) -> None:
        if service.is_running():
            stopped = service.stop()
            notify_tray(
                icon,
                "\u76d1\u63a7\u5df2\u505c\u6b62" if stopped else "\u505c\u6b62\u76d1\u63a7\u8d85\u65f6",
            )
        else:
            service.start()
            notify_tray(icon, "\u76d1\u63a7\u5df2\u542f\u52a8")

    def toggle_sound(icon, _item) -> None:
        settings = load_settings()
        settings.sound_enabled = not settings.sound_enabled
        success, message = save_settings(settings)
        notify_tray(icon, message)
        if not success and args.debug:
            print(f"[tray] {message}", flush=True)

    def choose_custom_sound(icon, _item) -> None:
        selected = choose_sound_file()
        if selected is None:
            return

        settings = load_settings()
        settings.sound_path = str(selected)
        settings.sound_mode = "custom"
        success, message = save_settings(settings)
        if success:
            message = f"\u5df2\u8bbe\u7f6e\u81ea\u5b9a\u4e49\u63d0\u793a\u97f3: {selected.name}"
        notify_tray(icon, message)
        if not success and args.debug:
            print(f"[tray] {message}", flush=True)

    def reset_sound(icon, _item) -> None:
        settings = load_settings()
        settings.sound_path = None
        settings.sound_mode = "default"
        success, message = save_settings(settings)
        if success:
            message = "\u5df2\u6062\u590d\u9ed8\u8ba4\u63d0\u793a\u97f3"
        notify_tray(icon, message)
        if not success and args.debug:
            print(f"[tray] {message}", flush=True)

    def test_notify(_icon, _item) -> None:
        now = time.time()
        notify(
            TaskWindow(
                turn_id="manual-test",
                started_at=now - 6,
                output_tokens=12,
                reasoning_output_tokens=6,
            ),
            now - 1,
            Path("manual-test"),
            Path.cwd(),
        )

    def toggle_startup(icon, _item) -> None:
        success, message = set_startup_enabled(not is_startup_enabled())
        notify_tray(icon, message)
        if not success and args.debug:
            print(f"[tray] {message}", flush=True)

    def quit_app(icon, _item) -> None:
        service.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem(toggle_watch_text, toggle_watch),
        pystray.MenuItem("\u6d4b\u8bd5\u901a\u77e5", test_notify),
        pystray.MenuItem(
            "\u63d0\u793a\u97f3",
            toggle_sound,
            checked=lambda _item: load_settings().sound_enabled,
        ),
        pystray.MenuItem("\u9009\u62e9\u63d0\u793a\u97f3", choose_custom_sound),
        pystray.MenuItem("\u6062\u590d\u9ed8\u8ba4\u63d0\u793a\u97f3", reset_sound),
        pystray.MenuItem(
            "\u5f00\u673a\u542f\u52a8",
            toggle_startup,
            checked=lambda _item: is_startup_enabled(),
        ),
        pystray.MenuItem("\u9000\u51fa", quit_app),
    )
    icon = pystray.Icon("codex_task_watcher", create_tray_icon_image(), APP_NAME, menu)
    show_info_toast("\u5df2\u542f\u52a8\uff0c\u53ef\u5728\u7cfb\u7edf\u6258\u76d8\u4e2d\u627e\u5230\u56fe\u6807")
    try:
        icon.run()
        return 0
    finally:
        release_single_instance_mutex(mutex_handle)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Watch Codex session events and notify when a task completes."
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        help="Polling interval for checking new session events.",
    )
    parser.add_argument(
        "--lookback-bytes",
        type=int,
        default=DEFAULT_LOOKBACK_BYTES,
        help="How much recent session history to inspect when attaching mid-task.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Exit after the first notification.",
    )
    parser.add_argument(
        "--test-notify",
        action="store_true",
        help="Show a test notification immediately and exit.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print detailed watcher state transitions for diagnosis.",
    )
    parser.add_argument(
        "--tray",
        action="store_true",
        help="Run in the Windows system tray.",
    )
    parser.add_argument(
        "--show-toast-title",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--show-toast-message",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--toast-seconds",
        type=float,
        default=5.0,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--show-toast-workspace",
        default=None,
        help=argparse.SUPPRESS,
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    should_default_to_tray = (
        is_frozen_app()
        and len(sys.argv) == 1
        and args.show_toast_title is None
        and args.show_toast_message is None
        and not args.test_notify
    )

    if args.show_toast_title is not None and args.show_toast_message is not None:
        return render_custom_toast(
            args.show_toast_title,
            args.show_toast_message,
            args.toast_seconds,
            parse_workspace_dir(args.show_toast_workspace),
        )

    if args.tray or should_default_to_tray:
        return run_tray_app(args)

    if args.test_notify:
        now = time.time()
        notify(
            TaskWindow(
                turn_id="manual-test",
                started_at=now - 6,
                output_tokens=12,
                reasoning_output_tokens=6,
            ),
            now - 1,
            Path("manual-test"),
            Path.cwd(),
            sound_wait_until_done=True,
        )
        return 0

    return watch_sessions(
        poll_seconds=args.poll_seconds,
        lookback_bytes=args.lookback_bytes,
        debug_enabled=args.debug,
        once=args.once,
    )


if __name__ == "__main__":
    raise SystemExit(main())
