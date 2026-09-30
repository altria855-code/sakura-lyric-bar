# 字幕栏(Sakura Plugin API v4)设计

- 日期:2026-09-25
- 目标宿主:Sakura 1.2.1(Plugin API v4),Windows
- 插件 ID:`local.sakura.lyric-bar`
- 插件显示名:字幕栏

## 1. 目标

给 Sakura 加一个**桌面级浮窗**:角色说话时,台词像歌词一样显示在屏幕上,鼠标移上去可以打字发送消息。

必须满足:

1. 浮窗可自由拖动,**始终置顶于其他普通窗口之上**(类似 QQ 音乐歌词;独占全屏游戏的例外见第 11 节)。
2. 显示角色最新一条回复,整段可见,**当前正在朗读的那一句高亮**,栏内自动滚动跟随。
3. 显示模式可切换:双语(日文原文 + 中文译文)/ 只中文 / 只日文。
4. 鼠标移上去出现输入框,点击聚焦后可打字,回车发出消息;消息走 Sakura 正常聊天链路。
5. 外观在插件设置里直接改,改完即时生效:字号、文字颜色、栏宽、背景色、背景透明度、整体透明度、字体、圆角、边框、文字描边与阴影、当前句高亮色。
6. 记住拖动后的位置(含多显示器)。
7. 以 ZIP 交付安装。

## 2. 非目标

- 不显示历史对话滚动,不显示角色名/头像。
- 不做快捷键呼出、不做托盘图标、不做鼠标穿透模式。
- 不排队连续发送;回复进行中不允许再次发送。
- 不跨平台,仅 Windows。
- 不修改 Sakura 本体,不依赖任何私有接口。

## 3. 已验证的宿主约束

以下事实在用户本机 Sakura 1.2.1 安装目录与官方文档中核实过,是设计的地基:

| 事实 | 证据 |
|---|---|
| 第三方插件不能向宿主窗口贡献 UI(无自定义窗口/WebView/HTML/样式) | `docs/devdocs/SAKURA_PLUGIN_SDK.md`「当前暂不开放的界面能力」 |
| 插件可以启动并管理自己的子进程 | SDK 公开 `sakura_process.terminate_process_tree` |
| 插件运行环境 Python 3.12.8,**无 tkinter**,`ctypes`/`user32`/`gdi32` 完整可用 | `<安装目录>/python/python.exe` 实测 |
| 同目录存在 `pythonw.exe`(无控制台),与 `python.exe` 共用 `python312._pth` 与标准库 | 目录实测 |
| 时间线助手条目载荷含 `segments: [{text, translation}]`;用户条目含 `text` | `core/app/core_host/history.py:_entry_mapping` |
| TTS 播放逐句串行,每句一对 started/finished,句间无缝 | 用户 `data/logs/sakura-runtime.log` 实测 |
| 宿主向插件广播 `sakura.host.tts.started` / `.ended`,载荷含 `{playbackId, recordingId, outcome}`；可识别的录音还携带 `characterId/historyEntryId/segmentIndex` | `core/app/core_host/tts_boundary.py:_handle_playback_observe` |
| `sakura.host.conversation.begin(character_id, text, artifact)` 把文本交给正常聊天通道 | `core/app/core_host/conversation_host.py:begin` |
| 设置字段类型仅 string/password/boolean/integer/number/select/readonly/status/resource,**无取色器**;`placement: section_header` 仅 status 可用;select 选项上限 64 | `core/app/core_host/plugin_host_services.py` 字段校验 |
| 宿主收录图标仅 49 个,含 `messages-square` | `desktop/frontend/core/icons.js` |

**由此导出的两个关键结论:**

- 浮窗必须是**插件自己启动的独立进程**,不能是宿主的窗口。
- 「当前句高亮」使用播放事件的段落身份；事件不携带正文，台词从 Timeline 按条目 ID 查询。

## 4. 总体架构

