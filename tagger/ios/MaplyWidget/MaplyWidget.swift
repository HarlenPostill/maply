import SwiftUI
import WidgetKit

struct RunEntry: TimelineEntry {
    let date: Date
    let run: CaptureRun?
    let message: String?

    static let sample = RunEntry(date: .now, run: CaptureRun(
        id: "sample", jobId: "job", jobName: "City of Sydney", status: "running",
        config: RunConfig(yolo: true), requests: 412, costUsd: 1.37, finished: 128, error: nil,
        createdAt: "2026-10-27T01:00:00+00:00", expiresAt: "2026-10-27T09:00:00+00:00"), message: nil)
}

struct RunProvider: TimelineProvider {
    func placeholder(in context: Context) -> RunEntry { .sample }

    func getSnapshot(in context: Context, completion: @escaping (RunEntry) -> Void) {
        if context.isPreview { return completion(.sample) }
        Task { completion(await entry()) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<RunEntry>) -> Void) {
        Task {
            let current = await entry()
            // WidgetKit budgets refreshes; ask more often while a run is capturing.
            let minutes = current.run?.status == "running" ? 5.0 : 30.0
            completion(Timeline(entries: [current], policy: .after(.now.addingTimeInterval(minutes * 60))))
        }
    }

    private func entry() async -> RunEntry {
        do {
            let list = try await MaplyAPI().runs()
            return RunEntry(date: .now, run: list.active, message: list.runs.isEmpty ? "No capture runs" : nil)
        } catch {
            return RunEntry(date: .now, run: nil, message: error.localizedDescription)
        }
    }
}

struct MaplyWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: RunEntry

    var body: some View {
        Group {
            if let run = entry.run {
                switch family {
                case .accessoryInline: inline(run)
                case .accessoryCircular: circular(run)
                case .accessoryRectangular: rectangular(run)
                default: small(run)
                }
            } else {
                switch family {
                case .accessoryInline: Label("Maply: open app", systemImage: "map")
                case .accessoryCircular: Image(systemName: "map").font(.title2)
                default:
                    VStack(alignment: .leading) {
                        Label("Maply", systemImage: "map").font(.headline)
                        Text(entry.message ?? "Open Maply to connect").font(.caption).lineLimit(3)
                    }
                }
            }
        }
        .widgetURL(URL(string: "maply://runs"))
        .containerBackground(for: .widget) { Color.clear }
    }

    private func inline(_ run: CaptureRun) -> some View {
        Label("\(run.status.capitalized) · \(run.finished)/\(run.config.maxPhotos) · \(run.costUsd.usd)", systemImage: run.statusSymbol)
    }

    private func circular(_ run: CaptureRun) -> some View {
        Gauge(value: run.progress) {
            Image(systemName: run.statusSymbol)
        } currentValueLabel: {
            Image(systemName: run.status == "running" ? "play.fill" : run.status == "paused" ? "pause.fill" : "stop.fill")
        }
        .gaugeStyle(.accessoryCircularCapacity)
    }

    private func rectangular(_ run: CaptureRun) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            HStack(spacing: 4) {
                Image(systemName: run.statusSymbol)
                Text(run.status.capitalized + (run.isYolo ? " · YOLO" : "")).font(.headline)
            }
            Text(run.title).font(.caption).lineLimit(1)
            Text("\(run.finished.formatted())/\(run.config.maxPhotos.formatted()) · \(run.costUsd.usd)")
                .font(.caption).monospacedDigit()
            if let error = run.error, run.status != "running", !error.isEmpty {
                Text(error).font(.caption2).lineLimit(1)
            } else if run.isLive, let expires = run.expires {
                Text("Ends \(expires.formatted(date: .omitted, time: .shortened))").font(.caption2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .widgetAccentable()
    }

    private func small(_ run: CaptureRun) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Label(run.status.capitalized, systemImage: run.statusSymbol).font(.headline)
            Text(run.title).font(.caption).lineLimit(2)
            ProgressView(value: run.progress)
            Text("\(run.finished.formatted())/\(run.config.maxPhotos.formatted()) handled").font(.caption).monospacedDigit()
            Text(run.costUsd.usd + (run.isYolo ? " · YOLO" : "")).font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

struct MaplyWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "MaplyRun", provider: RunProvider()) { entry in
            MaplyWidgetView(entry: entry)
        }
        .configurationDisplayName("Capture run")
        .description("Status of the active Maply capture run.")
        .supportedFamilies([.accessoryRectangular, .accessoryCircular, .accessoryInline, .systemSmall])
    }
}

@main
struct MaplyWidgetBundle: WidgetBundle {
    var body: some Widget { MaplyWidget() }
}
