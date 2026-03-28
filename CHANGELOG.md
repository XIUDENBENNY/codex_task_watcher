# Changelog

## 🐔 v0.1.1

🐔 修复部分场景下点击 toast 后没有反应的问题  
🐔 调整回到 Cursor 的逻辑，优先聚焦已打开窗口，避免在已有窗口时重复拉起  
🐔 强化 Windows 前台窗口激活流程，减少点击通知后“看起来没切回去”的情况  

## 🎉 v0.1.0

✨ 首个可发布版本，已经可以直接打包并放到 GitHub。  

### 🚀 新增功能

🔹 支持 Windows 托盘常驻运行  
🔹 支持任务完成 toast 提示  
🔹 支持点击提示后跳回 Cursor 工作区  
🔹 支持开机启动  
🔹 支持提示音开关与自定义语音包  
🔹 支持打包为 `exe` 发布  

### 🐔 提示音说明

🐔 首次运行默认预选 `assets/ikunganma.aac`  
🐔 发布版默认听到的是爱坤的“你干嘛”  
🐔 可以在托盘菜单里点击“恢复默认提示音”，回到 `assets/notify.wav`  
🐔 也可以自行选择本地语音包  

### 📦 发布方式

🔹 推荐下载 `dist\CodexTaskWatcher.zip`  
🔹 解压后双击 `CodexTaskWatcher.exe` 即可使用  
