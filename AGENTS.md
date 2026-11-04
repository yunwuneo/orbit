# Orbit — 原生 iOS App

AI 陪伴聊天 App：引导式人设共建 + 模拟世界系统。设计文档见 `docs/design.md`，改动设计前先读它，设计与代码不一致时同步更新文档。

## 技术栈

- **SwiftUI + Swift 6**，iOS 26+（Liquid Glass 是核心 UI 诉求，系统组件默认获得玻璃材质，自定义视图用 `.glassEffect()`）
- **GRDB**（SQLite）：世界日志、人设档案、关系边全部落本地，local-first，无后端
- **XcodeGen**：`Orbit.xcodeproj` 由 `project.yml` 生成，已被 gitignore——**不要手改 xcodeproj，改 `project.yml` 后跑 `xcodegen`**

## SwiftUI 与 iOS 26 API 不要凭记忆写

Liquid Glass / Foundation Models 等新 API 迭代很快。写涉及这些 API 的代码前，核对当前 SDK 的真实接口（查 Apple 文档或 SDK 头文件），不要依赖训练数据里的签名。

## 命令

```bash
xcodegen                          # 重新生成 Xcode 工程
xcodebuild -project Orbit.xcodeproj -scheme Orbit \
  -destination 'platform=iOS Simulator,name=iPhone 17' build   # 编译验证
```

完成任何任务前必须通过编译验证。

## 架构约定

- `Orbit/App/`：App 入口与根视图
- `Orbit/Database/`：GRDB 数据库层（`AppDatabase` 持有 DatabaseQueue 与 migrations）
- `Orbit/Models/`：记录类型，与数据库表一一对应
- 世界日志（`world_events`）是唯一事实源：agent 记忆 = 日志按知情人过滤的视图 + 私密状态，不要另建平行的"记忆表"
- JSON 列用于结构化但探索中的字段（人设细节、参与者列表、知情人集合），核心查询字段必须是独立列
