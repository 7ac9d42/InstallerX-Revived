# AOSP APK 与系统替换模块

此 fork 提供手动工作流 **AOSP APK and Module** 和自动工作流
**Sync and Deliver Upstream Preview**，不修改上游应用代码。

## 自动同步与 Telegram 交付

每 30 分钟（UTC 每小时第 17、47 分钟）检查上游新预发布，也可手动运行检查。
只有发现尚未交付的预发布才同步、构建和发送。上游 main 的普通提交不触发这些操作。
GitHub 调度可能延迟，不保证发布后即时到达。

自动流程先保存预发布正文，再将标签解析为完整提交，fork main 也只同步到这个提交。
同步采用正常合并，保留 fork 的 `.github/workflows/`、`.github/scripts/`、
`.github/aosp-module/` 和本说明；上游新增的工作流不会混入。
保护路径以外的源码冲突会停止同步，不强推。继承的上游普通包发布流程已禁用。

AOSP 构建沿用现有固定签名，Telegram 只发送校验后的 AOSP APK，
并附上游该次预发布的“更新日志（Changelog）”节及 Full Changelog 链接。
模块和其他校验产物保留在 Actions，不发送 Telegram。
较短日志放在 APK caption 中；超过 Telegram 限制时分成后续完整日志消息。

Telegram 使用 Repository Secrets `TG_BOT_TOKEN` 和 `CHATID`，不需要 Telethon session。
Secrets 只提供给发送步骤，日志不输出其值。构建信息记录预发布来源、源码提交和 CI 配置提交。

独立 `automation-state` 分支保存按源码完整 SHA 索引的交付检查点，不影响应用版本码。
相同提交已交付后不再重复发送，即使上游删除重建了预发布。
明确失败可以手动重跑检查，恢复未完成消息；发送结果不明时保留 `pending_index` 并停止，
需要先核实 Telegram 实际接收情况再修正对应状态，避免重复发送。

首次部署会用当前预发布实际发送一次 APK 与对应日志，随后检查应跳过同一提交。

## 手动构建

在 Actions 中选择该工作流，点击 Run workflow。`source_ref` 为上游
`wxxsfxyzm/InstallerX-Revived` 的分支、标签或提交，默认为 `main`。
工作流单独检出完整上游历史，运行：

```bash
bash ./gradlew assemblePreviewRelease --no-daemon --no-configuration-cache -PAPP_ID=com.android.packageinstaller
```

APK 的版本名和版本码由上游提交生成，不受此 fork 的 CI 配置提交影响。
产物保留 30 天，包括同一次构建的 APK、模块 ZIP、SHA256SUMS 和 build-info.json。
后者记录上游完整提交、APK 包名、版本及签名校验结果。工作流不会创建 Release。

## 签名

必须配置以下 Actions Secrets，缺失时停止构建，不回退到临时 debug 签名：

- `SIGNING_KEY_STORE_BASE64`
- `SIGNING_STORE_PASSWORD`
- `SIGNING_KEY_ALIAS`
- `SIGNING_KEY_PASSWORD`

后续构建应始终使用同一密钥。这是 fork 的独立签名，不是作者签名或 ROM 平台签名；
不能直接覆盖作者签名的 AOSP 构建。普通包名 `com.rosan.installer.x.revived`
也不能更新此构建，即使文件名被改成 PackageInstaller.apk。

## 模块范围与限制

模块针对已有以下布局的设备：

```text
/system/priv-app/PackageInstaller/PackageInstaller.apk
```

模块提供该目录的 APK 替换、目录 `.replace` 标记、同分区权限允许列表及管理器操作按钮。
ZIP 根目录直接包含 module.prop、META-INF 和 system；安装时检查原系统路径。
如 APK 包含原生库，打包脚本从本次 APK 提取到对应系统应用架构目录。

安装前仍须确认 ROM 的签名校验兼容方式、实际权限定义及系统挂载后端。
权限允许列表不能授予仅限平台签名的权限。KernelSU 需要可工作的模块挂载实现，
并注意针对系统安装器的模块卸载策略。

模块不会自动恢复被当前用户卸载的 AOSP 安装器，不会禁用 MIUI 安装器，
不会配置 HyperOS 的原生安装器路由，不会清理全局包扫描缓存，也不会自动重启。
因此“模块构建成功”不代表设备已经由 InstallerX 接管安装。
对于 AOSP 包在当前用户下为 installed=false 的设备，安装状态和 ROM 路由需要单独处理。

本工作流只验证 APK 的真实包名、签名和模块结构，不包含实机启动或安装测试。
系统替换前应具备在模块无法启动时从恢复环境停用模块的条件。

上游集成说明：
https://wxxsfxyzm.github.io/InstallerX-Revived-Website/zh/guide/system-integration.html
