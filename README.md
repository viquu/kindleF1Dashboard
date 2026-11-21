# Kindle F1 Dashboard

在闲置的 Kindle Paperwhite 1 上运行的 F1 信息牌:E-Ink 屏保显示下一场比赛、双积分榜、上场比赛结果和赛道轮廓,唤醒自动刷新,不影响正常阅读。

渲染在 Oracle Cloud 云端完成(systemd timer 定时出图 + nginx 静态服务),Kindle 只在唤醒时下载成图——唤醒刷新从几十秒压到几秒,配合"一键回睡"按一下电源键就能看到最新锁屏。

```
+------------------------------------------------+
| 11-14 20:13      |       DASHBOARD              |  ← 大字标题条(顶边并入外框,左上角为更新时刻)
+----------------------+-------------------------+
| DRIVER STANDINGS — TOP 5 | CONSTRUCTOR — TOP 5 |
| 1 ANTONELLI ......... 390 | 1 MERCEDES ..... 652 |
| ...                       | ...                 |
+------------------------------------------------+
| NEXT RACE                                      |
| LAS VEGAS GRAND PRIX      |   赛道轮廓图        |
| Round 21                  |   LAS VEGAS STRIP…  |
| [LIGHTS OUT  NOV 22  SUNDAY  12:00]            |  ← 反色开赛时刻条
+------------------------------------------------+
| LAST RACE — BRAZILIAN                          |
|     1st        |      2nd       |     3rd      |
|   NORRIS       |  VERSTAPPEN    |   HADJAR     |
+----------------+----------------+--------------+
| HANGZHOU TODAY  | FC BARCELONA | GITHUB TRENDING|
| - High/Low/降水 | - Next match | - #1 repo      |
| - 明日概要      | - 开球(北京) | - stars today |
+------------------------------------------------+
```

> 赛事周末(距开赛 <3 天)时,上面的仪表盘会被替换为**赛事海报**:
> 大号赛道轮廓作主角 + 比赛名/地点 + 大字开赛时刻,氛围更浓;
> 周末里排位赛/冲刺赛结束后,海报**渐进**补出对应赛果(Top 5),正赛前更"有用"。

