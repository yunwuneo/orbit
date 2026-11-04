import Foundation
import GRDB

/// 世界引擎（GodView 的 v1 形态）：惰性快进。
///
/// 不实时空跑模拟——用户回来时，计算距上次滴答的时长，
/// 用勤务模型快进推演这段时间的世界事件，更新主角状态、推进未完结线索。
/// 世界日志是唯一事实源；每条事件带知情人集合，主角只看到自己视角。
@MainActor
@Observable
final class WorldEngine {
    let db: DatabaseQueue
    let llmStore: LLMConfigurationStore

    private(set) var isTicking = false
    private(set) var lastTickSummary = ""

    /// 距上次滴答不足该间隔则不推演（省 token）
    static let minTickInterval: TimeInterval = 2 * 3600
    /// 快进窗口上限：超出部分压缩成一条背景事件
    static let maxFastForwardDays = 7

    private static let lastTickKey = "world.lastTickAt"

    init(db: DatabaseQueue, llmStore: LLMConfigurationStore) {
        self.db = db
        self.llmStore = llmStore
    }

    // MARK: - 滴答入口

    func tickIfNeeded() async {
        guard !isTicking else { return }
        let now = Date()
        let lastTick = lastTickAt() ?? ignitionAt() ?? now
        let elapsed = now.timeIntervalSince(lastTick)
        guard elapsed >= Self.minTickInterval else { return }
        guard let client = llmStore.client(for: .utility) ?? llmStore.client(for: .main) else { return }

        isTicking = true
        defer { isTicking = false }

        do {
            let result = try await runGodView(client: client, from: lastTick, to: now)
            try await persist(result: result, windowStart: lastTick, now: now)
            saveLastTickAt(now)
        } catch {
            // 滴答失败不阻塞聊天：世界原地等待下一次机会
            lastTickSummary = ""
        }
    }

    // MARK: - GodView 推演

    struct TickEventPayload: Codable, Sendable {
        var type: String          // routine / relationship / external / milestone
        var summary: String
        var detail: String
        var dayOffset: Int        // 0 = 今天，-1 = 昨天……
        var participants: [String]
        var knowers: [String]
    }
    struct StatePayload: Codable, Sendable {
        var mood: String
        var currentActivity: String
        var currentConcern: String
    }
    struct LoopUpdatePayload: Codable, Sendable {
        var loopId: Int
        var action: String        // advance / resolve / drop
        var note: String
    }
    struct TickResult: Codable, Sendable {
        var events: [TickEventPayload]
        var protagonistState: StatePayload
        var loopUpdates: [LoopUpdatePayload]
        var newLoops: [String]
    }

    private func runGodView(client: LLMClient, from start: Date, to end: Date) async throws -> TickResult {
        let context = try await db.read { db in
            (
                protagonist: try Protagonist.fetchOne(db, key: Protagonist.singletonID),
                state: try ProtagonistState.fetchOne(db, key: ProtagonistState.singletonID),
                npcs: try NPC.fetchAll(db),
                relations: try Relation.fetchAll(db),
                loops: try OpenLoop
                    .filter(Column("status") == OpenLoop.Status.open.rawValue)
                    .fetchAll(db),
                recentEvents: try WorldEvent
                    .order(Column("occurredAt").desc).limit(10).fetchAll(db).reversed()
            )
        }
        guard let p = context.protagonist else { throw LLMError.decodingFailed("主角人设不存在") }

        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "M月d日 EEEE HH:mm"
        let hours = Int(end.timeIntervalSince(start) / 3600)
        let windowDescription = hours < 36
            ? "过去 \(hours) 小时（\(formatter.string(from: start)) 至 \(formatter.string(from: end))）"
            : "过去 \(min(hours / 24, Self.maxFastForwardDays)) 天（\(formatter.string(from: start)) 至 \(formatter.string(from: end))）"

        var npcLines: [String] = []
        for npc in context.npcs {
            let relation = context.relations.first {
                $0.fromID == npc.id && $0.toID == Relation.protagonistNodeID
            }
            var line = "- \(npc.name)（\(npc.relationToProtagonist)）：\(npc.personalitySketch)"
            if !npc.secret.isEmpty { line += "【秘密：\(npc.secret)】" }
            if let relation, !relation.recentChange.isEmpty { line += " 近期：\(relation.recentChange)" }
            npcLines.append(line)
        }

        let loopLines = context.loops.map {
            "[id=\($0.id ?? 0)] \($0.content)"
        }.joined(separator: "\n")

        let eventLines = context.recentEvents.map { "- \($0.summary)" }.joined(separator: "\n")

        let system = """
        你是「上帝视角」，掌管 \(p.name) 的生活世界。\(p.gender)，\(p.occupation)。
        性格：\(p.temperament.map(\.choice).joined(separator: "、"))
        \(p.backstory.isEmpty ? "" : "过去：\(p.backstory)")
        作息（工作日）：\(p.schedule.weekday.joined(separator: "；"))
        作息（周末）：\(p.schedule.weekend.joined(separator: "；"))

        身边的人：
        \(npcLines.isEmpty ? "（无）" : npcLines.joined(separator: "\n"))

        近期已发生的事：
        \(eventLines.isEmpty ? "（世界刚点火）" : eventLines)

        悬而未决的线索：
        \(loopLines.isEmpty ? "（无）" : loopLines)

        你的任务：快进推演【\(windowDescription)】里这个世界发生的事。规则：
        1. 事件要符合作息与身份，符合性格；日常为主，偶尔有小波澜，不要天天高潮
        2. 窗口在 36 小时内给 2~4 个事件；超过则每天 1~2 个、总量不超过 10 个
        3. 线索要自然推进：该了结的了结（resolve），该发展的发展（advance，note 写推进后的新状态），失去意义的丢弃（drop）；也可以从事件中生长出新线索（newLoops）
        4. knowers 填谁知道这件事："protagonist" 或 NPC 名字。主角不知道的事（比如别人的密谋）不要放进主角视野，但也不要编造主角不可能知道的事
        5. participants 和 knowers 里主角一律写 "protagonist"，NPC 写名字
        6. dayOffset：今天 0，昨天 -1，以此类推
        7. 最后给出主角此刻的状态：心情、正在做的事（结合当前时刻 \(formatter.string(from: end)) 与作息）、正在烦的事
        8. summary 控制在 25 字以内，从主角视角写（第一人称感受优先）
        """

        return try await client.structured(
            [.system(system), .user("开始推演。")],
            name: "world_tick",
            schema: JSONSchemaObject(object: [
                "type": "object",
                "properties": [
                    "events": [
                        "type": "array",
                        "items": [
                            "type": "object",
                            "properties": [
                                "type": ["type": "string"],
                                "summary": ["type": "string"],
                                "detail": ["type": "string"],
                                "dayOffset": ["type": "integer"],
                                "participants": ["type": "array", "items": ["type": "string"]],
                                "knowers": ["type": "array", "items": ["type": "string"]],
                            ],
                            "required": ["type", "summary", "detail", "dayOffset", "participants", "knowers"],
                            "additionalProperties": false,
                        ],
                    ],
                    "protagonistState": [
                        "type": "object",
                        "properties": [
                            "mood": ["type": "string"],
                            "currentActivity": ["type": "string"],
                            "currentConcern": ["type": "string"],
                        ],
                        "required": ["mood", "currentActivity", "currentConcern"],
                        "additionalProperties": false,
                    ],
                    "loopUpdates": [
                        "type": "array",
                        "items": [
                            "type": "object",
                            "properties": [
                                "loopId": ["type": "integer"],
                                "action": ["type": "string"],
                                "note": ["type": "string"],
                            ],
                            "required": ["loopId", "action", "note"],
                            "additionalProperties": false,
                        ],
                    ],
                    "newLoops": ["type": "array", "items": ["type": "string"]],
                ],
                "required": ["events", "protagonistState", "loopUpdates", "newLoops"],
                "additionalProperties": false,
            ]),
            as: TickResult.self)
    }

