import Foundation

/// The only way the app gets a route. Implementations talk to the Maply route service; the UI
/// never calculates or edits a route itself.
protocol RoutePlanningClient {
    /// The surveyed area and standing limits, or nil when there is no service.
    func coverage() async throws -> RouteCoverage?
    func plan(_ request: RoutePlanRequest) async throws -> RoutePlan
}

/// Used until a route service is configured. Always says so plainly.
struct NotConnectedRoutePlanningClient: RoutePlanningClient {
    func coverage() async throws -> RouteCoverage? { nil }
    func plan(_ request: RoutePlanRequest) async throws -> RoutePlan { throw RoutePlanningError.notConnected }
}

// MARK: HTTP seam

/// Sends one request. URLSession in the app, a stub in tests.
protocol HTTPTransport: Sendable {
    func send(_ request: URLRequest) async throws -> (Data, URLResponse)
}

extension URLSession: HTTPTransport {
    func send(_ request: URLRequest) async throws -> (Data, URLResponse) { try await data(for: request) }
}

/// Client for `maply-route-preview/1`. Sends the graph version it last saw, and when the service
/// says the data changed, reloads coverage and asks once more.
actor MaplyRouteService: RoutePlanningClient {
    let baseURL: URL
    private let transport: HTTPTransport
    private var graphVersion: String?

    init(baseURL: URL, transport: HTTPTransport) { self.baseURL = baseURL; self.transport = transport }

    func coverage() async throws -> RouteCoverage? {
        var r = URLRequest(url: baseURL.appending(path: "v1/coverage"))
        r.timeoutInterval = 20
        let (data, status) = try await send(r)
        guard status == 200 else { throw Self.planningError(status: status, data: data) }
        let c = try RoutePreviewDecoding.coverage(data)
        graphVersion = c.graphVersion
        return c
    }

    func plan(_ request: RoutePlanRequest) async throws -> RoutePlan {
        try await plan(request, retry: true)
    }

    private func plan(_ request: RoutePlanRequest, retry: Bool) async throws -> RoutePlan {
        var r = URLRequest(url: baseURL.appending(path: "v1/route-preview"))
        r.httpMethod = "POST"
        r.timeoutInterval = 20
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.httpBody = try RoutePreviewDecoding.requestBody(request, expectedGraphVersion: graphVersion)
        let (data, status) = try await send(r)
        if status == 200 {
            let plan = try RoutePreviewDecoding.plan(data)
            if let v = plan.release.graphVersion { graphVersion = v }
            return plan
        }
        switch RoutePreviewDecoding.failure(status: status, data: data) {
        case .snapshotChanged where retry:
            graphVersion = nil
            _ = try? await coverage()
            return try await plan(request, retry: false)
        case .snapshotChanged:
            throw RoutePlanningError.serviceError
        case .planning(let e):
            throw e
        }
    }

    private static func planningError(status: Int, data: Data) -> RoutePlanningError {
        if case .planning(let e) = RoutePreviewDecoding.failure(status: status, data: data) { return e }
        return .serviceError
    }

    private func send(_ r: URLRequest) async throws -> (Data, Int) {
        do {
            let (data, response) = try await transport.send(r)
            guard let http = response as? HTTPURLResponse else { throw RoutePlanningError.invalidResponse }
            return (data, http.statusCode)
        } catch let e as RoutePlanningError {
            throw e
        } catch is CancellationError {
            throw CancellationError()
        } catch let e as URLError where e.code == .cancelled {
            throw CancellationError()
        } catch {
            throw RoutePlanningError.unavailable
        }
    }
}

// MARK: Configuration

enum RouteServiceConfiguration {
    /// The local route service from `cd atlas && uv run python -m atlas serve`.
    static let localDefault = "http://127.0.0.1:8765"

    /// Picks the client for this run. Release builds have no route service until one is approved
    /// and published. Debug builds use the local service on loopback (`PP_ROUTE_API` to change it,
    /// `PP_ROUTE_API=off` for none), or sample layout data with `PP_ROUTE_SAMPLE=1`.
    static func makeClient(environment: [String: String] = debugEnvironment) -> RoutePlanningClient {
        #if DEBUG
        if environment["PP_ROUTE_SAMPLE"] != nil { return SampleRoutePlanningClient() }
        if let url = loopbackURL(environment["PP_ROUTE_API"] ?? localDefault) {
            return MaplyRouteService(baseURL: url, transport: URLSession.shared)
        }
        #endif
        return NotConnectedRoutePlanningClient()
    }

    /// Debug settings from the environment, or from `PP_NAME=value` launch arguments for tools that can
    /// only pass arguments, such as the simulator driver.
    static var debugEnvironment: [String: String] {
        var e = ProcessInfo.processInfo.environment
        #if DEBUG
        for a in ProcessInfo.processInfo.arguments where a.hasPrefix("PP_") {
            let kv = a.split(separator: "=", maxSplits: 1)
            e[String(kv[0])] = kv.count > 1 ? String(kv[1]) : "1"
        }
        #endif
        return e
    }

    /// Only plain-HTTP loopback addresses are accepted for local testing.
    static func loopbackURL(_ string: String?) -> URL? {
        guard let string, let url = URL(string: string), url.scheme == "http",
              let host = url.host(), ["127.0.0.1", "localhost", "::1"].contains(host) else { return nil }
        return url
    }
}
