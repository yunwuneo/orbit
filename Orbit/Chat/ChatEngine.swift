import Foundation
import GRDB

/// 对话引擎：上下文组装管线 + 流式回复。
///
/// 喂给主角的上下文 = 人设核心 + 语言习惯 + 示例对话
///   + 用户模型 + 近期世界事件（主角视角过滤）+ 未完结线索 + 当前状态与时刻。
/// 主角只能访问自己视角的素材——世界日志按知情人集合过滤。
@MainActor
@Observable
final class ChatEngine {
    let db: DatabaseQueue
    let llmStore: LLMConfigurationStore
    let distiller: UserModelDistiller

    var messages: [ChatMessage] = []
    var streamingText = ""
    var isReplying = false
    var lastError: String?

    init(db: DatabaseQueue, llmStore: LLMConfigurationStore) {
        self.db = db
        self.llmStore = llmStore
        self.distiller = UserModelDistiller(db: db, llmStore: llmStore)
    }

    func loadHistory(limit: Int = 50) {
        messages = (try? db.read { db in
            try ChatMessage
                // 主动消息可能带未来时间戳：到点才浮现
                .filter(Column("sentAt") <= Date())
                .order(Column("sentAt").desc)
                .limit(limit)
                .fetchAll(db)
                .reversed()
        }) ?? []
    }

    func send(_ text: String) async {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !isReplying else { return }
        guard let client = llmStore.client(for: .main) else {
            lastError = WizardAIError.noModel.localizedDescription
            return
        }

        let now = Date()
        do {
            try await db.write { db in
                var userMessage = ChatMessage(
                    id: nil, role: .user, content: trimmed,
                    sentAt: now, relatedEventIDs: [])
                try userMessage.insert(db)
            }
        } catch {
            lastError = "消息保存失败：\(error.localizedDescription)"
            return
        }

        isReplying = true
        streamingText = ""
        lastError = nil
        defer { isReplying = false }

        do {
            let prompt = try await buildPrompt(userInput: trimmed)
            for try await delta in client.stream(prompt) {
                streamingText += delta
            }
            let reply = streamingText
            guard !reply.isEmpty else { throw LLMError.emptyContent }
            try await db.write { db in
                var assistantMessage = ChatMessage(
                    id: nil, role: .protagonist, content: reply,
                    sentAt: Date(), relatedEventIDs: [])
                try assistantMessage.insert(db)
            }
            streamingText = ""
            loadHistory()
            // 聊完一轮，后台沉淀用户模型（主角越聊越懂你）
            Task { await distiller.distillIfNeeded() }
        } catch {
            lastError = error.localizedDescription
        }
    }

    // MARK: - 上下文组装

