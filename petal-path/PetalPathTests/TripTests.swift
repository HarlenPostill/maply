import XCTest
import MapKit
@testable import PetalPath

// MARK: Stubs

@MainActor
final class StubPlaceSearch: PlaceSearching {
    var queries: [String] = []
    var results: [PlaceSuggestion] = []
    var failure: PlaceSearchError?
    var places: [String: Place] = [:]

    func suggestions(for query: String, near region: MKCoordinateRegion) async throws -> [PlaceSuggestion] {
        queries.append(query)
        if let failure { throw failure }
        return results
    }
    func resolve(_ s: PlaceSuggestion) async throws -> Place {
        guard let p = places[s.id] else { throw PlaceSearchError.notFound }
        return p
    }
}

final class RecordingClient: RoutePlanningClient, @unchecked Sendable {
    var requests: [RoutePlanRequest] = []
    var result: Result<RoutePlan, Error> = .failure(RoutePlanningError.notConnected)
    var coverageResult: RouteCoverage?
    var coverageError: Error?
    func coverage() async throws -> RouteCoverage? {
        if let coverageError { throw coverageError }
        return coverageResult
    }
    func plan(_ request: RoutePlanRequest) async throws -> RoutePlan {
        requests.append(request)
        return try result.get()
    }
}

/// Holds each plan until the test lets it go, to test replies that arrive late.
final class GatedClient: RoutePlanningClient, @unchecked Sendable {
    var gates: [CheckedContinuation<Void, Never>] = []
    var reply: RoutePlan
    init(reply: RoutePlan) { self.reply = reply }
    func coverage() async throws -> RouteCoverage? { nil }
    func plan(_ request: RoutePlanRequest) async throws -> RoutePlan {
        await withCheckedContinuation { gates.append($0) }
        return reply
    }
    func release() { gates.forEach { $0.resume() }; gates = [] }
}

/// Holds each place lookup until the test lets it go.
@MainActor
final class GatedPlaceSearch: PlaceSearching {
    var gates: [CheckedContinuation<Void, Never>] = []
    var place: Place
    init(place: Place) { self.place = place }
    func suggestions(for query: String, near region: MKCoordinateRegion) async throws -> [PlaceSuggestion] { [] }
    func resolve(_ s: PlaceSuggestion) async throws -> Place {
        await withCheckedContinuation { gates.append($0) }
        return place
    }
    func release() { gates.forEach { $0.resume() }; gates = [] }
}

/// Replies in order and records what was sent.
final class ScriptedTransport: HTTPTransport, @unchecked Sendable {
    var replies: [() throws -> (Data, Int)]
    var sent: [URLRequest] = []
    init(_ replies: [() throws -> (Data, Int)]) { self.replies = replies }
    func send(_ request: URLRequest) async throws -> (Data, URLResponse) {
        sent.append(request)
        let (data, status) = try replies.removeFirst()()
        return (data, HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
    }
}

private let quay = GeoCoordinate.circularQuay
private let townHall = GeoCoordinate(latitude: -33.8732, longitude: 151.2061)
private let t0 = Date(timeIntervalSince1970: 1_800_000_000)

private func fix(_ c: GeoCoordinate = quay, accuracy: Double = 8, age: TimeInterval = 2) -> LocationFix {
    LocationFix(coordinate: c, horizontalAccuracy: accuracy, timestamp: t0.addingTimeInterval(-age))
}

/// The backend's generated contract examples. Tests fail if they move or drift.
private func example(_ name: String) throws -> Data {
    let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().appending(path: "atlas/docs/routing-examples/\(name)")
    return try Data(contentsOf: url)
}

private func tampered(_ name: String, _ change: (inout [String: Any]) -> Void) throws -> Data {
    var o = try JSONSerialization.jsonObject(with: example(name)) as! [String: Any]
    change(&o)
    return try JSONSerialization.data(withJSONObject: o)
}

private func plan(legs: [RouteLeg] = [], warnings: [ServiceWarning] = [], observations: Int = 4,
                  slope: SlopeObservations = SlopeObservations(), limits: [String] = []) -> RoutePlan {
    RoutePlan(routeMetres: 300, streetMetres: 300, connectorMetres: 0, endpointGapMetres: 10,
              evidence: EvidenceMetres(observed: 300), designation: DesignationMetres(ordinary: 300), imagery: ImageryAgeMetres(withinThreeYears: 300),
              imageryMonths: nil, crossings: 0, sharedSurfaceTransitions: 0, connectors: 0, observations: observations, flaggedObservations: 0,
              slope: slope, warnings: warnings, legs: legs, origin: nil, destination: nil, limits: limits, release: ReleaseInfo())
}

private func warning(_ id: Int, _ code: String, _ s: ServiceWarning.Severity, leg: Int? = nil, on: String? = nil, _ m: String = "Message.") -> ServiceWarning {
    ServiceWarning(id: id, code: code, severity: s, leg: leg, at: nil, observedOn: on, message: m)
}

@MainActor
private func waitUntil(_ condition: () -> Bool, timeout: TimeInterval = 2) async {
    let end = Date().addingTimeInterval(timeout)
    while !condition() && Date() < end { try? await Task.sleep(for: .milliseconds(10)) }
}

// MARK: Real fix separation

final class LocationFixTests: XCTestCase {
    func testLatestFixIsTheRawReadingAndIgnoresJoystickWander() {
        let g = GPSLocationSource(start: quay)
        g.receive(townHall, accuracy: 9, at: t0)
        g.steer(SIMD2(0, 10))                       // walk north with the joystick
        for _ in 0..<120 { g.tick(dt: 1.0 / 60) }
        XCTAssertTrue(g.isWandering)
        XCTAssertGreaterThan(g.coordinate.latitude, townHall.latitude)
        XCTAssertEqual(g.latestFix, LocationFix(coordinate: townHall, horizontalAccuracy: 9, timestamp: t0))
    }

    func testRoughFixesAreRecordedHonestlyAndStopClearsThem() {
        let g = GPSLocationSource(start: quay)
        g.receive(townHall, accuracy: 9, at: t0)
        g.receive(quay, accuracy: 480, at: t0.addingTimeInterval(1))   // too rough to move the avatar
        XCTAssertEqual(g.latestFix?.horizontalAccuracy, 480)
        g.receive(quay, accuracy: -1, at: t0.addingTimeInterval(2))    // invalid: not a reading
        XCTAssertEqual(g.latestFix?.horizontalAccuracy, 480)
        g.stop()
        XCTAssertNil(g.latestFix)
    }

    func testMyLocationNeedsAFreshCloseFixWhileFollowingTheDevice() {
        func status(_ following: Bool, _ f: LocationFix?) -> MyLocationStatus {
            MyLocationStatus.evaluate(TripLocationSnapshot(followingDevice: following, deviceFix: f, character: quay, characterSimulated: false), now: t0)
        }
        XCTAssertEqual(status(false, fix()), .off)
        XCTAssertEqual(status(true, nil), .finding)
        XCTAssertEqual(status(true, fix(age: 31)), .stale)
        XCTAssertEqual(status(true, fix(accuracy: 51)), .rough(accuracy: 51))
        XCTAssertEqual(status(true, fix(accuracy: 50, age: 30)).fix, fix(accuracy: 50, age: 30))
    }
}

// MARK: Planner

@MainActor
final class TripPlannerTests: XCTestCase {
    private var search: StubPlaceSearch!
    private var client: RecordingClient!
    private var planner: TripPlanner!
    private let destination = Place(name: "Town Hall", address: "George St", coordinate: townHall)

