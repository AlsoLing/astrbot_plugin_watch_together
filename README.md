# 一起观影 · astrbot_plugin_watch_together

> AstrBot 装在 NAS 上，人在电脑前看电影——这个插件把两边接起来：
> 电脑浏览器打开观影室就能点播 NAS 里的片子，AI 读着字幕（可选再看画面）陪你一起看，
> 看到有意思的地方小声吐槽一句，还能在聊天里接着剧情跟你聊。

- 支持 AstrBot `>= 4.5.7`
- 只依赖 Python 标准库，不用额外装包

---

## 功能

- 🎬 **片库点播**：扫描 NAS 上的电影目录，网页里点一下就开始放（mp4 / webm 原生支持）。
- ☁️ **媒体服务器直连**：可连 Jellyfin / Emby，直接读它的片单、海报、简介和字幕。
- 🎞 **内嵌字幕**：MKV/MP4 里封装的字幕轨用 ffmpeg 自动提取，不用再找外挂字幕。
- 📡 **局域网播放识别**：轮询 Jellyfin/Emby 的「正在播放」或 VLC Web 界面，自动跟着进度点评。
- 🧠 **长期记忆**：看完自动记一笔（片名、进度、聊过的话），下次开场白会带上；
  还能写进记忆插件（默认适配 `astrbot_plugin_sugarmemory_clean`）或 webhook。
- 🧠 **AI 真的在"看"**：把刚刚的台词（SRT / ASS / VTT 自动解析）和播放进度交给模型；
  开启画面理解后每 20 秒截一帧低清画面，让它点评镜头、表情、构图。
- 💬 **字幕式弹幕**：点评像字幕一样浮在画面下方，同时记在右侧「观影笔记」里，点时间戳就能跳回。
- 📱 **聊天桥接**：看电影时在 QQ / Telegram 里说话，AI 会接着当前剧情回应；点评也会推到聊天窗口。
- 🖥 **三种看法**：NAS 点播 / 打开电脑本地文件 / 外部播放器（PotPlayer、VLC 等）同步计时。
- 🧩 **WebUI 页面**：插件详情页里的「观影室」，看状态、刷片库、海报墙、一键开播。

---

## 工作原理

```
┌───────────── NAS ─────────────┐          ┌──────── 你的电脑 ────────┐
│ AstrBot + 本插件               │          │ 浏览器打开观影室          │
│  ├─ 片库扫描 / 字幕解析         │  影片流  │  <video> 播放             │
│  ├─ HTTP 观影室 :6116  ◄───────┼──────────┤  每 2 秒上报进度          │
│  └─ 每 N 秒：字幕片段 + 截图     │  进度    │  截图 / 说话              │
│         ↓                      │ ────────►│                          │
│     大模型 → 一句点评 ──────────┼──────────┤  弹幕 + 笔记 + 推送到聊天 │
└────────────────────────────────┘          └──────────────────────────┘
```

插件不参与视频解码（mp4/webm 由浏览器直接播）， NAS 几乎没有负担；
AI 看到的是「字幕文本 + 一帧截图」，成本低、延迟小，也不容易剧透。

---

## 安装

### 插件市场 / 从链接安装

在 AstrBot WebUI「插件」页搜索「一起观影」，或「安装插件(+) → 从链接安装」填仓库地址。

### 从文件安装

「安装插件(+) → 从文件安装」，上传 `astrbot_plugin_watch_together.zip`（zip 内直接是 `main.py`）。

### 手动安装

把仓库放到 `data/plugins/astrbot_plugin_watch_together/`，确保目录结构是：

```
astrbot_plugin_watch_together/
├─ main.py
├─ metadata.yaml
├─ _conf_schema.json
├─ watchlib/          # 内部实现
├─ web/               # 观影室前端
└─ views/watch/       # WebUI 页面
```

然后在插件页点「重载插件」。

---

## 配置