```
Sakura Core
 └─ 插件进程 local.sakura.lyric-bar        (python.exe,隔离依赖环境)
      ├─ Host Service 消费
      │   ├─ context.on("sakura.host.chat.completed") ─→ timeline.read_since(cursor)
      │   ├─ context.on("sakura.host.tts.started"/".ended") ─→ 句序推进
      │   ├─ sakura.host.conversation.begin/poll/cancel ─→ 发送用户文本
      │   ├─ sakura.host.character.current() ─→ 当前角色
      │   ├─ sakura.host.settings ─→ 设置区块
      │   ├─ sakura.host.logging ─→ 宿主日志
      │   └─ context.data_path("state.json") ─→ 位置记忆
      └─ 子进程:pythonw.exe overlay.py      (无控制台)
           ↕ 标准输入 / 标准输出,UTF-8 JSON Lines
```

**为什么浮窗单起进程:**

1. 可脱离 Sakura 单独运行(`overlay.py --demo`),外观调试不需要启动宿主。
2. 浮窗崩溃不影响插件状态机;插件崩溃不影响已显示的浮窗(浮窗收到 stdin EOF 自行退出)。
3. 浮窗不占用插件的后台线程,插件对宿主回调的响应时间可控。

**生命周期:** 插件 `setup()` 启动浮窗并登记 `context.effect()` 清理;停用/重载/Sakura 退出时,浮窗因 stdin EOF 自行退出,不依赖清理代码兜底。`effect` 仍用 `terminate_process_tree` 保证有界回收。

**进程通信:** stdin/stdout 管道,UTF-8,每行一条 JSON 对象。不用本地端口(避免冲突与防火墙),不用共享文件。

## 5. 浮窗内部结构

两个窗口配对。这是 Windows 上同时满足「背景与文字透明度独立」和「中日文输入法可用」的可行解:逐像素透明的分层窗口无法正常渲染子控件,而自绘输入框无法正确承载输入法候选窗。

### 5.1 画布窗口

- 样式:`WS_POPUP | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE`
  - `NOACTIVATE` 保证点它不会抢走当前前台程序焦点;需要键盘时由输入框窗口去激活。
  - `TOOLWINDOW` 使其不出现在任务栏与 Alt+Tab。
- 绘制:`UpdateLayeredWindow` + 32bpp 预乘 alpha 的 DIB;每次状态变化整体重绘并一次性提交。
- 绘制流水线:所有元素先在内存 DIB 上光栅化为**覆盖度**(GDI 只负责把白色形状/字形画到黑底上),再由插件自己的代码按「颜色 + 各自 alpha」合成到预乘 alpha 的目标缓冲。**GDI 直接画彩色会丢掉 alpha 通道**,这是透明浮窗最常见的坑,必须走覆盖度合成。
- 圆角与边框:4 倍超采样光栅化再降采样,得到抗锯齿边缘(纯 GDI,不引入 GDI+ 依赖)。
- 文字:`CreateFontW`(`ANTIALIASED_QUALITY` 灰度抗锯齿,不用 ClearType —— 亚像素抗锯齿在透明底上会产生彩色毛边)+ `DrawTextW`;先 `DT_CALCRECT` 量高。阴影/描边 = 同一覆盖度以不同偏移和颜色多次合成。
- 透明度:背景 alpha 由「背景不透明度」控制,文字 alpha 由「整体不透明度」控制,互不影响。
- 置顶加固:每 3 秒一次 `SetWindowPos(HWND_TOPMOST, SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE)`。
- 拖动:画布空白处 `WM_LBUTTONDOWN` 触发 `SetCapture` 拖动,`WM_LBUTTONUP` 结束并上报坐标。
- 悬停:`TrackMouseEvent` 监听 `WM_MOUSELEAVE`,配合「收起延时」控制输入框显隐。

### 5.2 输入框窗口

