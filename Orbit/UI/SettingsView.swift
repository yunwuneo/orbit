import SwiftUI
import GRDB

/// BYOK 设置页：双层端点配置。Key 只进 Keychain，不落数据库。
struct SettingsView: View {
    let store: LLMConfigurationStore

    var body: some View {
        Form {
            WorldSection(db: store.db)
            UsageSection(db: store.db)
            ForEach(LLMTier.allCases, id: \.self) { tier in
                TierSection(store: store, tier: tier)
            }
            Section {
                Label("API Key 仅保存在本机钥匙串，所有数据不出设备。", systemImage: "lock.shield")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
        .navigationTitle("设置")
    }
}

/// 用量仪表盘：按模型层统计今天与累计的 token
private struct UsageSection: View {
    let db: DatabaseQueue

    @State private var todayByTier: [String: Int] = [:]
    @State private var totalByTier: [String: Int] = [:]

    var body: some View {
        Section {
            Text("今天合计：\(formatted(todayByTier.values.reduce(0, +))) tokens")
                .font(.subheadline)
            ForEach(LLMTier.allCases, id: \.self) { tier in
                HStack {
                    Text(tier.displayName)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Text("\(formatted(todayByTier[tier.rawValue] ?? 0)) 今日")
                    Text("/")
                        .foregroundStyle(.secondary)
                    Text("\(formatted(totalByTier[tier.rawValue] ?? 0)) 累计")
                }
                .font(.subheadline.monospacedDigit())
            }
            Button("清空记录") {
                try? db.write { db in try UsageRecord.deleteAll(db) }
                load()
            }
            .foregroundStyle(.red)
        } header: {
            Text("用量")
        } footer: {
            Text("BYOK 下费用由你的模型账号承担，这里只记录各层模型的 token 消耗。")
        }
        .onAppear { load() }
    }

    private func formatted(_ value: Int) -> String {
        value.formatted(.number)
    }

    private func load() {
        let now = Date()
        let startOfToday = Calendar.current.startOfDay(for: now)
        let sums = try? db.read { db in
            (
                today: try UsageRecord
                    .filter(Column("createdAt") >= startOfToday)
                    .fetchAll(db),
                all: try UsageRecord.fetchAll(db)
            )
        }
        guard let sums else { return }
        todayByTier = Dictionary(grouping: sums.today, by: \.tier)
            .mapValues { $0.reduce(0) { $0 + $1.promptTokens + $1.completionTokens } }
        totalByTier = Dictionary(grouping: sums.all, by: \.tier)
            .mapValues { $0.reduce(0) { $0 + $1.promptTokens + $1.completionTokens } }
    }
}

/// 世界策略：暂停世界 + 缺席反应规则（用户定规则，人设定表达）
private struct WorldSection: View {
    let db: DatabaseQueue

    @State private var paused = false
    @State private var absenceInstruction = ""

    var body: some View {
        Section {
            Toggle("暂停世界", isOn: $paused)
            TextField("缺席时 TA 的反应规则", text: $absenceInstruction, axis: .vertical)
                .lineLimit(2...5)
        } header: {
            Text("世界")
        } footer: {
            Text("暂停后世界冻结，恢复时从当下继续，跳过的日子不补。缺席规则示例：「她会担心，但嘴上说没事」——你定规则，人设决定怎么表达。")
        }
        .onAppear { load() }
        .onChange(of: paused) { _, newValue in savePaused(newValue) }
        .onChange(of: absenceInstruction) { _, newValue in saveAbsenceInstruction(newValue) }
    }

    private func load() {
        paused = (try? db.read {
            try Setting.fetchOne($0, key: WorldSettingKey.paused)?.value == "true"
        }) ?? false
        absenceInstruction = (try? db.read {
            try Setting.fetchOne($0, key: WorldSettingKey.absenceInstruction)?.value ?? ""
        }) ?? ""
    }

    private func savePaused(_ newValue: Bool) {
        try? db.write { db in
            var setting = Setting(key: WorldSettingKey.paused, value: newValue ? "true" : "false")
            try setting.save(db)
            // 解冻时从当下重新计时：暂停期间的日子不快进补演
            if !newValue {
                var tick = Setting(
                    key: WorldEngine.lastTickKey,
                    value: ISO8601DateFormatter().string(from: Date()))
                try tick.save(db)
            }
        }
    }

    private func saveAbsenceInstruction(_ text: String) {
        try? db.write { db in
            var setting = Setting(key: WorldSettingKey.absenceInstruction, value: text)
            try setting.save(db)
        }
    }
}

private struct TierSection: View {
    let store: LLMConfigurationStore
    let tier: LLMTier

    @State private var baseURL = ""
    @State private var model = ""
    @State private var apiKey = ""
    @State private var saved = false

    var body: some View {
        Section {
            TextField("端点地址（如 https://api.openai.com/v1）", text: $baseURL)
                .textContentType(.URL)
                .keyboardType(.URL)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
            TextField("模型名（如 gpt-4o / deepseek-chat）", text: $model)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
            SecureField("API Key", text: $apiKey)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)

            HStack {
                statusView
                Spacer()
                Button(saved ? "已保存" : "保存") { save() }
                    .disabled(saved)
            }
        } header: {
            Text(tier.displayName)
        } footer: {
            if tier == .utility {
                Text("世界引擎的滴答、摘要等后台任务使用此模型，建议选便宜快速的。")
            }
        }
        .onAppear { load() }
    }

    @ViewBuilder private var statusView: some View {
        if store.isReady(tier) {
            Label("就绪", systemImage: "checkmark.circle.fill")
                .foregroundStyle(.green)
        } else {
            Label("未配置", systemImage: "exclamationmark.circle")
                .foregroundStyle(.orange)
        }
    }

    private func load() {
        let config = store.endpoint(for: tier)
        baseURL = config.baseURL
        model = config.model
        apiKey = store.apiKey(for: tier)
    }

    private func save() {
        let config = LLMEndpointConfig(
            baseURL: baseURL.trimmingCharacters(in: .whitespaces),
            model: model.trimmingCharacters(in: .whitespaces))
        try? store.saveEndpoint(config, for: tier)
        try? store.saveAPIKey(apiKey.trimmingCharacters(in: .whitespaces), for: tier)
        saved = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { saved = false }
    }
}
