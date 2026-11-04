import Foundation
import GRDB

/// 用户模型沉淀器：聊天之后，用勤务模型从最近对话里
/// 提取关于用户的事实（UserFact）、改写主角对用户的整体印象（UserModel）。
///
/// 与用户主动填写不同，这是主角"越聊越懂你"的来源；
/// 失败静默，不阻塞聊天。
@MainActor
final class UserModelDistiller {
    let db: DatabaseQueue
    let llmStore: LLMConfigurationStore

    /// 两次沉淀的最小间隔（省 token）
    static let minInterval: TimeInterval = 10 * 60
    /// 记忆事实的保有上限，超出后淘汰最久未更新的
    static let maxFacts = 60

    private static let lastRunKey = "userModel.lastDistillAt"

    init(db: DatabaseQueue, llmStore: LLMConfigurationStore) {
        self.db = db
        self.llmStore = llmStore
    }

    struct FactPayload: Codable, Sendable {
        var fact: String
        var category: String
    }
    struct DistillResult: Codable, Sendable {
        var newFacts: [FactPayload]
        var impression: String
        var relationshipStage: String
    }

    func distillIfNeeded() async {
        let now = Date()
        if let last = lastRunAt(), now.timeIntervalSince(last) < Self.minInterval { return }
        guard let client = llmStore.client(for: .utility) ?? llmStore.client(for: .main) else { return }

        do {
            guard let input = try await gatherInput(since: lastRunAt()) else { return }
            let result = try await run(client: client, input: input)
            try await persist(result: result, existingFacts: input.existingFacts, now: now)
            saveLastRunAt(now)
        } catch {
            // 沉淀失败静默：下次再试
        }
    }

    // MARK: - 输入组装

    private struct Input {
        var impression: String
        var relationshipStage: String
        var existingFacts: [String]
        var dialogueLines: [String]
    }

    /// 没有新对话则返回 nil（不白费 token）
    private func gatherInput(since lastRun: Date?) async throws -> Input? {
        let context = try await db.read { db in
            (
                userModel: try UserModel.fetchOne(db, key: UserModel.singletonID),
                facts: try UserFact.order(Column("updatedAt").desc).limit(40).fetchAll(db),
                messages: try ChatMessage
                    .order(Column("sentAt").desc)
                    .limit(16)
                    .fetchAll(db)
                    .reversed()
            )
        }

        let newMessages = context.messages.filter {
            $0.role == .user && (lastRun == nil || $0.sentAt > lastRun!)
        }
        guard !newMessages.isEmpty else { return nil }

        let dialogueLines = context.messages.map {
            ($0.role == .user ? "用户" : "主角") + "：" + $0.content
        }

        return Input(
            impression: context.userModel?.impression ?? "",
            relationshipStage: context.userModel?.relationshipStage ?? "",
            existingFacts: context.facts.map(\.fact),
            dialogueLines: dialogueLines)
    }

    // MARK: - 推演

    private func run(client: LLMClient, input: Input) async throws -> DistillResult {
        let system = """
        你是记忆沉淀器。以下是主角与用户的最近对话，以及主角目前已有的用户档案。
        任务：
        1. 从对话中提取【新的】关于用户的事实：习惯、经历、偏好、近况、关系事件。
           只提对话里明确出现或强烈暗示的信息，不要推测；已有事实不要重复；最多 5 条。
           category 填：习惯 / 经历 / 偏好 / 近况 / 关系事件
        2. 改写主角对用户的整体印象（impression）：在原印象基础上融入新对话带来的变化，
           80 字以内，用主角的口吻（"TA 这个人……"）。没变化就原样返回。
        3. 判断关系阶段（relationshipStage）：如初识 / 熟络 / 信任 / 亲密，可带一句注脚。

        当前印象：\(input.impression.isEmpty ? "（还没有）" : input.impression)
        当前关系阶段：\(input.relationshipStage.isEmpty ? "（未判断）" : input.relationshipStage)
        已有事实（不要重复）：
        \(input.existingFacts.isEmpty ? "（无）" : input.existingFacts.map { "- \($0)" }.joined(separator: "\n"))
        """

        return try await client.structured(
            [.system(system), .user("最近对话：\n" + input.dialogueLines.joined(separator: "\n"))],
            name: "user_distill",
            schema: JSONSchemaObject(object: [
                "type": "object",
                "properties": [
                    "newFacts": [
                        "type": "array",
                        "items": [
                            "type": "object",
                            "properties": [
                                "fact": ["type": "string"],
                                "category": ["type": "string"],
                            ],
                            "required": ["fact", "category"],
                            "additionalProperties": false,
                        ],
                    ],
                    "impression": ["type": "string"],
                    "relationshipStage": ["type": "string"],
                ],
                "required": ["newFacts", "impression", "relationshipStage"],
                "additionalProperties": false,
            ]),
            as: DistillResult.self)
    }

    // MARK: - 落库

    private func persist(result: DistillResult, existingFacts: [String], now: Date) async throws {
        let known = Set(existingFacts.map { $0.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() })
        let maxFacts = Self.maxFacts

        try await db.write { db in
            for payload in result.newFacts.prefix(5) {
                let normalized = payload.fact.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !normalized.isEmpty,
                      !known.contains(normalized.lowercased()) else { continue }
                var fact = UserFact(
                    id: nil, fact: normalized, category: payload.category,
                    createdAt: now, updatedAt: now)
                try fact.insert(db)
            }

            // 超出上限时淘汰最久未更新的事实
            let overflow = try UserFact.fetchCount(db) - maxFacts
            if overflow > 0 {
                try UserFact
                    .order(Column("updatedAt"))
                    .limit(overflow)
                    .deleteAll(db)
            }

            if var model = try UserModel.fetchOne(db, key: UserModel.singletonID) {
                if !result.impression.isEmpty { model.impression = result.impression }
                if !result.relationshipStage.isEmpty { model.relationshipStage = result.relationshipStage }
                model.updatedAt = now
                try model.update(db)
            } else if !result.impression.isEmpty {
                var model = UserModel(
                    id: UserModel.singletonID,
                    impression: result.impression,
                    relationshipStage: result.relationshipStage,
                    updatedAt: now)
                try model.insert(db)
            }
        }
    }

    // MARK: - 沉淀时间记录

    private func lastRunAt() -> Date? {
        (try? db.read { try Setting.fetchOne($0, key: Self.lastRunKey)?.value })
            .flatMap { ISO8601DateFormatter().date(from: $0) }
    }

    private func saveLastRunAt(_ date: Date) {
        var setting = Setting(key: Self.lastRunKey, value: ISO8601DateFormatter().string(from: date))
        try? db.write { try setting.save($0) }
    }
}