    private func buildPrompt(userInput: String) async throws -> [LLMMessage] {
        let context = try await db.read { db in
            (
                protagonist: try Protagonist.fetchOne(db, key: Protagonist.singletonID),
                state: try ProtagonistState.fetchOne(db, key: ProtagonistState.singletonID),
                userModel: try UserModel.fetchOne(db, key: UserModel.singletonID),
                userFacts: try UserFact.order(Column("updatedAt").desc).limit(20).fetchAll(db),
                // 主角视角：只能看到知情人集合包含 "protagonist" 的事件
                recentEvents: try WorldEvent
                    .filter(Column("knowers").like("%\"protagonist\"%"))
                    .order(Column("occurredAt").desc)
                    .limit(10)
                    .fetchAll(db)
                    .reversed(),
                openLoops: try OpenLoop
                    .filter(Column("status") == OpenLoop.Status.open.rawValue)
                    .order(Column("createdAt").desc)
                    .limit(5)
                    .fetchAll(db),
                lastUserAt: try ChatMessage
                    .filter(Column("role") == ChatMessage.Role.user.rawValue)
                    .order(Column("sentAt").desc)
                    .fetchOne(db)?.sentAt,
                absenceRule: try Setting.fetchOne(db, key: WorldSettingKey.absenceInstruction)?.value ?? ""
            )
        }

        guard let p = context.protagonist else {
            throw LLMError.decodingFailed("主角人设不存在")
        }

        var system = """
        你是 \(p.name)。\(p.gender)\(p.age.map { "，\($0) 岁" } ?? "")。\(p.occupation.isEmpty ? "" : "身份：\(p.occupation)。")
        你正在和 \(p.nicknameForUser.isEmpty ? "用户" : "「\(p.nicknameForUser)」") 聊天，你们的关系：\(p.relationToUser)。
        你就是 \(p.name) 本人，不是 AI 助手。永远不要跳出角色，不要提及"人设""模型"这类词。
        """

        if !p.appearance.isEmpty {
            system += "\n\n【外貌】\(p.appearance)"
        }
        if !p.temperament.isEmpty {
            let lines = p.temperament.map { "\($0.question) → \($0.choice)" }.joined(separator: "\n")
            system += "\n\n【性格（行为倾向）】\n\(lines)"
        }

        var styleLines: [String] = []
        if !p.speechStyle.catchphrases.isEmpty {
            styleLines.append("口头禅：\(p.speechStyle.catchphrases.joined(separator: "、"))")
        }
        if !p.speechStyle.sentenceHabit.isEmpty { styleLines.append("打字习惯：\(p.speechStyle.sentenceHabit)") }
        if !p.speechStyle.emojiHabit.isEmpty { styleLines.append("emoji：\(p.speechStyle.emojiHabit)") }
        if !p.speechStyle.replyRhythm.isEmpty { styleLines.append("回复节奏：\(p.speechStyle.replyRhythm)") }
        if !styleLines.isEmpty {
            system += "\n\n【说话方式】严格遵守：\n\(styleLines.joined(separator: "\n"))"
        }

        if !p.sampleDialogues.isEmpty {
            system += "\n\n【典型对话示例】模仿这种风格，但不要重复内容：\n"
                + p.sampleDialogues.joined(separator: "\n---\n")
        }

        var coreLines: [String] = []
        if !p.values.intimacyView.isEmpty { coreLines.append("亲密关系观：\(p.values.intimacyView)") }
        if !p.values.boundaries.isEmpty { coreLines.append("雷区：\(p.values.boundaries.joined(separator: "、"))") }
        if !p.flaws.flaws.isEmpty { coreLines.append("自己的缺陷：\(p.flaws.flaws.joined(separator: "、"))") }
        if !p.flaws.contradictions.isEmpty { coreLines.append("言行不一之处：\(p.flaws.contradictions.joined(separator: "、"))") }
        if !coreLines.isEmpty {
            system += "\n\n【内核】\n\(coreLines.joined(separator: "\n"))"
        }
        if !p.backstory.isEmpty {
            system += "\n\n【忘不掉的往事】\(p.backstory)"
        }

        if let um = context.userModel, !um.impression.isEmpty {
            system += "\n\n【你对 TA 的印象】\(um.impression)"
        }
        if !context.userFacts.isEmpty {
            system += "\n\n【你记住的关于 TA 的事】\n"
                + context.userFacts.map { "- \($0.fact)" }.joined(separator: "\n")
        }

        if !context.recentEvents.isEmpty {
            system += "\n\n【你最近的生活】\n"
                + context.recentEvents.map { "- \($0.summary)" }.joined(separator: "\n")
        }
        if !context.openLoops.isEmpty {
            system += "\n\n【悬而未决的事】\n"
                + context.openLoops.map { "- \($0.content)" }.joined(separator: "\n")
        }

        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "M月d日 EEEE HH:mm"
        var nowLines = ["现在是 \(formatter.string(from: Date()))"]
        if let s = context.state {
            if !s.currentActivity.isEmpty { nowLines.append("你正在：\(s.currentActivity)") }
            if !s.mood.isEmpty { nowLines.append("心情：\(s.mood)") }
            if !s.currentConcern.isEmpty { nowLines.append("正在烦的事：\(s.currentConcern)") }
        }
        // 用户缺席超过一天：告诉她多久没来了，以及用户定下的缺席反应规则
        if let lastUserAt = context.lastUserAt {
            let gapDays = Int(Date().timeIntervalSince(lastUserAt) / 86400)
            if gapDays >= 1 {
                nowLines.append("TA 已经 \(gapDays) 天没来找你了")
                if !context.absenceRule.isEmpty {
                    nowLines.append("关于你如何面对 TA 的缺席：\(context.absenceRule)")
                }
            }
        }
        system += "\n\n【此刻】\n" + nowLines.joined(separator: "\n")

        var prompt: [LLMMessage] = [.system(system)]
        for message in messages.suffix(30) {
            switch message.role {
            case .user: prompt.append(.user(message.content))
            case .protagonist: prompt.append(.assistant(message.content))
            case .system: break
            }
        }
        prompt.append(.user(userInput))
        return prompt
    }
}