    override func setUp() async throws {
        search = StubPlaceSearch(); client = RecordingClient()
        planner = TripPlanner(search: search, client: client, debounce: .milliseconds(30), now: { t0 })
    }

    private func snap(following: Bool = true, fix f: LocationFix? = fix(), character: GeoCoordinate = quay, simulated: Bool = false) -> TripLocationSnapshot {
        TripLocationSnapshot(followingDevice: following, deviceFix: f, character: character, characterSimulated: simulated)
    }

    private func previewArrived() -> Bool { if case .preview = planner.phase { true } else { false } }

    func testDefaultStartIsTheRealFixWhenUsable() {
        planner.open(with: snap())
        XCTAssertEqual(planner.originChoice, .myLocation)
        XCTAssertFalse(planner.characterAvailable(snap()))
    }

    func testJoystickModeOffersOnlyTheSimulatedCharacter() {
        let s = snap(following: false, fix: nil, simulated: true)
        planner.open(with: s)
        XCTAssertEqual(planner.originChoice, .character)
        XCTAssertNil(planner.myLocationStatus(s).fix)
    }

    func testRoughFixOnTheRealPositionOffersNoStartRatherThanTheCharacter() {
        let s = snap(fix: fix(accuracy: 140), simulated: false)
        planner.open(with: s)
        XCTAssertNil(planner.originChoice)
        XCTAssertFalse(planner.characterAvailable(s))
    }

    func testAFixArrivingLaterBecomesTheStartOnlyIfNoneWasChosen() {
        planner.open(with: snap(fix: fix(age: 60)))
        XCTAssertNil(planner.originChoice)
        planner.positionsChanged(snap())
        XCTAssertEqual(planner.originChoice, .myLocation)

        planner.set(Place(name: "Central", address: nil, coordinate: townHall), for: .start)
        planner.positionsChanged(snap())
        XCTAssertEqual(planner.originChoice, .place)
    }

    func testMyLocationSendsTheRawFixNotTheWanderingCharacter() async {
        let wandered = GeoCoordinate(latitude: -33.85, longitude: 151.22)
        let s = snap(fix: fix(townHall, accuracy: 12), character: wandered, simulated: true)
        planner.open(with: s)
        planner.set(destination, for: .destination)
        client.result = .success(plan())
        planner.plan(s)
        await waitUntil(previewArrived)
        XCTAssertEqual(client.requests.first?.origin, TripEndpoint(coordinate: townHall, label: TripCopy.myLocation, source: .device(accuracy: 12)))
    }

    func testCharacterStartIsLabelledSimulatedAndWarnedFirst() async {
        let s = snap(following: false, fix: nil, character: quay, simulated: true)
        planner.open(with: s)
        planner.set(destination, for: .destination)
        client.result = .success(plan())
        planner.plan(s)
        await waitUntil(previewArrived)
        XCTAssertEqual(client.requests.first?.origin.source, .simulatedCharacter)
        let g = RouteSummary.groups(for: plan(), origin: planner.plannedOrigin, destinationName: "Town Hall")
        XCTAssertEqual(g.first?.code, "simulated_start")
        XCTAssertEqual(g.first?.severity, .prominent)
    }

    func testAFixThatGoesStaleIsCheckedAgainAndNeverSent() {
        planner.open(with: snap())
        planner.set(destination, for: .destination)
        planner.plan(snap(fix: fix(age: 45)))
        XCTAssertEqual(planner.phase, .failed(.startNotAvailable))
        XCTAssertTrue(client.requests.isEmpty)
    }

    func testADestinationOutsideTheSurveyedAreaIsNotSent() async throws {
        client.coverageResult = try RoutePreviewDecoding.coverage(example("coverage.json"))
        planner.open(with: snap())
        await waitUntil { self.planner.coverage != nil }
        planner.set(destination, for: .destination)         // Town Hall is outside the synthetic test town
        planner.plan(snap())
        XCTAssertEqual(planner.phase, .failed(.outsideCoverage(.origin)))
        XCTAssertTrue(client.requests.isEmpty)
    }

    func testNotConnectedServiceSaysSoAndNothingIsInvented() async {
        planner.open(with: snap())
        planner.set(destination, for: .destination)
        planner.plan(snap())
        await waitUntil { self.planner.phase != .planning }
        XCTAssertEqual(planner.phase, .failed(.notConnected))
    }

    func testSearchWaitsForTypingToPauseAndUsesTheLastQuery() async {
        planner.open(with: snap())
        XCTAssertEqual(planner.editing, .destination)
        for q in ["To", "Tow", "Town", "Town H"] { planner.query = q }
        await waitUntil { self.planner.searchStatus == .done }
        XCTAssertEqual(search.queries, ["Town H"])
    }

    func testOneLetterDoesNotSearch() async {
        planner.open(with: snap())
        planner.query = "T"
        try? await Task.sleep(for: .milliseconds(80))
        XCTAssertTrue(search.queries.isEmpty)
        XCTAssertEqual(planner.searchStatus, .idle)
    }

    func testSearchFailureIsShownNotSwallowed() async {
        search.failure = .busy
        planner.open(with: snap())
        planner.query = "Town Hall"
        await waitUntil { self.planner.searchStatus != .searching }
        XCTAssertEqual(planner.searchStatus, .failed(.busy))
    }

    func testChoosingASuggestionSetsTheDestinationAndClearsAnOldPreview() async {
        let s = PlaceSuggestion(title: "Town Hall", subtitle: "George St", completion: nil)
        search.places[s.id] = destination
        planner.open(with: snap())
        planner.query = "Town Hall"
        await planner.select(s)
        XCTAssertEqual(planner.destination, destination)
        XCTAssertNil(planner.editing)

        client.result = .success(plan())
        planner.plan(snap())
        await waitUntil(previewArrived)
        planner.changeDestination()
        XCTAssertEqual(planner.editing, .destination)
        XCTAssertEqual(planner.destination, destination, "the old destination stays until a new one is chosen")
        XCTAssertTrue(previewArrived())
        planner.endEditing()
        XCTAssertEqual(planner.destination, destination)
        XCTAssertTrue(previewArrived(), "cancelling an edit keeps the preview")

        let other = Place(name: "Central", address: nil, coordinate: GeoCoordinate(latitude: -33.883, longitude: 151.206))
        planner.changeDestination()
        planner.set(other, for: .destination)
        XCTAssertEqual(planner.phase, .choosing, "a new destination drops the old preview")
    }

    func testFollowingTheDeviceWhileFindingNeverPicksTheCharacter() {
        let finding = snap(following: true, fix: nil, simulated: true)
        planner.open(with: finding)
        XCTAssertNil(planner.originChoice, "waiting for a fix, not a simulated start")
        XCTAssertTrue(planner.characterAvailable(finding), "still there to choose by hand")
        planner.positionsChanged(finding)
        XCTAssertNil(planner.originChoice)
        planner.positionsChanged(snap())
        XCTAssertEqual(planner.originChoice, .myLocation)
    }