| 配置项 | 说明 |
| --- | --- |
| `media_dirs` | **常用**。电影目录，填 **AstrBot 容器/主机内** 的真实路径，可填多个。 |
| `provider_id` | 使用的聊天模型；想让它"看画面"，选支持视觉的模型（GPT-4o、Gemini、Qwen-VL 等）。 |
| `server_port` | 观影室端口，默认 `6116`，Docker 记得 `-p 6116:6116`。 |
| `public_host` | 留空自动探测 NAS 局域网 IP。Docker 桥接网络探测到的是容器 IP，电脑打不开时手填 NAS 的 IP。 |
| `comment_interval` | 点评间隔，默认 90 秒。 |
| `vision_enabled` | 是否允许截图给模型看。 |
| `chat_bridge` | 观影时聊天是否接入剧情（默认开）。 |
| `push_comments_to_chat` | 点评是否推送到聊天窗口（默认开）。 |
| `ffmpeg_mode` | `off` / `remux` / `transcode`，见下文「MKV 怎么办」。 |
| `media_server_type` | `off` / `jellyfin` / `emby`，启用媒体服务器直连。 |
| `media_server_url` | 例如 `http://192.168.1.10:8096`。 |
| `media_server_api_key` | Jellyfin 控制台 → API 密钥 → 新建。 |
| `media_server_username` / `media_server_password` | 没有 API Key 时用账号密码登录（Emby / 绿联媒体服务器通常只给账号密码）。 |
| `media_server_path_map` | 媒体服务器路径 → 容器路径，例如 `/volume1/影视中心:/media`。 |
| `media_server_refresh` | 片单刷新间隔，默认 300 秒。 |
| `embedded_subtitles` | 是否提取内嵌字幕（需要 ffmpeg/ffprobe），默认开。 |
| `auto_watch_enabled` | 自动识别局域网播放（Jellyfin/Emby 会话、VLC），默认关。 |
| `auto_watch_interval` | 播放检测间隔，默认 10 秒。 |
| `auto_watch_umo` | 自动识别时把点评推到哪个会话（可选）。 |
| `vlc_url` | VLC Web 界面地址，例如 `http://192.168.1.20:8080`。 |
| `memory_enabled` | 长期记忆开关，默认开。 |
| `memory_plugin` | 要适配的记忆插件名，默认 `astrbot_plugin_sugarmemory_clean`。 |
| `memory_webhook` | 可选，把观影摘要 POST 到外部地址。 |

### Docker 挂载

AstrBot 容器里看到的路径和 NAS 宿主机不同，必须把电影目录挂进容器，并用容器内路径：

```yaml
services:
  astrbot:
    image: soulter/astrbot:latest
    ports:
      - "6185:6185"      # WebUI
      - "6116:6116"      # 观影室
    volumes:
      - ./data:/AstrBot/data
      - /volume1/video:/media   # 电影目录
```

`media_dirs` 填 `/media`。群晖的 `@eaDir`、回收站目录会自动跳过。

---

## 使用

### 聊天指令

```
/观影                 # 看片库 + 观影室地址
/观影 让子弹飞         # 按片名搜索并开始
/观影 1               # 按列表序号开始
/观影 状态             # 当前进度、字幕、条数
/观影 停止             # 结束
```

机器人会回一个地址，例如 `http://192.168.1.10:6116/?s=3f1a2b9c8d7e`，
在电脑浏览器打开就能看。也可以直接打开 `http://192.168.1.10:6116/`，在片库里点。

### WebUI 页面

插件详情页 →「观影室」：

- 四张状态卡：观影室、片库、媒体服务器、正在看的场次；
- 海报墙 + 搜索，点「一起看」会创建一场并打开播放页；
- 「刷新片库」「测试媒体服务器」按钮。

### 播放页面

- **左侧片库**：搜索、刷新，点一下开始播。
- **中间画面**：底部是 AI 的弹幕，右上角显示"AI 在想…"。
- **右侧观影笔记**：每条点评带时间戳，点一下跳回那一刻；底部可以直接和 AI 说话。
- **设置**：说话间隔、画面理解开关。
- **左下角**还有两个入口：
  - `📂 打开电脑上的影片`：片子在你电脑上时用（浏览器直接读本地文件，记得点「加载字幕」）。
  - `🕒 用外部播放器`：用 PotPlayer / VLC 看任何格式，网页只负责计时同步；
    先在下拉框选一部参考片源（AI 靠它的字幕），点「开始同步」后在播放器里按播放。

