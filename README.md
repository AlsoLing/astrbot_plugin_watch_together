# 一起观影（astrbot_plugin_watch_together）

> AstrBot 装在 NAS 上，人在电脑前看电影——这个插件把两边接起来：
> 电脑浏览器打开观影室就能点播 NAS 里的片子，AI 读着字幕（可选再看画面）陪你一起看，
> 看到有意思的地方小声吐槽一句。

---

## 1. 它能做什么

- 🎬 **NAS 片库直接点播**：扫描你配置的电影目录，网页里点一下就开始放（mp4 / webm 原生支持）。
- 🧠 **AI 真的"在看"**：把刚刚的台词（SRT / ASS / VTT 自动解析）和播放进度喂给模型；
  打开"画面理解"后还会每 20 秒截一帧低清画面，让它点评镜头、表情、构图。
- 💬 **字幕式弹幕**：AI 的点评会像字幕一样浮在画面下方，同时记在右侧"观影笔记"里，点时间戳就能跳回去。
- 📱 **聊天桥接**：看电影时在 QQ / Telegram 里直接说话，AI 会接着当前剧情回应你；
  点评也可以同步推送到聊天窗口，手机上也能看到。
- 🖥 **三种看法**：NAS 点播 / 打开电脑本地文件 / 外部播放器（PotPlayer、VLC 等）同步计时。
- 📦 **零额外依赖**：只用 Python 标准库，不需要装 aiohttp、ffmpeg（除非要播 MKV）。

---

## 2. 工作原理（30 秒版）

```
┌───────────── NAS ─────────────┐          ┌──────── 你的电脑 ────────┐
│ AstrBot + 本插件               │          │ 浏览器打开观影室          │
│  ├─ 片库扫描 / 字幕解析         │  影片流  │  <video> 播放             │
│  ├─ HTTP 观影室 :6199  ◄───────┼──────────┤  每 2 秒上报进度          │
│  └─ 每 N 秒：字幕片段 + 截图     │  进度    │  截图 / 说话              │
│         ↓                      │ ────────►│                          │
│     大模型 → 一句点评 ──────────┼──────────┤  弹幕 + 笔记 + 推送到聊天 │
└────────────────────────────────┘          └──────────────────────────┘
```

关键点：**插件不碰视频解码**（mp4/webm 由浏览器直接播），所以 NAS 几乎没有负担；
AI 看到的是"字幕文本 + 一帧截图"，成本低、延迟小，也不会剧透。

---

## 3. 安装

### 方式 A：WebUI 上传（推荐）

1. 把整个 `astrbot_plugin_watch_together` 文件夹压成 zip（压缩包里直接是 `main.py`，不要再套一层目录）。
   仓库/交付包里已经附带 `astrbot_plugin_watch_together.zip`，可直接用。
2. AstrBot WebUI →「插件」→ 右下角「安装插件（+）」→「从文件安装」→ 上传 zip。
3. 回到「已安装」，确认「一起观影」已启用。

### 方式 B：手动拷贝

```bash
# AstrBot 的插件目录，通常在 data/plugins
cd /path/to/AstrBot/data/plugins
git clone <你的仓库地址> astrbot_plugin_watch_together
# 或直接把文件夹拷进来
```

然后在 WebUI 插件页点「重载插件」。

### 方式 C：传到 GitHub，从链接安装（推荐长期用）

AstrBot 的「从链接安装」会克隆仓库，**仓库根目录必须直接是插件根目录**，不要再套一层文件夹：

```
你的仓库/
├─ main.py              ← 必须在仓库根目录
├─ metadata.yaml
├─ _conf_schema.json
├─ requirements.txt
├─ README.md
├─ logo.png
├─ __init__.py
├─ watchlib/
│  ├─ __init__.py
│  ├─ engine.py
│  ├─ media.py
│  ├─ subtitles.py
│  ├─ prompts.py
│  ├─ server.py
│  └─ ffmpeg.py
└─ web/
   ├─ index.html
   ├─ app.js
   ├─ style.css
   └─ favicon.svg
```

上传步骤（有 git 的机器上）：

```bash
cd astrbot_plugin_watch_together
git init
git add .
git commit -m "feat: watch together plugin"
git branch -M main
git remote add origin https://github.com/<你的用户名>/astrbot_plugin_watch_together.git
git push -u origin main
```

没有 git 的话，也可以在 GitHub 网页新建仓库，把 **整个文件夹拖进去** 上传；
上传后确认仓库首页能同时看到 `watchlib` 和 `web` 两个文件夹（网页上传漏掉子目录，
就会出现下面那个 `No module named 'watchlib'` 的报错）。

传好后：

1. 在 AstrBot「插件 → 安装插件（+）→ 从链接安装」填仓库地址；
2. 安装完把 `metadata.yaml` 里的 `repo:` 改成你自己的仓库地址（方便以后更新和市场展示）；
3. 以后改代码只要 `git push`，WebUI 里点「更新」即可。

