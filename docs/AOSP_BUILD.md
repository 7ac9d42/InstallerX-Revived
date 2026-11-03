# AOSP APK 与系统替换模块

此 fork 增加手动工作流 **AOSP APK and Module**，不修改上游应用代码。

## 构建

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
