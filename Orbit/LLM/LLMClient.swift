import Foundation

struct LLMMessage: Codable, Sendable, Equatable {
    enum Role: String, Codable, Sendable {
        case system, user, assistant
    }
    var role: Role
    var content: String

    static func system(_ content: String) -> LLMMessage { LLMMessage(role: .system, content: content) }
    static func user(_ content: String) -> LLMMessage { LLMMessage(role: .user, content: content) }
    static func assistant(_ content: String) -> LLMMessage { LLMMessage(role: .assistant, content: content) }
}

enum LLMError: Error, LocalizedError {
    case invalidEndpoint
    case httpError(status: Int, body: String)
    case emptyContent
    case decodingFailed(String)

    var errorDescription: String? {
        switch self {
        case .invalidEndpoint: "端点地址无效"
        case .httpError(let status, let body): "请求失败（HTTP \(status)）：\(body.prefix(300))"
        case .emptyContent: "模型返回了空内容"
        case .decodingFailed(let detail): "结构化输出解析失败：\(detail.prefix(300))"
        }
    }
}

/// JSON Schema 的 Sendable 包装（[String: Any] 本身不满足 Sendable）
struct JSONSchemaObject: @unchecked Sendable {
    let object: [String: Any]
}

/// OpenAI 兼容端点客户端：流式对话 + structured output。
/// BYOK 架构下由用户的 Key 直连用户自己的端点，不经过任何中转。
struct LLMClient: Sendable {
    let config: LLMEndpointConfig
    let apiKey: String
    /// 每次调用完成后回报 token 用量（由配置层注入，落库统计）
    var usageHandler: (@Sendable (LLMUsage) -> Void)?

    private var chatCompletionsURL: URL? {
        let base = config.baseURL.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        return URL(string: base + "/chat/completions")
    }

    // MARK: - 非流式

    func chat(_ messages: [LLMMessage], temperature: Double = 0.9) async throws -> String {
        var collected = ""
        for try await delta in stream(messages, temperature: temperature) {
            collected += delta
        }
        guard !collected.isEmpty else { throw LLMError.emptyContent }
        return collected
    }

    // MARK: - 流式（SSE）

    func stream(_ messages: [LLMMessage], temperature: Double = 0.9) -> AsyncThrowingStream<String, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    guard let url = chatCompletionsURL else { throw LLMError.invalidEndpoint }
                    var request = URLRequest(url: url)
                    request.httpMethod = "POST"
                    request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
                    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    request.httpBody = try JSONSerialization.data(withJSONObject: [
                        "model": config.model,
                        "messages": messages.map { ["role": $0.role.rawValue, "content": $0.content] },
                        "temperature": temperature,
                        "stream": true,
                        "stream_options": ["include_usage": true],
                    ])