### 看电影时聊天

只要这场观影还在进行，在 QQ / Telegram 里直接说话，AI 会结合当前台词回应你：

```
刚才那个镜头好绝
```

群聊里默认需要 @ 机器人；可以在配置里把 `chat_bridge_mode` 改成 `always`。

---

## 媒体服务器（Jellyfin / Emby / 绿联）

在插件配置里把 `media_server_type` 设为 `jellyfin` 或 `emby`，填地址和 API Key 即可：

- 片单、海报、简介、年份来自媒体服务器；
- **如果**媒体服务器里的文件路径在 AstrBot 容器里可读（用 `media_server_path_map` 映射），
  播放、字幕、ffmpeg 转封装都和本地片库一样；
- 如果路径不可读，播放会 **302 跳到媒体服务器直接串流**，此时 AI 只能靠画面理解（截图）。

**关于绿联影视中心**：绿联从 2026 年 8 月起把影视中心开放为「媒体服务器」，
但目前只对 VidHub 等合作播放器开放私有协议，没有公开 API 文档，所以插件无法直接对接它。
可行的两种做法：

1. 在绿联上部署 Jellyfin / Emby，媒体库指向同一个「影视中心」共享文件夹，
   插件连 Jellyfin——这是最省事、功能最全的方案；
2. 如果绿联的媒体服务器兼容 Emby 协议，可以试着选 `emby`，填入它给出的地址、端口和账号密码
   （插件支持 `/Users/AuthenticateByName` 账号密码登录，不保证所有版本都兼容）。

不接媒体服务器也完全能用：直接扫描共享文件夹（例如 `/volume1/影视中心`），
AI 的点评、字幕、聊天功能都一样，只是没有海报墙。

---

## 内嵌字幕

很多 MKV/MP4 把字幕作为轨道封装在文件里。插件找不到外挂字幕时，会用 ffmpeg：

1. `ffprobe` 列出字幕流（语言、编码、默认/forced）；
2. 优先中文、非 forced、文本编码；
3. `ffmpeg -map 0:<index> -c:s srt` 转成 SRT，喂给 AI，同时给播放器做字幕轨。

前提：能找到 `ffmpeg`（`ffmpeg_path`），并且 `embedded_subtitles` 打开。
**位图字幕（PGS / VobSub）不能转文本**，会跳过——这种只能靠「画面理解」或外挂字幕。

## 局域网自动识别

插件在 NAS 上，看不到电脑播放器的进度。能自动识别的只有两类：

1. **Jellyfin / Emby**：服务器知道每个客户端的正在播放内容和进度。配置好媒体服务器后打开
   `auto_watch_enabled`，任何客户端（电视、手机、电脑）播放时插件会自动建一场、跟着进度点评；
   `auto_watch_umo` 可以指定把点评推到哪个聊天。
2. **VLC**：电脑上的 VLC 打开「工具 → 偏好设置 → 主界面 → Web」，设置密码和端口（默认 8080），
   然后填 `vlc_url`（有密码写 `http://user:pass@192.168.1.20:8080`）。

**PotPlayer、系统自带播放器、MPV 默认没有网络接口，插件识别不到**——这类请用网页里的
「用外部播放器」手动同步（或者换 VLC / Jellyfin）。

## 长期记忆

默认开启，数据存在插件数据目录的 `watch_memory.json`，只记录：

- 片名、看到的时间点；
- AI 说过的最后两句、你说过的最后两句；
- 时间与会话标识。

下次开场白和聊天时会把最近 3 条带进提示词（"你们之前一起看过…"）。

**适配记忆插件**：如果 `astrbot_plugin_sugarmemory_clean` 这类插件在运行时暴露了
`add_memory / save_memory / store_memory / remember` 之类的方法，插件会把观影摘要写进去。
启动日志会打印 `[watch-together] 记忆插件接口：[...]`；如果显示"未发现"，说明接口名不同，
把那一行日志或插件代码发我，我再按实际接口适配。

