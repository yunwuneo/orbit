import SwiftUI

// MARK: - 第一章 锚点

struct AnchorChapter: View {
    @Bindable var state: WizardState

    var body: some View {
        WizardField(title: "名字", text: $state.name)
        VStack(alignment: .leading, spacing: 6) {
            Text("性别").font(.subheadline.weight(.medium))
            ChipRow(options: ["女", "男", "非二元"], selection: $state.gender)
            if !["", "女", "男", "非二元"].contains(state.gender) {
                TextField("自定义", text: $state.gender)
                    .padding(10)
                    .background(Color(.secondarySystemBackground), in: .rect(cornerRadius: 10))
            } else if state.gender.isEmpty {
                TextField("或自定义", text: $state.gender)
                    .padding(10)
                    .background(Color(.secondarySystemBackground), in: .rect(cornerRadius: 10))
            }
        }
        WizardField(title: "年龄", hint: "可以留空", text: $state.ageText)
        WizardField(title: "外貌", hint: "几个关键词即可，比如：齐肩短发、总穿 oversized 卫衣", text: $state.appearance)
    }
}

// MARK: - 第二章 气质

struct TemperamentChapter: View {
    @Bindable var state: WizardState

    var body: some View {
        Text("不贴标签，看行为。每个情境选一个最像 TA 的反应。")
            .font(.caption)
            .foregroundStyle(.secondary)
        ForEach(Array(WizardState.temperamentQuestions.enumerated()), id: \.offset) { index, item in
            VStack(alignment: .leading, spacing: 8) {
                Text(item.question).font(.subheadline.weight(.medium))
                ChipRow(options: item.options, selection: binding(for: index))
            }
        }
    }

    private func binding(for index: Int) -> Binding<String> {
        Binding(
            get: {
                let question = WizardState.temperamentQuestions[index].question
                return state.temperament.first { $0.question == question }?.choice ?? ""
            },
            set: { choice in
                let question = WizardState.temperamentQuestions[index].question
                state.temperament.removeAll { $0.question == question }
                if !choice.isEmpty {
                    state.temperament.append(TemperamentChoice(question: question, choice: choice))
                }
            })
    }
}

// MARK: - 第三章 声音

struct VoiceChapter: View {
    @Bindable var state: WizardState
    var onError: (Error) -> Void

    @State private var isGenerating = false
    @State private var proposals: [String] = []

    var body: some View {
        Text("语言习惯对「像 TA」的贡献，远超性格描述。")
            .font(.caption)
            .foregroundStyle(.secondary)
        WizardField(title: "口头禅", hint: "每行一个，比如：笑死 / 绝了 / 不是吧", text: $state.catchphrasesText, multiline: true)
        WizardField(title: "打字习惯", hint: "比如：短句连发、几乎不用标点、爱用括号补充", text: $state.sentenceHabit)
        WizardField(title: "emoji 习惯", hint: "比如：只用 😭😭、从不用 emoji、每条必带表情包", text: $state.emojiHabit)
        WizardField(title: "回复节奏", hint: "比如：秒回但短、轮回但很长、已读乱回", text: $state.replyRhythm)

        Divider()

        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("典型对话片段").font(.subheadline.weight(.medium))
                Spacer()
                Button(isGenerating ? "生成中…" : "让 AI 提议") { generate() }
                    .disabled(isGenerating)
            }
            Text("AI 会基于目前的人设写 3 段对话，挑最像的留下、可以改。这几段将强烈约束 TA 未来的说话风格。")
                .font(.caption)
                .foregroundStyle(.secondary)

            ForEach(proposals, id: \.self) { proposal in
                Button {
                    state.sampleDialogues.append(proposal)
                    proposals.removeAll { $0 == proposal }
                } label: {
                    VStack(alignment: .leading) {
                        Text(proposal).font(.caption)
                        Label("采纳这段", systemImage: "plus.circle")
                            .font(.caption.weight(.medium))
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10)
                    .background(Color.accentColor.opacity(0.08), in: .rect(cornerRadius: 10))
                }
                .buttonStyle(.plain)
            }

            ForEach($state.sampleDialogues, id: \.self) { $dialogue in
                VStack(alignment: .leading, spacing: 4) {
                    TextEditor(text: $dialogue)
                        .frame(minHeight: 100)
                        .padding(8)
                        .background(Color(.secondarySystemBackground), in: .rect(cornerRadius: 10))
                    Button("移除", role: .destructive) {
                        state.sampleDialogues.removeAll { $0 == dialogue }
                    }
                    .font(.caption)
                }
            }
        }
    }

    private func generate() {
        isGenerating = true
        Task {
            defer { isGenerating = false }
            do {
                proposals = try await state.generateSampleDialogues()
            } catch {
                onError(error)
            }
        }
    }
}

// MARK: - 第四章 内核

struct CoreChapter: View {
    @Bindable var state: WizardState

    var body: some View {
        Text("完美的人设三天就腻。缺陷和矛盾，才是「像人」的核心。")
            .font(.caption)
            .foregroundStyle(.secondary)
        WizardField(title: "TA 怎么看待亲密关系？", hint: "比如：嘴上说不需要，其实最怕被丢下", text: $state.intimacyView, multiline: true)
        WizardField(title: "雷区与底线", hint: "每行一个，比如：被已读不回 / 被拿来和别人比较", text: $state.boundariesText, multiline: true)
        WizardField(title: "TA 讨厌自己什么地方？", hint: "每行一个", text: $state.flawsText, multiline: true)
        WizardField(title: "TA 在哪件事上言行不一？", hint: "每行一个，比如：劝别人早睡，自己熬到三点", text: $state.contradictionsText, multiline: true)
        WizardField(title: "一件 TA 忘不掉的往事", hint: "不需要完整，一个画面就够", text: $state.backstory, multiline: true)
    }
}