- 样式:`WS_POPUP | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED`。
- **命中测试按「该点的合成像素」判定**(实测结论,早期记录为误):键色像素会让鼠标消息穿过去 —— 把整个客户区填成键色后,`WindowFromPoint` 返回的是**下层画布**,注入真实点击也确实点不中。但**子控件自己的不透明像素是算数的**:父窗口全区键色、只把 EDIT 的刷子换成非键色时,光标落在 EDIT 上点击可正常聚焦。
- 因此本窗口的可见表面 = **实心绘制的客户区** + **圆角区域塑形**;`LWA_COLORKEY` 仅作为兜底护栏保留(防止主题色恰好等于键色时整窗被透掉而点不中),不再承担「边距透底」的职责。
- **圆角与透明度跟随主题**:用 `SetWindowRgn(CreateRoundRectRgn(...))` 把窗口裁成与栏同半径的圆角(裁掉的部分不参与命中测试,正好是想要的效果);再用 `SetLayeredWindowAttributes(hwnd, KEY_COLOR, alpha, LWA_COLORKEY | LWA_ALPHA)` 指定整体 alpha = `backgroundOpacity% × overallOpacity%`。两个标志可以同时用。外观参数变化时必须在 `apply_theme()` 里重新施加。
- **已知限制(stderr 转发的残余通道)**:浮窗的 stderr 会被转成宿主日志(只记长度 + 前 200 字符)。若浮窗的 traceback 里带出消息内容的 repr(台词正文就在消息里),**前 200 字符内就可能带出用户文本**。这是"日志要能诊断"与"日志不写台词"之间的固有张力,已按"只截前 200 字符"取平衡;要彻底消除得让浮窗自己保证不把消息内容写进 stderr。
- **已知限制**:`LWA_ALPHA` 作用于整窗,所以输入框里的**文字也会一起变成半透明**(原生 EDIT 无法只让背景透明)。这是「可点击」与「逐像素透明」不可兼得后的取舍;若用户更看重文字清晰度,把 `背景不透明度` 调到 100 即可让输入框变实心。
- 内容:一个原生 `EDIT` 子控件(单行),字体与主题一致;`ImmSetCompositionWindow` 把输入法候选窗定位到输入框下方。
- 位置:贴在画布下边缘外侧,与画布同宽,垂直间距 6px。这样不遮挡台词。画布靠近屏幕底边时改为出现在画布上方。
- 平时 `SW_HIDE`;鼠标进入画布 → 显示;离开画布与输入框超过「收起延时」→ 隐藏(保留草稿)。
- 点击输入框才 `SetFocus` 并激活窗口;未聚焦时画布保持不抢焦点。

### 5.3 布局规则

- 每句 = 一个段落块。双语时每块两行(原文在上、译文在下),只单语时一块一行。
- 行高 = 字号 × 行距,由 `layout` 统一决定;`layout` 输出的是**紧贴文字块的盒子**,不含任何纵向留白。
- 纵向与横向内边距(文字与圆角边框之间的空隙)由画布在计算窗口尺寸与绘制时添加,`layout` 不参与 —— 避免两处各加一次。
- 栏高 = `min(内容总高, 最多显示行数 × 行高)`,不小于一行;内容总高与栏高都不含内边距。
- 超出上限时栏内滚动,当前句始终滚动到可见区;滚动位置随高亮句变化,不打断用户(输入框不参与滚动)。
- 空状态(无任何台词):`没内容时收起成小条` 开启时,收成一条高度 6px、宽度等于栏宽的细条,**细条同样响应悬停**,鼠标移上去仍可唤出输入框打字;关闭时整窗隐藏,只能等下一句台词出现。

### 5.4 全屏应用的隐藏

`hideInFullscreen` 开启时,浮窗在前台是全屏应用期间整体隐藏(`SW_HIDE`,不是变透明),检测周期 1 秒。

判定采用两道判据,任一命中即视为全屏应用在前台:

1. `SHQueryUserNotificationState` 返回 `QUNS_RUNNING_D3D_FULL_SCREEN(3)` 或 `QUNS_PRESENTATION_MODE(4)`。
2. 几何判据:`GetForegroundWindow()` 的窗口矩形覆盖它所在显示器的完整区域(`rcMonitor`,即整块屏幕),且该窗口不是桌面/Shell 窗口(共五个:`Progman`、`WorkerW`、`Shell_TrayWnd`、`Shell_SecondaryTrayWnd`、`Windows.UI.Core.CoreWindow`)。

