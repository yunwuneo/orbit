import SwiftUI

struct RootView: View {
    let database: AppDatabase
    let llmStore: LLMConfigurationStore

    @State private var showSettings = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 16) {
                Image(systemName: "moon.stars.circle.fill")
                    .font(.system(size: 64))
                    .foregroundStyle(.secondary)
                Text("世界尚未点火")
                    .font(.title2)
                Text("完成人设向导后，TA 的世界将开始运转。")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            .navigationTitle("Orbit")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showSettings = true
                    } label: {
                        Image(systemName: "gearshape")
                    }
                }
            }
            .sheet(isPresented: $showSettings) {
                NavigationStack {
                    SettingsView(store: llmStore)
                        .toolbar {
                            ToolbarItem(placement: .topBarLeading) {
                                Button("完成") { showSettings = false }
                            }
                        }
                }
            }
        }
    }
}

#Preview {
    RootView(database: try! .inMemory(), llmStore: LLMConfigurationStore(db: try! AppDatabase.inMemory().db))
}
