import SwiftUI
import GRDB

struct RootView: View {
    let database: AppDatabase
    let llmStore: LLMConfigurationStore

    @State private var showSettings = false
    @State private var protagonist: Protagonist?

    var body: some View {
        NavigationStack {
            Group {
                if let protagonist {
                    ChatView(
                        engine: ChatEngine(db: database.db, llmStore: llmStore),
                        protagonistName: protagonist.name)
                } else {
                    WizardView(state: WizardState(db: database.db, llmStore: llmStore)) {
                        loadProtagonist()
                    }
                }
            }
            .navigationTitle(protagonist?.name ?? "Orbit")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showSettings = true } label: {
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
            .onAppear(perform: loadProtagonist)
        }
    }

    private func loadProtagonist() {
        protagonist = try? database.db.read { db in
            try Protagonist.fetchOne(db, key: Protagonist.singletonID)
        }
    }
}

#Preview {
    let db = try! AppDatabase.inMemory()
    return RootView(database: db, llmStore: LLMConfigurationStore(db: db.db))
}