F1 数据来自 [Jolpica F1](https://api.jolpi.ca)(原 Ergast API 的继任者,免费、无需 Key);底部资讯盒三块同样免费免 Key:天气取 [Open-Meteo](https://open-meteo.com) **每日预报**(杭州:当日高低温/天气/降水概率 + 明日概要,不取几分钟后就失真的当前气温),足球取 [ESPN 公开接口](https://site.api.espn.com)(巴萨下一场 LaLiga 比赛 + 榜位积分,北京时间),GitHub Trending 抓取 [github.com/trending](https://github.com/trending) 首页(今日第一仓 + stars today)。赛道轮廓图形来自 [F1DB circuit assets](https://github.com/f1db/f1db/tree/main/src/assets/circuits)(Jules Roy,CC BY 4.0);页眉头盔图标来自 [Material Design Icons](https://pictogrammers.com)(Pictogrammers,Apache 2.0,`racing-helmet`);页眉 F1 徽标来自 [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Formula_One_logo.svg)(Public domain;F1 徽标为 Formula One Group 注册商标,仅个人非商用显示,赛事海报页眉仍在用)。渲染上移云端后,Kindle 只下载成图、不做本机渲染:云端不可达时直接保留最后一张屏保图。

## 功能

- **信息牌模式**:设备休眠时屏保显示 F1 面板(零功耗);唤醒后自动下载云端成图(秒级)
- **一键回睡(Auto Sleep)**:信息牌模式下按一下电源键 → Kindle 秒级拉取新图 → 自动回睡,锁屏永远是最新的;想正常阅读时在 KUAL 菜单切回 OFF
- **常显模式(Always On)**:设备永不入睡,每小时拉云端成图 eips 全屏重显示,前置光强制关闭;与"信息牌+一键回睡"互补,是全天候实时版。Wi-Fi 常开不关(实测软关无效、硬关有断图风险),代价 ~30-60 mAh/h、基本一天一充(内置电池计量,`battreport` 外推)。拉图后本地把电量角标(电池图标 + 百分比 + 实测外推续航,≤15% 加顶部警告条)叠进右下角白边——framework 停了没有状态栏电量,屏上直接看还能撑多久
- **赛事周末海报**:距下一场开赛 **<3 天**(含比赛进行中)时,屏保自动切换为"赛事海报"版式——大号赛道轮廓、比赛名、地点与大字开赛时刻,替代日常信息密集的仪表盘;周末里排位赛/冲刺赛结束后**渐进**补出对应赛果(Top 5),正赛前信息更有用;赛程剩余 >3 天时自动回到仪表盘
- **屏保内容(仪表盘)**:大字标题条(左上角小牌显示更新时刻,北京时间)、车手/车队积分榜 Top 5(点线引导)、下一场比赛面板(赛名、轮次、反色开赛时刻条、赛道轮廓图)、上场比赛领奖台 Top 3、底部资讯盒三联(杭州每日天气 / 巴萨下一场 LaLiga + 榜位 / GitHub Trending 今日第一仓)
- **刷新屏保**:KUAL 里一键 Refresh Now(下载云端成图,秒级)
- **自动刷新**:清醒时每 30 分钟刷新一次
- **局部降级**:任一数据接口失败只影响对应区块(显示 no data),页面整体存活;云端主数据源失败时保留上一张成功图,Kindle 端不做本机渲染
- 完全不影响正常阅读(屏保模式实现,无常驻界面)

## 前置依赖(需先安装)

项目假设 Kindle 已越狱,并安装以下组件(本项目不包含它们):

| 依赖 | 用途 | 获取方式 |
|---|---|---|
| 越狱(WinterBreak2) | 基础 | kindlemodding.org 指南 |
| **KUAL** | 扩展启动菜单 | 越狱后安装 |
| **MRPI / MRInstaller** | 安装 .bin 包 | KUAL 配套 |
| **kindle-python**(NiLuJe) | Python 3.9 + Pillow + certifi(必需,选 `touch_pw` 版本) | [MobileRead t=225030](https://www.mobileread.com/forums/showthread.php?t=225030) |
| **linkss**(ScreenSavers Hack) | 自定义屏保(信息牌模式必需,选 `touch_pw` 版本) | [bookfere 指南](https://bookfere.com/post/311.html) |
| usbnet(可选) | WiFi SSH,开发调试用 | 越狱配套 |

> 两个 `.bin` 包都选 **`touch_pw`** 变体(Kindle Touch / Paperwhite 1),不是 `pw2_kt2_kv_pw3`。
> 安装方式:文件放入 `/mnt/us/mrpackages/`,Kindle 搜索框输入 `;log mrpi`,装完重启。

## 安装

### 云端(Oracle Cloud,一次性)

```bash
bash cloud/deploy.sh     # 渲染代码 + systemd timer(每 10 分钟)+ nginx(:8080)+ iptables
```

前置:本机 `~/.ssh/config` 有 `oracle` 别名(ubuntu + 免密 sudo);Oracle 控制台
**VCN Security List 需放行 TCP 8080**(iptables 脚本里已放行,云防火墙需手动加)。
可选:`bash cloud/sync_fonts.sh`(Kindle 在线时)把 Kindle 的 Futura 拷到云端当出图字体
(几何风格与版式最搭);不拷则自动用 Jost(OFL 免费字体,版式自动适配)。

### Kindle(SSH 或 USB)

```bash
scp -r kindle/f1dash.py kindle/tracks.json kindle/f1icon.json kindle/rtc.py kindle/make_ss_test.py root@<KINDLE_IP>:/mnt/us/f1dash/
scp -r kindle/extensions/f1 root@<KINDLE_IP>:/mnt/us/extensions/
```

> `tracks.json` 是赛道轮廓坐标(赛道图形用,由 `tools/make_tracks.py` 从 F1DB SVG 生成),
> `f1icon.json` 是页眉头盔图标坐标(来自 MDI `racing-helmet`,由 `tools/make_icon.py` 生成),
> 云端与 Kindle 两端都要有。

确认 `/mnt/us/extensions/f1/config.xml` 存在(KUAL 通过它识别扩展),
重启 Kindle 让 KUAL 重建菜单缓存,打开 KUAL 应看到 **F1 Dashboard** 菜单。

## 使用

| 场景 | 操作 |
|---|---|
| 当信息牌摆着 | KUAL → **Start Service** → **Auto Sleep 切到 ON** → 按电源键休眠,屏保显示面板;之后按一下电源键,它自己拉新图并回睡,锁屏始终最新 |
| 全天候信息牌 | KUAL → **Always On 切到 ON**:设备自动停掉 framework + pillow(纯净全屏,无状态栏/对话框),每小时拉图,成图或电量有变才 eips 全屏重显示;Wi-Fi 常开不关(实测软关无效,见 f1dash.py `wifi()`)。**运行中 KUAL 不可用**,恢复阅读跑 `ssh kindle /mnt/us/extensions/f1/bin/always_on_stop.sh`(自动拉回 framework+pillow)或直接重启 Kindle |
| 看电量/续航 | 常显屏右下角角标(成图或电量变化刷新时更新,含实测外推续航);想看完整报表再 `ssh kindle "/mnt/us/python3/bin/python3.9 /mnt/us/f1dash/f1dash.py battreport"`(采样 CSV 在 `/mnt/us/f1dash/battery.csv`;Wi-Fi 常开,SSH 随时可连) |
| 正常阅读 | KUAL → **Auto Sleep 切到 OFF**,唤醒后不再自动回睡 |
| 立即刷新屏保 | KUAL → **Refresh Now**(优先拉云端成图,下次休眠显示新图) |
| 停用服务 | KUAL → **Stop Service** |
| 断网恢复 | KUAL → **Enable Wifi** |

首次使用建议先 `Refresh Now` 确认能拉到云端图(`service.log` 里看 `wake refresh done (pull)`)。

## 常见问题

| 问题 | 原因 / 处理 |
|---|---|
| KUAL 里没有 F1 Dashboard | 扩展缺 `config.xml` 或 KUAL 菜单缓存未刷新(重启 Kindle) |
| 屏幕顶部被系统状态栏盖住 | 正常现象,状态栏归 Kindle 框架所有;内容从状态栏下方开始绘制 |
| 唤醒后锁屏还是旧图 | 云端不可达时保留上一张(设备不做本机渲染);看 `/mnt/us/f1dash/service.log`,`wake refresh done (pull)` 才是拉图成功 |
| Auto Sleep 把 Kindle 抢走 | 设计行为(信息牌模式);想阅读时 KUAL → Auto Sleep 切回 OFF |
| 屏保内容缺块/过期 | 云端拉 Jolpica 数据失败时保留上一张成功图;`ssh oracle` 看 `journalctl -u f1dash-render.service`;域名为 `api.jolpi.ca`(旧 `jolpica.com` 已失效) |
| 云端图不更新 | `ssh oracle` 看 `systemctl status f1dash-render.service` 与 nginx;公网拉不通先查 Oracle 控制台 VCN Security List 是否放行 8080 |
| linkss 屏保失效 | usbnet 与 linkss 已知冲突,重装 linkss 恢复 |

## 项目结构

```
├── docs/项目说明.md      # 完整技术文档(架构、踩坑记录)
├── cloud/
│   ├── render.py         # 云端渲染入口(import kindle/f1dash.py 复用取数与渲染)
│   ├── deploy.sh         # 一键部署到 Oracle(systemd timer + nginx + iptables)
│   ├── sync_fonts.sh     # Kindle 在线时把 Futura 拷到云端(可选)
│   ├── systemd/          # f1dash-render.service / .timer
│   └── nginx/            # :8080 静态服务配置
├── tools/
│   ├── make_tracks.py    # 开发机专用:SVG → tracks.json 转换与验证工具
│   ├── make_icon.py      # 开发机专用:MDI 头盔图标 SVG → f1icon.json
│   └── make_logo.py      # 开发机专用:F1 官方徽标 SVG → f1logo.json
└── kindle/
    ├── f1dash.py         # 主程序:png / pull / refresh / service / alwayson / battreport 六模式
    ├── tracks.json       # 赛道轮廓坐标(F1DB,CC BY 4.0)
    ├── f1icon.json       # 页眉头盔图标坐标(MDI racing-helmet,Apache 2.0)
    ├── f1logo.json       # F1 官方徽标坐标(Wikimedia Commons,Public domain)
    ├── rtc.py            # RTC 闹钟(备用代码,RTC 唤醒方案已确认不可行)
    ├── make_ss_test.py   # 屏保定位测试图生成器
    └── extensions/f1/    # KUAL 插件,部署到 /mnt/us/extensions/f1/
        ├── config.xml    # 扩展注册(必需)
        ├── menu.json     # 菜单定义(含 Auto Sleep / Always On 开关)
        └── bin/          # refresh / autosleep_toggle / always_on_* / start / stop 脚本
```

## 技术要点

- **云端渲染**:Oracle Cloud 上 systemd timer 每 10 分钟跑 `cloud/render.py`
  (直接 import `kindle/f1dash.py`,渲染逻辑零分叉)→ `/opt/f1dash/out/latest.png`,
  nginx 出 `http://<host>:8080/f1/latest.png`;渲染 4× 超采样再 LANCZOS 缩回,
  线条/文字边缘抗锯齿;字体 Barlow Semi Condensed(仓库 `cloud/fonts/`,OFL)
  为主,Futura 副本 / Jost 后备
- **Kindle 瘦客户端**:守护进程每 3 秒轮询 `powerd state`,唤醒即 `pull` 云端成图
  (PNG 魔数校验 + 原子替换 + 内容不变跳过写盘),云端不可达保留上一张屏保;
  `auto_sleep` 标志存在且刷新成功时发 `lipc-set-prop com.lab126.powerd powerButton 1` 一键回睡
- **赛事海报触发**:`race_poster_active()` 判 `下一场开赛时间 - 现在 <= 3 天`,命中则渲染反色海报;
  赛道名/开赛时刻用 `_fit()` 动态缩字,任何赛道名(如 "United States Grand Prix")都不会溢出
- 本设备(PW1 5.6.1.1)外部 RTC 闹钟无法唤醒挂起内核,故不做"休眠中定时刷新";
  设备睡着时云端图再新也刷不上去,这是硬件上限(详见 docs §6.4)
- **常显模式设备事实(实测)**:`preventScreenSaver`(rw)置 1 阻止入睡,本固件无
  `suspendScreenSaver`/`batLevel`;前光是 `flIntensity`/`flAuto`;eips 不在默认 PATH,
  在 `/usr/sbin/eips`(`-g` 解析 PNG 且路径须紧跟其后;`-g -f` 只是 "full update",
  实测清不掉残影,整屏 flash 须 `eips -s w=758,h=1024 -f` 以当前 fb 内容闪翻全屏);
  电池走 `yoshi_battery0` 电芯计量(`battery_mAH`/`battery_lmd`=真实满容量/
  battery_voltage=mV),电流原始单位不明,续航一律用 mAH 衰减速率外推;
  `battreport` 同时检测采样空洞(>5 分钟区分进程重启与挂起)
- **纯净全屏三件套(实测)**:①停 framework —— 但 `com.lab126.cmd wirelessEnable`
  就是 framework 提供的(一调就弹飞行模式对话框,框架死了无人响应);②Wi-Fi 开关
  改走 `com.lab126.wifid enable`(独立守护,框架生死都可用,已实测);③只停 framework
  不够 —— `pillowd`(状态栏/对话框渲染进程)还在跑、时钟仍会盖屏,`stop pillow`
  也要执行。恢复 = `start framework` + `start pillow`。方案参照
  [paperwhite-weather](https://github.com/estcarisimo/paperwhite-weather)(其
  DEVICE.md 实测:停框架后 Wi-Fi 保持连接、贴图 15 分钟无人覆盖,功耗约 1.3%/h)
- 详见 [`docs/项目说明.md`](docs/项目说明.md) §6 踩坑记录