也可以用 `memory_webhook`：填一个 URL，插件会把
`{"content": "...", "umo": "...", "source": "watch_together"}` POST 过去，方便接自建服务。

---

## 常见问题

### 浏览器打不开观影室？

1. 看日志里有没有 `[watch-together] 观影室已启动：...`；
2. 检查防火墙 / Docker 端口映射（`-p 6116:6116`）；
3. 电脑和 NAS 要在同一局域网，用返回的 IP，不要用 `127.0.0.1`；
4. 返回 `172.17.x.x` 这类容器地址时，在配置里把 `public_host` 填成 NAS 的局域网 IP；
5. 端口被占用就换 `server_port`。

### 加载失败，提示 `No module named 'watchlib'`？

插件目录缺了 `watchlib/` 文件夹，通常是压缩包/仓库少了一层目录。
1.1.0 起插件会直接报「插件文件不完整，缺少 watchlib/ 目录」并列出当前目录内容。
把插件删掉，用完整仓库重新安装即可。

### MKV 放不了 / 只有声音没画面？

浏览器不支持 MKV 容器。三条路：

1. **转封装（推荐先试）**：`ffmpeg_mode = remux`，只换容器不重编码，几乎不吃 CPU；
2. **实时转码**：`ffmpeg_mode = transcode`，什么都能播但很吃 CPU；
3. **先转成 mp4**：`ffmpeg -i movie.mkv -c copy movie.mp4`；
4. 或者直接用「用外部播放器」模式。

> AstrBot 官方 Docker 镜像通常没有 ffmpeg，可以自己装或把宿主机的 ffmpeg 挂进容器，
> 再把 `ffmpeg_path` 指到它。

### AI 一句话都不说？

- 确认 `media_dirs` 或媒体服务器里有片子（`/观影` 能看到）；
- 没有字幕时它只能靠画面：检查 `vision_enabled` 和模型是否支持读图；
- 看日志里的 `[watch-together] 生成点评失败`，常见是模型没选、API Key 失效、超时；
- 播放必须是"播放中"状态（网页每 2 秒上报一次进度），暂停时不会触发点评。

### 字幕没被识别？

找字幕规则：同目录 `影片名.srt / .ass / .vtt`，`影片名.zh-CN.srt` 这类带语言后缀，
或同目录 `Subs/`、`字幕/` 子文件夹。中文编码（GBK/GB18030）会自动识别。
媒体服务器片源如果本地路径不可读，会尝试从服务器下载字幕流；本地文件模式可以手动「加载字幕」。

**内嵌字幕**需要 ffmpeg/ffprobe：

```bash
ffmpeg -version && ffprobe -version
```

装好后把 `embedded_subtitles` 保持开启即可；位图字幕（PGS）无解，只能开画面理解。

### 为什么识别不到我的播放器？

自动识别只支持有网络接口的播放源：

- Jellyfin / Emby / 绿联等媒体服务器客户端（走 `/Sessions`）；
- VLC（Web 界面）。

PotPlayer、Windows 自带播放器、MP4 双击打开这类没有接口，插件拿不到进度，
只能用网页里的「用外部播放器」手动点「开始同步」。

### 记忆插件没生效？

看启动日志里的 `[watch-together] 记忆插件接口：[...]`：

- 有内容：说明已经找到保存方法，看完一场就会写入；
- `未发现`：说明插件的保存方法名不在 `add_memory / save_memory / store_memory / remember` 里，
  把它 `main.py` 里保存记忆的函数名发我，或先用 `memory_webhook` 兜底。

### 会不会剧透？

提示词明确要求只看已播内容、不剧透，但模型偶尔仍会联想。
介意的话在 `persona_prompt` 里加一句"绝不预测后续剧情"。

### 安全吗？

观影室**没有登录鉴权**，默认监听 `0.0.0.0`，同局域网的人拿到地址就能看片库。
只在内网用没问题；**不要直接暴露到公网**，需要公网访问请套反代 + 认证或 VPN。

---

## 许可

MIT。作者 **AlsoLing**，基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件接口编写。