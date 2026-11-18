# CreatorRadar

CreatorRadar 提供对标账号跟踪、手动及定时扫描、情报流、跨平台关键词搜索和快速采集。三种入口的作品汇合到选题库，支持查看来源、视频转写、AI 拆解、归档和恢复。

## 本机启动

准备 Git、uv、Python 3.12（由 uv 按需安装）、Node.js 22.12 或更新的 LTS 版本以及 npm。启动脚本安装后端和前端依赖，迁移数据库，准备平台采集和默认转写环境，构建前端并启动 API 和 Worker。

Windows 使用系统自带的 Windows PowerShell 5.1。在项目根目录运行以下命令。

```powershell
./start.ps1
```

macOS 使用系统自带的 Bash。在项目根目录运行以下命令。

```bash
bash start.sh
```

打开 [本机工作台](http://127.0.0.1:8765/)。全新工作区首次打开时为空。按 Ctrl+C 停止服务。新工作区的数据根目录在 Windows 上为 `%LOCALAPPDATA%\CreatorRadar2`，在 macOS 上为 `~/Library/Application Support/CreatorRadar2`。数据库在该根目录的 `production/workspace.sqlite3`，进程启动和错误输出在 `production/service/`。已有项目内 `.creator-radar` 工作区且系统目录没有同名数据库时，沿用原路径。启动时会显示实际目录，也可在「系统设置 → 存储与日志」查看数据库和临时媒体路径。[本机 API 健康检查](http://127.0.0.1:8765/api/v1/health)

使用 `./start.ps1 -Dev`（Windows）或 `bash start.sh --dev`（macOS）启动 Vite 热更新，浏览器访问 [Vite 开发地址](http://127.0.0.1:5173/)。可用 Windows 的 `-Port`、`-DevPort`、`-DataDir` 或 macOS 的 `--port`、`--dev-port`、`--data-dir` 覆盖端口与数据根目录。

## 构建离线安装包

在目标系统的构建机上准备 `uv`、Git、Node.js/npm、FFmpeg 和 FFprobe。第一次构建需要联网准备依赖。Windows x64 构建机还需要 Inno Setup 6 和 Microsoft Visual C++ x64 Redistributable，可用 `--iscc` 指定 `ISCC.exe` 的位置。

```powershell
.\package.ps1
```

在 Apple Silicon 和 Intel Mac 上分别执行以下命令。通过 `--ffmpeg-dir` 指向包含独立版 `ffmpeg` 和 `ffprobe` 的目录，通过 `--node` 指向独立版 Node 可执行文件。省略参数时使用 `PATH` 上的工具。

```bash
bash package.sh --ffmpeg-dir /path/to/ffmpeg/bin --node /path/to/node
```

安装包输出到 `release/`。Windows 生成 `CreatorRadar-<版本>-windows-x64.exe`，macOS 生成 `CreatorRadar-<版本>-macos-arm64.pkg` 或 `CreatorRadar-<版本>-macos-x86_64.pkg`。包内包含应用、MediaCrawler、Chromium、Node、Python 运行时、FFmpeg、FFprobe、CPU 版 faster-whisper 和 `small`。首次安装和启动可以离线完成，实际平台登录与采集需要联网。

构建时可加 `--sample <本地中文音频>`，用随包 Chromium 执行采集环境自检，并用 `small` 生成带时间戳的中文逐字稿。`--stage-only` 可留下组装好的目录供调试。macOS 上从「应用程序」打开 CreatorRadar，Windows 上从开始菜单打开。启动器会打开浏览器，工作区和日志写入用户数据目录。

GitHub Actions 中的 `Build offline installers` 工作流会在推送 `v*` 标签时构建三个安装包，也可以从 Actions 页面手动运行。构建完成后，在该次运行的 Artifacts 中下载对应系统的包。包内的版本号取自 `web/package.json`，构建产物保留 14 天。
