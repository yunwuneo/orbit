import SwiftUI

@main
struct OrbitApp: App {
    let database: AppDatabase

    init() {
        do {
            database = try AppDatabase()
        } catch {
            fatalError("数据库初始化失败: \(error)")
        }
    }

    var body: some Scene {
        WindowGroup {
            RootView()
        }
    }
}
