# CreatorRadar 2

CreatorRadar 2 提供对标账号跟踪、手动及定时扫描、情报流、跨平台关键词搜索和快速采集。三种入口的作品汇合到选题库，支持查看来源、视频转写、AI 拆解、归档和恢复。原型位于 `prototype/`。

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

## 平台采集

Windows 和 macOS 14 或更新版本会在首次启动时自动安装固定版本的 MediaCrawler、独立 Python 3.12 环境和 Chromium。之后启动会先做离线自检，成功后直接复用。已有同一提交的环境时，可将 `CREATOR_RADAR_CRAWLER_RUNTIME` 设为该检出目录的绝对路径。

安装失败时仍可打开工作台。终端会显示安装日志位置，重启时会自动重试。启用采集样例的隔离验收工作区会跳过真实采集环境安装。

在工作台「系统设置」启用平台并点击「登录平台」，在本机打开的浏览器里扫码或完成验证。登录任务会关闭并重开浏览器，确认会话可恢复。会话保存在对应工作区的 `profiles/<平台>`。待登录任务可在登录成功后从任务页或搜索结果页重试。平台限流时，等待平台恢复后重试。

在「采集任务」中可手动采集全部或单个平台，也可设置每日扫描的时间、平台范围、回看天数和采集上限。定时扫描使用服务机器的本地日期和时间。当天到时后启动服务也会派发当日尚未提交的扫描。每个启用账号每天最多派发一次，手动扫描可随时发起。

「运行日志」支持按任务查看实时事件、取消或重试任务、运行队列自检和清理事件日志。清理日志后仍可查看任务记录和采集结果。

需要单独诊断媒体下载时，安装 FFprobe，先收录视频选题，再在与服务配置相同的终端执行以下命令。任务结果显示媒体相对于工作区的路径。

```powershell
Set-Location backend
uv run --locked python -m creator_radar fetch-media <选题ID>
```

## 选题处理

将 FFmpeg 和 FFprobe 加入服务进程的 PATH。默认使用 `faster-whisper` 的 `small` 模型转写中文。启动时会自动安装 CPU 版 faster-whisper 并将模型下载到 Hugging Face 本机缓存。后续启动复用缓存。下载失败时，工作台仍可打开，终端会显示日志路径，重启时自动重试。没有提供有声样例的隔离验收环境会跳过 ASR 安装和下载。

在「系统设置」选择引擎、模型和语言，保存后点击「检查 ASR 环境」。检查会尝试加载本机缓存的模型。选用非 `small` 的 faster-whisper 模型时，首次转写会按需下载，默认保存到项目的 `.phase1/asr-models/`，也可通过 `HF_HUB_CACHE` 指定缓存目录。工作台对长度超过一小时的音轨给出提示，平台下载大小沿用现有采集上限。

如需使用 FunASR，在「系统设置 → ASR 转写」选择 FunASR，填写兼容 OpenAI 的 API 地址（例如 `https://asr.example.com/v1`）和 API Key。服务须支持 `/audio/transcriptions` 的 `verbose_json` 分段时间戳。转写时工作台会向所配置的服务发送作品音轨。

在选题详情的「逐字稿」页发起视频转写，完成后可复制带时间戳的文本。重新转写会在保留期内复用音轨或视频。视频转写成功后可发起 AI 拆解，图文可使用平台正文直接拆解。拆解结果中的依据可以展开阅读。

在「系统设置 → AI 拆解」选择执行方式，保存后点击「测试 AI 连接」。

- **OpenAI 兼容 API**：填写基础地址、模型和 API Key。远程地址使用 HTTPS，按服务文档填写必要的路径前缀，例如 `/v1`。密钥按工作区和服务地址存入 Windows 凭据管理器或 macOS 钥匙串。
- **Codex CLI**：在运行工作台的电脑上安装 Codex CLI，将 `codex` 加入 PATH，再运行 `codex login`。工作台读取 CLI 提供的模型及其思考等级，可显式选择，也可跟随 CLI 配置。新提交的拆解任务使用当前保存的执行方式。

## 隔离工作区验收