    func testAStartThatBecameUnavailableIsNeverSwappedForTheCharacter() {
        planner.open(with: snap())
        planner.set(destination, for: .destination)
        XCTAssertEqual(planner.originChoice, .myLocation)
        let staleButCharacterThere = snap(fix: fix(age: 45), simulated: true)
        planner.open(with: staleButCharacterThere)
        XCTAssertEqual(planner.originChoice, .myLocation, "kept as the person's choice, waiting")
        XCTAssertFalse(planner.canPlan(staleButCharacterThere))
        planner.plan(staleButCharacterThere)
        XCTAssertEqual(planner.phase, .failed(.startNotAvailable))
        XCTAssertEqual(planner.originChoice, .myLocation)
        XCTAssertTrue(client.requests.isEmpty)
    }

    func testAPreviewKnowsWhenItsCapturedStartNoLongerMatches() async {
        planner.open(with: snap())
        planner.set(destination, for: .destination)
        client.result = .success(plan())
        planner.plan(snap())
        await waitUntil(previewArrived)
        XCTAssertFalse(planner.previewStartChanged(snap()))
        XCTAssertFalse(planner.previewStartChanged(snap(fix: fix(GeoCoordinate(latitude: quay.latitude + 0.0001, longitude: quay.longitude)))), "10 m of jitter")
        XCTAssertTrue(planner.previewStartChanged(snap(fix: fix(townHall))), "moved well away")
        XCTAssertTrue(planner.previewStartChanged(snap(fix: fix(age: 45))), "start no longer available")
        XCTAssertEqual(planner.plannedOrigin?.source, .device(accuracy: 8), "the preview keeps the start it was planned from")
    }

    func testALatePlanReplyAfterTheTripChangedIsDropped() async {
        let gated = GatedClient(reply: plan())
        planner = TripPlanner(search: search, client: gated, debounce: .milliseconds(30), now: { t0 })
        planner.open(with: snap())
        planner.set(destination, for: .destination)
        planner.plan(snap())
        await waitUntil { !gated.gates.isEmpty }
        XCTAssertEqual(planner.phase, .planning)
        planner.set(Place(name: "Central", address: nil, coordinate: GeoCoordinate(latitude: -33.883, longitude: 151.206)), for: .destination)
        gated.release()
        try? await Task.sleep(for: .milliseconds(80))
        XCTAssertEqual(planner.phase, .choosing, "the reply for the old destination is never shown")
    }

    func testALookupThatFinishesAfterCancelIsIgnored() async {
        let gated = GatedPlaceSearch(place: Place(name: "Late", address: nil, coordinate: townHall))
        planner = TripPlanner(search: gated, client: client, debounce: .milliseconds(30), now: { t0 })
        planner.open(with: snap())
        planner.set(destination, for: .destination)
        planner.changeDestination()
        let lookup = Task { await planner.select(PlaceSuggestion(title: "Late", subtitle: "", completion: nil)) }
        await waitUntil { !gated.gates.isEmpty }
        planner.endEditing()
        gated.release()
        await lookup.value
        XCTAssertEqual(planner.destination, destination, "a cancelled search never changes the trip")
        XCTAssertNil(planner.editing)
    }

    func testCoverageFailureIsRecordedAndEarlierCoverageKept() async throws {
        client.coverageResult = try RoutePreviewDecoding.coverage(example("coverage.json"))
        planner.open(with: snap())
        await waitUntil { self.planner.coverageState == .loaded }
        client.coverageError = RoutePlanningError.loading
        planner.loadCoverage()
        await waitUntil { self.planner.coverageState == .failed }
        XCTAssertNotNil(planner.coverage, "a failed refresh keeps the limits already shown")
    }

    func testSearchedStartIsUsedAsAPlace() {
        planner.open(with: snap(following: false, fix: nil, simulated: true))
        planner.chooseOrigin(.place, snap())
        XCTAssertEqual(planner.editing, .start)
        planner.set(Place(name: "Central", address: nil, coordinate: GeoCoordinate(latitude: -33.883, longitude: 151.206)), for: .start)
        XCTAssertEqual(planner.originChoice, .place)
        XCTAssertEqual(planner.originEndpoint(snap())?.source, .searchedPlace)
    }
}

// MARK: Contract: maply-route-preview/1

final class RoutePreviewContractTests: XCTestCase {
    func testTheExamplePreviewDecodesFaithfully() throws {
        let p = try RoutePreviewDecoding.plan(example("route-preview.json"))
        XCTAssertEqual(p.readiness, .planningPreview)
        XCTAssertFalse(p.isSample)
        XCTAssertEqual(p.routeMetres, 130)
        XCTAssertEqual(p.endpointGapMetres, 18)
        XCTAssertEqual(p.evidence.total, p.streetMetres, accuracy: 0.05, "evidence partitions street metres")
        XCTAssertEqual(p.legs.map(\.kind), [.endpointGap, .footpath, .crossing, .footpath, .endpointGap])
        XCTAssertNil(p.legs[2].lengthMetres, "crossings are not measured")
        XCTAssertEqual(p.legs[2].coordinates.count, 1)
        XCTAssertEqual(p.legs[2].streetName, "Side Street")
        XCTAssertEqual(p.legs[2].nearby?.kerbRampPresent, 1)
        XCTAssertEqual(p.legs[1].facing, "north")
        XCTAssertEqual(p.legs[3].evidence, EvidenceMetres(observed: 45, uncertain: 15, unknown: 0))
        XCTAssertEqual(p.legs[0].gapEnd, .origin)
        XCTAssertEqual(p.crossings, 1)
        XCTAssertEqual(p.observations, 9)
        XCTAssertEqual(p.imageryMonths, MonthRange(from: "2019-06", to: "2024-03"))
        XCTAssertEqual(p.imagery.olderThanThreeYears, 30)
        XCTAssertEqual(p.warnings.count, 6)
        XCTAssertEqual(p.warnings.first { $0.code == "steep_slope_observed" }?.severity, .prominent)
        XCTAssertEqual(p.release.graphVersion, try RoutePreviewDecoding.coverage(example("coverage.json")).graphVersion)
        XCTAssertEqual(p.imagery, ImageryAgeMetres(withinThreeYears: 100, olderThanThreeYears: 30, undated: 0, noObservation: 0))
        XCTAssertEqual(p.origin?.gapMetres, 16)
    }

    func testSlopeCountsAreTagsNeverInventedAsLevel() throws {
        let s = try RoutePreviewDecoding.plan(example("route-preview.json")).slope
        XCTAssertEqual(s.observationsWithoutSlope, 8)
        XCTAssertEqual(s.count(.steep), 1)
        XCTAssertEqual(s.entries.first, .init(feature: .footpath, axis: .longitudinal, category: .steep, count: 1))
        XCTAssertTrue(s.entries.contains(.init(feature: .footpath, axis: .cross, category: .apparentlyLevel, count: 1)))
        XCTAssertFalse(s.hasUnreadFeatures)
    }