判据 2 覆盖无边框窗口化的游戏,判据 1 覆盖独占全屏。**基准取 `rcMonitor`(整块屏幕)而非 `rcWork`(工作区)是关键**:任务栏可见时,普通最大化窗口比屏幕矮一条任务栏,因此不会被判成全屏。两道判据仍需一起用 —— 通知状态 API 只覆盖少数情形(`QUNS_QUIET_TIME(6)`、`QUNS_ACCEPTS_NOTIFICATIONS(5)` 都与全屏无关)。
(状态码取值以 MSDN `QUERY_USER_NOTIFICATION_STATE` 为准;早期记录误把 5 当成勿扰时段,已更正。)

检测到全屏时不显示输入框、不响应拖动;浮窗状态与位置保留,恢复后按原样显示。检测 API 不可用时(极端情况)按「不隐藏」处理并记 warning 一次,不影响台词显示。**该行为由主循环承担**:浮窗启动时做一次可用性检查,失败则记一次 warning 并在本轮生命周期内跳过全屏检测 —— 检测函数本身只把异常吞掉并返回「非全屏」。

**已知边界:** 独占全屏的最高优先级窗口之下,`WS_EX_TOPMOST` 窗口仍会被盖住 —— 隐藏是避免干扰游戏全屏优化,不是让浮窗在独占全屏上可见(见第 11 节)。

## 6. 数据契约

### 6.1 显示角色台词的时序

1. 订阅 `sakura.host.chat.completed`(载荷 `{characterId, turnId, cursor}`)。
2. 事件到达 → 用保存的 `lastCursor` 调 `timeline.read_since({cursor, limit})`。
3. 过滤出 `kind == "assistant"` 且 `characterId == 当前角色` 的最后一条 → 取 `payload.segments`。
4. 同时,期间出现的 `kind == "human"` 条目 → 作为「你:…」显示。**去重规则**:浮窗自己发出的文本已在发送成功时立即显示,因此文本与最近一次本地发送相同、且时间间隔在 5 秒内的 human 条目不再重复显示;桌面端直接输入的 human 条目正常显示。
5. 更新 `lastCursor`(失效时 `TIMELINE_CURSOR_INVALID` → 改用 `read_recent` 重建)。
6. 推给浮窗 `{"type":"reply","segments":[…]}`。语音模式等待实际播放事件；估算模式从第 1 句开始。

插件启动时用 `timeline.read_recent({limit: 1})` 初始化 `lastCursor`,不重放历史。

### 6.2 高亮同步

- `sakura.host.tts.started` 提供 `characterId/historyEntryId/segmentIndex` 时，按当前角色校验后定位段落。`segmentIndex` 是 Timeline 原始段落的零基下标，空段落被过滤后仍保留索引映射。
- 当前显示的是另一条回复时，调用 `timeline.get_entry({entryId})` 取得对应历史条目。只接收匹配当前角色、条目 ID 和助手类型的结果；查询期间切换角色或停用插件时丢弃旧结果。
- `playbackId` 关联开始和终态。重复 started 不推进，旧播放的 ended 不清除新播放的高亮，已结束或被替换的播放不能再次激活。播放结束、停止或失败后清除高亮。
- 缺少段落身份时不推测当前句。语音模式不自动退化为估算；`按字数估算` 使用每字 130ms 加句末 350ms，`不高亮` 始终不高亮。
- 切换角色时清空显示并取消发送；新回复按条目身份区分，即使文字相同也不会沿用上一条回复的播放状态。

### 6.3 发送用户消息

