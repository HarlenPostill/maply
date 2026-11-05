import Foundation

struct RunConfig: Codable, Equatable {
    var model = "google/gemini-3.1-flash-lite"
    var fallbackModels: [String] = []
    var maxPhotos = 200
    var maxRequests = 1000
    var maxCostUsd = 5.0
    var hours = 8.0
    var maxSteps = 6
    var minConfidence = 0.9
    var loadSeconds = 4.0
    var yolo: Bool? = false
}

struct CaptureRun: Codable, Identifiable, Equatable {
    let id: String
    let jobId: String
    let jobName: String?
    let status: String
    let config: RunConfig
    let requests: Int
    let costUsd: Double
    let finished: Int
    let error: String?
    let createdAt: String
    let expiresAt: String

    var isLive: Bool { status == "running" || status == "paused" }
    var isYolo: Bool { config.yolo ?? false }
    var progress: Double { config.maxPhotos > 0 ? min(1, Double(finished) / Double(config.maxPhotos)) : 0 }
    var expires: Date? { MaplyDate.parse(expiresAt) }
    var created: Date? { MaplyDate.parse(createdAt) }
    var title: String { jobName ?? "Capture run" }

    var statusSymbol: String {
        switch status {
        case "running": "play.circle.fill"
        case "paused": "pause.circle.fill"
        case "completed": "checkmark.circle.fill"
        case "stopped": "stop.circle.fill"
        default: "questionmark.circle"
        }
    }
}

struct RunList: Codable {
    let managed: Bool?
    let runs: [CaptureRun]

    /// The run the widget follows: a running run, else a paused one, else the newest.
    var active: CaptureRun? {
        runs.first { $0.status == "running" } ?? runs.first { $0.status == "paused" } ?? runs.first
    }
}

struct JobCounts: Codable, Equatable {
    let total: Int?
    let pending: Int?
}

struct Job: Codable, Identifiable, Equatable, Hashable {
    let id: String
    let name: String
    let counts: JobCounts?

    static func == (a: Job, b: Job) -> Bool { a.id == b.id }
    func hash(into hasher: inout Hasher) { hasher.combine(id) }
}

struct ModelOption: Codable, Identifiable, Hashable {
    let id: String
    let name: String
}

struct ModelList: Codable {
    let models: [ModelOption]
}

struct NewRun: Encodable {
    let jobId: String
    let config: RunConfig

    enum CodingKeys: String, CodingKey { case jobId }

    func encode(to encoder: Encoder) throws {
        try config.encode(to: encoder)
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(jobId, forKey: .jobId)
    }
}

enum MaplyDate {
    private static let formatter = ISO8601DateFormatter()

    /// Python isoformat() emits 0 or 6 fractional digits; ISO8601DateFormatter wants neither.
    static func parse(_ value: String) -> Date? {
        let trimmed = value.replacingOccurrences(of: #"\.\d+"#, with: "", options: .regularExpression)
        return formatter.date(from: trimmed)
    }
}

extension Double {
    var usd: String { String(format: "US$%.2f", self) }
}
