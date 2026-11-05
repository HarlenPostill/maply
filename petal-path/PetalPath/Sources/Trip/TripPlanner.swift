import Foundation
import MapKit

/// State behind the trip sheet: search, the chosen start and destination, and the plan the route
/// service returned. It holds no routing logic.
@MainActor
final class TripPlanner: ObservableObject {
    enum Field: Equatable { case destination, start }
    enum SearchStatus: Equatable { case idle, searching, done, failed(PlaceSearchError) }
    enum Phase: Equatable {
        case choosing
        case planning
        case preview(RoutePlan)
        case failed(RoutePlanningError)
    }
    enum CoverageState: Equatable { case idle, loading, loaded, failed }

    @Published var isOpen = false
    /// The field search is filling in, or nil when not searching. Changing it ends any lookup
    /// still running for the old field.
    @Published private(set) var editing: Field?
    @Published var query = "" { didSet { if query != oldValue { scheduleSearch() } } }
    @Published private(set) var suggestions: [PlaceSuggestion] = []
    @Published private(set) var searchStatus = SearchStatus.idle
    @Published private(set) var resolving: PlaceSuggestion.ID?
    @Published private(set) var destination: Place?
    @Published private(set) var startPlace: Place?
    @Published private(set) var originChoice: OriginChoice?
    @Published private(set) var phase = Phase.choosing
    /// The start the current plan or request was made from, captured when planning began.
    @Published private(set) var plannedOrigin: TripEndpoint?
    /// The surveyed area and the service's standing limits. Kept when a refresh fails.
    @Published private(set) var coverage: RouteCoverage?
    @Published private(set) var coverageState = CoverageState.idle

    private let search: PlaceSearching
    private let client: RoutePlanningClient
    private let debounce: Duration
    private let now: () -> Date
    private var searchTask: Task<Void, Never>?
    private var planTask: Task<Void, Never>?
    private var coverageTask: Task<Void, Never>?
    /// Bumped whenever editing starts, stops or switches field. A lookup from an older session is dropped.
    private var editSession = 0
    /// Bumped for every plan request or reset. A reply for an older one is dropped.
    private var planGeneration = 0
    /// Where search is biased towards. Updated from the player's position when the sheet opens.
    private(set) var searchCentre = GeoCoordinate.circularQuay

    init(search: PlaceSearching, client: RoutePlanningClient, debounce: Duration = .milliseconds(300), now: @escaping () -> Date = Date.init) {
        self.search = search; self.client = client; self.debounce = debounce; self.now = now
    }

    static func live() -> TripPlanner {
        TripPlanner(search: AppleMapsPlaceSearch(), client: RouteServiceConfiguration.makeClient())
    }

    // MARK: Opening

    func open(with snapshot: TripLocationSnapshot) {
        searchCentre = MyLocationStatus.evaluate(snapshot, now: now()).fix?.coordinate ?? snapshot.character
        positionsChanged(snapshot)
        if destination == nil { beginEditing(.destination) }
        isOpen = true
        if coverage == nil || coverageState == .failed { loadCoverage() }
    }

    func close() {
        isOpen = false
        endEditing()
    }

    // MARK: Coverage

    func loadCoverage() {
        guard coverageTask == nil else { return }
        coverageState = .loading
        coverageTask = Task { [weak self, client] in
            do {
                let c = try await client.coverage()
                guard let self else { return }
                if let c { self.coverage = c; self.coverageState = .loaded } else { self.coverageState = .idle }
            } catch {
                self?.coverageState = .failed       // any earlier coverage stays in place
            }
            self?.coverageTask = nil
        }
    }

    /// Which end, if any, is known to be outside the surveyed area. Unknown until coverage loads;
    /// the service decides either way.
    func outsideCoverage(origin: TripEndpoint?) -> EndpointEnd? {
        guard let coverage else { return nil }
        if let o = origin, !coverage.contains(o.coordinate) { return .origin }
        if let d = destination, !coverage.contains(d.coordinate) { return .destination }
        return nil
    }

    /// Search area: about 20 km around the player, as a preference rather than a limit. Whether a
    /// place is inside the surveyed area is the route service's answer, not the search's.
    var searchRegion: MKCoordinateRegion {
        MKCoordinateRegion(center: searchCentre.clCoordinate, span: MKCoordinateSpan(latitudeDelta: 0.18, longitudeDelta: 0.18))
    }

