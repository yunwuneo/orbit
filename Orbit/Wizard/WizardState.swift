import Foundation
import GRDB

struct NPCDraft: Identifiable, Equatable {
    let id = UUID()
    var name = ""
    var relation = ""
    var personality = ""
    var secret = ""

    var isValid: Bool { !name.trimmingCharacters(in: .whitespaces).isEmpty }
}

/// 人设向导的状态容器：七章的填写进度 + AI 提议能力 + 最终落库。
/// 混合主动权：每一步用户可自己写 / 用 AI 提议改 / 让 AI 代定。
@MainActor
@Observable
final class WizardState {
    let db: DatabaseQueue
    let llmStore: LLMConfigurationStore

    init(db: DatabaseQueue, llmStore: LLMConfigurationStore) {
        self.db = db
        self.llmStore = llmStore
    }

    // 第一章 锚点
    var name = ""
    var gender = ""
    var ageText = ""
    var appearance = ""

    // 第二章 气质（情境选择题，行为倾向而非形容词）
    var temperament: [TemperamentChoice] = []

    // 第三章 声音
    var catchphrasesText = ""
    var sentenceHabit = ""
    var emojiHabit = ""
    var replyRhythm = ""
    var sampleDialogues: [String] = []

    // 第四章 内核
    var intimacyView = ""
    var boundariesText = ""
    var flawsText = ""
    var contradictionsText = ""
    var backstory = ""

    // 第五章 生活
    var occupation = ""
    var scheduleWeekday = ""
    var scheduleWeekend = ""
    var currentConcern = ""

    // 第六章 世界（4 个 NPC 速写）
    static let npcSlots = ["密友", "同事 / 同学", "家人", "张力角色（暧昧 / 死对头 / 前任…）"]
    var npcs: [NPCDraft] = WizardState.npcSlots.map { NPCDraft(relation: $0) }
    var npcRelationsNote = ""

    // 第七章 你们
    var relationToUser = ""
    var nicknameForUser = ""

    // MARK: - 气质题库

    static let temperamentQuestions: [(question: String, options: [String])] = [
        ("遇到烦心事，TA 更可能……", ["找人倾诉", "自己闷着", "阴阳怪气两句", "转头就忘"]),
        ("朋友深夜发来消息，TA……", ["秒回", "看完心情再说", "假装睡了", "直接打电话过去"]),
        ("被夸奖时，TA……", ["开心但嘴硬", "坦然接受", "害羞转移话题", "反夸回去"]),
        ("计划被打乱时，TA……", ["烦躁但能调整", "随遇而安", "崩溃十分钟再继续", "早就备了 Plan B"]),
        ("在陌生聚会上，TA……", ["主动破冰", "找角落观察", "粘着熟人", "先溜为敬"]),
        ("和人意见不合时，TA……", ["据理力争", "点头但心里不服", "打圆场", "冷处理"]),
    ]

    var canFinish: Bool {
        !name.trimmingCharacters(in: .whitespaces).isEmpty
            && !gender.trimmingCharacters(in: .whitespaces).isEmpty
    }

    // MARK: - AI 提议（主力模型）

    private var mainClient: LLMClient? { llmStore.client(for: .main) ?? llmStore.client(for: .utility) }

    /// 当前已积累的人设摘要，作为 AI 提议的上下文
    private func personaSoFar() -> String {
        var parts: [String] = []
        if !name.isEmpty { parts.append("名字：\(name)") }
        if !gender.isEmpty { parts.append("性别：\(gender)") }
        if !ageText.isEmpty { parts.append("年龄：\(ageText)") }
        if !appearance.isEmpty { parts.append("外貌：\(appearance)") }
        for t in temperament { parts.append("\(t.question) → \(t.choice)") }
        if !catchphrasesText.isEmpty { parts.append("口头禅：\(catchphrasesText)") }
        if !sentenceHabit.isEmpty { parts.append("打字习惯：\(sentenceHabit)") }
        if !intimacyView.isEmpty { parts.append("亲密关系观：\(intimacyView)") }
        if !flawsText.isEmpty { parts.append("缺陷：\(flawsText)") }
        if !backstory.isEmpty { parts.append("过去：\(backstory)") }
        if !occupation.isEmpty { parts.append("身份：\(occupation)") }
        return parts.joined(separator: "\n")
    }

    struct DialogueProposals: Codable, Sendable { var dialogues: [String] }
    struct NPCProposal: Codable, Sendable { var name: String; var personality: String; var secret: String }

    /// 第三章：基于已有的人设生成 3 段典型对话片段供挑选/修改
    func generateSampleDialogues() async throws -> [String] {
        guard let client = mainClient else { throw WizardAIError.noModel }
        let result = try await client.structured(
            [
                .system("你是角色塑造师。基于给定的人设，写出 3 段这个角色与他人发消息的典型对话片段。每段 4~8 条消息，用「对方：」「\(name.isEmpty ? "TA" : name)：」分行标注。要求：体现口头禅、打字习惯、性格，风格真实不油腻。三段分别体现：日常闲聊、关心对方、遇到烦心事。"),
                .user("人设：\n\(personaSoFar())"),
            ],
            name: "sample_dialogues",
            schema: JSONSchemaObject(object: [
                "type": "object",
                "properties": [
                    "dialogues": ["type": "array", "items": ["type": "string"], "minItems": 3, "maxItems": 3],
                ],
                "required": ["dialogues"],
                "additionalProperties": false,
            ]),
            as: DialogueProposals.self)
        return result.dialogues
    }