- 浮窗回车 → `{"type":"submit","text":"…"}` → 插件调 `conversation.begin(character_id, text, None)`。
- 成功后进入轮询:`conversation.poll(jobId)`,间隔 500ms,总超时 120s;`cancel` 在浮窗关闭或插件停用时调用。
- 发送成功 → 推 `{"type":"user","text":"…"}`;失败 → 推 `{"type":"notice","text":"发送失败:…"}` 并记录日志。
- 回复进行中再次回车 → 插件**不自己排队**:照常把文本交给宿主,由宿主判定是否可以受理。宿主拒绝时(错误码 `CHAT_EXECUTION_LIMIT_EXCEEDED`)回推 `{"type":"notice","text":"还在回复中"}`。
  (为什么不做本地排队:角色是否正在回复**只有宿主知道**,插件维护一份镜像状态会在切角色、取消、宿主重启等情况下失真;让权威方拒绝更可靠。)
  发送等待超时(`SEND_TIMEOUT`)也提示「还在回复中」。
- `notice` 作为**末尾一行浅色小字**追加显示,5 秒后自动消失;它属于内容块的一部分(会随内容滚动、占用一个行位),不进入位置记忆。

### 6.4 进程间消息

插件 → 浮窗:

| 消息 | 载荷 | 用途 |
|---|---|---|
| `hello` | `theme` | 启动后首次同步主题(初始状态由插件随后逐条推具体消息;浮窗不解析任何「状态包」) |
| `theme` | `theme` | 外观设置变化 |
| `reply` | `segments[]` | 新的角色回复 |
| `user` | `text` | 你刚发出的文本 |
| `notice` | `text` | 提示(发送中/失败/还在回复中) |
| `current` | `index` \| `null` | 当前句序号 |
| `demo` | `segments[]` | 预览效果 |
| `clear` | — | 清空(切角色) |
| `hide` | — | 主题里 `enabled` 为假时的即时隐藏(权威来源仍是主题,这只是立刻生效) |
| `place` | `x`, `y`, `screenId` | 按记住的位置摆放浮窗 |
| `reset-position` | — | 回到默认位置(主屏下方居中、底边锚定) |
| `bye` | — | 请求浮窗退出(浮窗退出时也会回发一条同类型消息,方向相反) |

浮窗 → 插件:

| 消息 | 载荷 | 用途 |
|---|---|---|
| `ready` | `pid`, `dpi`, `screens[]` | 浮窗就绪 |
| `submit` | `text` | 用户回车 |
| `moved` | `x`, `y`, `screenId` | 拖动结束,用于位置记忆 |
| `error` | `code`, `detail` | 浮窗内部错误(进宿主日志) |

未知消息类型一律忽略并记 `debug` 日志;单行超过 1 MiB 视为协议错误,断开并重启浮窗。

## 7. 配置与设置页

单个设置区块 `sectionId: bar`,标题「字幕栏」。分组标题用 `placement: section_header` 的 status 字段实现。

### 7.1 字段

**显示**

| key | 标签 | 类型 | 默认 | 约束 |
|---|---|---|---|---|
| `mode` | 显示模式 | select | `bilingual` | bilingual / zh / ja |
| `fontFamily` | 字体 | select | `Microsoft YaHei UI` | 启动时枚举系统字体,优先中/日文字体,最多 64 项;当前配置值不在列表中时补入;枚举失败时回落 `Segoe UI` |
| `fontSize` | 字号 | integer | 20 | 10–72 |
| `width` | 栏宽 | integer | 460 | 200–1600 |
| `maxLines` | 最多显示行数 | integer | 10 | 2–40。此处「行」指渲染行,双语模式下每句占两行 |
| `lineSpacing` | 行距 | number | 1.35 | 1.0–3.0,`placement: advanced` |

**颜色与材质**(颜色统一为字符串,接受 `#RRGGBB` 或 `#AARRGGBB`)