                    let (bytes, response) = try await URLSession.shared.bytes(for: request)
                    if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                        var body = ""
                        for try await line in bytes.lines { body += line + "\n" }
                        throw LLMError.httpError(status: http.statusCode, body: body)
                    }

                    var lastUsage: LLMUsage?
                    for try await line in bytes.lines {
                        try Task.checkCancellation()
                        guard line.hasPrefix("data: ") else { continue }
                        let payload = String(line.dropFirst(6))
                        if payload == "[DONE]" { break }
                        guard let data = payload.data(using: .utf8),
                              let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
                        // 开启 include_usage 后，末块带 usage 且 choices 为空
                        if let usage = Self.parseUsage(json) {
                            lastUsage = usage
                            continue
                        }
                        guard let choices = json["choices"] as? [[String: Any]],
                              let delta = choices.first?["delta"] as? [String: Any],
                              let content = delta["content"] as? String else { continue }
                        continuation.yield(content)
                    }
                    if let lastUsage { usageHandler?(lastUsage) }
                    continuation.finish()
                } catch is CancellationError {
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    // MARK: - Structured Output（JSON Schema）

    func structured<T: Decodable & Sendable>(
        _ messages: [LLMMessage],
        name: String,
        schema: JSONSchemaObject,
        as type: T.Type,
        temperature: Double = 0.7
    ) async throws -> T {
        let content = try await requestStructured(
            messages: messages, name: name, schema: schema, temperature: temperature)
        if let value = Self.decode(content, as: type) { return value }

        // 端点可能忽略了 json_schema 约束（返回了 markdown 或散文）。
        // 重试一次：把错误输出贴回去，明确要求只输出纯 JSON。
        let repairMessages = messages + [
            .assistant(content),
            .user("你的输出不符合格式要求。请重新输出：只输出符合 JSON Schema 的纯 JSON，不要包含任何解释、markdown 代码块或多余文字。"),
        ]
        let retryContent = try await requestStructured(
            messages: repairMessages, name: name, schema: schema, temperature: temperature)
        if let value = Self.decode(retryContent, as: type) { return value }

        // 最后兜底：换全新上下文，让模型专门做"修复损坏 JSON"这一件事
        let repaired = try await requestStructured(
            messages: [
                .system("你是 JSON 修复工具。用户会给你一段损坏的 JSON，请修复为合法 JSON：保持原意，只输出修复后的 JSON，不要任何解释。"),
                .user(retryContent),
            ],
            name: name, schema: schema, temperature: 0.1)
        if let value = Self.decode(repaired, as: type) { return value }

        throw LLMError.decodingFailed("原始内容: \(repaired)")
    }

    /// 直接解码；失败则尝试从文本中提取 JSON 片段（去代码围栏、截取首个 { 到末个 }）再解码
    private static func decode<T: Decodable>(_ content: String, as type: T.Type) -> T? {
        if let value = try? JSONDecoder().decode(T.self, from: Data(content.utf8)) {
            return value
        }
        guard let extracted = extractJSON(from: content) else { return nil }
        return try? JSONDecoder().decode(T.self, from: Data(extracted.utf8))
    }

    private static func extractJSON(from text: String) -> String? {
        var trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.hasPrefix("```") {
            trimmed = trimmed
                .replacingOccurrences(of: "```json", with: "")
                .replacingOccurrences(of: "```", with: "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }
        guard let start = trimmed.firstIndex(of: "{"),
              let end = trimmed.lastIndex(of: "}"),
              start <= end else { return nil }
        return String(trimmed[start...end])
    }

    private func requestStructured(
        messages: [LLMMessage],
        name: String,
        schema: JSONSchemaObject,
        temperature: Double
    ) async throws -> String {
        guard let url = chatCompletionsURL else { throw LLMError.invalidEndpoint }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        // 不少兼容端点会忽略 response_format，把 schema 同时写进提示词作为双保险
        var wireMessages = messages
        if let schemaData = try? JSONSerialization.data(withJSONObject: schema.object),
           let schemaText = String(data: schemaData, encoding: .utf8) {
            wireMessages.insert(
                .system("你必须只输出一个符合以下 JSON Schema 的 JSON 对象，不要输出任何其他内容：\(schemaText)"),
                at: 0)
        }

        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "model": config.model,
            "messages": wireMessages.map { ["role": $0.role.rawValue, "content": $0.content] },
            "temperature": temperature,
            "response_format": [
                "type": "json_schema",
                "json_schema": [
                    "name": name,
                    "strict": true,
                    "schema": schema.object,
                ],
            ],
        ])

        let (data, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw LLMError.httpError(status: http.statusCode, body: body)
        }
        guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let choices = json["choices"] as? [[String: Any]],
              let message = choices.first?["message"] as? [String: Any],
              let content = message["content"] as? String else {
            throw LLMError.emptyContent
        }
        if let usage = Self.parseUsage(json) { usageHandler?(usage) }
        return content
    }

    // MARK: - usage 解析

    private static func parseUsage(_ json: [String: Any]) -> LLMUsage? {
        guard let usage = json["usage"] as? [String: Any],
              let prompt = usage["prompt_tokens"] as? Int,
              let completion = usage["completion_tokens"] as? Int else { return nil }
        return LLMUsage(promptTokens: prompt, completionTokens: completion)
    }
}