    // MARK: Start

    func myLocationStatus(_ s: TripLocationSnapshot) -> MyLocationStatus { MyLocationStatus.evaluate(s, now: now()) }

    /// The character is only offered when she is somewhere other than the real position, and is
    /// always labelled as simulated.
    func characterAvailable(_ s: TripLocationSnapshot) -> Bool { s.characterSimulated }

    func isAvailable(_ c: OriginChoice, _ s: TripLocationSnapshot) -> Bool {
        switch c {
        case .myLocation: myLocationStatus(s).fix != nil
        case .character: characterAvailable(s)
        case .place: startPlace != nil
        }
    }

    /// While following the device, only the real fix is ever picked automatically: the character
    /// can still be chosen by hand, but a simulated start is never slipped in while waiting.
    func defaultOrigin(_ s: TripLocationSnapshot) -> OriginChoice? {
        if s.followingDevice { return isAvailable(.myLocation, s) ? .myLocation : nil }
        return isAvailable(.character, s) ? .character : nil
    }

    /// Fills an empty start once one is available, and drops a character start that no longer
    /// exists. Never replaces a start the person chose with a different kind.
    func positionsChanged(_ s: TripLocationSnapshot) {
        if originChoice == .character && !characterAvailable(s) { originChoice = nil }
        if originChoice == nil, let d = defaultOrigin(s) { originChoice = d }
    }

    func chooseOrigin(_ c: OriginChoice, _ s: TripLocationSnapshot) {
        if c == .place && startPlace == nil { beginEditing(.start); return }
        guard isAvailable(c, s) else { return }
        if c != originChoice { originChoice = c; resetPlan() }
        else if case .failed = phase { resetPlan() }
    }

    /// Resolves the chosen start against the latest positions. A device fix is checked again here,
    /// so a fix that went stale while the sheet was open is never sent.
    func originEndpoint(_ s: TripLocationSnapshot) -> TripEndpoint? {
        switch originChoice {
        case .myLocation:
            guard let fix = myLocationStatus(s).fix else { return nil }
            return TripEndpoint(coordinate: fix.coordinate, label: TripCopy.myLocation, source: .device(accuracy: fix.horizontalAccuracy))
        case .character:
            guard s.characterSimulated else { return nil }
            return TripEndpoint(coordinate: s.character, label: TripCopy.characterPosition, source: .simulatedCharacter)
        case .place:
            guard let p = startPlace else { return nil }
            return TripEndpoint(coordinate: p.coordinate, label: p.name, source: .searchedPlace)
        case nil:
            return nil
        }
    }

    /// The start a shown preview was planned from no longer matches the chosen start: a different
    /// kind of start, a move of more than 25 m, or no longer available.
    func previewStartChanged(_ s: TripLocationSnapshot) -> Bool {
        guard case .preview = phase, let planned = plannedOrigin else { return false }
        guard let current = originEndpoint(s) else { return true }
        func kind(_ e: TripEndpoint) -> Int { switch e.source { case .device: 0; case .simulatedCharacter: 1; case .searchedPlace: 2 } }
        return kind(current) != kind(planned) || current.coordinate.distance(to: planned.coordinate) > 25
    }

    // MARK: Search

    func beginEditing(_ f: Field) {
        editSession += 1
        editing = f
        query = ""; suggestions = []; searchStatus = .idle; resolving = nil
    }

    /// Leaves search without changing anything that was already chosen.
    func endEditing() {
        editSession += 1
        searchTask?.cancel()
        editing = nil
        query = ""; suggestions = []; searchStatus = .idle; resolving = nil
    }

    func retrySearch() { scheduleSearch() }

    private func scheduleSearch() {
        searchTask?.cancel()
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard editing != nil, q.count >= 2 else {
            suggestions = []; searchStatus = .idle; return
        }
        searchStatus = .searching
        let region = searchRegion, delay = debounce, session = editSession
        searchTask = Task { [weak self] in
            try? await Task.sleep(for: delay)
            guard !Task.isCancelled, let self, self.editSession == session else { return }
            do {
                let found = try await self.search.suggestions(for: q, near: region)
                guard !Task.isCancelled, self.editSession == session else { return }
                self.suggestions = found; self.searchStatus = .done
            } catch is CancellationError {
            } catch {
                guard !Task.isCancelled, self.editSession == session else { return }
                self.suggestions = []
                self.searchStatus = .failed(error as? PlaceSearchError ?? .offline)
            }
        }
    }