// MARK: - 第五章 生活

struct LifeChapter: View {
    @Bindable var state: WizardState

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("身份").font(.subheadline.weight(.medium))
            ChipRow(options: ["公司职员", "大学生", "留学生", "自由职业", "备考中"], selection: $state.occupation)
            if state.occupation.isEmpty || !["公司职员", "大学生", "留学生", "自由职业", "备考中"].contains(state.occupation) {
                TextField("或自定义（可以写具体点，如：广告公司文案）", text: $state.occupation)
                    .padding(10)
                    .background(Color(.secondarySystemBackground), in: .rect(cornerRadius: 10))
            }
        }
        WizardField(title: "工作日的典型一天", hint: "每行一条，比如：9 点打卡 / 午休刷手机 / 晚上健身房", text: $state.scheduleWeekday, multiline: true)
        WizardField(title: "周末的典型一天", hint: "每行一条", text: $state.scheduleWeekend, multiline: true)
        WizardField(title: "最近在忙什么、烦什么？", hint: "这会成为 TA 世界里的第一条线索", text: $state.currentConcern, multiline: true)
    }
}

// MARK: - 第六章 世界

struct WorldChapter: View {
    @Bindable var state: WizardState
    var onError: (Error) -> Void

    @State private var generatingSlot: Int? = nil

    var body: some View {
        Text("真人不会只活在与你的对话里。给 TA 身边的人，世界才转得起来。每个配角 30 秒速写即可，细节交给世界去补完。")
            .font(.caption)
            .foregroundStyle(.secondary)

        ForEach(Array(state.npcs.indices), id: \.self) { index in
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(WizardState.npcSlots[index])
                        .font(.subheadline.weight(.medium))
                    Spacer()
                    Button(generatingSlot == index ? "提议中…" : "AI 提议") { propose(index) }
                        .font(.caption)
                        .disabled(generatingSlot != nil)
                }
                WizardField(title: "名字", text: $state.npcs[index].name)
                WizardField(title: "一句话性格", text: $state.npcs[index].personality)
                WizardField(title: "一个秘密或执念", hint: "TA 知道或不知道的都可以，这是戏剧性的燃料", text: $state.npcs[index].secret)
            }
            .padding()
            .background(Color(.secondarySystemBackground).opacity(0.5), in: .rect(cornerRadius: 12))
        }

        WizardField(title: "他们彼此的关系（可选）", hint: "一句话即可，比如：Celina 看不惯小 A", text: $state.npcRelationsNote)
    }

    private func propose(_ index: Int) {
        generatingSlot = index
        Task {
            defer { generatingSlot = nil }
            do {
                let proposal = try await state.proposeNPC(slot: WizardState.npcSlots[index])
                state.npcs[index].name = proposal.name
                state.npcs[index].personality = proposal.personality
                state.npcs[index].secret = proposal.secret
            } catch {
                onError(error)
            }
        }
    }
}

// MARK: - 第七章 你们

struct UsChapter: View {
    @Bindable var state: WizardState

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("TA 和你的关系起点").font(.subheadline.weight(.medium))
            ChipRow(options: ["初识", "旧友", "青梅竹马", "网友", "前任"], selection: $state.relationToUser)
            if state.relationToUser.isEmpty || !["初识", "旧友", "青梅竹马", "网友", "前任"].contains(state.relationToUser) {
                TextField("或自定义", text: $state.relationToUser)
                    .padding(10)
                    .background(Color(.secondarySystemBackground), in: .rect(cornerRadius: 10))
            }
        }
        WizardField(title: "TA 怎么称呼你？", hint: "比如：你的名字、一个外号、「喂」", text: $state.nicknameForUser)
    }
}

// MARK: - 收尾 人设卡

struct ReviewChapter: View {
    let state: WizardState

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("\(state.name)\(state.ageText.isEmpty ? "" : "，\(state.ageText) 岁")")
                .font(.title3.bold())
            if !state.gender.isEmpty || !state.occupation.isEmpty {
                Text([state.gender, state.occupation].filter { !$0.isEmpty }.joined(separator: " · "))
                    .foregroundStyle(.secondary)
            }
            if !state.appearance.isEmpty { row("外貌", state.appearance) }
            if !state.temperament.isEmpty {
                row("气质", state.temperament.map(\.choice).joined(separator: "、"))
            }
            if !state.catchphrasesText.isEmpty { row("口头禅", state.catchphrasesText) }
            if !state.intimacyView.isEmpty { row("亲密关系观", state.intimacyView) }
            if !state.flawsText.isEmpty { row("缺陷", state.flawsText) }
            if !state.backstory.isEmpty { row("往事", state.backstory) }
            let validNPCs = state.npcs.filter(\.isValid)
            if !validNPCs.isEmpty {
                row("身边的人", validNPCs.map { "\($0.name)（\($0.relation)）" }.joined(separator: "、"))
            }
            if !state.relationToUser.isEmpty {
                row("和你", "\(state.relationToUser)\(state.nicknameForUser.isEmpty ? "" : "，叫你「\(state.nicknameForUser)」")")
            }
            Text("点火后，TA 的世界开始运转：作息、日常、身边的人，都会随真实时间流动。")
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.top, 8)
        }
    }

    private func row(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.subheadline)
        }
    }
}