> ⚠️ 别把 `__pycache__` 提交上去（已提供 `.gitignore`）。

### 目录结构

```
astrbot_plugin_watch_together/
├─ main.py              # 插件入口：命令、聊天桥、生命周期
├─ metadata.yaml
├─ _conf_schema.json    # 可视化配置
├─ watchlib/
│  ├─ media.py          # 片库扫描 / 字幕发现 / nfo 简介
│  ├─ subtitles.py      # SRT / ASS / VTT 解析
│  ├─ prompts.py        # 提示词
│  ├─ engine.py         # 观影会话与点评节奏
│  ├─ server.py         # HTTP 观影室（Range 串流）
│  └─ ffmpeg.py         # 可选：MKV 实时转封装
└─ web/                 # 观影室前端（原生 JS，无需构建）
```

---

## 4. 配置（重要）

打开 WebUI →「插件」→「一起观影」→「配置」：

| 配置项 | 说明 |
| --- | --- |
| `media_dirs` | **必填**。电影目录，填 **AstrBot 容器/主机内** 的真实路径。可填多个。 |
| `provider_id` | 选一个聊天模型。想让它"看画面"，选支持视觉的（GPT-4o、Gemini、Qwen-VL 等）。 |
| `server_port` | 观影室端口，默认 `6199`。Docker 部署记得 `-p 6199:6199`。 |
| `public_host` | 留空自动探测 NAS 局域网 IP；走域名/反代时再填。**Docker 桥接网络下自动探测到的是容器 IP（172.x），电脑打不开时，一定要手填 NAS 的局域网 IP**，例如 `192.168.1.10`。 |
| `comment_interval` | 点评间隔，默认 90 秒。 |
| `vision_enabled` | 是否允许截图给模型看。 |
| `chat_bridge` | 观影时聊天是否接入剧情（默认开）。 |
| `push_comments_to_chat` | 点评是否推送到聊天窗口（默认开）。 |
| `ffmpeg_mode` | `off` / `remux` / `transcode`，见下文「MKV 怎么办」。 |

### Docker 部署注意

AstrBot 容器里看到的路径和 NAS 宿主机不一样，**必须把电影目录挂进容器**，并用容器内路径：

```yaml
services:
  astrbot:
    image: soulter/astrbot:latest
    ports:
      - "6185:6185"      # WebUI
      - "6199:6199"      # 观影室（新增）
    volumes:
      - ./data:/AstrBot/data
      - /volume1/video:/media   # 电影目录映射
```

然后 `media_dirs` 填 `/media`（或 `/media/movies`）。

> 群晖用户注意：`/volume1/video` 下面可能有一堆 `@eaDir` 缩略图目录，插件会自动跳过。

---

## 5. 使用

### 开始观影

在聊天里发：

```
/观影                 # 看片库 + 观影室地址
/观影 让子弹飞         # 按片名搜索并开始
/观影 1               # 按列表序号开始
/观影 状态             # 当前进度、字幕、条数
/观影 停止             # 结束
```

机器人会回你一个地址，例如：

```
🎬 已就位：《让子弹飞》
MKV · 2.1 GB · 有字幕

在电脑上打开：http://192.168.1.10:6199/?s=3f1a2b9c8d7e
```

**在电脑浏览器里打开这个地址**，点播放，AI 就开始了。

也可以直接打开 `http://192.168.1.10:6199/`，在左侧片库里点片子。

### 观影页面上有什么

- **左侧片库**：搜索、刷新；点一下开始播。
- **中间画面**：底部是 AI 的"弹幕"；右上角显示"AI 在想…"。
- **右侧观影笔记**：每条点评带时间戳，点一下跳回那一刻；底部可以直接和 AI 说话。
- **设置**：调整说话间隔、开关画面理解。
- **左下角**还有两个入口：
  - `📂 打开电脑上的影片`：片子在你电脑上时用这个（浏览器直接读本地文件，AI 照样点评；记得点"加载字幕"）。
  - `🕒 用外部播放器`：用 PotPlayer / VLC 看任何格式，网页只负责计时同步。
    先在下拉框里选一部片库里的电影（这样 AI 能读到它的字幕），点「开始同步」后
    在播放器里按下播放；点评会生成并推到聊天/笔记里。

### 看电影时聊天

看完片头，在 QQ / Telegram 里直接发：

```
刚才那个镜头好绝
```

只要这场观影还在进行，AI 会结合当前台词和画面前后文回答你，而不是普通聊天。
（群聊里默认需要 @ 机器人；可在配置里把 `chat_bridge_mode` 改成 `always`。）

---

## 6. 常见问题

### Q：加载失败，提示 `No module named 'watchlib'`

说明插件目录里缺了 `watchlib/` 文件夹。最常见的原因是：

1. **GitHub 网页上传时只传了根目录文件**，子文件夹没传上去；
2. 压缩包解压后多套了一层目录，`main.py` 和 `watchlib/` 不在同一级。