    func select(_ s: PlaceSuggestion) async {
        guard let field = editing else { return }
        let session = editSession
        resolving = s.id
        do {
            let place = try await search.resolve(s)
            guard editSession == session, editing == field else { return }   // cancelled or moved on
            resolving = nil
            set(place, for: field)
        } catch {
            guard editSession == session else { return }
            resolving = nil
            searchStatus = .failed(error as? PlaceSearchError ?? .offline)
        }
    }

    /// Sets a place directly. Search uses this, and so does the debug destination hook.
    func set(_ place: Place, for field: Field) {
        switch field {
        case .destination:
            if destination != place { destination = place; resetPlan() }
        case .start:
            if startPlace != place || originChoice != .place { startPlace = place; originChoice = .place; resetPlan() }
        }
        endEditing()
    }

    /// Opens search for a new destination. The current one stays until a new one is chosen.
    func changeDestination() { beginEditing(.destination) }

    // MARK: Planning

    func canPlan(_ s: TripLocationSnapshot) -> Bool {
        guard destination != nil, phase != .planning, let c = originChoice else { return false }
        return isAvailable(c, s)
    }

    func plan(_ s: TripLocationSnapshot) {
        guard let destination else { return }
        guard let origin = originEndpoint(s) else {
            if originChoice == .character { originChoice = nil }
            phase = .failed(.startNotAvailable); return
        }
        let request = RoutePlanRequest(origin: origin, destination: TripEndpoint(coordinate: destination.coordinate, label: destination.name, source: .searchedPlace))
        planTask?.cancel()
        planGeneration += 1
        let generation = planGeneration
        plannedOrigin = origin
        if coverage == nil && coverageState != .loading { loadCoverage() }
        if let outside = outsideCoverage(origin: origin) {
            phase = .failed(.outsideCoverage(outside)); return
        }
        phase = .planning
        planTask = Task { [weak self, client] in
            do {
                let plan = try await client.plan(request)
                guard !Task.isCancelled, let self, self.planGeneration == generation else { return }
                self.phase = .preview(plan)
                // The service moved to new data: refresh the area and limits, keeping the old ones until then.
                if let v = plan.release.graphVersion, !plan.isSample, self.coverage?.graphVersion != v { self.loadCoverage() }
            } catch is CancellationError {
            } catch {
                guard !Task.isCancelled, let self, self.planGeneration == generation else { return }
                self.phase = .failed(error as? RoutePlanningError ?? .invalidResponse)
            }
        }
    }

    /// Back to choosing, dropping any preview or request in flight.
    func resetPlan() {
        planTask?.cancel(); planTask = nil
        planGeneration += 1
        phase = .choosing
        plannedOrigin = nil
    }

    #if DEBUG
    /// Test hooks for runs without touch, such as Mac Catalyst: `PP_TRIP=1` opens the sheet,
    /// `PP_DEST=lat,lon[,name]` sets a destination without searching, `PP_TRIP_PLAN=1` plans at once.
    func applyLaunchHooks(_ s: TripLocationSnapshot, environment: [String: String] = RouteServiceConfiguration.debugEnvironment) {
        guard environment["PP_TRIP"] != nil || environment["PP_DEST"] != nil else { return }
        if let p = environment["PP_DEST"]?.split(separator: ",", maxSplits: 2), p.count >= 2,
           let lat = Double(p[0]), let lon = Double(p[1]) {
            set(Place(name: p.count > 2 ? String(p[2]) : "Debug destination", address: nil,
                      coordinate: GeoCoordinate(latitude: lat, longitude: lon)), for: .destination)
        }
        open(with: s)
        if environment["PP_TRIP_PLAN"] != nil { plan(s) }
    }
    #endif
}

extension GameState {
    /// The positions trip planning may use, read at the moment it is asked.
    var tripLocation: TripLocationSnapshot {
        TripLocationSnapshot(followingDevice: useMyLocation, deviceFix: deviceFix,
                             character: characterCoordinate, characterSimulated: characterSimulated)
    }
}
