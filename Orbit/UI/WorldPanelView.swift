import SwiftUI
import GRDB

/// 世界面板（只读）：把 worldEvent 这条唯一事实源摊给用户看——
/// 主角现在怎样、世界欠着哪些线头、NPC 们如何、最近发生了什么。
struct WorldPanelView: View {
    let db: DatabaseQueue

    @State private var protagonistState: ProtagonistState?
    @State private var loops: [OpenLoop] = []
    @State private var npcs: [NPC] = []
    @State private var warmthByNPCID: [String: Int] = [:]
    @State private var events: [WorldEvent] = []
    @State private var expandedEventIDs: Set<Int64> = []

    var body: some View {
        List {
            protagonistSection
            loopsSection
            npcsSection
            eventsSection
        }
        .navigationTitle("世界")
        .onAppear { load() }
        .refreshable { load() }
    }

    // MARK: - Sections

    private var protagonistSection: some View {
        Section("主角状态") {
            if let protagonistState {
                LabeledContent("心情", value: protagonistState.mood)
                LabeledContent("精力", value: "\(protagonistState.energy)")
                LabeledContent("正在做", value: protagonistState.currentActivity)
                LabeledContent("正在烦", value: protagonistState.currentConcern)
            } else {
                Text("世界还没有开始流转")
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var loopsSection: some View {
        Section("未结线索") {
            if loops.isEmpty {
                Text("暂时没有悬着的事")
                    .foregroundStyle(.secondary)
            }
            ForEach(loops) { loop in
                VStack(alignment: .leading, spacing: 2) {
                    Text(loop.content)
                    if let expected = loop.expectedAdvanceAt {
                        Text("预计 \(expected, format: .dateTime.month(.twoDigits).day(.twoDigits).hour().minute()) 推进")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    private var npcsSection: some View {
        Section("身边的人") {
            ForEach(npcs) { npc in
                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Text(npc.name)
                            .font(.body.weight(.medium))
                        Text(npc.relationToProtagonist)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Spacer()
                        if let warmth = warmthByNPCID[npc.id] {
                            Text("温度 \(warmth)")
                                .font(.caption.monospacedDigit())
                                .foregroundStyle(.secondary)
                        }
                    }
                    Text(npc.personalitySketch)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    if !npc.mood.isEmpty || !npc.currentConcern.isEmpty {
                        Text([npc.mood, npc.currentConcern]
                                .filter { !$0.isEmpty }
                                .joined(separator: " · "))
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
        }
    }

    private var eventsSection: some View {
        Section("事件时间线") {
            if events.isEmpty {
                Text("还没有发生任何事件")
                    .foregroundStyle(.secondary)
            }
            ForEach(events) { event in
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 6) {
                        Text(event.type.label)
                            .font(.caption2.weight(.semibold))
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(event.type.color.opacity(0.15), in: .capsule)
                            .foregroundStyle(event.type.color)
                        Text(event.occurredAt, format: .dateTime.month(.twoDigits).day(.twoDigits).hour().minute())
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Text(event.summary)
                    if let id = event.id, expandedEventIDs.contains(id) {
                        Text(event.detail)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                }
                .contentShape(Rectangle())
                .onTapGesture { toggleExpanded(event) }
            }
        }
    }

    // MARK: - 数据

    private func toggleExpanded(_ event: WorldEvent) {
        guard let id = event.id else { return }
        withAnimation(.easeInOut(duration: 0.15)) {
            if expandedEventIDs.contains(id) {
                expandedEventIDs.remove(id)
            } else {
                expandedEventIDs.insert(id)
            }
        }
    }

    private func load() {
        protagonistState = try? db.read {
            try ProtagonistState.fetchOne($0, key: ProtagonistState.singletonID)
        }
        loops = (try? db.read {
            try OpenLoop
                .filter(Column("status") == OpenLoop.Status.open.rawValue)
                .order(Column("createdAt").desc)
                .fetchAll($0)
        }) ?? []
        npcs = (try? db.read {
            try NPC.order(Column("createdAt")).fetchAll($0)
        }) ?? []
        let relations = (try? db.read { try Relation.fetchAll($0) }) ?? []
        warmthByNPCID = Dictionary(uniqueKeysWithValues: relations.compactMap { relation in
            if relation.fromID == Relation.protagonistNodeID { return (relation.toID, relation.warmth) }
            if relation.toID == Relation.protagonistNodeID { return (relation.fromID, relation.warmth) }
            return nil
        })
        events = (try? db.read {
            try WorldEvent
                .order(Column("occurredAt").desc)
                .limit(100)
                .fetchAll($0)
        }) ?? []
    }
}

private extension WorldEventType {
    var label: String {
        switch self {
        case .routine: "日常"
        case .relationship: "关系"
        case .external: "外部"
        case .milestone: "里程碑"
        }
    }

    var color: Color {
        switch self {
        case .routine: .secondary
        case .relationship: .pink
        case .external: .blue
        case .milestone: .orange
        }
    }
}

#Preview {
    let db = try! AppDatabase.inMemory()
    return NavigationStack {
        WorldPanelView(db: db.db)
    }
}
