import Foundation
import GRDB

/// NPC：30 秒速写产生的轻量人设，细节由 GodView 在模拟中补完并沉淀回来。
/// 动态状态比主角简化一档，直接内嵌。
struct NPC: Codable, FetchableRecord, MutablePersistableRecord, Identifiable {
    static let databaseTableName = "npc"

    var id: String                   // UUID
    var name: String
    var relationToProtagonist: String // 密友/同事/家人/张力角色…
    var personalitySketch: String     // 一句话性格
    var secret: String                // 一个秘密或执念：世界戏剧性的初始燃料
    var profileExtra: String          // GodView 补完的人设细节（追加式文本）

    // 简化动态状态
    var mood: String
    var currentConcern: String
    var stateUpdatedAt: Date

    var createdAt: Date
    var updatedAt: Date
}

/// 关系边：主角与 NPC、NPC 与 NPC 之间。`protagonist` 为主角的固定节点 ID。
struct Relation: Codable, FetchableRecord, MutablePersistableRecord, Identifiable {
    static let databaseTableName = "relation"
    static let protagonistNodeID = "protagonist"

    var id: Int64?
    var fromID: String
    var toID: String
    var kind: String                 // 关系性质（闺蜜/同事/母子/暧昧…）
    var warmth: Int                  // 温度 -100...100
    var recentChange: String         // 近期变化（关系演化的叙事注脚）
    var updatedAt: Date

    mutating func didInsert(_ inserted: InsertionSuccess) {
        id = inserted.rowID
    }
}
