import Foundation
import GRDB

/// 一次 LLM 调用的 token 用量（LLMClient 回调用）
struct LLMUsage: Sendable {
    var promptTokens: Int
    var completionTokens: Int
}

/// 用量记录：BYOK 下费用发生在用户自己的账号上，
/// 这里只负责记清楚每一层模型各花了多少 token
struct UsageRecord: Codable, FetchableRecord, MutablePersistableRecord, Identifiable {
    static let databaseTableName = "usageRecord"

    var id: Int64?
    var tier: String            // main / utility
    var model: String
    var promptTokens: Int
    var completionTokens: Int
    var createdAt: Date

    mutating func didInsert(_ inserted: InsertionSuccess) {
        id = inserted.rowID
    }
}