| key | 标签 | 默认 |
|---|---|---|
| `textColor` | 原文颜色 | `#FFFFFF` |
| `translationColor` | 译文颜色 | `#CFCFCF` |
| `highlightColor` | 当前句文字颜色 | `#FFD400` |
| `highlightBackground` | 当前句底色 | 空(不填底色) |
| `backgroundColor` | 背景色 | `#0E0E12` |
| `backgroundOpacity` | 背景不透明度 | 65 |
| `overallOpacity` | 整体不透明度 | 100 |
| `cornerRadius` | 圆角半径 | 14 |
| `borderWidth` | 边框粗细 | 0 |
| `borderColor` | 边框颜色 | `#FFFFFF` |
| `textShadow` | 文字阴影 | 开 |
| `textOutlineWidth` | 文字描边粗细 | 0 |
| `textOutlineColor` | 文字描边颜色 | `#000000` |

**行为**

| key | 标签 | 类型 | 默认 |
|---|---|---|---|
| `enabled` | 显示浮窗 | boolean | 开 |
| `idleCollapse` | 没内容时收起成小条 | boolean | 开 |
| `syncMode` | 高亮跟随方式 | select | `tts`(跟随语音 / 按字数估算 / 不高亮) |
| `hoverDelayMs` | 鼠标移开后收起延时 | integer | 800(200–3000,advanced) |
| `hideInFullscreen` | 全屏应用时自动隐藏 | boolean | 开 |
| `overlayStatus` | 浮窗状态 | status | 只读:运行中 / 未运行 |
| `positionStatus` | 当前位置 | status | 只读:屏幕名 + 坐标 |

### 7.2 Action

| actionId | 标签 | 行为 |
|---|---|---|
| `preview` | 预览效果 | 向浮窗推一段示例台词,不改任何持久状态 |
| `resetPosition` | 回到默认位置 | 浮窗回到主屏下方居中 |
| `restartOverlay` | 重新启动浮窗 | 仅浮窗未运行时可用(`enabledWhen` 控制) |

字段与 Action 数量对照宿主上限:3 个分组标题 + 24 个可编辑字段 + 2 个只读状态字段 = 29 项字段(上限 32),3 个 Action(上限 15)。

### 7.3 校验与热应用

- 两个 `status` 字段按宿主合同返回完整结构 `{state, label, message}`,且不携带 options/minimum/maximum/step/maxLength(宿主校验会拒绝 status 字段上的这些属性)。
- 颜色解析失败(非 `#RRGGBB`/`#AARRGGBB`)→ `save` 拒绝并返回 `error` 与中文原因;插件侧同时保留上次合法值。
- 其余数值字段由宿主范围校验兜底,插件再夹取一次。
- `save` 调用 `context.config.update(values)` 后立即把新主题推给浮窗,返回 `applied`(无需重启插件)。
- 浮窗未运行时 `save` 仍写入配置并返回 `applied`,浮窗下次启动时生效。
- `setup()` 注册 `context.config.on_change`,处理外部(配置文件/其他入口)改动;返回 `applied`。
- 位置与 DPI 状态存 `context.data_path("state.json")`(**不写配置**,避免每次拖动都触发配置写入);格式 `{"x":…, "y":…, "screenId":…, "width":…, "height":…}`。

## 8. 错误处理

| 场景 | 原因码 | 行为 |
|---|---|---|
| 浮窗进程启动失败 | `OVERLAY_SPAWN_FAILED`(浮窗自报启动失败时用同一个码) | 状态置「未运行」,设置页可点重启,日志 error |
| 浮窗进程中途退出 | `OVERLAY_EXITED` | 检测 stdout EOF;状态置「未运行」;不自动重启(由用户点按钮);日志 warning |
| 浮窗报告内部错误 | 由浮窗给出 | 转写宿主日志,浮窗保持运行 |
| 发送被拒/超时 | `SEND_FAILED` | 浮窗显示「发送失败」,日志 error;不重试 |
| 时间线 cursor 失效 | `TIMELINE_CURSOR_INVALID` | 用 `read_recent` 重建 lastCursor 并重试一次;再失败仅记日志 |
| 宿主事件丢失 | — | 事件是 best-effort。每轮回复都以 `read_since` 结果为准,不依赖事件本身携带内容 |
| 高度或宽度异常 | — | 布局阶段夹取到合法范围 |
| 记住的屏幕已不存在 | — | 回落主屏下方居中 |
| 全屏检测 API 不可用 | `FULLSCREEN_PROBE_UNAVAILABLE` | 按「不隐藏」处理,记 warning 一次,不重复刷日志 |
| 配置的字体已卸载 | — | 回落到系统默认字体并记 warning |
| 协议行超长/JSON 解析失败 | `OVERLAY_PROTOCOL_INVALID` | 断开并重启浮窗一次,日志 error |

