import SwiftUI

@main
struct OrbitApp: App {
    let database: AppDatabase
    let llmStore: LLMConfigurationStore

    init() {
        do {
            let db = try AppDatabase()
            database = db
            llmStore = LLMConfigurationStore(db: db.db)
        } catch {
            fatalError("数据库初始化失败: \(error)")
        }
    }

    var body: some Scene {
        WindowGroup {
            RootView(database: database, llmStore: llmStore)
        }
    }
}
