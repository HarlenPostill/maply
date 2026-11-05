import SwiftUI

struct RunsView: View {
    @EnvironmentObject private var store: RunStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var showSettings = false
    @State private var showNewRun = false
    @State private var stopping: CaptureRun?

    private var live: [CaptureRun] { store.list?.runs.filter(\.isLive) ?? [] }
    private var ended: [CaptureRun] { store.list?.runs.filter { !$0.isLive } ?? [] }

    var body: some View {
        NavigationStack {
            List {
                if let error = store.error {
                    Section { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.red) }
                }
                if !store.credentials.isComplete {
                    Section {
                        Button("Connect to Maply") { showSettings = true }
                    } footer: {
                        Text("Set the Maply server address in Settings. The local server needs no login.")
                    }
                } else if store.list == nil && store.error == nil {
                    Section { ProgressView().frame(maxWidth: .infinity) }
                }
                if !live.isEmpty {
                    Section("Active") {
                        ForEach(live) { run in
                            RunCard(run: run, busy: store.busyRun == run.id,
                                    onAction: { action in Task { await store.control(run, action) } },
                                    onStop: { stopping = run })
                        }
                    }
                } else if store.list != nil {
                    Section("Active") {
                        Text("No running or paused runs.").foregroundStyle(.secondary)
                    }
                }
                if !ended.isEmpty {
                    Section("Recent") {
                        ForEach(ended) { RunCard(run: $0, busy: false, onAction: { _ in }, onStop: {}) }
                    }
                }
            }
            .navigationTitle("Capture runs")
            .refreshable { await store.refresh() }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { showSettings = true } label: { Image(systemName: "gearshape") }
                        .accessibilityLabel("Settings")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showNewRun = true } label: { Image(systemName: "plus") }
                        .accessibilityLabel("New capture run")
                        .disabled(!store.credentials.isComplete)
                }
            }
            .confirmationDialog("Stop this run?", isPresented: Binding(get: { stopping != nil }, set: { if !$0 { stopping = nil } }),
                                titleVisibility: .visible, presenting: stopping) { run in
                Button("Stop run", role: .destructive) { Task { await store.control(run, "stop") } }
            } message: { _ in
                Text("A stopped run cannot be resumed. Create a new run to continue capturing.")
            }
            .sheet(isPresented: $showSettings) { SettingsView() }
            .sheet(isPresented: $showNewRun) { NewRunView() }
        }
        .onOpenURL { url in
            if url.host == "new" && store.credentials.isComplete { showNewRun = true }
        }
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            if !store.credentials.isComplete { showSettings = true }
            #if DEBUG
            if ProcessInfo.processInfo.environment["MAPLY_OPEN"] == "new" { showNewRun = true }
            #endif
            // Poll like the web panel while the app is open.
            while !Task.isCancelled {
                await store.refresh()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }
}

struct RunCard: View {
    let run: CaptureRun
    let busy: Bool
    let onAction: (String) -> Void
    let onStop: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label(run.status.capitalized, systemImage: run.statusSymbol)
                    .font(.headline)
                    .foregroundStyle(color)
                if run.isYolo {
                    Text("YOLO").font(.caption2.bold()).padding(.horizontal, 6).padding(.vertical, 2)
                        .background(.orange.opacity(0.2), in: Capsule()).foregroundStyle(.orange)
                }
                Spacer()
                if busy { ProgressView() }
            }
            Text(run.title).font(.subheadline.weight(.semibold))
            ProgressView(value: run.progress)
            HStack {
                Text("\(run.finished.formatted()) / \(run.config.maxPhotos.formatted()) handled")
                Spacer()
                Text("\(run.costUsd.usd) of \(run.config.maxCostUsd.usd)")
            }
            .font(.caption).monospacedDigit()
            HStack {
                Text("\(run.requests.formatted()) / \(run.config.maxRequests.formatted()) requests")
                Spacer()
                if let expires = run.expires {
                    Text(run.isLive ? "Ends \(expires.formatted(date: .omitted, time: .shortened))" : (run.created?.formatted(date: .abbreviated, time: .shortened) ?? ""))
                }
            }
            .font(.caption).foregroundStyle(.secondary).monospacedDigit()
            Text(run.config.model).font(.caption2).foregroundStyle(.secondary)
            if let error = run.error, !error.isEmpty {
                Text(error).font(.caption).foregroundStyle(run.status == "running" ? Color.secondary : Color.orange)
            }
            if run.isLive {
                HStack {
                    Button {
                        onAction(run.status == "running" ? "pause" : "resume")
                    } label: {
                        Label(run.status == "running" ? "Pause" : "Resume",
                              systemImage: run.status == "running" ? "pause.fill" : "play.fill")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    Button(role: .destructive, action: onStop) {
                        Label("Stop", systemImage: "stop.fill").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                }
                .disabled(busy)
                .padding(.top, 4)
            }
        }
        .padding(.vertical, 4)
    }

    private var color: Color {
        switch run.status {
        case "running": .green
        case "paused": .orange
        case "completed": .blue
        default: .secondary
        }
    }
}
