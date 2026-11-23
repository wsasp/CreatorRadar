# CreatorRadar

CreatorRadar 是一个本地运行的创作者工作台。它支持跟踪对标账号、搜索和采集作品，并在选题库中整理素材、转写视频和进行 AI 拆解。

## 调试启动

先安装 Git、uv 和 Node.js 22.12 或更高版本（含 npm）。启动脚本会准备 Python 3.12 和项目依赖。调试视频转写时，还需将 FFmpeg 和 FFprobe 加入 `PATH`。

在项目根目录运行对应命令，服务会使用 `acceptance` 工作区。

### Windows

```powershell
.\start.bat -Workspace acceptance -Port 18865
```

### macOS

```bash
bash start.sh --workspace acceptance --port 18865
```

启动后打开 [本机工作台](http://127.0.0.1:18865/)。按 `Ctrl+C` 停止服务。

如需前端热更新，Windows 命令末尾加 `-Dev`，macOS 命令末尾加 `--dev`，然后打开 [Vite 调试页面](http://127.0.0.1:5173/)。