    func testAnUnknownSlopeCategoryIsFlaggedNotDropped() throws {
        let data = try tampered("route-preview.json") { o in
            var s = o["slope_observations"] as! [String: Any]
            s["footpath"] = ["longitudinal": ["very_steep": 2]]
            o["slope_observations"] = s
        }
        XCTAssertTrue(try RoutePreviewDecoding.plan(data).slope.hasUnreadFeatures)
    }

    func testAnythingButANavigationOffUnverifiedPreviewIsRefused() throws {
        for change: (inout [String: Any]) -> Void in [
            { $0["navigation_allowed"] = true },
            { $0["status"] = "verified" },
            { $0["api_version"] = "maply-route-preview/2" },
            { $0.removeValue(forKey: "navigation_allowed") },
        ] {
            XCTAssertThrowsError(try RoutePreviewDecoding.plan(tampered("route-preview.json", change))) {
                XCTAssertEqual($0 as? RoutePlanningError, .invalidResponse)
            }
        }
    }

    func testAnUnfamiliarSeverityIsNeverPlayedDown() throws {
        let data = try tampered("route-preview.json") { o in
            var w = o["warnings"] as! [[String: Any]]
            w[0]["severity"] = "critical"
            o["warnings"] = w
        }
        XCTAssertEqual(try RoutePreviewDecoding.plan(data).warnings[0].severity, .prominent)
    }

    func testCoverageDecodesTheBoundaryAndLimits() throws {
        let c = try RoutePreviewDecoding.coverage(example("coverage.json"))
        let p = try RoutePreviewDecoding.plan(example("route-preview.json"))
        XCTAssertTrue(c.contains(GeoCoordinate(latitude: -33.89982, longitude: 151.20141)), "the example origin is inside")
        XCTAssertFalse(c.contains(quay))
        XCTAssertEqual(c.graphVersion, p.release.graphVersion)
        XCTAssertNotNil(c.limitMessages["preview_only"])
        XCTAssertTrue(p.limits.allSatisfy { c.limitMessages[$0] != nil }, "every route limit has wording")
    }

    func testRequestCarriesOnlyTheContractFields() throws {
        let r = RoutePlanRequest(origin: TripEndpoint(coordinate: quay, label: "A", source: .device(accuracy: 5)),
                                 destination: TripEndpoint(coordinate: townHall, label: "B", source: .searchedPlace))
        let with = try JSONSerialization.jsonObject(with: RoutePreviewDecoding.requestBody(r, expectedGraphVersion: "r1-x")) as! [String: Any]
        XCTAssertEqual(Set(with.keys), ["origin", "destination", "expected_graph_version"])
        XCTAssertEqual(with["origin"] as? [String: Double], ["lat": quay.latitude, "lng": quay.longitude])
        let without = try JSONSerialization.jsonObject(with: RoutePreviewDecoding.requestBody(r, expectedGraphVersion: nil)) as! [String: Any]
        XCTAssertEqual(Set(without.keys), ["origin", "destination"], "the service rejects unknown or null fields")
    }

    func testEveryExampleErrorMapsToAPlainFailure() throws {
        let all = try JSONSerialization.jsonObject(with: example("errors.json")) as! [String: Any]
        let status = ["outside_coverage": 422, "too_far_from_network": 422, "no_eligible_footpath_nearby": 422, "same_location": 422,
                      "no_route": 404, "snapshot_changed": 409, "invalid_request": 400, "data_unavailable": 503, "busy": 503, "route_timeout": 504]
        for (name, body) in all {
            let data = try JSONSerialization.data(withJSONObject: body)
            let code = ((body as! [String: Any])["error"] as! [String: Any])["code"] as! String
            let f = RoutePreviewDecoding.failure(status: status[code] ?? 400, data: data)
            switch code {
            case "snapshot_changed": XCTAssertEqual(f, .snapshotChanged)
            case "invalid_request": XCTAssertEqual(f, .planning(.serviceError))
            default:
                guard case .planning(let e) = f else { return XCTFail(name) }
                XCTAssertNotEqual(e, .serviceError, "\(name) should map to its own message")
            }
        }
        let d = RoutePreviewDecoding.failure(status: 404, data: try JSONSerialization.data(withJSONObject: all["no_route_disconnected"]!))
        XCTAssertEqual(d, .planning(.noRoute(.disconnected, originIsolated: true, destinationIsolated: false)))
    }

    func testBarrierExclusionsKeepTheirReasons() {
        let body = #"{"api_version":"maply-route-preview/1","error":{"code":"no_eligible_footpath_nearby","message":"x","details":{"which":"destination","max_m":40,"excluded_nearby":1,"excluded_reasons":["fixed_barrier_flagged","footpath_absent"]}}}"#
        XCTAssertEqual(RoutePreviewDecoding.failure(status: 422, data: Data(body.utf8)),
                       .planning(.noEligibleFootpathNearby(.destination, reasons: [.fixedBarrierFlagged, .footpathAbsent])))
    }
}

// MARK: HTTP client

final class MaplyRouteServiceTests: XCTestCase {
    private let request = RoutePlanRequest(origin: TripEndpoint(coordinate: quay, label: "A", source: .searchedPlace),
                                           destination: TripEndpoint(coordinate: townHall, label: "B", source: .searchedPlace))
    private let url = URL(string: "http://127.0.0.1:8765")!

    func testAChangedSnapshotReloadsCoverageAndAsksOnceMore() async throws {
        let changed = try JSONSerialization.data(withJSONObject: (JSONSerialization.jsonObject(with: example("errors.json")) as! [String: Any])["snapshot_changed"]!)
        let t = ScriptedTransport([{ (changed, 409) }, { (try example("coverage.json"), 200) }, { (try example("route-preview.json"), 200) }])
        let p = try await MaplyRouteService(baseURL: url, transport: t).plan(request)
        XCTAssertEqual(p.routeMetres, 130)
        XCTAssertEqual(t.sent.map { $0.url!.path() }, ["/v1/route-preview", "/v1/coverage", "/v1/route-preview"])
        let retried = try JSONSerialization.jsonObject(with: t.sent[2].httpBody!) as! [String: Any]
        XCTAssertEqual(retried["expected_graph_version"] as? String, try RoutePreviewDecoding.coverage(example("coverage.json")).graphVersion)
    }

    func testOnlyOneRetryOnAChangedSnapshot() async throws {
        let changed = try JSONSerialization.data(withJSONObject: (JSONSerialization.jsonObject(with: example("errors.json")) as! [String: Any])["snapshot_changed"]!)
        let t = ScriptedTransport([{ (changed, 409) }, { (try example("coverage.json"), 200) }, { (changed, 409) }])
        do { _ = try await MaplyRouteService(baseURL: url, transport: t).plan(request); XCTFail() }
        catch { XCTAssertEqual(error as? RoutePlanningError, .serviceError) }
    }