    /// 第六章：为指定生态位提议一个 NPC 速写
    func proposeNPC(slot: String) async throws -> NPCProposal {
        guard let client = mainClient else { throw WizardAIError.noModel }
        let existing = npcs.filter(\.isValid).map(\.name).joined(separator: "、")
        return try await client.structured(
            [
                .system("你是角色塑造师，正在为一个 AI 陪伴角色的生活世界设计配角。为指定的生态位设计一个配角速写：名字（中文圈常见但不烂大街）、一句话性格（要有棱角，不要老好人）、一个秘密或执念（世界戏剧性的燃料）。\(!existing.isEmpty ? "已有配角：\(existing)，注意区分度。" : "")"),
                .user("主角：\n\(personaSoFar())\n\n生态位：\(slot)"),
            ],
            name: "npc_proposal",
            schema: JSONSchemaObject(object: [
                "type": "object",
                "properties": [
                    "name": ["type": "string"],
                    "personality": ["type": "string"],
                    "secret": ["type": "string"],
                ],
                "required": ["name", "personality", "secret"],
                "additionalProperties": false,
            ]),
            as: NPCProposal.self)
    }

    // MARK: - 落库：世界点火

    func save() throws {
        let now = Date()
        let protagonistID = Protagonist.singletonID

        try db.write { database in
            var protagonist = Protagonist(
                id: protagonistID,
                name: name.trimmingCharacters(in: .whitespaces),
                gender: gender.trimmingCharacters(in: .whitespaces),
                age: Int(ageText),
                appearance: appearance,
                temperament: temperament,
                speechStyle: SpeechStyle(
                    catchphrases: splitLines(catchphrasesText),
                    sentenceHabit: sentenceHabit,
                    emojiHabit: emojiHabit,
                    replyRhythm: replyRhythm),
                values: ValuesProfile(
                    intimacyView: intimacyView,
                    boundaries: splitLines(boundariesText)),
                flaws: FlawsProfile(
                    flaws: splitLines(flawsText),
                    contradictions: splitLines(contradictionsText)),
                backstory: backstory,
                sampleDialogues: sampleDialogues,
                occupation: occupation,
                schedule: ScheduleTemplate(
                    weekday: splitLines(scheduleWeekday),
                    weekend: splitLines(scheduleWeekend)),
                relationToUser: relationToUser,
                nicknameForUser: nicknameForUser,
                createdAt: now,
                updatedAt: now)
            try protagonist.save(database)

            var state = ProtagonistState(
                id: ProtagonistState.singletonID,
                mood: "", energy: 100,
                currentActivity: "", currentConcern: currentConcern,
                updatedAt: now)
            try state.save(database)

            var userModel = UserModel(
                id: UserModel.singletonID,
                impression: "", relationshipStage: relationToUser,
                updatedAt: now)
            try userModel.save(database)

            for draft in npcs where draft.isValid {
                var npc = NPC(
                    id: UUID().uuidString,
                    name: draft.name.trimmingCharacters(in: .whitespaces),
                    relationToProtagonist: draft.relation,
                    personalitySketch: draft.personality,
                    secret: draft.secret,
                    profileExtra: "",
                    mood: "", currentConcern: "",
                    stateUpdatedAt: now, createdAt: now, updatedAt: now)
                try npc.insert(database)

                var relation = Relation(
                    id: nil,
                    fromID: npc.id, toID: Relation.protagonistNodeID,
                    kind: draft.relation, warmth: 50, recentChange: "",
                    updatedAt: now)
                try relation.insert(database)
            }

            if !npcRelationsNote.trimmingCharacters(in: .whitespaces).isEmpty {
                var note = Setting(key: "world.npcRelationsNote", value: npcRelationsNote)
                try note.save(database)
            }

            // 世界点火：世界日志的第一条事件
            var ignition = WorldEvent(
                id: nil, occurredAt: now, type: .milestone,
                summary: "世界点火",
                detail: "\(protagonist.name) 的世界开始运转。",
                participants: [Relation.protagonistNodeID],
                knowers: [Relation.protagonistNodeID],
                consequences: [],
                createdAt: now)
            try ignition.insert(database)

            // 近期烦恼成为第一条未完结线索
            let concern = currentConcern.trimmingCharacters(in: .whitespaces)
            if !concern.isEmpty {
                var loop = OpenLoop(
                    id: nil, content: concern,
                    participants: [Relation.protagonistNodeID],
                    expectedAdvanceAt: nil,
                    status: .open,
                    sourceEventID: ignition.id,
                    createdAt: now, resolvedAt: nil)
                try loop.insert(database)
            }
        }
    }

    private func splitLines(_ text: String) -> [String] {
        text.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }
}

enum WizardAIError: Error, LocalizedError {
    case noModel

    var errorDescription: String? {
        "尚未配置模型。请先在「模型设置」中配置端点，或手动填写。"
    }
}
