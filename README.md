# Relay Desk 预览版

Relay Desk 是第三方 Windows 工作台，用于本机 Codex 账号管理、额度查看和环境诊断。

## 环境

- Windows x64，标准用户会话，Microsoft Edge WebView2 Runtime。
- Python 3.12 或以上，通过 Windows Python 启动器 `py` 运行。
- 官方 Codex CLI 0.140.0 或以上。请先在 PowerShell 确认 `codex --version` 可用。设备登录的可用性取决于账号和工作区设置。

## 构建和首次使用

在源码根目录打开 PowerShell，执行：

```powershell
.\Build.ps1
```

脚本使用 Windows 的 .NET Framework 编译器，并下载、校验指定版本的 WebView2 SDK。构建产物位于 `dist`，请保留该目录中的程序和配套文件。

1. 打开 `dist\Relay Desk Preview.exe`。
2. 工作台自动沿用本机网络。配置了环境变量或 Windows 系统代理时，工作台会读取已配置的地址和端口。使用系统路由的 VPN 或 TUN 也可直接使用。
3. 在「账号管理」点 **添加账号**，再到 OpenAI 官方页面完成设备登录。可在卡片里管理账号，也可以刷新全部额度。
4. 工作台会在启动时检查两个服务域名的连通性。点 **查看网络** 可查看检测结果、重新检测和检查 CLI 环境。代理软件只开放本地端口时，可选择 **手动指定代理**，填写带端口的 HTTP 或 HTTPS 地址。

网络检测使用 Windows 自带的 `curl.exe`，向 `chatgpt.com` 和 `auth.openai.com` 发出无账号凭据的 HTTPS 请求。检测结果反映域名的连通性和响应状态。手动代理作用于工作台的账号查询和设备登录请求，浏览器页面沿用浏览器本身的网络设置。

关闭窗口会收进托盘。双击托盘图标可以恢复，右键选择「退出 Relay Desk」可结束应用。

## 本地数据和问题反馈

预览版的账号、设置和额度缓存位于 `%LOCALAPPDATA%\RelayDeskPreview`。设备登录使用 Codex CLI 的系统凭据存储，登录代码仅在工作台中临时显示。每个监控账号使用独立的 Codex 配置目录。

报告问题时，可提供 Windows 版本、Codex CLI 版本、失败步骤和工作台错误提示。提交前请检查截图、日志和路径中是否有邮箱、设备代码、凭据或业务内容。

安全问题请按 [安全问题反馈](SECURITY.md) 提交。

本预览版的发布范围为账号管理、额度查看和本机诊断。

## 测试

```powershell
py -3 -B -m unittest discover -s tests
```

测试使用临时目录和模拟账号。GitHub Actions 会在 Windows 环境中执行这些测试并构建工作台。设备登录验收需要在交互式桌面中完成。

依赖来源见 [第三方说明](THIRD_PARTY_NOTICES.md)。
