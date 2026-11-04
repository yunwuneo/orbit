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
                    ])

                    let (bytes, response) = try await URLSession.shared.bytes(for: request)
                    if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                        var body = ""
                        for try await line in bytes.lines { body += line + "\n" }
                        throw LLMError.httpError(status: http.statusCode, body: body)
                    }

                    for try await line in bytes.lines {
                        try Task.checkCancellation()
                        guard line.hasPrefix("data: ") else { continue }
                        let payload = String(line.dropFirst(6))
                        if payload == "[DONE]" { break }
                        guard let data = payload.data(using: .utf8),
                              let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                              let choices = json["choices"] as? [[String: Any]],
                              let delta = choices.first?["delta"] as? [String: Any],
                              let content = delta["content"] as? String else { continue }
                        continuation.yield(content)
                    }
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
        guard let url = chatCompletionsURL else { throw LLMError.invalidEndpoint }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "model": config.model,
            "messages": messages.map { ["role": $0.role.rawValue, "content": $0.content] },
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
        do {
            return try JSONDecoder().decode(T.self, from: Data(content.utf8))
        } catch {
            throw LLMError.decodingFailed("\(error); 原始内容: \(content)")
        }
    }
}
