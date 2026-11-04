import Foundation
import GRDB

/// 模型分层：主力模型（主角对话，吃质量）与勤务模型（滴答/摘要，吃便宜）
enum LLMTier: String, Codable, CaseIterable, Sendable {
    case main
    case utility

    var displayName: String {
        switch self {
        case .main: "主力模型（对话）"
        case .utility: "勤务模型（世界引擎）"
        }
    }

    var keychainKey: String { "llm.\(rawValue).apiKey" }
    var settingsKey: String { "llm.\(rawValue).endpoint" }
}

/// 一个 OpenAI 兼容端点的配置（不含 API Key）
struct LLMEndpointConfig: Codable, Equatable, Sendable {
    var baseURL: String = ""   // 如 https://api.openai.com/v1
    var model: String = ""

    var isComplete: Bool {
        !baseURL.trimmingCharacters(in: .whitespaces).isEmpty
            && !model.trimmingCharacters(in: .whitespaces).isEmpty
    }
}

/// 端点配置的读取与写入（设置表存配置，Keychain 存密钥）
struct LLMConfigurationStore: Sendable {
    let db: DatabaseQueue

    func endpoint(for tier: LLMTier) -> LLMEndpointConfig {
        (try? db.read { db in
            try Setting.fetchOne(db, key: tier.settingsKey)?.value
        })
        .flatMap { try? JSONDecoder().decode(LLMEndpointConfig.self, from: Data($0.utf8)) }
        ?? LLMEndpointConfig()
    }

    func saveEndpoint(_ config: LLMEndpointConfig, for tier: LLMTier) throws {
        let json = String(data: try JSONEncoder().encode(config), encoding: .utf8)!
        var setting = Setting(key: tier.settingsKey, value: json)
        try db.write { db in
            try setting.save(db)
        }
    }

    func apiKey(for tier: LLMTier) -> String {
        KeychainHelper.get(tier.keychainKey) ?? ""
    }

    func saveAPIKey(_ key: String, for tier: LLMTier) throws {
        if key.isEmpty {
            KeychainHelper.delete(tier.keychainKey)
        } else {
            try KeychainHelper.set(key, for: tier.keychainKey)
        }
    }

    /// 该层是否可用（配置完整且有 Key）
    func isReady(_ tier: LLMTier) -> Bool {
        endpoint(for: tier).isComplete && !apiKey(for: tier).isEmpty
    }

    func client(for tier: LLMTier) -> LLMClient? {
        let config = endpoint(for: tier)
        let key = apiKey(for: tier)
        guard config.isComplete, !key.isEmpty else { return nil }
        var client = LLMClient(config: config, apiKey: key)
        client.usageHandler = { [db] usage in
            try? db.write { db in
                var record = UsageRecord(
                    id: nil, tier: tier.rawValue, model: config.model,
                    promptTokens: usage.promptTokens,
                    completionTokens: usage.completionTokens,
                    createdAt: Date())
                try record.insert(db)
            }
        }
        return client
    }
}