    // MARK: - 落库

    private func persist(result: TickResult, windowStart: Date, now: Date) async throws {
        let npcIDByName = try await db.read { db in
            Dictionary(uniqueKeysWithValues: try NPC.fetchAll(db).map { ($0.name, $0.id) })
        }
        try await db.write { db in
            for payload in result.events.prefix(12) {
                let occurredAt = Calendar.current.date(
                    byAdding: .day, value: payload.dayOffset, to: now) ?? now
                let resolveIDs = { (tokens: [String]) in
                    tokens.compactMap { $0 == "protagonist" ? Relation.protagonistNodeID : npcIDByName[$0] }
                }
                var event = WorldEvent(
                    id: nil,
                    occurredAt: min(occurredAt, now),
                    type: WorldEventType(rawValue: payload.type) ?? .routine,
                    summary: payload.summary,
                    detail: payload.detail,
                    participants: resolveIDs(payload.participants),
                    knowers: resolveIDs(payload.knowers),
                    consequences: [],
                    createdAt: now)
                // 主角必须在知情人集合里，否则这条事件对对话无意义（GodView 私藏留待 v2）
                if !event.knowers.contains(Relation.protagonistNodeID) {
                    event.knowers.append(Relation.protagonistNodeID)
                }
                try event.insert(db)
            }

            if var state = try ProtagonistState.fetchOne(db, key: ProtagonistState.singletonID) {
                state.mood = result.protagonistState.mood
                state.currentActivity = result.protagonistState.currentActivity
                state.currentConcern = result.protagonistState.currentConcern
                state.updatedAt = now
                try state.update(db)
            }

            for update in result.loopUpdates {
                guard var loop = try OpenLoop.fetchOne(db, key: Int64(update.loopId)) else { continue }
                switch update.action {
                case "resolve":
                    loop.status = .resolved
                    loop.resolvedAt = now
                case "drop":
                    loop.status = .dropped
                    loop.resolvedAt = now
                case "advance":
                    if !update.note.isEmpty { loop.content = update.note }
                default:
                    continue
                }
                try loop.update(db)
            }

            for content in result.newLoops.prefix(3) {
                var loop = OpenLoop(
                    id: nil, content: content,
                    participants: [Relation.protagonistNodeID],
                    expectedAdvanceAt: nil, status: .open,
                    sourceEventID: nil, createdAt: now, resolvedAt: nil)
                try loop.insert(db)
            }
        }

        lastTickSummary = result.events.last?.summary ?? ""
    }

    // MARK: - 滴答时间记录

    private func lastTickAt() -> Date? {
        (try? db.read { try Setting.fetchOne($0, key: Self.lastTickKey)?.value })
            .flatMap { ISO8601DateFormatter().date(from: $0) }
    }

    private func saveLastTickAt(_ date: Date) {
        var setting = Setting(key: Self.lastTickKey, value: ISO8601DateFormatter().string(from: date))
        try? db.write { try setting.save($0) }
    }

    /// 没有滴答记录时，以世界点火事件为起点
    private func ignitionAt() -> Date? {
        try? db.read { db in
            try WorldEvent.order(Column("occurredAt")).fetchOne(db)?.occurredAt
        }
    }
}