    func testOfflineCancelledAndLoadingAreDistinct() async throws {
        let s1 = MaplyRouteService(baseURL: url, transport: ScriptedTransport([{ throw URLError(.notConnectedToInternet) }]))
        do { _ = try await s1.plan(request); XCTFail() } catch { XCTAssertEqual(error as? RoutePlanningError, .unavailable) }

        let s2 = MaplyRouteService(baseURL: url, transport: ScriptedTransport([{ throw URLError(.cancelled) }]))
        do { _ = try await s2.plan(request); XCTFail() } catch { XCTAssertTrue(error is CancellationError) }

        let loading = try JSONSerialization.data(withJSONObject: (JSONSerialization.jsonObject(with: example("errors.json")) as! [String: Any])["data_unavailable"]!)
        let s3 = MaplyRouteService(baseURL: url, transport: ScriptedTransport([{ (loading, 503) }]))
        do { _ = try await s3.plan(request); XCTFail() } catch { XCTAssertEqual(error as? RoutePlanningError, .loading) }
    }

    func testOnlyLoopbackHTTPIsAcceptedForLocalTesting() {
        XCTAssertNotNil(RouteServiceConfiguration.loopbackURL("http://127.0.0.1:8765"))
        XCTAssertNotNil(RouteServiceConfiguration.loopbackURL("http://localhost:8765"))
        XCTAssertNil(RouteServiceConfiguration.loopbackURL("http://192.168.1.20:8765"))
        XCTAssertNil(RouteServiceConfiguration.loopbackURL("https://example.com"))
        XCTAssertNil(RouteServiceConfiguration.loopbackURL("off"))
    }

    func testDebugBuildsUseTheLocalServiceUnlessTurnedOff() async {
        let local = RouteServiceConfiguration.makeClient(environment: [:], deployed: nil, build: .init(debug: true, runsOnThisMac: true))
        XCTAssertTrue(local is MaplyRouteService)
        if let s = local as? MaplyRouteService { let base = await s.baseURL; XCTAssertEqual(base.absoluteString, "http://127.0.0.1:8765") }
        XCTAssertTrue(RouteServiceConfiguration.makeClient(environment: ["PP_ROUTE_API": "off"], deployed: nil, build: .init(debug: true, runsOnThisMac: true)) is NotConnectedRoutePlanningClient)
    }

    private let railway = URL(string: "https://atlas-api.up.railway.app")!
    private typealias C = RouteServiceConfiguration

    func testEveryBuildPicksItsEndpoint() {
        let mac = C.Build(debug: true, runsOnThisMac: true), phone = C.Build(debug: true, runsOnThisMac: false)
        let release = C.Build(debug: false, runsOnThisMac: false), releaseMac = C.Build(debug: false, runsOnThisMac: true)
        let loop = URL(string: "http://127.0.0.1:8765")!
        // Simulator and Mac keep the local service by default, even when a deployed URL exists.
        XCTAssertEqual(C.endpoint(environment: [:], deployed: railway, build: mac), .local(loop))
        // A debug build on a phone uses the deployed service: 127.0.0.1 there is the phone itself.
        XCTAssertEqual(C.endpoint(environment: [:], deployed: railway, build: phone), .deployed(railway))
        XCTAssertEqual(C.endpoint(environment: [:], deployed: nil, build: phone), .none(.noDeployedURL))
        // Release uses only the deployed service and ignores every debug override.
        XCTAssertEqual(C.endpoint(environment: [:], deployed: railway, build: release), .deployed(railway))
        XCTAssertEqual(C.endpoint(environment: [:], deployed: railway, build: releaseMac), .deployed(railway))
        XCTAssertEqual(C.endpoint(environment: [:], deployed: nil, build: release), .none(.noDeployedURL))
        XCTAssertEqual(C.endpoint(environment: ["PP_ROUTE_API": "http://127.0.0.1:8765", "PP_ROUTE_SAMPLE": "1"], deployed: railway, build: release), .deployed(railway))
    }

    func testExplicitOverridesAreHonouredOrRefusedNeverSwapped() {
        let mac = C.Build(debug: true, runsOnThisMac: true), phone = C.Build(debug: true, runsOnThisMac: false)
        XCTAssertEqual(C.endpoint(environment: ["PP_ROUTE_API": "http://localhost:9000"], deployed: railway, build: phone), .local(URL(string: "http://localhost:9000")!))
        XCTAssertEqual(C.endpoint(environment: ["PP_ROUTE_API": "https://staging.example.org"], deployed: railway, build: mac), .deployed(URL(string: "https://staging.example.org")!))
        XCTAssertEqual(C.endpoint(environment: ["PP_ROUTE_API": "off"], deployed: railway, build: phone), .none(.turnedOff))
        for bad in ["http://atlas-api.up.railway.app", "http://192.168.1.20:8765", "ftp://x.example.org", "railway", "https://"] {
            XCTAssertEqual(C.endpoint(environment: ["PP_ROUTE_API": bad], deployed: railway, build: mac), .none(.invalidOverride), bad)
        }
        XCTAssertEqual(C.endpoint(environment: ["PP_ROUTE_SAMPLE": "1"], deployed: railway, build: phone), .sample)
    }

    func testOnlyAPlainHTTPSBaseURLCountsAsDeployed() {
        XCTAssertEqual(C.deployedURL("https://atlas-api.up.railway.app"), railway)
        XCTAssertEqual(C.deployedURL(" https://Atlas-API.up.railway.app/ \n"), railway, "trimmed, lower-cased, trailing slash dropped")
        XCTAssertEqual(C.deployedURL("https://atlas.example.org:8443")?.absoluteString, "https://atlas.example.org:8443")
        for bad: String? in [nil, "", "$(MAPLY_ROUTE_API_URL)", "http://atlas-api.up.railway.app", "https://localhost", "https://127.0.0.1",
                             "https://user:pw@atlas.example.org", "https://atlas.example.org/v1", "https://atlas.example.org/?key=1",
                             "https://atlas.example.org/#x", "https://intranet", "atlas-api.up.railway.app"] {
            XCTAssertNil(C.deployedURL(bad), bad ?? "nil")
        }
    }

    func testTheDeployedURLKeyIsInTheBuiltInfoPlist() {
        // Present in this (Debug) build's plist, from MAPLY_ROUTE_API_URL. Empty until deployment.
        XCTAssertNotNil(Bundle.main.object(forInfoDictionaryKey: C.infoKey) as? String)
    }

    func testRateLimitingAndGatewayPagesUseExistingStates() {
        XCTAssertEqual(RoutePreviewDecoding.failure(status: 429, data: Data("Too Many Requests".utf8)), .planning(.busy))
        let envelope = #"{"api_version":"maply-route-preview/1","error":{"code":"rate_limited","message":"x"}}"#
        XCTAssertEqual(RoutePreviewDecoding.failure(status: 429, data: Data(envelope.utf8)), .planning(.busy))
        for gateway in [502, 503, 504] {
            XCTAssertEqual(RoutePreviewDecoding.failure(status: gateway, data: Data("<html>Application failed to respond</html>".utf8)), .planning(.unavailable))
        }
        XCTAssertEqual(RoutePreviewDecoding.failure(status: 500, data: Data()), .planning(.serviceError))
        XCTAssertTrue(TripCopy.canRetry(.busy))
    }

