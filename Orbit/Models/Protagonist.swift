import Foundation
import GRDB

// MARK: - 人设 JSON 子结构

/// 气质层：情境选择题的回答（行为倾向，而非平面形容词）
struct TemperamentChoice: Codable, Equatable {
    var question: String
    var choice: String
}

/// 语言习惯：对聊天真实感的贡献远超性格描述
struct SpeechStyle: Codable, Equatable {
    var catchphrases: [String] = []
    var sentenceHabit: String = ""      // 长短句、标点习惯
    var emojiHabit: String = ""         // emoji/表情包频率与偏好
    var replyRhythm: String = ""        // 秒回/轮回、会不会已读乱回
}

/// 价值观与边界
struct ValuesProfile: Codable, Equatable {
    var intimacyView: String = ""       // 如何看待亲密关系
    var boundaries: [String] = []       // 雷区与底线
    var notes: String = ""              // 其他（金钱观等）
}

/// 缺陷与矛盾：完美人设三天就腻，矛盾感是"像人"的核心
struct FlawsProfile: Codable, Equatable {
    var flaws: [String] = []            // 讨厌自己的地方
    var contradictions: [String] = []   // 言行不一之处
}

/// 作息模板：对称时间下主角的生活骨架
struct ScheduleTemplate: Codable, Equatable {
    var weekday: [String] = []          // 工作日典型日程（自由格式条目）
    var weekend: [String] = []
}

// MARK: - 主角人设（静态，单例行）

struct Protagonist: Codable, FetchableRecord, MutablePersistableRecord, Identifiable {
    static let databaseTableName = "protagonist"
    static let singletonID: Int64 = 1

    var id: Int64
    var name: String
    var gender: String
    var age: Int?
    var appearance: String

    var temperament: [TemperamentChoice]     // JSON 列
    var speechStyle: SpeechStyle             // JSON 列
    var values: ValuesProfile                // JSON 列
    var flaws: FlawsProfile                  // JSON 列
    var backstory: String                    // 成长背景与关键经历
    var sampleDialogues: [String]            // JSON 列，典型对话片段
    var occupation: String                   // 身份：职员/学生/…
    var schedule: ScheduleTemplate           // JSON 列

    /// 主角与用户的关系起点（旧友/初识/青梅竹马…）
    var relationToUser: String
    var nicknameForUser: String

    var createdAt: Date
    var updatedAt: Date
}

// MARK: - 主角动态状态（被模拟持续改写，单例行）

struct ProtagonistState: Codable, FetchableRecord, MutablePersistableRecord, Identifiable {
    static let databaseTableName = "protagonistState"
    static let singletonID: Int64 = 1

    var id: Int64
    var mood: String                 // 当前心情
    var energy: Int                  // 精力 0...100
    var currentActivity: String      // 正在做的事
    var currentConcern: String       // 正在烦的事
    var updatedAt: Date
}

// MARK: - 主角的用户模型（单例行 + 事实表）

struct UserModel: Codable, FetchableRecord, MutablePersistableRecord, Identifiable {
    static let databaseTableName = "userModel"
    static let singletonID: Int64 = 1

    var id: Int64
    var impression: String           // 对用户的整体印象（持续演化）
    var relationshipStage: String    // 关系阶段
    var updatedAt: Date
}

/// 主角记住的关于用户的事实
struct UserFact: Codable, FetchableRecord, MutablePersistableRecord, Identifiable {
    static let databaseTableName = "userFact"

    var id: Int64?
    var fact: String
    var category: String             // 习惯/经历/偏好/关系事件…
    var createdAt: Date
    var updatedAt: Date

    mutating func didInsert(_ inserted: InsertionSuccess) {
        id = inserted.rowID
    }
}
