import Foundation

struct MaplyError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

/// Talks to the local Maply API or an explicitly configured authenticated server.
struct MaplyAPI {
    let credentials: Credentials

    init(_ credentials: Credentials = .load()) { self.credentials = credentials }

    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    private static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        return e
    }()

    func runs() async throws -> RunList { try await send("GET", "/api/capture/runs") }
    func jobs() async throws -> [Job] { try await send("GET", "/api/jobs") }
    func models() async throws -> [ModelOption] { (try await send("GET", "/api/tagging/provider/models") as ModelList).models }

    func create(_ run: NewRun) async throws -> CaptureRun {
        try await send("POST", "/api/capture/runs", body: try Self.encoder.encode(run))
    }

    /// action is pause, resume or stop.
    func control(_ run: CaptureRun, _ action: String) async throws -> CaptureRun {
        try await send("POST", "/api/capture/runs/\(run.id)/\(action)", body: Data("{}".utf8))
    }

    private func send<T: Decodable>(_ method: String, _ path: String, body: Data? = nil) async throws -> T {
        guard credentials.isComplete, let url = URL(string: credentials.server.trimmingCharacters(in: .whitespacesAndNewlines) + path) else {
            throw MaplyError(message: "Add the server address in Settings. Remote servers also need their login.")
        }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20)
        request.httpMethod = method
        if !credentials.isLocal {
            let login = Data("\(credentials.username):\(credentials.password)".utf8).base64EncodedString()
            request.setValue("Basic \(login)", forHTTPHeaderField: "Authorization")
        }
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            if status == 401 { throw MaplyError(message: "The gateway rejected the username or password.") }
            throw MaplyError(message: Self.detail(data) ?? "Maply returned HTTP \(status).")
        }
        do {
            return try Self.decoder.decode(T.self, from: data)
        } catch {
            throw MaplyError(message: "Unexpected response from Maply: \(error.localizedDescription)")
        }
    }

    /// FastAPI errors are {"detail": "..."} or a list of validation messages.
    private static func detail(_ data: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        if let text = object["detail"] as? String { return text }
        if let items = object["detail"] as? [[String: Any]] {
            return items.compactMap { $0["msg"] as? String }.joined(separator: "\n")
        }
        return nil
    }
}
