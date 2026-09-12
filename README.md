# Kindle F1 Dashboard

在闲置的 Kindle Paperwhite 1 上运行的 F1 信息牌:E-Ink 屏保显示下一场比赛、双积分榜、上场比赛结果和赛道轮廓,唤醒自动刷新,不影响正常阅读。

```
+----------------------------+-------------+
| 🏎️ DASHBOARD        /// F1 |             |  ← 顶栏左侧白色赛车剪影
| NEXT RACE                  |  赛道轮廓图  |
| Dutch Grand Prix           |             |
| Round 12 | 2026-08-23      |             |
| in 7d 06h                  |             |
| SEASON PROGRESS ▓▓▓▓░░ 11/23             |
+------------------+-----------------------+
| DRIVER STANDINGS | CONSTRUCTOR           |
| 1 Antonelli  219 | 1 Mercedes     379    |
| 2 Hamilton   169 | 2 Ferrari      307    |
| ...              | ...                   |
+------------------+-----------------------+
| LAST RACE | HUNGARIAN GRAND PRIX         |
| 1 L. Norris    1:39:56.180               |
| 2 M. Verstappen +15.080                 |
+------------------------------------------+
| Last update 11:16                        |
+------------------------------------------+
```

> 赛事周末(距开赛 <3 天)时,上面的仪表盘会被替换为**赛事海报**:
> 大号赛道轮廓作主角 + 比赛名/地点 + 大字倒计时,氛围更浓;
> 周末里排位赛/冲刺赛结束后,海报**渐进**补出对应赛果(Top 5),正赛前更"有用"。

数据来自 [Jolpica F1](https://api.jolpi.ca)(原 Ergast API 的继任者,免费、无需 Key)。赛道轮廓图形来自 [F1DB circuit assets](https://github.com/f1db/f1db/tree/main/src/assets/circuits)(Jules Roy,CC BY 4.0);赛车剪影图标来自 [game-icons.net](https://game-icons.net)(Skoll,CC BY 3.0)。无服务端、无 Docker,全部运行在 Kindle 本机。

## 功能

- **信息牌模式**:设备休眠时屏保显示 F1 面板(零功耗);唤醒后 2 秒内自动刷新
- **赛事周末海报**:距下一场开赛 **<3 天**(含比赛进行中)时,屏保自动切换为"赛事海报"版式——大号赛道轮廓、比赛名、地点与大字倒计时,替代日常信息密集的仪表盘;周末里排位赛/冲刺赛结束后**渐进**补出对应赛果(Top 5),正赛前信息更有用;赛程剩余 >3 天时自动回到仪表盘
- **屏保内容(仪表盘)**:下一场比赛(含倒计时)、赛季进度条、车手积分榜 Top 8、车队积分榜 Top 8、上一场比赛结果 Top 5、下一场赛道轮廓图、F1 赛车剪影与字标
- **刷新屏保**:KUAL 里一键 Refresh Now,立即重新生成屏保图(下次休眠显示)
- **自动刷新**:清醒时每 30 分钟刷新一次
- **局部降级**:任一数据接口失败只影响对应区块(显示 no data),页面整体存活
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

1. 把本仓库的 `kindle/` 下文件传到设备(SSH 或 USB):

```bash
scp -r kindle/f1dash.py kindle/tracks.json kindle/car.json kindle/rtc.py kindle/make_ss_test.py root@<KINDLE_IP>:/mnt/us/f1dash/
scp -r kindle/extensions/f1 root@<KINDLE_IP>:/mnt/us/extensions/
```

> `tracks.json` 是 23 条赛道的轮廓坐标(赛道图形用),由 `tools/make_tracks.py` 从 F1DB 资产生成;`car.json` 是赛车剪影坐标(实心剪影用),由 `tools/make_car.py` 从 game-icons.net 生成。两者已随仓库提供,升级时保持和 `f1dash.py` 一起更新。

2. 确认 `/mnt/us/extensions/f1/config.xml` 存在(KUAL 通过它识别扩展)
3. 重启 Kindle(让 KUAL 重建菜单缓存),打开 KUAL 应看到 **F1 Dashboard** 菜单

## 使用

| 场景 | 操作 |
|---|---|
| 当信息牌摆着 | KUAL → **Start Service** → 按电源键休眠,屏保显示面板;之后每次唤醒自动刷新,再按电源键睡回去即可 |
| 立即刷新屏保 | KUAL → **Refresh Now**(下次休眠显示新图) |
| 停用服务 | KUAL → **Stop Service** |
| 断网恢复 | KUAL → **Enable Wifi** |

首次使用建议先 `Refresh Now` 确认能正常拉取数据。

## 常见问题

| 问题 | 原因 / 处理 |
|---|---|
| KUAL 里没有 F1 Dashboard | 扩展缺 `config.xml` 或 KUAL 菜单缓存未刷新(重启 Kindle) |
| 屏幕顶部被系统状态栏盖住 | 正常现象,状态栏归 Kindle 框架所有;内容从状态栏下方开始绘制 |
| 屏保显示旧时间 | 屏保图只在唤醒时刷新;唤醒后等 3 秒再入睡即可(守护进程每 3 秒轮询状态) |
| 显示 "FETCH FAILED" | 网络不通或 API 变动:确认 WiFi 已连;域名为 `api.jolpi.ca`(旧 `jolpica.com` 已失效) |
| linkss 屏保失效 | usbnet 与 linkss 已知冲突,重装 linkss 恢复 |

## 项目结构

```
├── docs/项目说明.md      # 完整技术文档(架构、踩坑记录)
├── tools/
│   ├── make_tracks.py    # 开发机专用:SVG → tracks.json 转换与验证工具
│   └── make_car.py       # 开发机专用:SVG → car.json(赛车剪影)转换与核对
└── kindle/
    ├── f1dash.py         # 主程序:png / service 两模式(无 FBInk 依赖)
    ├── tracks.json       # 23 条赛道轮廓坐标(F1DB,CC BY 4.0)
    ├── car.json          # F1 赛车剪影坐标(game-icons.net,CC BY 3.0)
    ├── rtc.py            # RTC 闹钟(备用代码,RTC 唤醒方案已确认不可行)
    ├── make_ss_test.py   # 屏保定位测试图生成器
    └── extensions/f1/    # KUAL 插件,部署到 /mnt/us/extensions/f1/
        ├── config.xml    # 扩展注册(必需)
        ├── menu.json     # 菜单定义
        └── bin/          # refresh / start / stop 脚本
```

## 技术要点

- 数据直连 [Jolpica F1](https://api.jolpi.ca/ergast/f1/)(HTTPS,Python 自带 certifi 证书)
- 屏保图片 758x1024 PNG,显示时等比缩放到 600x800 屏幕
- **赛事海报触发**:`race_poster_active()` 判 `下一场开赛时间 - 现在 <= 3 天`,命中则渲染反色海报;
  赛道名/倒计时用 `fit()` 动态缩字,任何赛道名(如 "United States Grand Prix")都不会溢出
- 守护进程每 3 秒轮询 `powerd state`,唤醒即刷新
- 本设备(PW1 5.6.1.1)外部 RTC 闹钟无法唤醒挂起内核,故不做"休眠中定时刷新"
- 详见 [`docs/项目说明.md`](docs/项目说明.md) §6 踩坑记录