1.0.1 起插件改成按文件路径加载（不再依赖 `sys.path`），缺文件时会直接报
「插件文件不完整，缺少 watchlib/ 目录」并列出当前目录内容。解决：

```bash
# 看插件目录里到底有什么
ls -la /path/to/AstrBot/data/plugins/astrbot_plugin_watch_together
```

确认 `main.py`、`watchlib/`、`web/` 在同一级；不对就删掉插件目录，用完整仓库重装。

### Q：装了插件但网页打不开 / 只有接口没有界面

同样检查 `web/` 是否完整（`index.html`、`app.js`、`style.css`）。
日志会提示「缺少 web/index.html，观影室无法启动」。

### Q：浏览器打不开观影室？

1. 确认 AstrBot 日志里有 `[watch-together] 观影室已启动：http://0.0.0.0:6199/`；
2. 检查防火墙 / Docker 端口映射（`-p 6199:6199`）；
3. 电脑和 NAS 要在同一局域网；用 `/观影` 返回的那个 IP，不要用 `127.0.0.1`；
4. 如果返回的是 `172.17.x.x` 这类容器地址，说明 Docker 桥接网络探测不到宿主机 IP，
   请在插件配置 `public_host` 填写 NAS 的局域网 IP（例如 `192.168.1.10`），重载插件；
5. 端口被占用就换一个 `server_port`，重载插件。

### Q：MKV 放不了 / 只有声音没画面？

浏览器对容器的支持有限（MKV 基本不支持）。三条路：

1. **转封装（推荐先试）**：配置 `ffmpeg_mode = remux`，并保证 NAS 上有 ffmpeg。
   它只换容器不重编码，几乎不吃 CPU；要求视频是 H.264/H.265、音频是 AAC/MP3 这类。
   > 官方 AstrBot Docker 镜像里通常没有 ffmpeg，可以自己装，或把宿主机的 ffmpeg 挂进容器，
   > 然后把 `ffmpeg_path` 指到它。
2. **实时转码**：`ffmpeg_mode = transcode`。能播一切，但很吃 CPU，NAS 性能弱会卡成 PPT。
3. **转成 mp4 再放**（最省事）：
   ```bash
   ffmpeg -i movie.mkv -c copy movie.mp4
   ```
   如果 `-c copy` 后浏览器还是不能播（比如 HEVC），换：
   ```bash
   ffmpeg -i movie.mkv -c:v libx264 -crf 20 -c:a aac movie.mp4
   ```
4. 或者直接用「用外部播放器」模式，什么格式都不怕。

### Q：AI 一句话都不说？

- 确认 `media_dirs` 没填错（`/观影` 里能看到片子）；
- 片源没有同名字幕时，它只能靠画面。检查是否开了 `vision_enabled`，以及模型是否支持读图；
- 看 AstrBot 日志里的 `[watch-together] 生成点评失败`，常见原因是模型没选（`provider_id` 为空）、
  API Key 失效、超时；
- 播放必须处于**播放中**状态（网页每 2 秒上报一次进度），暂停时不会触发点评。

### Q：字幕没被识别？

插件找字幕的规则：同目录下 `影片名.srt / .ass / .vtt`，或 `影片名.zh-CN.srt` 这种带语言后缀，
或放在同目录的 `Subs/`、`字幕/` 子文件夹里。中文编码（GBK/GB18030）会自动识别。
本地文件模式可以在页面上点「加载字幕」手动传。

### Q：点评太吵 / 太安静？

页面上点「设置」调间隔（30–300 秒），或改 `comment_interval`。
也可以写 `persona_prompt` 控制风格，比如"只在该吐槽的时候说话，平时安静看片"。

### Q：会不会剧透？

提示词里明确要求只看已播内容、不剧透；但模型偶尔仍可能联想。
如果介意，可以在 `persona_prompt` 里加一句"绝不预测后续剧情"。

### Q：安全吗？

观影室 **没有登录鉴权**，默认监听 `0.0.0.0`，同局域网的人拿到地址就能看你的片库。
只在内网用没问题；**不要直接暴露到公网**。需要公网访问请套反代 + Basic Auth / VPN。

---

## 7. 调试

- 日志关键字：`[watch-together]`。
- 健康检查：`http://NAS:6199/api/health` 会返回版本、ffmpeg 状态、会话数。
- 想看更多请求日志，可以把 `server.py` 里 `WatchHandler.log_message` 的 `return` 去掉。

---

## 8. 已知限制

- 观影室是单机内存态：AstrBot 重启后会话/点评不会保留（片库和字幕会重新扫描）。
- 转封装模式下无法原生拖动进度，页面提供「跳转 mm:ss」输入框（会重新起 ffmpeg）。
- 画面上报的是低清截图（480px 宽），模型看不清细节是正常的。
- 只支持单路播放（一个人看）；多点同时看也可以，但共享同一个"AI 节奏"。

---

## 9. 致谢与许可

- 插件基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件接口编写。
- 字幕解析、提示词、前端均为本插件原创实现，MIT 许可，随意修改。