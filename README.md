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
- 🎞 **内嵌字幕**：MKV/MP4 里封装的字幕轨自动提取；装了 ffmpeg 用 ffmpeg，没装也能用内置解析器读文本字幕。
- 📡 **局域网播放识别**：轮询 Jellyfin/Emby 的「正在播放」或 VLC Web 界面，自动跟着进度点评。
- 🧠 **长期记忆串联**：直接接上 [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)，
  开场前读旧记忆、散场后写新记忆；没装时退回插件自带的本地观影记忆。
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
| `embedded_subtitles` | 是否提取内嵌字幕（ffmpeg 或内置解析器），默认开。 |
| `auto_watch_enabled` | 自动识别局域网播放（Jellyfin/Emby 会话、VLC），默认关。 |
| `auto_watch_interval` | 播放检测间隔，默认 10 秒。 |
| `auto_watch_umo` | 自动识别时把点评推到哪个会话（可选）。 |
| `vlc_url` | VLC Web 界面地址，例如 `http://192.168.1.20:8080`。 |
| `memory_enabled` | 长期记忆开关，默认开。 |
| `livingmemory_mode` | `auto`（默认，自动串联 LivingMemory）/ `off`（只用本地记忆）。 |
| `memory_plugin` | 其它记忆插件的名字，默认 `astrbot_plugin_sugarmemory_clean`。 |
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

很多 MKV/MP4 把字幕作为轨道封装在文件里，播放器能显示，但浏览器 `<video>` 读不到。
插件会在没找到外挂字幕时自己提取：

1. 装了 ffmpeg：`ffprobe` 列字幕流 → 优先中文、非 forced、文本编码 → `ffmpeg -c:s srt`；
2. 没装 ffmpeg：用内置的纯 Python 解析器直接读 MKV（SRT/ASS/SSA/WebVTT 文本轨）
   和 MP4（`mov_text`）里的字幕，不依赖任何外部程序；
3. 绿联 / Emby 的片单里没给字幕流时，会按索引 0–7 逐个向媒体服务器请求，拿到文本就用。

**位图字幕（PGS / VobSub / DVB）不能转文本**——那是图片，浏览器和 AI 都读不了。
这种会明确提示，想看只能：用绿联影视中心 / VLC 播放（它们能渲染图片字幕），
或手动上传一份 srt，或让媒体服务器转成文本字幕。

> 网页里没字幕时，点「字幕诊断」会列出：外挂字幕找到没有、内嵌轨是什么编码、
> 是不是位图字幕、媒体服务器有没有给字幕。比瞎猜快。

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

两部分，默认都开：

**1. 插件自带的本地记忆**（`watch_memory.json`）

只记录片名、看到的时间点、AI 和你说过的最后几句话。下次开场白和聊天时会带上最近 3 条
（"你们之前一起看过…"）。不依赖任何其它插件。

**2. 串联 LivingMemory**（推荐）

装了 [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)
的话，插件会自动找到同进程里它的记忆引擎（`MemoryEngine`），直接读写：

- **开场前读**：用片名 + "一起看电影 观影 剧情 偏好" 做语义检索，把相关旧记忆塞进开场白和聊天提示词；
- **散场后写**：把"和用户一起看了《X》，看到几分几秒，我说过…你说过…"写成一条长期记忆，
  带 `topics`（一起看电影、片名）和 `key_facts`，importance 0.6；
- 写入的 `session_id` 就是当前会话的 `umo`，所以它在聊天里也能被正常召回。

不需要改 LivingMemory 的配置，也不走它的外部 HTTP API。日志里会看到：

```
[watch-together] 已接上 LivingMemory 长期记忆（LivingMemoryPlugin.initializer.memory_engine）
[watch-together] 已写入长期记忆（id=123）：和用户一起看了《让子弹飞》…
```

没装、还没初始化完、或版本接口对不上时，会安静跳过并退回本地记忆，不会影响看片。
想关掉只保留本地记忆：`livingmemory_mode = off`。

**其它记忆插件 / 自建服务**：`memory_plugin` 会尝试 `add_memory / save_memory / store_memory / remember`
这类方法；`memory_webhook` 则把 `{"content","umo","source"}` POST 到你的地址。

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

**先点网页里的「字幕诊断」**，它会告诉你是哪一种情况。常见原因：

1. **外挂字幕名字对不上**：插件认 `影片名.srt`、`影片名.chs.srt`、`影片名_chs.srt`、
   `影片名-简体.srt`，也会看 `Subs/`、`字幕/` 子文件夹；目录里只有一部片子时，
   名字完全对不上的字幕也会被采用。中文编码（GBK/GB18030）自动识别。
2. **内嵌字幕但没装 ffmpeg**：从 v1.3 起会退回内置的纯 Python 解析器，直接读 MKV 的
   SRT/ASS/SSA/WebVTT 文本轨和 MP4 的 `mov_text`，不需要 ffmpeg。
3. **位图字幕（PGS / VobSub / DVB）**：这是图片，转不成文本。绿联影视中心、VLC 能渲染，
   浏览器 `<video>` 和 AI 都读不了。三条出路：用外部播放器看（插件只跟着计时点评）、
   手动上传一份 srt、或在媒体服务器里转成文本字幕。
4. **媒体服务器片源**：片单里没给字幕流时会按索引 0–7 逐个向服务器要，拿到文本就用；
   NAS 上看不到原始文件时，还会用本地片库里同名/同标题的文件兜底。
5. **硬字幕**（烧进画面的）：本来就没有字幕轨，只能靠「画面理解」。

还是不行的话，`/观影 状态` 会显示字幕状态，日志里也有 `[watch-together] 已获取字幕（N 条）`。

### 记忆插件没生效？

装了 LivingMemory 的话，看启动日志：

- `[watch-together] 已接上 LivingMemory 长期记忆（...）`：接上了；
- `[watch-together] 已写入长期记忆（id=...）`：散场后写入成功；
- 什么都没有：说明当时 LivingMemory 还没初始化完（它是异步启动的，插件会每 20 秒重试一次），
  或者你把 `livingmemory_mode` 设成了 `off`。

WebUI「观影室」的「长期记忆」卡片会显示 `LivingMemory 已接上` 和读写次数。
用其它记忆插件时看 `[watch-together] 记忆插件接口：[...]`；显示"未发现"就说明方法名不在
`add_memory / save_memory / store_memory / remember` 里，可以先用 `memory_webhook` 兜底。

### 为什么识别不到我的播放器？

自动识别只支持有网络接口的播放源：

- Jellyfin / Emby / 绿联等媒体服务器客户端（走 `/Sessions`）；
- VLC（Web 界面）。

PotPlayer、Windows 自带播放器、MP4 双击打开这类没有接口，插件拿不到进度，
只能用网页里的「用外部播放器」手动点「开始同步」。

### 会不会剧透？

提示词明确要求只看已播内容、不剧透，但模型偶尔仍会联想。
介意的话在 `persona_prompt` 里加一句"绝不预测后续剧情"。

### 安全吗？

观影室**没有登录鉴权**，默认监听 `0.0.0.0`，同局域网的人拿到地址就能看片库。
只在内网用没问题；**不要直接暴露到公网**，需要公网访问请套反代 + 认证或 VPN。

---

## 许可

MIT。作者 **AlsoLing**，基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件接口编写。