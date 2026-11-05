import Foundation
import GRDB

/// 本地数据库入口：local-first，所有数据不出设备。
final class AppDatabase: Sendable {
    let db: DatabaseQueue

    init(path: String? = nil) throws {
        let dbPath: String
        if let path {
            dbPath = path
        } else {
            let dir = try FileManager.default.url(
                for: .applicationSupportDirectory, in: .userDomainMask,
                appropriateFor: nil, create: true)
            dbPath = dir.appendingPathComponent("orbit.sqlite").path
        }
        var config = Configuration()
        config.foreignKeysEnabled = true
        db = try DatabaseQueue(path: dbPath, configuration: config)
        try migrator.migrate(db)
    }

    /// 内存数据库，供测试与 Preview 使用
    static func inMemory() throws -> AppDatabase {
        try AppDatabase(path: ":memory:")
    }
}

// MARK: - Migrations

extension AppDatabase {
    var migrator: DatabaseMigrator {
        var migrator = DatabaseMigrator()

        migrator.registerMigration("v1_initialSchema") { db in
            // 主角人设（静态，单例行 id = 1）
            try db.create(table: Protagonist.databaseTableName) { t in
                t.primaryKey("id", .integer)
                t.column("name", .text).notNull()
                t.column("gender", .text).notNull()
                t.column("age", .integer)
                t.column("appearance", .text).notNull().defaults(to: "")
                t.column("temperament", .jsonText).notNull().defaults(to: "[]")
                t.column("speechStyle", .jsonText).notNull().defaults(to: "{}")
                t.column("values", .jsonText).notNull().defaults(to: "{}")
                t.column("flaws", .jsonText).notNull().defaults(to: "{}")
                t.column("backstory", .text).notNull().defaults(to: "")
                t.column("sampleDialogues", .jsonText).notNull().defaults(to: "[]")
                t.column("occupation", .text).notNull().defaults(to: "")
                t.column("schedule", .jsonText).notNull().defaults(to: "{}")
                t.column("relationToUser", .text).notNull().defaults(to: "")
                t.column("nicknameForUser", .text).notNull().defaults(to: "")
                t.column("createdAt", .datetime).notNull()
                t.column("updatedAt", .datetime).notNull()
            }

            // 主角动态状态（单例行 id = 1）
            try db.create(table: ProtagonistState.databaseTableName) { t in
                t.primaryKey("id", .integer)
                t.column("mood", .text).notNull().defaults(to: "")
                t.column("energy", .integer).notNull().defaults(to: 100)
                t.column("currentActivity", .text).notNull().defaults(to: "")
                t.column("currentConcern", .text).notNull().defaults(to: "")
                t.column("updatedAt", .datetime).notNull()
            }

            // 主角的用户模型（单例行 id = 1）
            try db.create(table: UserModel.databaseTableName) { t in
                t.primaryKey("id", .integer)
                t.column("impression", .text).notNull().defaults(to: "")
                t.column("relationshipStage", .text).notNull().defaults(to: "")
                t.column("updatedAt", .datetime).notNull()
            }

            // 用户事实库
            try db.create(table: UserFact.databaseTableName) { t in
                t.autoIncrementedPrimaryKey("id")
                t.column("fact", .text).notNull()
                t.column("category", .text).notNull().defaults(to: "")
                t.column("createdAt", .datetime).notNull()
                t.column("updatedAt", .datetime).notNull()
            }

            // NPC（含简化动态状态）
            try db.create(table: NPC.databaseTableName) { t in
                t.primaryKey("id", .text)
                t.column("name", .text).notNull()
                t.column("relationToProtagonist", .text).notNull()
                t.column("personalitySketch", .text).notNull().defaults(to: "")
                t.column("secret", .text).notNull().defaults(to: "")
                t.column("profileExtra", .text).notNull().defaults(to: "")
                t.column("mood", .text).notNull().defaults(to: "")
                t.column("currentConcern", .text).notNull().defaults(to: "")
                t.column("stateUpdatedAt", .datetime).notNull()
                t.column("createdAt", .datetime).notNull()
                t.column("updatedAt", .datetime).notNull()
            }

            // 关系边（主角 ↔ NPC，NPC ↔ NPC）
            try db.create(table: Relation.databaseTableName) { t in
                t.autoIncrementedPrimaryKey("id")
                t.column("fromID", .text).notNull()
                t.column("toID", .text).notNull()
                t.column("kind", .text).notNull().defaults(to: "")
                t.column("warmth", .integer).notNull().defaults(to: 0)
                t.column("recentChange", .text).notNull().defaults(to: "")
                t.column("updatedAt", .datetime).notNull()
                t.uniqueKey(["fromID", "toID"])
            }

            // 世界日志：全局唯一事实源
            try db.create(table: WorldEvent.databaseTableName) { t in
                t.autoIncrementedPrimaryKey("id")
                t.column("occurredAt", .datetime).notNull().indexed()
                t.column("type", .text).notNull()
                t.column("summary", .text).notNull()
                t.column("detail", .text).notNull().defaults(to: "")
                t.column("participants", .jsonText).notNull().defaults(to: "[]")
                t.column("knowers", .jsonText).notNull().defaults(to: "[]")
                t.column("consequences", .jsonText).notNull().defaults(to: "[]")
                t.column("createdAt", .datetime).notNull()
            }

            // 未完结线索
            try db.create(table: OpenLoop.databaseTableName) { t in
                t.autoIncrementedPrimaryKey("id")
                t.column("content", .text).notNull()
                t.column("participants", .jsonText).notNull().defaults(to: "[]")
                t.column("expectedAdvanceAt", .datetime)
                t.column("status", .text).notNull().defaults(to: OpenLoop.Status.open.rawValue)
                t.column("sourceEventID", .integer)
                    .references(WorldEvent.databaseTableName, onDelete: .setNull)
                t.column("createdAt", .datetime).notNull()
                t.column("resolvedAt", .datetime)
            }

            // 聊天消息
            try db.create(table: ChatMessage.databaseTableName) { t in
                t.autoIncrementedPrimaryKey("id")
                t.column("role", .text).notNull()
                t.column("content", .text).notNull()
                t.column("sentAt", .datetime).notNull().indexed()
                t.column("relatedEventIDs", .jsonText).notNull().defaults(to: "[]")
            }

            // 设置
            try db.create(table: Setting.databaseTableName) { t in
                t.primaryKey("key", .text)
                t.column("value", .text).notNull()
            }
        }

        migrator.registerMigration("v2_usageRecords") { db in
            // 用量记录（仪表盘只读统计，可整体清空）
            try db.create(table: UsageRecord.databaseTableName) { t in
                t.autoIncrementedPrimaryKey("id")
                t.column("tier", .text).notNull()
                t.column("model", .text).notNull().defaults(to: "")
                t.column("promptTokens", .integer).notNull().defaults(to: 0)
                t.column("completionTokens", .integer).notNull().defaults(to: 0)
                t.column("createdAt", .datetime).notNull().indexed()
            }
        }

        return migrator
    }
}