日志遵守宿主规范:只写自编短消息 + 稳定码 + 异常类型 + 耗时,不写台词正文、不写用户输入、不写凭据。

## 9. 插件包结构

```
plugin/                        ← ZIP 的根即为本目录内容
├── plugin.yaml                api 4,id local.sakura.lyric-bar,entry plugin:LyricBarPlugin
├── plugin.py                  插件入口:服务消费、事件接线、设置注册、状态持久化
├── overlay.py                 浮窗进程入口:--run / --demo / --selftest / --render-sample
├── theme.py                   配置投影、颜色解析、夹取(两进程共用)
├── protocol.py                JSON 行编解码与消息构造(两进程共用)
├── layout.py                  分行分块、高度、滚动偏移、高亮定位(纯逻辑)
├── raster.py                  GDI 覆盖度光栅化:圆角、文字、超采样
├── compose.py                 覆盖度 → 预乘 BGRA 合成、PNG 导出(纯字节运算)
├── win32.py                   ctypes 绑定、结构体、常量
├── canvas.py                  画布窗口:置顶、拖动、悬停、UpdateLayeredWindow
├── inputbox.py                输入框窗口:原生 EDIT、输入法定位、回车/Esc
├── fullscreen.py              全屏应用检测
├── view.py                    浮窗视图状态机:台词、高亮、提示、滚动
├── hostlink.py                插件侧:浮窗子进程管理与 IPC
├── timeline_source.py         插件侧:时间线读取与投影
├── sender.py                  插件侧:conversation 发送与轮询
├── settings_schema.py         设置区块描述符、字体枚举
└── README.md                  安装、每项设置含义、已知限制、日志位置
```

包外(不进入 ZIP):`tests/` 放单测,`tools/fake_overlay.py` 是供插件侧 IPC 测试使用的假浮窗。

- `plugin.yaml`:`provides: []`(不发布 Service);`requires: [sakura.host.logging, sakura.host.settings, sakura.host.timeline, sakura.host.conversation, sakura.host.character]`;`presentation: {kind: extension, category: other, icon: messages-square}`;第三方插件默认 `enabled: false`。
- 入口为零依赖实现,不提供 `requirements.txt`。
- 代码不导入 `app.*` 与其他插件源码;两进程仅通过 JSON 行通信。
- 浮窗进程用 `Path(sys.executable).with_name("pythonw.exe")` 定位解释器;缺失时回退 `sys.executable` 并以 `CREATE_NO_WINDOW` 启动。
- README 写入「重新安装文件夹后生效」「ZIP 安装方式」「位置记录文件位置」。

## 10. 测试与验收

### 10.1 可自动化的部分

- `overlay.py --selftest`:不建窗口,验证主题解析、颜色解析、分句布局计算(栏高/行数上限)、协议 JSON 编解码与边界。
- `overlay.py --demo`:真实浮窗,内置示例台词与假逐句推进,供人工看外观与拖动。
- 插件侧纯逻辑单测(stdlib `unittest`):精确播放身份、重复和过期事件隔离、估算定时推进、`read_since` 结果到 `reply` 消息的投影、颜色与数值夹取、配置变更到主题投影。
- 协议测试:伪造浮窗(脚本读 stdin 写 stdout)验证插件侧 IPC 状态机,包括 EOF 与超长行。

### 10.2 上机验收清单