    func testRequestsWaitLongEnoughForTheHostToWakeButNotForever() async throws {
        XCTAssertEqual(MaplyRouteService.requestTimeout, 120)
        let t = ScriptedTransport([{ (try example("coverage.json"), 200) }, { (try example("route-preview.json"), 200) }])
        let s = MaplyRouteService(baseURL: railway, transport: t)
        _ = try await s.coverage()
        _ = try await s.plan(RoutePlanRequest(origin: TripEndpoint(coordinate: quay, label: "A", source: .searchedPlace),
                                              destination: TripEndpoint(coordinate: townHall, label: "B", source: .searchedPlace)))
        XCTAssertEqual(t.sent.map(\.timeoutInterval), [120, 120], "both coverage and route requests")
        XCTAssertEqual(t.sent.count, 2, "one request each: no retry loop")
    }

    func testATimedOutRequestIsUnavailableNotRetried() async {
        let t = ScriptedTransport([{ throw URLError(.timedOut) }])
        do { _ = try await MaplyRouteService(baseURL: railway, transport: t).coverage(); XCTFail() }
        catch { XCTAssertEqual(error as? RoutePlanningError, .unavailable) }
        XCTAssertEqual(t.sent.count, 1)
    }

    func testARateLimitedCoverageRequestFailsSoTheCoreLimitsStillShow() async {
        let t = ScriptedTransport([{ (Data("slow down".utf8), 429) }])
        do { _ = try await MaplyRouteService(baseURL: railway, transport: t).coverage(); XCTFail() }
        catch { XCTAssertEqual(error as? RoutePlanningError, .busy) }
        XCTAssertEqual(t.sent.first?.url?.absoluteString, "https://atlas-api.up.railway.app/v1/coverage")
    }
}

// MARK: Summary and wording

final class RouteSummaryTests: XCTestCase {
    func testServiceWarningsAreGroupedProminentFirstWithEveryInstanceKept() {
        let legs = [RouteLeg(id: 1, kind: .footpath, coordinates: [], streetName: "Pitt Street")]
        let p = plan(legs: legs, warnings: [
            warning(0, "endpoint_gap_not_assessed", .info),
            warning(1, "crossing_unverified", .prominent, leg: 2),
            warning(2, "steep_slope_observed", .prominent, leg: 1, on: "2021-03"),
            warning(3, "crossing_unverified", .prominent, leg: 4),
        ])
        let g = RouteSummary.groups(for: p, origin: nil, destinationName: "Town Hall")
        XCTAssertEqual(g.map(\.code), ["steep_slope_observed", "crossing_unverified", "endpoint_gap_not_assessed", "entrance_not_checked"])
        XCTAssertEqual(g[1].items.count, 2)
        XCTAssertEqual(g[1].summary, "2 crossings")
        XCTAssertEqual(g[0].items[0].context?.hasPrefix("Pitt Street · Photo from "), true)
        XCTAssertTrue(g[0].items[0].context?.hasSuffix("2021") == true)
    }

    func testMissingSlopeTagsAreInformationNeverSilence() {
        let p = plan(warnings: [warning(0, "crossing_unverified", .prominent), warning(1, "endpoint_gap_not_assessed", .info)],
                     observations: 9, slope: SlopeObservations(observationsWithoutSlope: 8))
        let g = RouteSummary.groups(for: p, origin: nil, destinationName: "X")
        let slope = g.first { $0.code == "slope_not_recorded" }
        XCTAssertEqual(slope?.severity, .info, "slopes never change the route, so this informs rather than warns")
        XCTAssertEqual(slope?.items.first?.message, "8 of 9 survey photos along this route have no slope tag. Slope there is unknown, not level.")
        XCTAssertEqual(g.map(\.code), ["crossing_unverified", "endpoint_gap_not_assessed", "slope_not_recorded", "entrance_not_checked"])
        let none = RouteSummary.groups(for: plan(observations: 0), origin: nil, destinationName: "X")
        XCTAssertNotNil(none.first { $0.code == "slope_not_recorded" })
        let tagged = RouteSummary.groups(for: plan(observations: 2, slope: SlopeObservations(observationsWithoutSlope: 0)), origin: nil, destinationName: "X")
        XCTAssertNil(tagged.first { $0.code == "slope_not_recorded" })
    }

    func testBlockagesRankFirstWhereverTheyAreOnTheRoute() {
        let p = plan(warnings: [
            warning(0, "footpath_unknown", .prominent, leg: 1),
            warning(1, "crossing_unverified", .prominent, leg: 2),
            warning(2, "connector_inferred", .prominent),
            warning(3, "steep_slope_observed", .prominent),
            warning(4, "temporary_obstruction_observed", .prominent),
            warning(5, "blocking_permanence_unknown", .prominent),     // last on the route
            warning(6, "something_new", .prominent),
        ])
        let codes = RouteSummary.groups(for: p, origin: TripEndpoint(coordinate: quay, label: "", source: .simulatedCharacter), destinationName: "X").map(\.code)
        XCTAssertEqual(Array(codes.prefix(8)), ["simulated_start", "blocking_permanence_unknown", "temporary_obstruction_observed", "steep_slope_observed",
                                                "something_new", "footpath_unknown", "connector_inferred", "crossing_unverified"])
    }

    func testTheLargestUnsurveyedStretchIsShownFirstWithTheTotal() {
        let legs = [RouteLeg(id: 1, kind: .footpath, coordinates: [], streetName: "Bent Street", evidence: EvidenceMetres(observed: 88, unknown: 1)),
                    RouteLeg(id: 3, kind: .footpath, coordinates: [], streetName: "Young Street", evidence: EvidenceMetres(observed: 99, unknown: 49))]
        let p = plan(legs: legs, warnings: [warning(0, "footpath_unknown", .prominent, leg: 1, "1 m of this footpath has no observation."),
                                            warning(1, "footpath_unknown", .prominent, leg: 3, "49 m of this footpath has no observation.")])
        let g = RouteSummary.groups(for: p, origin: nil, destinationName: "X").first { $0.code == "footpath_unknown" }!
        XCTAssertEqual(g.items.first?.message, "49 m of this footpath has no observation.")
        XCTAssertEqual(g.summary, "50 m in 2 places")
        XCTAssertEqual(TripCopy.groupHeading(g), "Footpath not surveyed · 50 m in 2 places")
    }

    func testATruncatedWarningListSaysSoFirst() {
        var p = plan(warnings: [warning(0, "blocking_permanence_unknown", .prominent)])
        p.warningsTruncated = true
        let g = RouteSummary.groups(for: p, origin: nil, destinationName: "X")
        XCTAssertEqual(g.first?.code, "warnings_truncated")
        XCTAssertEqual(g.first?.severity, .prominent)
    }

    func testGapWordingSaysStartNotOrigin() {
        let legs = [RouteLeg(id: 0, kind: .endpointGap, coordinates: [], lengthMetres: 5, gapEnd: .origin)]
        let p = plan(legs: legs, warnings: [warning(0, "endpoint_gap_not_assessed", .info, leg: 0, "The 5 m between the origin and the footpath is not assessed.")])
        let g = RouteSummary.groups(for: p, origin: nil, destinationName: "X").first { $0.code == "endpoint_gap_not_assessed" }!
        XCTAssertEqual(g.items.first?.message, "The 5 m between the start and the footpath isn't assessed.")
    }

