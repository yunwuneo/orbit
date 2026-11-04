import SwiftUI

/// 人设向导容器：七章 + 收尾人设卡。
/// 原则：不是 AI 一次生成，也不是用户一次全写完——每一步都是一次选择。
struct WizardView: View {
    @State var state: WizardState
    var onFinished: () -> Void

    @State private var chapter = 0
    @State private var showError = false
    @State private var errorMessage = ""
    @State private var isSaving = false

    private static let chapters: [(title: String, subtitle: String)] = [
        ("锚点", "TA 是谁"),
        ("气质", "遇到事情，TA 会怎么做"),
        ("声音", "TA 说话的样子"),
        ("内核", "TA 在意什么、怕什么"),
        ("生活", "TA 的日子怎么过"),
        ("世界", "TA 身边有谁"),
        ("你们", "TA 和你的关系"),
        ("点火", "确认 TA 的样子"),
    ]

    var body: some View {
        VStack(spacing: 0) {
            progressBar
            ScrollView {
                chapterContent
                    .padding()
                    .animation(.default, value: chapter)
            }
            navigationBar
        }
        .navigationTitle("认识 TA")
        .navigationBarTitleDisplayMode(.inline)
        .alert("出错了", isPresented: $showError) {
            Button("好", role: .cancel) {}
        } message: {
            Text(errorMessage)
        }
    }

    private var progressBar: some View {
        HStack(spacing: 4) {
            ForEach(0..<Self.chapters.count, id: \.self) { i in
                Capsule()
                    .fill(i <= chapter ? Color.accentColor : Color.secondary.opacity(0.25))
                    .frame(height: 4)
            }
        }
        .padding(.horizontal)
        .padding(.top, 8)
    }

    @ViewBuilder private var chapterContent: some View {
        let meta = Self.chapters[chapter]
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 4) {
                Text("第\(chapter + 1)章 · \(meta.title)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(meta.subtitle)
                    .font(.title2.bold())
            }
            switch chapter {
            case 0: AnchorChapter(state: state)
            case 1: TemperamentChapter(state: state)
            case 2: VoiceChapter(state: state, onError: showAIError)
            case 3: CoreChapter(state: state)
            case 4: LifeChapter(state: state)
            case 5: WorldChapter(state: state, onError: showAIError)
            case 6: UsChapter(state: state)
            default: ReviewChapter(state: state)
            }
        }
    }

    private var navigationBar: some View {
        HStack {
            if chapter > 0 {
                Button("上一章") { withAnimation { chapter -= 1 } }
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if chapter < Self.chapters.count - 1 {
                Button("下一章") { withAnimation { chapter += 1 } }
                    .buttonStyle(.borderedProminent)
                    .disabled(chapter == 0 && !state.canFinish)
            } else {
                Button(isSaving ? "点火中…" : "世界点火") { finish() }
                    .buttonStyle(.borderedProminent)
                    .disabled(!state.canFinish || isSaving)
            }
        }
        .padding()
    }

    private func showAIError(_ error: Error) {
        errorMessage = error.localizedDescription
        showError = true
    }

    private func finish() {
        isSaving = true
        do {
            try state.save()
            onFinished()
        } catch {
            isSaving = false
            errorMessage = "保存失败：\(error.localizedDescription)"
            showError = true
        }
    }
}

// MARK: - 共用组件

struct WizardField: View {
    let title: String
    var hint: String? = nil
    @Binding var text: String
    var multiline = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.subheadline.weight(.medium))
            if let hint {
                Text(hint).font(.caption).foregroundStyle(.secondary)
            }
            if multiline {
                TextEditor(text: $text)
                    .frame(minHeight: 72)
                    .padding(8)
                    .background(Color(.secondarySystemBackground), in: .rect(cornerRadius: 10))
            } else {
                TextField("", text: $text)
                    .padding(10)
                    .background(Color(.secondarySystemBackground), in: .rect(cornerRadius: 10))
            }
        }
    }
}

struct ChipRow: View {
    let options: [String]
    @Binding var selection: String

    var body: some View {
        FlowLayout(spacing: 8) {
            ForEach(options, id: \.self) { option in
                Button {
                    selection = (selection == option) ? "" : option
                } label: {
                    Text(option)
                        .font(.subheadline)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 7)
                        .background(
                            selection == option ? Color.accentColor.opacity(0.2) : Color(.secondarySystemBackground),
                            in: .capsule)
                }
                .buttonStyle(.plain)
            }
        }
    }
}

/// 简单的流式布局（chips 换行）
struct FlowLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? 0
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0, maxX: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x + size.width > width, x > 0 {
                y += rowHeight + spacing
                x = 0
                rowHeight = 0
            }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
            maxX = max(maxX, x)
        }
        return CGSize(width: maxX, height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x + size.width > bounds.maxX, x > bounds.minX {
                y += rowHeight + spacing
                x = bounds.minX
                rowHeight = 0
            }
            subview.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}