1. 从文件夹安装 → 启用 → 保存,浮窗出现在主屏下方居中。
2. 触发一次角色回复:整段台词出现,高亮跟朗读逐句下移,长回复时栏内滚动跟随。
3. 切换显示模式为「只中文」,浮窗即时只显示译文。
4. 改字号 / 背景色 / 背景不透明度 / 整体不透明度,浮窗即时变化;验证背景变淡时文字清晰度不变(证明两者独立)。
5. 拖动浮窗到副屏 → 关闭 Sakura 再启动 → 位置与屏幕保持。
6. 鼠标移上去输入框出现 → 点击聚焦 → 用中文输入法打字 → 回车发送 → 浮窗显示「你:…」→ 角色回复并朗读。
7. 回复进行中再次回车 → 提示「还在回复中」,草稿保留。
8. 关掉 TTS 再触发一次回复 → 显示台词且不高亮；切到「按字数估算」后按估算节奏推进。
9. 停用插件 → 浮窗消失,任务管理器无残留进程。
10. 停用插件时点「预览效果」等 Action 不报错崩溃。
11. 用一个无边框全屏程序(如浏览器 F11 全屏)验证 `hideInFullscreen`:进入时浮窗自动隐藏,退出后原样恢复;关掉该开关则始终可见。
12. 把普通窗口最大化(浏览器/资源管理器)确认**浮窗不隐藏**(几何判据没有把"最大化"误当"全屏")。若任务栏设为自动隐藏,此条会失败 —— 属已知限制,见第 11 节。

### 10.3 不做验证的部分

不验证 macOS、Linux;不验证多显示器热插拔的极端时序;不用真实游戏验证独占全屏(该模式下浮窗必然不可见,已由第 11 节说明)。

## 11. 已知限制

- 仅 Windows。
- **游戏显示模式决定能否显示**:无边框窗口化 / 全屏窗口化下浮窗正常可见并跟读;**独占全屏(Exclusive Fullscreen)下所有普通窗口都画不上去**(桌面合成被绕过,歌词类软件同样如此)。不采用注入游戏渲染管线的方式(反作弊风险),因此独占全屏下唯一的做法是自动隐藏(见 `hideInFullscreen`)。
- 常驻置顶窗口可能让部分独占全屏游戏无法进入全屏优化模式,`hideInFullscreen` 默认开启正是为此。
- **字体名来自注册表,未必能被 GDI 接受**:注册表值名常是**合并名**(如 `Microsoft YaHei & Microsoft YaHei UI`)或带样式词,而 GDI 的字体名是单一家族名 —— 传错名字时 GDI **静默回落到默认字体**(画面不变、无报错)。已做的处理:枚举时把合并名按 ` & ` 拆开;仍有个别名不保证可用。若发现选了字体却看不出变化,先确认该名字在别的程序里也能选中。
- **任务栏设为「自动隐藏」时会误判**:此时 `rcWork == rcMonitor`,而 Windows 的最大化窗口会向外扩约 8px(不可见边框),于是**最大化任何普通窗口都会命中几何判据、浮窗被隐藏**。机制已实测确认。缓解:任务栏改成常显,或在设置里关掉「全屏应用时自动隐藏」。彻底区分可用 `GWL_STYLE & WS_MAXIMIZE` 配合有无标题栏/可调边框(`WS_CAPTION`/`WS_THICKFRAME`),但有反向风险(部分无边框游戏也带 `WS_MAXIMIZE`),故本次不做。
- 输入框为单行,不支持 Shift+Enter 换行。
- 颜色需手填十六进制(宿主设置页没有取色控件)。
- 字体候选上限 64 个,新装字体后需重启插件才会出现。
- 平时显示最新回复；历史朗读显示对应回复，浮窗不提供独立历史浏览。
- 发送不排队。
- 关闭 Sakura 后浮窗随即消失(浮窗依附于插件进程存活)。

## 12. 后续可选扩展(不在本次范围)

- 角色名/头像行、历史回看、快捷键呼出、托盘图标、多行输入、逐字打字机效果、实时流式显示。