    func testMixedLegsAreMixedWithNoThreshold() {
        XCTAssertEqual(EvidenceMetres(observed: 99, unknown: 1).style, .mixed(leastKnown: .unknown))
        XCTAssertEqual(EvidenceMetres(observed: 1, unknown: 99).style, .mixed(leastKnown: .unknown), "1% observed is still partly observed")
        XCTAssertEqual(EvidenceMetres(observed: 40, uncertain: 2).style, .mixed(leastKnown: .uncertain))
        XCTAssertEqual(EvidenceMetres(observed: 40).style, .whole(.observed))
        XCTAssertEqual(EvidenceMetres(unknown: 5).style, .whole(.unknown))
        XCTAssertEqual(EvidenceMetres().style, .whole(.unknown), "nothing reported is never shown as observed")
    }

    func testCrossingsAreNumberedAndNeverInstructions() {
        let legs = [RouteLeg(id: 0, kind: .footpath, coordinates: [], designation: .pedestrian),
                    RouteLeg(id: 1, kind: .crossing, coordinates: [], streetName: "Hunter Street"),
                    RouteLeg(id: 2, kind: .footpath, coordinates: [], streetName: "Bent Street"),
                    RouteLeg(id: 3, kind: .sharedSurfaceTransition, coordinates: [], streetName: "Loftus Lane")]
        let p = plan(legs: legs)
        XCTAssertEqual(p.junctionNumbers, [1: 1, 3: 2])
        XCTAssertEqual(TripCopy.legTitle(legs[1], number: 1), "1. Crossing of Hunter Street")
        XCTAssertEqual(TripCopy.legTitle(legs[0]), "Pedestrian street")
        XCTAssertFalse(legs.map { TripCopy.legTitle($0) }.contains { $0.hasPrefix("Cross ") })
        XCTAssertFalse(TripCopy.legAccessibility(legs[1], number: 1).contains("·"))
    }

    func testAtAGlanceStatesTheThreeKindsOfEvidenceInMetres() {
        var p = plan(); p.evidence = EvidenceMetres(observed: 660, uncertain: 39, unknown: 96); p.crossings = 4; p.sharedSurfaceTransitions = 1
        let f = Dictionary(uniqueKeysWithValues: RouteSummary.facts(p).map { ($0.label, $0.value) })
        XCTAssertEqual(f[TripCopy.evidence(.observed)], "660 m")
        XCTAssertEqual(f[TripCopy.evidence(.uncertain)], "39 m")
        XCTAssertEqual(f[TripCopy.evidence(.unknown)], "96 m")
        XCTAssertEqual(f["Crossings not checked"], "5")
    }

    func testLimitsNeverVanishWhenCoverageIsMissing() throws {
        let p = plan(limits: ["preview_only", "connectors_inferred"])
        let none = RouteSummary.limitLines(p, coverage: nil)
        XCTAssertEqual(none.source, .fallback)
        XCTAssertEqual(none.lines.first, TripCopy.fallbackLimit("preview_only"))
        XCTAssertTrue(none.lines.contains(TripCopy.fallbackLimit("connectors_inferred")))
        XCTAssertGreaterThanOrEqual(none.lines.count, TripCopy.coreLimitCodes.count, "every core limit is always shown")
        let empty = RouteSummary.limitLines(plan(limits: []), coverage: nil)
        XCTAssertEqual(empty.lines.count, TripCopy.coreLimitCodes.count)
        let c = try RoutePreviewDecoding.coverage(example("coverage.json"))
        let loaded = RouteSummary.limitLines(p, coverage: c)
        XCTAssertEqual(loaded.source, .service)
        XCTAssertEqual(loaded.lines.first, c.limitMessages["preview_only"])
    }

    func testTheArrivalAnnouncementCountsKindsOfWarning() {
        let p = plan(warnings: [warning(0, "crossing_unverified", .prominent), warning(1, "crossing_unverified", .prominent)])
        let a = RouteSummary.announcement(for: p, groups: RouteSummary.groups(for: p, origin: nil, destinationName: "X"))
        XCTAssertTrue(a.hasSuffix("1 kind of warning to check."), a)
    }

    func testOneProminentInstanceMakesTheGroupProminent() {
        let g = RouteSummary.groups(for: plan(warnings: [warning(0, "flagged_observation", .info), warning(1, "flagged_observation", .prominent)]),
                                    origin: nil, destinationName: "X")
        XCTAssertEqual(g.first { $0.code == "flagged_observation" }?.severity, .prominent)
    }

    func testBlockagesAreDescribedOnlyAsTheSurveyRecordedThem() {
        XCTAssertEqual(TripCopy.warningHeading("blocking_permanence_unknown"), "Blockage in a survey photo, may be fixed or temporary")
        XCTAssertFalse(TripCopy.warningHeading("blocking_permanence_unknown").hasPrefix("Temporary"))
        XCTAssertTrue(TripCopy.warningHeading("temporary_obstruction_observed").hasPrefix("Temporary"))
        let flagged = TripCopy.exclusion(.fixedBarrierFlagged)
        XCTAssertTrue(flagged.contains("flagged for re-checking") && flagged.contains("stays left out"))
        XCTAssertFalse(TripCopy.exclusion(.fixedBarrier).contains("re-check"))
        XCTAssertTrue(TripCopy.planningFailure(.noEligibleFootpathNearby(.destination, reasons: [.fixedBarrierFlagged])).hasPrefix("The footpath next to the destination is left out"))
    }

    func testAnIsolatedPlazaIsExplainedNotJustRefused() {
        let t = TripCopy.planningFailure(.noRoute(.disconnected, originIsolated: false, destinationIsolated: true))
        XCTAssertTrue(t.hasPrefix("The destination is on a small stretch of surveyed footpath"), t)
        XCTAssertTrue(t.hasSuffix("That's a gap in the survey data, not a finding about the place itself."), t)
    }

    func testCrossingContextIsNeverAVerdict() {
        let t = TripCopy.nearby(NearbyCrossingObservations(kerbRampPresent: 2))!
        XCTAssertTrue(t.hasSuffix("They may not be at this crossing."))
        XCTAssertNil(TripCopy.nearby(NearbyCrossingObservations()))
    }

    func testDistancesAndMonths() {
        XCTAssertEqual(TripCopy.distance(0.4), "under 1 m")
        XCTAssertEqual(TripCopy.distance(37.4), "37 m")
        XCTAssertEqual(TripCopy.distance(344), "340 m")
        XCTAssertEqual(TripCopy.distance(1420), String(format: "%.1f km", 1.42))
        XCTAssertTrue(TripCopy.month("2024-03")?.hasSuffix("2024") == true)
        XCTAssertEqual(TripCopy.month("2024"), "2024")
        XCTAssertNil(TripCopy.month("soon"))
    }

