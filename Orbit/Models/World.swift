import Foundation
import GRDB

/// 世界事件类型
enum WorldEventType: String, Codable, CaseIterable {
    case routine       // 日常
    case relationship  // 关系
    case external      // 外部（天气/节日/大环境）
    case milestone     // 里程碑
}

/// 世界日志条目：全局唯一事实源。
/// 每个 agent 的记忆 = 本表按 `knowers` 过滤的视图 + 各自的私密状态。
struct WorldEvent: Codable, FetchableRecord, MutablePersistableRecord, Identifiable {
    static let databaseTableName = "worldEvent"

    var id: Int64?
    var occurredAt: Date             // 世界时间（与真实时间对称）
    var type: WorldEventType
    var summary: String              // 一句话摘要（快进压缩的粒度）
    var detail: String               // 完整描述
    var participants: [String]       // JSON 列：节点 ID（"protagonist" 或 NPC UUID）
    var knowers: [String]            // JSON 列：知情人集合，信息不对称的关键
    var consequences: [String]       // JSON 列：后果（状态/关系变化的记录）
    var createdAt: Date

    mutating func didInsert(_ inserted: InsertionSuccess) {
        id = inserted.rowID
    }
}

/// 未完结线索：世界永远欠着几个线头，对话就永远有水源
struct OpenLoop: Codable, FetchableRecord, MutablePersistableRecord, Identifiable {
    static let databaseTableName = "openLoop"

    enum Status: String, Codable {
        case open
        case resolved
        case dropped
    }

    var id: Int64?
    var content: String              // 线索内容（"明天要面试，很紧张"）
    var participants: [String]       // JSON 列
    var expectedAdvanceAt: Date?     // 预期推进时间
    var status: Status
    var sourceEventID: Int64?        // 来源事件
    var createdAt: Date
    var resolvedAt: Date?

    mutating func didInsert(_ inserted: InsertionSuccess) {
        id = inserted.rowID
    }
}

/// 聊天消息
struct ChatMessage: Codable, FetchableRecord, MutablePersistableRecord, Identifiable {
    static let databaseTableName = "chatMessage"

    enum Role: String, Codable {
        case user
        case protagonist
        case system
    }

    var id: Int64?
    var role: Role
    var content: String
    var sentAt: Date
    var relatedEventIDs: [Int64]     // JSON 列：本条消息涉及的世界事件

    mutating func didInsert(_ inserted: InsertionSuccess) {
        id = inserted.rowID
    }
}

/// 设置（世界模式/缺席策略/活跃度档位/模型端点/预算/静默时段），key-value JSON
struct Setting: Codable, FetchableRecord, MutablePersistableRecord, Identifiable {
    static let databaseTableName = "setting"

    var id: String { key }
    var key: String
    var value: String                // JSON 编码的值
}
