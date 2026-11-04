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

        return migrator
    }
}