    /// Every sentence the app itself writes for the trip screens, across all cases.
    private var allCopy: [String] {
        let codes = ["crossing_unverified", "shared_surface_transition_unverified", "connector_inferred", "footpath_unknown",
                     "steep_slope_observed", "temporary_obstruction_observed", "blocking_permanence_unknown", "flagged_observation",
                     "narrowed_passage", "footpath_interrupted", "missing_surface_observed", "driveway_lip_or_channel",
                     "endpoint_side_ambiguous", "endpoint_gap_not_assessed", "hazard_observed", "surface_concern", "driveway_dips",
                     "driveway_tilts_sideways", "driveway_interrupted", "no_separate_footpath", "slope_not_recorded",
                     "simulated_start", "entrance_not_checked", "something_new"]
        var s = [TripCopy.title, TripCopy.previewNotice, TripCopy.characterDetail, TripCopy.sendsNote, TripCopy.sampleBanner,
                 TripCopy.previewLine, TripCopy.overviewHint, TripCopy.overviewOffset, TripCopy.slopeCaveat, TripCopy.simulatedStart,
                 TripCopy.widthNotMeasured, TripCopy.warningsTruncated, TripCopy.slopeUnreadFeatures, TripCopy.coverageApproximate,
                 TripCopy.legendInferred, TripCopy.legendCrossing, TripCopy.entranceNotChecked("Town Hall"),
                 TripCopy.outsideArea(.origin, area: nil), TripCopy.outsideArea(.destination, area: "City of Sydney"),
                 TripCopy.previewNoticeShort, TripCopy.legendMixed, TripCopy.mixedRule, TripCopy.startChanged, TripCopy.limitsUnavailable,
                 TripCopy.limitsNote, TripCopy.coverageEdge, TripCopy.gapNotAssessed(.origin, 5), TripCopy.gapNotAssessed(.destination, 2),
                 TripCopy.plannedFrom(TripEndpoint(coordinate: quay, label: "Central", source: .searchedPlace)),
                 TripCopy.plannedFrom(TripEndpoint(coordinate: quay, label: "", source: .device(accuracy: 5))),
                 TripCopy.plannedFrom(TripEndpoint(coordinate: quay, label: "", source: .simulatedCharacter))]
        s += (TripCopy.coreLimitCodes + ["connectors_inferred", "no_footway_network", "no_grade_separation", "approximate_boundary", "new"]).map(TripCopy.fallbackLimit)
        s += codes.map(TripCopy.warningHeading)
        s += Evidence.allCases.flatMap { [TripCopy.evidence($0), TripCopy.legend($0)] }
        s += [MyLocationStatus.off, .finding, .stale, .rough(accuracy: 90), .ready(fix())].map(TripCopy.myLocationDetail)
        s += [PlaceSearchError.busy, .offline, .notFound].map(TripCopy.searchFailure)
        let reasons: [ExclusionReason] = [.fixedBarrier, .fixedBarrierFlagged, .footpathAbsent, .footpathEnds, .other]
        s += reasons.map(TripCopy.exclusion)
        let errors: [RoutePlanningError] = [.notConnected, .outsideCoverage(nil), .outsideCoverage(.origin), .tooFarFromNetwork(.destination, maxMetres: 40),
                                            .noEligibleFootpathNearby(.origin, reasons: reasons), .sameLocation,
                                            .noRoute(.disconnected, originIsolated: true, destinationIsolated: true),
                                            .noRoute(.blockedByExclusions, originIsolated: false, destinationIsolated: false),
                                            .noRoute(.other, originIsolated: false, destinationIsolated: false),
                                            .busy, .loading, .timedOut, .unavailable, .serviceError, .invalidResponse, .startNotAvailable]
        s += errors.map(TripCopy.planningFailure)
        let legs: [RouteLeg] = [
            RouteLeg(id: 0, kind: .footpath, coordinates: [], lengthMetres: 50, streetName: "A St", facing: "east", designation: .shared,
                     evidence: EvidenceMetres(observed: 20, uncertain: 10, unknown: 20)),
            RouteLeg(id: 1, kind: .crossing, coordinates: [], streetName: "B St"),
            RouteLeg(id: 2, kind: .sharedSurfaceTransition, coordinates: []),
            RouteLeg(id: 3, kind: .connector, coordinates: [], lengthMetres: 12),
            RouteLeg(id: 4, kind: .endpointGap, coordinates: [], lengthMetres: 8, gapEnd: .destination),
        ]
        s += legs.flatMap { [TripCopy.legTitle($0), TripCopy.legDetail($0)] }
        s += TripCopy.designation(DesignationMetres(ordinary: 1, pedestrian: 2, shared: 3, ambiguous: 4))
        s.append(TripCopy.nearby(NearbyCrossingObservations(kerbRampPresent: 1, kerbRampAbsent: 1, kerbRampLipOrStep: 1,
                                                            crossingUnmodifiedKerb: 1, crossingSteps: 1, crossingLevelOrRamp: 1))!)
        var p = plan(); p.crossings = 2
        s += [TripCopy.measuredNote(p), TripCopy.slopeNotRecorded(SlopeObservations(observationsWithoutSlope: 3), observations: 4),
              TripCopy.slopeNotRecorded(SlopeObservations(), observations: 0), TripCopy.observations(9, flagged: 1),
              TripCopy.imageryRange(MonthRange(from: "2019-06", to: "2024-03")), TripCopy.imageryRange(nil)]
        s += SlopeObservations.Category.allCases.map { TripCopy.slopeEntry(.init(feature: .kerbRamp, axis: .cross, category: $0, count: 1)) }
        s.append(RouteSummary.announcement(for: p, groups: RouteSummary.groups(for: p, origin: nil, destinationName: "X")))
        return s
    }

    func testNoWordingClaimsARouteIsSafeOrChecked() {
        let banned = ["safe", "accessib", "step-free", "step free", "verif", "navigable", "friendly", "guarantee", "barrier-free", "no barriers", "clear route"]
        for text in allCopy {
            for word in banned { XCTAssertFalse(text.lowercased().contains(word), "“\(text)” contains “\(word)”") }
        }
    }

    func testSlopeWordingNeverGivesALengthGradientOrSlopeCount() {
        let texts = [TripCopy.slopeCaveat, TripCopy.slopeNotRecorded(SlopeObservations(observationsWithoutSlope: 3), observations: 4)]
            + SlopeObservations.Category.allCases.map { TripCopy.slopeEntry(.init(feature: .footpath, axis: .longitudinal, category: $0, count: 3)) }
        for text in texts {
            XCTAssertNil(text.range(of: #"\d+(\.\d+)?\s?(m|km|metres?)\b"#, options: .regularExpression), text)
            XCTAssertFalse(text.contains("%"), text)
            XCTAssertNil(text.range(of: #"\d+ (steep |moderate |gentle )?slopes?\b"#, options: .regularExpression), "counts slopes: \(text)")
        }
    }

    #if DEBUG
    func testSampleClientIsMarkedAsSample() async throws {
        let p = try await SampleRoutePlanningClient().plan(RoutePlanRequest(
            origin: TripEndpoint(coordinate: quay, label: "A", source: .searchedPlace),
            destination: TripEndpoint(coordinate: townHall, label: "B", source: .searchedPlace)))
        XCTAssertTrue(p.isSample)
        XCTAssertEqual(p.readiness, .planningPreview)
    }
    #endif
}
