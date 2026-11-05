import SwiftUI

/// 向导第 0 步：模型配置。BYOK 架构下模型是必要前置，
/// 不配好主力模型就无法开始人设共建。
struct ModelSetupView: View {
    let store: LLMConfigurationStore
    let onReady: () -> Void

    @State private var mainReady = false

    var body: some View {
        Form {
            Section {
                Text("Orbit 使用你自己的 API Key 直连模型服务，所有数据都只存在这台设备上。开始之前，先配置一个模型。")
                    .foregroundStyle(.secondary)
            }

            TierSection(store: store, tier: .main, onSaved: refresh)

            TierSection(store: store, tier: .utility, onSaved: refresh)

            Section {
                Button("开始共建") { onReady() }
                    .frame(maxWidth: .infinity)
                    .disabled(!mainReady)
            } footer: {
                Text("勤务模型可跳过：不配置时，世界引擎的后台任务会改用主力模型。注意端点需支持 OpenAI 兼容的 structured output（strict JSON Schema），否则向导的 AI 提议和世界推演会失败。")
            }
        }
        .navigationTitle("欢迎使用 Orbit")
        .onAppear { refresh() }
    }

    private func refresh() {
        mainReady = store.isReady(.main)
    }
}

#Preview {
    let db = try! AppDatabase.inMemory()
    return NavigationStack {
        ModelSetupView(store: LLMConfigurationStore(db: db.db)) {}
    }
}
