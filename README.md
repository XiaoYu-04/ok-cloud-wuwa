# 云鸣潮 (Cloud Wuthering Waves) — ok-ww 插件

给 [ok-wuthering-waves](https://github.com/ok-oldking/ok-wuthering-waves) 加一个「云鸣潮」入口：
用系统浏览器跑库洛官方云游戏，让 ok-ww 的任务像对待本地游戏一样截图 + 操作它。

## 它是怎么工作的

ok-script 2.x 本身就内建了「浏览器设备」：

- 用 Playwright 拉起系统 Chrome/Edge（`channel='msedge' → 'chrome' → 'chromium'` 依次尝试）
- 用 Windows Graphics Capture 抓窗口
- 用 Playwright 注入键鼠（`BrowserInteraction`）

但 ok-ww 的 `config.py` 里没有 `browser` 这个配置项，所以 `DeviceManager` 从来不会注册这个设备，
UI 上也就看不到。

本插件只做一件事：在启动早期把 `browser_config` 补进 `DeviceManager`，于是
「选择窗口」自动出现「云鸣潮」，选中后由 ok-script 原生逻辑拉起浏览器并接管。

**没有修改 ok-ww 仓库的任何代码。**

## 安装

1. 打开 ok-ww → 左侧栏「插件」→「从 ZIP 导入」
2. 选择 `dist/cloud_wuwa-1.2.0.okscript`
3. 重启 ok-ww

重启后左侧栏会出现「云鸣潮」页。

## 使用

1. 打开「云鸣潮」页，选分辨率（默认 1280x720，与 ok-ww 的 `supported_resolution` 一致）
2. 点「打开云鸣潮」，在弹出的 Chrome 里登录库洛账号
3. 回到「截图方式」页，「选择窗口」里选中「云鸣潮」
4. 像本地游戏一样启动任务

登录态保存在 `cache/cloud_wuwa_chrome`，下次免登录。

输入是通过 Playwright 注入的 DOM 事件（`browser.keyboard / browser.mouse`），云游戏页面
会正常接收——实测按 `Escape` 能在云游戏里打开终端菜单。

## 已知限制

- **仅 Windows**。
- **需要系统装有 Chrome 或 Edge**。
- `supported_resolution` 要求 16:9，所以请保持 1280x720 / 1600x900 / 1920x1080 / 2560x1440。
- 云游戏串流画质受网络影响，OCR 识别率可能不如本地。
- 交互方式是 `BrowserInteraction`（Playwright 注入），不是 PostMessage。

## 帧源与浏览器复用

ok-script 原生的浏览器设备用 Windows Graphics Capture 抓窗口，而 WGC 只在窗口内容
**发生变化**时才推帧。云鸣潮的登录页、加载页、静态界面一旦停住就完全不出帧，
任务侧只会反复打印 `no frame available` 并把任务跳过。本插件因此做了两处改动：

1. **帧源改成 `page.screenshot(type="jpeg")`**，直接读合成后的页面，与窗口是否变化无关。
   插件抓帧路径实测 **约 21 fps**（1280x720 / jpeg q70）。窗口被 ok-ww 全屏盖住时不受影响
   （实测遮挡前后页面都在实时刷新，diff 119 / 132）。
2. **浏览器只启动一次**。启动时先读 profile 里的 `DevToolsActivePort`，如果指向一个还活着的
   实例就 `connect_over_cdp` 接上去，不会再多开窗口；关闭抓帧时只断开连接、不关浏览器，
   所以再点「打开云鸣潮」会回到原来那个窗口，登录状态和游戏画面都还在。

只有当浏览器被最小化时，Chromium 会暂停渲染，截图会挂住——插件检测到窗口最小化会自动
还原它。正常使用（ok-ww 全屏覆盖浏览器）不触发这个情况。

想彻底关掉浏览器，用「云鸣潮」页的「关闭浏览器」按钮。

### 为什么不用推送式帧源

试过 CDP 的 `Page.startScreencast`：静止页面只有 **1 fps**，和 WGC 是同一个毛病——只在画面
变化时才推帧，同样会让任务卡在 `no frame available`。按需截图是这里唯一可行的方案。

原始 CDP `Page.captureScreenshot` 比 `page.screenshot` 快约 30%（28 fps vs 21 fps），
但需要在插件里额外管理一条 CDP 会话的生命周期；21 fps 已足够驱动任务，故不采用。

## 分辨率

下拉框只列 **16:9** 档位：`1280x720`、`1366x768`、`1440x810`、`1600x900`、
`1920x1080`、`2048x1152`、`2560x1440`。

偏要限制成 16:9 的原因：ok-ww 的 `TaskExecutor.check_frame_and_resolution` 会拿画面
宽高比跟 `config.py` 里 `supported_resolution.ratio`（16:9）比对，容差 1%，并要求不小于
`min_size` `1280x720`。比例不符会直接报分辨率错误、任务失败。`1600x1000`、`1920x1200`、
`2560x1080` 这类尺寸技术上都能精确达到，但会被这条校验判死，所以不列。

屏幕比所选档位小时会自动降级到放得下的最大 16:9 尺寸：2560x1380 的屏幕上选 `2560x1440`
会变成 `2384x1341`（仍是精确 16:9，日志会写明）。设备登记的也是降级后的真实尺寸，
不会出现「登记 2560x1440、实际 2384x1341」的错配。

顺带修掉两个拖住启动的坑：窗口放不下目标尺寸时，迭代修正会把窗口越撑越大（实测请求
3440x1440 撑到 6062x1513）；残留的 `DevToolsActivePort` 指向已死端口时，探测会卡满
1.5s 超时。现在先探端口，连不上立刻作废并清掉标记文件（1500ms → 400ms）。

## 画面被裁切的问题（1.1.1 修复）

Chrome 的 `--window-size=1280,720` 设的是**窗口外框**，客户区实际只有 `1264x712`。
页面若按 1280x720 排版，右/下就会被切掉一截——表现为「有时全有时不全」。

原因是旧代码调用 `set_viewport_size(1280,720)` 做设备模拟，强制页面按 1280x720 排版，
与真实客户区不符。现在改为**按 CDP 迭代把客户区调准**：量出页面 `innerWidth/Height`，
按误差修正窗口尺寸，直到精确等于目标分辨率（实测收敛到 1280x720，外框 1296x759）。

注意两个坑，都踩过：

- 不能用 JS 的 `outerWidth - innerWidth` 算边框，Chrome `--app` 窗口报的是两者相等，差值恒为 0。
- 不能用 Win32 `GetWindowRect` 算，ok-ww 进程不是 DPI 感知的，拿到的是缩放后的虚拟坐标，
  按它换算会把窗口越调越大（实测被拉成 1616x908）。

## 配置持久化

选中「云鸣潮」后，`configs/devices.json` 的 `preferred` 会变成 `browser`；下次启动
ok-ww 会直接尝试启动浏览器设备。想切回本地游戏，在「选择窗口」里选回 PC 版即可。

## 开发

```powershell
# 打包
C:\Apps\Local\ok-ww\data\apps\ok-ww\python\python.exe scripts\build_okscript.py
```

产物在 `dist/`。脚本会用 ok-script 自己的 `validate_plugin_manifest` 和
`validate_archive` 校验一遍。

