import SwiftUI

/// BYOK 设置页：双层端点配置。Key 只进 Keychain，不落数据库。
struct SettingsView: View {
    let store: LLMConfigurationStore

    var body: some View {
        Form {
            ForEach(LLMTier.allCases, id: \.self) { tier in
                TierSection(store: store, tier: tier)
            }
            Section {
                Label("API Key 仅保存在本机钥匙串，所有数据不出设备。", systemImage: "lock.shield")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
        .navigationTitle("模型设置")
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
