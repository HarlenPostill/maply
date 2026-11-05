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

    /// One bounded wait per request, with no retry loop. The deployed service's free-plan host sleeps
    /// when idle and takes about 30–90 s to wake, so a shorter limit would fail exactly when it is
    /// starting up. Planning shows its progress state throughout, and changing the start or the
    /// destination cancels the request.
    static let requestTimeout: TimeInterval = 120

    init(baseURL: URL, transport: HTTPTransport) { self.baseURL = baseURL; self.transport = transport }

    func coverage() async throws -> RouteCoverage? {
        var r = URLRequest(url: baseURL.appending(path: "v1/coverage"))
        r.timeoutInterval = Self.requestTimeout
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
        r.timeoutInterval = Self.requestTimeout
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
    /// Info.plist key holding the deployed service's HTTPS base URL. Its value comes from the
    /// `MAPLY_ROUTE_API_URL` build setting. It is an address, never a secret: the app carries no
    /// keys, passwords or database details.
    static let infoKey = "MaplyRouteAPIURL"

    /// Where this run gets routes from.
    enum Endpoint: Equatable {
        case sample
        case local(URL)
        case deployed(URL)
        case none(Reason)
        enum Reason: Equatable { case turnedOff, invalidOverride, noDeployedURL }
    }

    /// The build this is. Only debug builds read overrides, and only simulator and Mac runs default
    /// to the local service, because on a phone 127.0.0.1 is the phone itself.
    struct Build: Equatable {
        var debug: Bool
        var runsOnThisMac: Bool

        static var current: Build {
            #if DEBUG
            let debug = true
            #else
            let debug = false
            #endif
            #if targetEnvironment(simulator) || targetEnvironment(macCatalyst)
            return Build(debug: debug, runsOnThisMac: true)
            #else
            return Build(debug: debug, runsOnThisMac: false)
            #endif
        }
    }

    /// Release builds: the deployed HTTPS service, and nothing else. Debug builds: `PP_ROUTE_SAMPLE=1`
    /// for labelled sample data; `PP_ROUTE_API` for an explicit service (loopback HTTP or any valid
    /// HTTPS URL; `off` for none; anything else is refused, never quietly replaced); otherwise the
    /// local service on the simulator or Mac, and the deployed service on a phone.
    static func endpoint(environment: [String: String], deployed: URL?, build: Build) -> Endpoint {
        guard build.debug else { return deployed.map(Endpoint.deployed) ?? .none(.noDeployedURL) }
        if environment["PP_ROUTE_SAMPLE"] != nil { return .sample }
        if let override = environment["PP_ROUTE_API"]?.trimmingCharacters(in: .whitespaces), !override.isEmpty {
            if override == "off" { return .none(.turnedOff) }
            if let url = loopbackURL(override) { return .local(url) }
            if let url = deployedURL(override) { return .deployed(url) }
            return .none(.invalidOverride)
        }
        if build.runsOnThisMac, let url = loopbackURL(localDefault) { return .local(url) }
        return deployed.map(Endpoint.deployed) ?? .none(.noDeployedURL)
    }

    static func makeClient(environment: [String: String] = debugEnvironment,
                           deployed: URL? = deployedURL(Bundle.main.object(forInfoDictionaryKey: infoKey) as? String),
                           build: Build = .current) -> RoutePlanningClient {
        switch endpoint(environment: environment, deployed: deployed, build: build) {
        case .sample:
            #if DEBUG
            return SampleRoutePlanningClient()
            #else
            return NotConnectedRoutePlanningClient()
            #endif
        case .local(let url), .deployed(let url):
            return MaplyRouteService(baseURL: url, transport: URLSession.shared)
        case .none:
            return NotConnectedRoutePlanningClient()
        }
    }

    /// Debug settings from the environment, or from `PP_NAME=value` launch arguments for tools that can
    /// only pass arguments, such as the simulator driver. Release builds read none.
    static var debugEnvironment: [String: String] {
        #if DEBUG
        var e = ProcessInfo.processInfo.environment
        for a in ProcessInfo.processInfo.arguments where a.hasPrefix("PP_") {
            let kv = a.split(separator: "=", maxSplits: 1)
            e[String(kv[0])] = kv.count > 1 ? String(kv[1]) : "1"
        }
        return e
        #else
        return [:]
        #endif
    }

    /// Only plain-HTTP loopback addresses are accepted for local testing.
    static func loopbackURL(_ string: String?) -> URL? {
        guard let string, let url = URL(string: string), url.scheme == "http",
              let host = url.host(), ["127.0.0.1", "localhost", "::1"].contains(host) else { return nil }
        return url
    }

    /// A deployed service: HTTPS, a real host name, no credentials, query or fragment, and no
    /// path beyond "/". The app appends "v1/…" itself.
    static func deployedURL(_ string: String?) -> URL? {
        guard let raw = string?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty, !raw.contains("$("),
              let c = URLComponents(string: raw), c.scheme?.lowercased() == "https",
              let host = c.host?.lowercased(), host.contains("."), !host.hasSuffix("."),
              !["127.0.0.1", "localhost", "::1", "0.0.0.0"].contains(host),
              c.user == nil, c.password == nil, c.query == nil, c.fragment == nil,
              c.path.isEmpty || c.path == "/" else { return nil }
        var clean = URLComponents(); clean.scheme = "https"; clean.host = host; clean.port = c.port
        return clean.url
    }
}
