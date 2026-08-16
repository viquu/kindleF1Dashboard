# Kindle F1 Dashboard

在闲置的 Kindle Paperwhite 1 上运行的 F1 信息牌:E-Ink 屏保显示下一场比赛、双积分榜、上场比赛结果和赛道轮廓,唤醒自动刷新,不影响正常阅读。

```
+----------------------------+-------------+
| F1 DASHBOARD               |             |
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
| 1 L. Norris    1:39:56.180              |
| 2 M. Verstappen +15.080                 |
+------------------------------------------+
| Last update 11:16                        |
+------------------------------------------+
```

数据来自 [Jolpica F1](https://api.jolpi.ca)(原 Ergast API 的继任者,免费、无需 Key)。赛道轮廓图形来自 [F1DB circuit assets](https://github.com/f1db/f1db/tree/main/src/assets/circuits)(Jules Roy,CC BY 4.0)。无服务端、无 Docker,全部运行在 Kindle 本机。

## 功能

- **信息牌模式**:设备休眠时屏保显示 F1 面板(零功耗);唤醒后 2 秒内自动刷新
- **屏保内容**:下一场比赛(含倒计时)、赛季进度条、车手积分榜 Top 8、车队积分榜 Top 8、上一场比赛结果 Top 5、下一场赛道轮廓图
- **临时查看**:KUAL 里一键 Refresh Now,屏幕直接显示
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
| **FBInk** | 屏幕直显(仅临时查看模式需要) | [FBInk](https://github.com/NiLuJe/FBInk) |
| usbnet(可选) | WiFi SSH,开发调试用 | 越狱配套 |

> 两个 `.bin` 包都选 **`touch_pw`** 变体(Kindle Touch / Paperwhite 1),不是 `pw2_kt2_kv_pw3`。
> 安装方式:文件放入 `/mnt/us/mrpackages/`,Kindle 搜索框输入 `;log mrpi`,装完重启。

## 安装

1. 把本仓库的 `kindle/` 下文件传到设备(SSH 或 USB):

```bash
scp -r kindle/f1dash.py kindle/tracks.json kindle/rtc.py kindle/make_ss_test.py root@<KINDLE_IP>:/mnt/us/f1dash/
scp -r kindle/extensions/f1 root@<KINDLE_IP>:/mnt/us/extensions/
```

> `tracks.json` 是 23 条赛道的轮廓坐标(赛道图形用),由 `tools/make_tracks.py` 从 F1DB 资产生成,已随仓库提供;升级时保持和 `f1dash.py` 一起更新。

2. 确认 `/mnt/us/extensions/f1/config.xml` 存在(KUAL 通过它识别扩展)
3. 重启 Kindle(让 KUAL 重建菜单缓存),打开 KUAL 应看到 **F1 Dashboard** 菜单

## 使用

| 场景 | 操作 |
|---|---|
| 当信息牌摆着 | KUAL → **Start Service** → 按电源键休眠,屏保显示面板;之后每次唤醒自动刷新,再按电源键睡回去即可 |
| 临时看一眼 | KUAL → **Refresh Now** → 屏幕直接显示 → Home 退出 |
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
│   └── make_tracks.py    # 开发机专用:SVG → tracks.json 转换与验证工具
└── kindle/
    ├── f1dash.py         # 主程序:show / png / service 三模式
    ├── tracks.json       # 23 条赛道轮廓坐标(F1DB,CC BY 4.0)
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
- 守护进程每 3 秒轮询 `powerd state`,唤醒即刷新
- 本设备(PW1 5.6.1.1)外部 RTC 闹钟无法唤醒挂起内核,故不做"休眠中定时刷新"
- 详见 [`docs/项目说明.md`](docs/项目说明.md) §6 踩坑记录
