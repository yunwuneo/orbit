import SwiftUI
import GRDB

struct RootView: View {
    let database: AppDatabase
    let llmStore: LLMConfigurationStore

    @Environment(\.scenePhase) private var scenePhase
    @State private var worldEngine: WorldEngine
    @State private var showSettings = false
    @State private var protagonist: Protagonist?

    init(database: AppDatabase, llmStore: LLMConfigurationStore) {
        self.database = database
        self.llmStore = llmStore
        _worldEngine = State(initialValue: WorldEngine(db: database.db, llmStore: llmStore))
    }

    var body: some View {
        NavigationStack {
            Group {
                if let protagonist {
                    ChatView(
                        engine: ChatEngine(db: database.db, llmStore: llmStore),
                        worldEngine: worldEngine,
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
            .task { await worldEngine.tickIfNeeded() }
            .onChange(of: scenePhase) { _, phase in
                switch phase {
                case .active:
                    Task {
                        await ProactiveNotifier.cancelPending()
                        await worldEngine.tickIfNeeded()
                    }
                case .background:
                    Task {
                        await ProactiveNotifier.schedulePendingMessages(
                            db: database.db, protagonistName: protagonist?.name ?? "Orbit")
                    }
                default:
                    break
                }
            }
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
