# codex_task_watcher

让 Codex 任务完成后，在 Windows 上给你一个更顺手的提醒。✨

## 功能一览 🚀

- 监听本机 `~/.codex/sessions` 中的最新会话
- 任务完成后弹出本地 toast 提示
- 点击 toast 可跳回对应的 Cursor 工作区
- 支持托盘常驻运行 🖥️
- 支持开机启动开关
- 支持提示音开关、恢复默认提示音、选择自定义语音包 🔊

## 这版提示音说明 🎵

- 首次运行且还没有配置文件时，会默认预选 `assets/ikunganma.aac`
- 也就是说，发布版默认听到的是爱坤的“你干嘛” 😄
- 如果你不想用它，可以在托盘菜单里点“恢复默认提示音”
- 恢复默认后，会回到内置提示音 `assets/notify.wav`
- 你也可以自己选本地语音包，比如 `wav`、`aac`、`mp3`、`m4a`、`wma`

## 使用方式 ✅

### 日常使用

直接双击：

- `dist\CodexTaskWatcher\CodexTaskWatcher.exe`

双击后会直接进入托盘模式，不需要再跑 `.vbs`。

### 调试运行

```powershell
cd D:\codex_task_watcher
python watch_codex_idle.py --debug
```

### 源码托盘模式

```powershell
cd D:\codex_task_watcher
python watch_codex_idle.py --tray
```

## 托盘菜单 🧩

托盘图标支持：

- 开始/停止监控
- 测试通知
- 提示音开关
- 选择提示音
- 恢复默认提示音
- 开机启动
- 退出

## 配置文件 📁

配置保存在：

- `%APPDATA%\codex_task_watcher\settings.json`

当前配置结构：

```json
{
  "sound_enabled": true,
  "sound_path": null,
  "sound_mode": "preset"
}
```

说明：

- `preset`：预设提示音，默认指向 `ikunganma.aac`
- `default`：恢复默认提示音后，使用 `notify.wav`
- `custom`：用户自行选择的语音包

## 打包发布 📦

运行：

```powershell
cd D:\codex_task_watcher
build_release.bat
```

会生成：

- `dist\CodexTaskWatcher\CodexTaskWatcher.exe`
- `dist\CodexTaskWatcher.zip`

## 仓库文件 📚

- `watch_codex_idle.py`
- `build_release.bat`
- `assets/notify.wav`
- `assets/ikunganma.aac`
- `dist\CodexTaskWatcher.zip`

## 小提醒 💡

- 如果你以前运行过旧版本，配置会沿用你本机 `%APPDATA%` 里的用户设置
- 如果想回到这次发布版的预设音效，可以删除 `settings.json` 后重新启动，或者我后续再给你加一个“恢复预设提示音”菜单项