在项目根目录启动验收工作区，访问 [验收工作台](http://127.0.0.1:18865/)。

```powershell
./start.ps1 -Workspace acceptance -Port 18865
```

```bash
bash start.sh --workspace acceptance --port 18865
```

在另一终端从项目根目录设置验收工作区，再进入 `backend`。Windows 运行 `$env:CREATOR_RADAR_WORKSPACE = 'acceptance'` 和 `Set-Location backend`，macOS 运行 `export CREATOR_RADAR_WORKSPACE=acceptance` 和 `cd backend`。每条投递命令会输出任务 ID，可以直接访问 `/jobs?job_id=<任务 ID>`。

```sh
uv run --locked python -m creator_radar enqueue-test --seconds 20
uv run --locked python -m creator_radar enqueue-test --scenario fail_once --seconds 2
uv run --locked python -m creator_radar enqueue-test --scenario waiting_login --seconds 2
uv run --locked python -m creator_radar enqueue-test --scenario batch --seconds 2
```

- 可用 `--key <键>` 和 `--platform bilibili|douyin|xiaohongshu` 控制业务键和平台。相同平台和键的在途单任务返回同一 ID。
- 批任务的三个子项分别成功、首次失败和首次待登录。可在详情中单独重试失败项。验收处理器在第二次尝试成功。
- 后端自动测试会强制终止并重启 Worker。失去租约的运行任务显示「中断」，排队任务可继续执行。默认租约为 10 秒。
- 如果用 `-DataDir` 或 `--data-dir` 启动，投递时将 `CREATOR_RADAR_DATA_DIR` 设为相同的绝对路径。正式与验收数据库分别位于数据根目录的 `production/` 和 `acceptance/`。

## 自动验证

从项目根目录运行后端检查。

```powershell
Push-Location backend
uv run --locked ruff check
uv run --locked pytest -q
Pop-Location
```

浏览器测试应使用一个新的隔离验收目录。第一个终端从项目根目录运行。

```powershell
$env:CREATOR_RADAR_CRAWLER_FIXTURE = 'true'
./start.ps1 -Workspace acceptance -Port 18865 -DataDir '.creator-radar/playwright-run'
```

macOS 使用以下命令启动验收环境。

```bash
CREATOR_RADAR_CRAWLER_FIXTURE=true bash start.sh --workspace acceptance --port 18865 --data-dir .creator-radar/playwright-run
```

如需运行有声视频端到端用例，在启动终端额外设置 `CREATOR_RADAR_CRAWLER_FIXTURE_MEDIA`，指向带中文语音的测试 MP4 绝对路径，并在运行 Playwright 的终端设置同一变量。阶段 1 的样例可用 FFmpeg 封装为测试视频。

```powershell
ffmpeg -nostdin -f lavfi -i 'color=c=black:s=320x240:r=12' -i .phase1/chinese-sample-16k.wav -shortest -c:v libx264 -c:a aac .creator-radar/asr-test.mp4
$env:CREATOR_RADAR_CRAWLER_FIXTURE_MEDIA = (Resolve-Path '.creator-radar/asr-test.mp4').Path
```

第二个终端从项目根目录运行。以下示例使用本机 Edge，也可将浏览器通道设置为 `chrome`。

```powershell
$env:CREATOR_RADAR_WORKSPACE = 'acceptance'
$env:CREATOR_RADAR_DATA_DIR = (Resolve-Path '.creator-radar/playwright-run').Path
$env:CREATOR_RADAR_TEST_URL = 'http://127.0.0.1:18865'
$env:CREATOR_RADAR_TEST_BROWSER = 'msedge'
Set-Location web
npm run test:e2e
```

macOS 在第二个终端运行以下命令。使用 Playwright 安装的 Chromium。

```bash
export CREATOR_RADAR_WORKSPACE=acceptance
export CREATOR_RADAR_DATA_DIR="$PWD/.creator-radar/playwright-run"
export CREATOR_RADAR_TEST_URL=http://127.0.0.1:18865
cd web
npx playwright install chromium
npm run test:e2e
```

真实平台联调使用另一个空验收目录，并在启动终端清除 `CREATOR_RADAR_CRAWLER_FIXTURE`。先从 B 站开始，再逐个平台检查扫码、重开后的会话恢复、账号扫描、搜索、作品直链、分享短链和有声视频转写。见 [HLD](docs/HLD.md)、[阶段 1 验证记录](docs/phase1-validation.md)、[阶段 3 验收记录](docs/phase3-validation.md)、[阶段 4 验收记录](docs/phase4-validation.md) 和 [阶段 5 验收记录](docs/phase5-validation.md)。

阶段 4 的平台验收在会话恢复后，从「快速采集」提交一条短小有声的作品链接，打开该选题的「逐字稿」并开始转写。可在任务详情核对媒体获取、音轨提取和转写阶段，在选题详情核对时间戳和可复制文本。若二维码过期，请在平台窗口刷新二维码，或在任务超时后重新发起登录。

## 故障排查

| 现象 | 操作 |
| --- | --- |
| 启动提示端口被占用 | 停止占用该端口的服务，或用 `-Port` / `--port` 换端口，再访问启动输出中的地址。 |
| 工作台可打开，任务持续排队 | 在「存储与日志」查看 Worker 状态，并检查工作区 `service/` 下最新的 `*-worker.err.log`。修复后重启，排队任务会继续执行，中断任务可从采集任务页重试。 |
| 平台待登录、要求验证或限流 | 在系统设置重新登录并完成扫码或平台验证，再从任务详情重试受影响的平台。遇到限流时等待平台恢复。 |
| 转写环境检查报错 | 在启动服务的终端确认 `ffmpeg -version` 和 `ffprobe -version` 可运行。检查 `asr-setup` 启动日志和所选模型的缓存状态。 |
| AI 密钥保存或连接失败 | 使用启动服务的同一系统用户访问凭据管理器或解锁钥匙串。检查服务地址、模型和 Key，再用设置里的连接测试验证。 |
| 搜索历史、临时媒体或日志到期 | Worker 会按保留期清理。选题、逐字稿和拆解在选题详情继续可读，重新转写时可以按需再次获取媒体。 |
