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
2. 选择 `dist/cloud_wuwa-1.0.0.okscript`
3. 重启 ok-ww

重启后左侧栏会出现「云鸣潮」页。

## 使用

1. 打开「云鸣潮」页，选分辨率（默认 1280x720，与 ok-ww 的 `supported_resolution` 一致）
2. 点「打开云鸣潮」，在弹出的 Chrome 里登录库洛账号
3. 回到「截图方式」页，「选择窗口」里选中「云鸣潮」
4. 像本地游戏一样启动任务

登录态保存在 `cache/playwright`，下次免登录。

## 已知限制

- **仅 Windows**。依赖 WGC（Windows 10 2004+）。
- **需要系统装有 Chrome 或 Edge**。
- `supported_resolution` 要求 16:9，所以请保持 1280x720 / 1600x900 / 1920x1080 / 2560x1440。
- 云游戏串流画质受网络影响，OCR 识别率可能不如本地。
- 浏览器窗口被最小化或完全遮挡时 WGC 可能抓不到帧，保持窗口可见即可。
- 交互方式是 `BrowserInteraction`（Playwright 注入），不是 PostMessage；后台运行时浏览器窗口不要最小化。

## 配置持久化

选中「云鸣潮」后，`configs/devices.json` 的 `preferred` 会变成 `browser`；下次启动
ok-ww 会直接尝试启动浏览器设备。想切回本地游戏，在「选择窗口」里选回 PC 版即可。

## 开发

```powershell
# 打包
C:\Apps\Local\ok-ww\data\apps\ok-ww\python\python.exe build_okscript.py
```

产物在 `dist/`。脚本会用 ok-script 自己的 `validate_plugin_manifest` 和
`validate_archive` 校验一遍。
