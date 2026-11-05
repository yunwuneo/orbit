import SwiftUI

struct ChatView: View {
    @State var engine: ChatEngine
    let worldEngine: WorldEngine
    let protagonistName: String

    @State private var input = ""
    @FocusState private var inputFocused: Bool

    private var canSend: Bool {
        !input.trimmingCharacters(in: .whitespaces).isEmpty && !engine.isReplying
    }

    var body: some View {
        VStack(spacing: 0) {
            if worldEngine.isTicking {
                HStack(spacing: 6) {
                    ProgressView()
                        .controlSize(.mini)
                    Text("世界流转中…")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .glassEffect(.regular, in: .capsule)
                .padding(.top, 4)
            }

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 10) {
                        ForEach(engine.messages) { message in
                            MessageBubble(message: message)
                                .id(message.id)
                                .transition(.opacity.combined(with: .scale(scale: 0.96, anchor: .bottom)))
                        }
                        if engine.isReplying {
                            MessageBubble(
                                message: ChatMessage(
                                    id: -1, role: .protagonist,
                                    content: engine.streamingText.isEmpty ? "…" : engine.streamingText,
                                    sentAt: Date(), relatedEventIDs: []))
                            .id("streaming")
                        }
                    }
                    .padding()
                    .animation(.spring(duration: 0.25), value: engine.messages.count)
                }
                .onChange(of: engine.messages.count) { scrollToBottom(proxy) }
                .onChange(of: engine.streamingText) { scrollToBottom(proxy) }
            }

            if let error = engine.lastError {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .padding(.horizontal)
                    .padding(.top, 4)
            }

            GlassEffectContainer(spacing: 10) {
                HStack(spacing: 10) {
                    TextField("发消息…", text: $input, axis: .vertical)
                        .lineLimit(1...4)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 9)
                        .glassEffect(.regular, in: .capsule)
                        .focused($inputFocused)
                    Button {
                        let text = input
                        input = ""
                        Task { await engine.send(text) }
                    } label: {
                        Image(systemName: "arrow.up")
                            .font(.body.weight(.semibold))
                    }
                    .buttonStyle(.glassProminent)
                    .disabled(!canSend)
                }
            }
            .padding(.horizontal)
            .padding(.vertical, 8)
        }
        .background(chatBackground)
        .navigationTitle(protagonistName)
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { engine.loadHistory() }
        // 主角的主动消息带未来时间戳：App 开着时每分钟检查一次是否到点
        .onReceive(Timer.publish(every: 60, on: .main, in: .common).autoconnect()) { _ in
            engine.loadHistory()
        }
    }

    /// 淡淡的渐变底，让玻璃气泡有内容可折射
    private var chatBackground: some View {
        LinearGradient(
            colors: [
                Color.accentColor.opacity(0.10),
                Color(.systemBackground),
                Color.purple.opacity(0.08),
            ],
            startPoint: .top,
            endPoint: .bottom)
            .ignoresSafeArea()
    }

    private func scrollToBottom(_ proxy: ScrollViewProxy) {
        withAnimation(.easeOut(duration: 0.15)) {
            if engine.isReplying {
                proxy.scrollTo("streaming", anchor: .bottom)
            } else if let last = engine.messages.last {
                proxy.scrollTo(last.id, anchor: .bottom)
            }
        }
    }
}

private struct MessageBubble: View {
    let message: ChatMessage

    private var isUser: Bool { message.role == .user }

    var body: some View {
        HStack {
            if isUser { Spacer(minLength: 48) }
            Text(message.content)
                .font(.body)
                .padding(.horizontal, 13)
                .padding(.vertical, 9)
                .glassEffect(
                    isUser
                        ? .regular.tint(Color.accentColor).interactive()
                        : .regular.interactive(),
                    in: .rect(cornerRadius: 18))
                .foregroundStyle(isUser ? .white : .primary)
                .textSelection(.enabled)
            if !isUser { Spacer(minLength: 48) }
        }
    }
}
