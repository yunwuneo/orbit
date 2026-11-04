import SwiftUI

struct ChatView: View {
    @State var engine: ChatEngine
    let worldEngine: WorldEngine
    let protagonistName: String

    @State private var input = ""
    @FocusState private var inputFocused: Bool

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
                .frame(maxWidth: .infinity)
                .padding(.vertical, 5)
                .background(.bar)
            }

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 10) {
                        ForEach(engine.messages) { message in
                            MessageBubble(message: message)
                                .id(message.id)
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

            HStack(spacing: 10) {
                TextField("发消息…", text: $input, axis: .vertical)
                    .lineLimit(1...4)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 9)
                    .background(Color(.secondarySystemBackground), in: .capsule)
                    .focused($inputFocused)
                Button {
                    let text = input
                    input = ""
                    Task { await engine.send(text) }
                } label: {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.system(size: 32))
                        .foregroundStyle(input.trimmingCharacters(in: .whitespaces).isEmpty || engine.isReplying
                                         ? Color.secondary : Color.accentColor)
                }
                .disabled(input.trimmingCharacters(in: .whitespaces).isEmpty || engine.isReplying)
            }
            .padding(.horizontal)
            .padding(.vertical, 8)
        }
        .navigationTitle(protagonistName)
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { engine.loadHistory() }
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
                .background(
                    isUser ? Color.accentColor.opacity(0.85) : Color(.secondarySystemBackground),
                    in: .rect(cornerRadius: 18))
                .foregroundStyle(isUser ? .white : .primary)
                .textSelection(.enabled)
            if !isUser { Spacer(minLength: 48) }
        }
    }
}