// MARK: - 数据管理（设置页的清空入口）

extension DatabaseQueue {
    /// 只清聊天记录，人设与世界不受影响
    func wipeChatHistory() throws {
        try write { db in
            try ChatMessage.deleteAll(db)
        }
    }

    /// 清世界运转产生的数据（事件/未结线索/主角动态状态），保留人设档案与 NPC
    @MainActor
    func wipeWorldData() throws {
        try write { db in
            try WorldEvent.deleteAll(db)
            try OpenLoop.deleteAll(db)
            try ProtagonistState.deleteAll(db)
            // 世界时钟重新锚定到当下，避免清空后立即补演一大段
            var tick = Setting(
                key: WorldEngine.lastTickKey,
                value: ISO8601DateFormatter().string(from: Date()))
            try tick.save(db)
        }
    }

    /// 全部重置：回到刚装好 App 的状态，仅保留 LLM 端点配置与钥匙串里的 Key
    func wipeAllUserData() throws {
        try write { db in
            try ChatMessage.deleteAll(db)
            try WorldEvent.deleteAll(db)
            try OpenLoop.deleteAll(db)
            try ProtagonistState.deleteAll(db)
            try Relation.deleteAll(db)
            try NPC.deleteAll(db)
            try UserFact.deleteAll(db)
            try UserModel.deleteAll(db)
            try Protagonist.deleteAll(db)
            try UsageRecord.deleteAll(db)
            try Setting.filter(!Column("key").like("llm.%")).deleteAll(db)
        }
    }
}
