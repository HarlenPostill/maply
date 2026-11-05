import SwiftUI
import WidgetKit

@main
struct MaplyApp: App {
    @StateObject private var store = RunStore()

    var body: some Scene {
        WindowGroup {
            RunsView()
                .environmentObject(store)
        }
    }
}

@MainActor
final class RunStore: ObservableObject {
    @Published var credentials = Credentials.load()
    @Published var list: RunList?
    @Published var error: String?
    @Published var busyRun: String?

    var api: MaplyAPI { MaplyAPI(credentials) }

    func refresh() async {
        guard credentials.isComplete else { return }
        do {
            let latest = try await api.runs()
            if latest.runs != list?.runs { WidgetCenter.shared.reloadAllTimelines() }
            list = latest
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    func control(_ run: CaptureRun, _ action: String) async {
        busyRun = run.id
        defer { busyRun = nil }
        do {
            _ = try await api.control(run, action)
            await refresh()
            WidgetCenter.shared.reloadAllTimelines()
        } catch {
            self.error = error.localizedDescription
        }
    }

    func save(_ credentials: Credentials) async {
        credentials.save()
        self.credentials = Credentials.load()
        list = nil
        await refresh()
        WidgetCenter.shared.reloadAllTimelines()
    }
}
