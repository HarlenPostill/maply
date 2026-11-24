import SwiftUI
import MapKit

/// Trip planning laid out like Apple Maps: a full-screen Apple map with the route drawn on it, and
/// a sheet that moves between search, the place card, directions and the route preview.
/// `TripPlanner` holds all the state; this only presents it.
struct TripMapScreen: View {
    @ObservedObject var planner: TripPlanner
    @ObservedObject var state: GameState
    @State private var detent = PresentationDetent.medium
    @State private var camera = MapCameraPosition.automatic

    static let collapsed = PresentationDetent.height(104)

    var body: some View {
        ZStack(alignment: .top) {
            if planner.isOpen {
                GeometryReader { geo in
                    TripMainMap(planner: planner, origin: planner.originEndpoint(state.tripLocation), camera: $camera)
                        // Keeps framed content and the map's own controls above the sheet.
                        .safeAreaPadding(.bottom, sheetInset(geo.size.height))
                        // And below the close button and the preview notice.
                        .safeAreaPadding(.top, geo.safeAreaInsets.top + 64)
                }
                .ignoresSafeArea()
                .transition(.opacity)

                HStack(alignment: .top) {
                    Button { planner.close() } label: {
                        Image(systemName: "xmark").font(.body.weight(.semibold)).frame(width: 44, height: 44)
                    }
                    .buttonStyle(.glass).buttonBorderShape(.circle)
                    .accessibilityLabel("Close trip planning").accessibilityIdentifier("tripDone")
                    Spacer()
                    Label(TripCopy.previewNoticeShort, systemImage: "info.circle.fill")
                        .font(.footnote.weight(.semibold)).lineLimit(2)
                        .padding(.horizontal, 12).padding(.vertical, 8)
                        .glassEffect(.regular, in: Capsule())
                        .accessibilityIdentifier("previewNotice")
                    Spacer()
                    Color.clear.frame(width: 44, height: 44)     // balances the close button; map controls sit below
                }
                .padding(.horizontal, 16).padding(.top, 4)
                .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.2), value: planner.isOpen)
        .environment(\.colorScheme, .light)
        .sheet(isPresented: $planner.isOpen, onDismiss: { planner.close() }) {
            TripPanel(planner: planner, state: state, detent: $detent)
                .presentationDetents([Self.collapsed, .medium, .large], selection: $detent)
                .presentationBackgroundInteraction(.enabled(upThrough: .medium))
                .presentationDragIndicator(.visible)
                .interactiveDismissDisabled()
                .environment(\.colorScheme, .light)
        }
        .onChange(of: planner.editing) { _, field in
            if field != nil { detent = .large } else if detent == .large { detent = .medium }
        }
        .onChange(of: planner.phase) { _, phase in
            if case .preview = phase { detent = .medium }
        }
    }

    private func sheetInset(_ height: CGFloat) -> CGFloat {
        switch detent {
        case .large: return 0
        case .medium: return height * 0.5
        default: return 104
        }
    }
}

// MARK: Map

/// The full-screen map. Apple's terms want Apple Maps search results shown with an Apple map: this
/// is that map, with the search area, the chosen ends and, once planned, the route preview.
struct TripMainMap: View {
    @ObservedObject var planner: TripPlanner
    var origin: TripEndpoint?
    @Binding var camera: MapCameraPosition

    private var plan: RoutePlan? { if case .preview(let p) = planner.phase { p } else { nil } }

    /// Refit when an end moves by more than about 10 m or a new route arrives, not on GPS jitter.
    private var fitKey: String {
        func r(_ c: GeoCoordinate?) -> String { c.map { String(format: "%.4f,%.4f", $0.latitude, $0.longitude) } ?? "-" }
        return "\(r(origin?.coordinate))|\(r(planner.destination?.coordinate))|\(plan?.legs.count ?? -1)|\(plan?.routeMetres ?? -1)"
    }

    var body: some View {
        Map(position: $camera) {
            CoverageShade(coverage: planner.coverage)
            if let plan { RouteLines(plan: plan) }
            UserAnnotation()
            if let origin {
                Annotation(origin.label, coordinate: origin.coordinate.clCoordinate, anchor: .center) {
                    Image(systemName: origin.source == .simulatedCharacter ? "figure.walk" : "figure.roll")
                        .font(.system(size: 13, weight: .bold)).foregroundStyle(.white)
                        .frame(width: 28, height: 28).background(Color.blue, in: Circle())
                        .overlay(Circle().stroke(.white, lineWidth: 3)).shadow(radius: 2, y: 1)
                }
            }
            if let d = planner.destination {
                Marker(d.name, coordinate: d.coordinate.clCoordinate).tint(.red)
            }
        }
        .mapStyle(.standard(elevation: .realistic, pointsOfInterest: .all))
        .mapControls {
            MapUserLocationButton()
            MapCompass()
            MapPitchToggle()
            MapScaleView()
        }
        .onAppear { refit(animated: false) }
        .onChange(of: fitKey) { refit(animated: true) }
    }

    private func refit(animated: Bool) {
        var points = [origin?.coordinate, planner.destination?.coordinate].compactMap { $0 }
        if let plan { points += plan.legs.flatMap(\.coordinates) }
        let target: MapCameraPosition = points.isEmpty
            ? .region(MKCoordinateRegion(center: planner.searchCentre.clCoordinate, latitudinalMeters: 1500, longitudinalMeters: 1500))
            : mapFraming(points, minimumMetres: 300)
        if animated { withAnimation(.easeInOut(duration: 0.6)) { camera = target } } else { camera = target }
    }
}

// MARK: Sheet

/// The sheet's contents, in Apple Maps' order: search, then the place, then directions and the route.
struct TripPanel: View {
    @ObservedObject var planner: TripPlanner
    @ObservedObject var state: GameState
    @Binding var detent: PresentationDetent
    @FocusState private var searchFocused: Bool
    @AccessibilityFocusState private var resultFocused: Bool

    private var snapshot: TripLocationSnapshot { state.tripLocation }

    var body: some View {
        ScrollViewReader { scroll in
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let field = planner.editing {
                        searchPanel(field)
                    } else if let d = planner.destination {
                        placeHeader(d)
                        directionsCard(d)
                        planArea(d).id("result")
                    } else {
                        searchButton
                        Text(TripCopy.previewNotice).font(.footnote).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.horizontal, 16).padding(.top, 18).padding(.bottom, 24)
            }
            .scrollDismissesKeyboard(.interactively)
            .onChange(of: planner.phase) { _, phase in
                switch phase {
                case .preview, .failed:
                    withAnimation(.easeOut(duration: 0.25)) { scroll.scrollTo("result", anchor: .top) }
                    announce(phase)
                    resultFocused = true
                default: break
                }
            }
        }
        .tint(.blue)
        .onChange(of: state.tripLocation) { _, s in planner.positionsChanged(s) }
        .task {
            // Ask for a current reading on open, and again while the last one is stale.
            if state.useMyLocation { state.wantsFreshFix = true }
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(8))
                if case .stale = planner.myLocationStatus(state.tripLocation) { state.wantsFreshFix = true }
            }
        }
    }

    // MARK: Search

    /// Looks like the search field and opens search, as in Apple Maps' collapsed sheet.
    private var searchButton: some View {
        Button { planner.changeDestination() } label: {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                Text(TripCopy.searchPrompt(.destination)).foregroundStyle(.secondary)
                Spacer()
            }
            .padding(.horizontal, 14).frame(minHeight: 48)
            .background(.fill.tertiary, in: Capsule())
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("chooseDestination")
    }

    private func searchPanel(_ field: TripPlanner.Field) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                HStack(spacing: 8) {
                    Image(systemName: "magnifyingglass").foregroundStyle(.secondary).accessibilityHidden(true)
                    TextField(TripCopy.searchPrompt(field), text: $planner.query)
                        .textInputAutocapitalization(.words).autocorrectionDisabled()
                        .submitLabel(.search).focused($searchFocused)
                        .onSubmit { searchFocused = false }
                        .accessibilityIdentifier("tripSearchField")
                    if !planner.query.isEmpty {
                        Button { planner.query = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary) }
                            .frame(minWidth: 32, minHeight: 44).accessibilityLabel("Clear search")
                    }
                }
                .padding(.horizontal, 14).frame(minHeight: 48)
                .background(.fill.tertiary, in: Capsule())
                if field == .start || planner.destination != nil {
                    Button("Cancel") { planner.endEditing() }
                        .frame(minHeight: 44).accessibilityIdentifier("cancelSearch")
                }
            }

            if field == .start { startOptions }

            searchStatus

            if !planner.suggestions.isEmpty {
                VStack(spacing: 0) {
                    ForEach(Array(planner.suggestions.enumerated()), id: \.element.id) { i, s in
                        if i > 0 { Divider().padding(.leading, 56) }
                        Button { Task { await planner.select(s) } } label: {
                            PanelRow(icon: "mappin", colour: .red, title: s.title, detail: s.subtitle.isEmpty ? nil : s.subtitle,
                                     busy: planner.resolving == s.id)
                        }
                        .buttonStyle(.plain)
                        .disabled(planner.resolving != nil)
                    }
                }
                .background(.background.secondary, in: RoundedRectangle(cornerRadius: 14))
                Text(TripCopy.appleMapsCredit).font(.caption).foregroundStyle(.secondary)
            }
        }
        .onAppear { searchFocused = true }
    }

    /// When choosing a start: the ready-made starts first, as Apple Maps puts My Location first.
    private var startOptions: some View {
        TimelineView(.periodic(from: .now, by: 5)) { _ in
            let s = snapshot, my = planner.myLocationStatus(s)
            VStack(spacing: 0) {
                Button {
                    planner.chooseOrigin(.myLocation, s); planner.endEditing()
                } label: {
                    PanelRow(icon: "location.fill", colour: .blue, title: TripCopy.myLocation, detail: TripCopy.myLocationDetail(my),
                             selected: planner.originChoice == .myLocation)
                }
                .buttonStyle(.plain).disabled(my.fix == nil)
                .accessibilityIdentifier("originMyLocation")
                if my == .off {
                    Divider().padding(.leading, 56)
                    Button { state.useMyLocation = true } label: {
                        PanelRow(icon: "location", colour: .gray, title: TripCopy.turnOnLocation, detail: nil)
                    }
                    .buttonStyle(.plain).accessibilityIdentifier("turnOnLocation")
                }
                if planner.characterAvailable(s) {
                    Divider().padding(.leading, 56)
                    Button {
                        planner.chooseOrigin(.character, s); planner.endEditing()
                    } label: {
                        PanelRow(icon: "figure.walk", colour: .purple, title: TripCopy.characterPosition, detail: TripCopy.characterDetail,
                                 selected: planner.originChoice == .character)
                    }
                    .buttonStyle(.plain).accessibilityIdentifier("originCharacter")
                }
                if let p = planner.startPlace {
                    Divider().padding(.leading, 56)
                    Button {
                        planner.chooseOrigin(.place, s); planner.endEditing()
                    } label: {
                        PanelRow(icon: "mappin", colour: .red, title: p.name, detail: p.address, selected: planner.originChoice == .place)
                    }
                    .buttonStyle(.plain).accessibilityIdentifier("originPlace")
                }
            }
            .background(.background.secondary, in: RoundedRectangle(cornerRadius: 14))
        }
    }

    @ViewBuilder private var searchStatus: some View {
        let q = planner.query.trimmingCharacters(in: .whitespaces)
        switch planner.searchStatus {
        case .idle:
            if q.count < 2 && planner.editing == .destination { hint(TripCopy.searchHint) }
        case .searching:
            HStack(spacing: 8) { ProgressView(); Text("Searching…") }.font(.footnote).foregroundStyle(.secondary)
                .accessibilityElement(children: .combine)
        case .done:
            if planner.suggestions.isEmpty { hint(TripCopy.noResults) }
        case .failed(let e):
            VStack(alignment: .leading, spacing: 4) {
                hint(TripCopy.searchFailure(e))
                if e != .notFound { Button(TripCopy.tryAgain) { planner.retrySearch() }.frame(minHeight: 44) }
            }
        }
    }

    private func hint(_ s: String) -> some View {
        Text(s).font(.footnote).foregroundStyle(.secondary).accessibilityIdentifier("searchStatus")
    }

    // MARK: Place and directions

    private func placeHeader(_ d: Place) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(d.name).font(.title2.bold()).lineLimit(2)
                    .accessibilityAddTraits(.isHeader)
                if let a = d.address { Text(a).font(.subheadline).foregroundStyle(.secondary) }
            }
            Spacer(minLength: 8)
            Button { planner.changeDestination() } label: {
                Image(systemName: "xmark").font(.subheadline.weight(.bold)).foregroundStyle(.secondary)
                    .frame(width: 32, height: 32).background(.fill.tertiary, in: Circle())
            }
            .frame(minWidth: 44, minHeight: 44)
            .accessibilityLabel(TripCopy.changeDestination).accessibilityIdentifier("changeDestination")
        }
    }

    /// From and To, like Apple Maps' directions card. Tapping a row changes that end.
    private func directionsCard(_ d: Place) -> some View {
        TimelineView(.periodic(from: .now, by: 5)) { _ in
            let s = snapshot
            let origin = planner.originEndpoint(s)
            VStack(spacing: 0) {
                Button { planner.beginEditing(.start) } label: {
                    PanelRow(icon: originIcon, colour: .blue, title: origin?.label ?? TripCopy.chooseStart,
                             detail: originDetail(s), caption: "From", chevron: true)
                }
                .buttonStyle(.plain).accessibilityIdentifier("changeStart")
                Divider().padding(.leading, 56)
                Button { planner.changeDestination() } label: {
                    PanelRow(icon: "mappin", colour: .red, title: d.name, detail: nil, caption: "To", chevron: true)
                }
                .buttonStyle(.plain)
            }
            .background(.background.secondary, in: RoundedRectangle(cornerRadius: 14))
        }
    }

    private var originIcon: String {
        switch planner.originChoice {
        case .myLocation: "location.fill"
        case .character: "figure.walk"
        case .place: "mappin"
        case nil: "questionmark"
        }
    }

    private func originDetail(_ s: TripLocationSnapshot) -> String? {
        switch planner.originChoice {
        case .myLocation: TripCopy.myLocationDetail(planner.myLocationStatus(s))
        case .character: TripCopy.characterDetail
        case .place: planner.startPlace?.address
        case nil: nil
        }
    }

    // MARK: Planning

    @ViewBuilder private func planArea(_ d: Place) -> some View {
        if let outside = planner.outsideCoverage(origin: planner.originEndpoint(snapshot)) {
            card {
                Label(TripCopy.outsideArea(outside, area: nil), systemImage: "map").font(.callout.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                Text(TripCopy.coverageApproximate).font(.footnote).foregroundStyle(.secondary)
                recoveryButton(for: outside)
            }
            .accessibilityIdentifier("outsideCoverage")
        }
        switch planner.phase {
        case .choosing:
            VStack(alignment: .leading, spacing: 8) {
                planButton
                Text(TripCopy.sendsNote).font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        case .planning:
            HStack(spacing: 10) { ProgressView(); Text(TripCopy.planning) }
                .font(.callout).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, minHeight: 50)
                .accessibilityElement(children: .combine)
        case .failed(let e):
            card {
                Label(TripCopy.planningFailure(e), systemImage: "exclamationmark.triangle.fill")
                    .font(.callout.weight(.semibold)).symbolRenderingMode(.multicolor)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityFocused($resultFocused)
                    .accessibilityIdentifier("planningFailure")
                failureActions(e)
            }
        case .preview(let plan):
            routeCard(plan)
            RouteSummaryView(plan: plan, origin: planner.plannedOrigin, destination: d, coverage: planner.coverage,
                             coverageState: planner.coverageState, startChanged: planner.previewStartChanged(snapshot),
                             resultFocus: $resultFocused, planAgain: { planner.plan(snapshot) },
                             retryCoverage: { planner.loadCoverage() }, showsOverviewMap: false)
                .padding(.top, 4)
        }
    }

    private var planButton: some View {
        Button { planner.plan(snapshot) } label: {
            Label(TripCopy.planButton, systemImage: "figure.roll")
                .font(.headline).frame(maxWidth: .infinity, minHeight: 50)
        }
        .buttonStyle(.borderedProminent).buttonBorderShape(.capsule)
        .disabled(!planner.canPlan(snapshot) || planner.outsideCoverage(origin: planner.originEndpoint(snapshot)) != nil)
        .accessibilityIdentifier("planRoute")
    }

    /// The selected route, as Apple Maps shows one: the big number first, the rest underneath.
    private func routeCard(_ plan: RoutePlan) -> some View {
        let checks = plan.warnings.filter { $0.severity == .prominent }.count
        return HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text(TripCopy.distance(plan.routeMetres)).font(.largeTitle.bold()).monospacedDigit()
                Text(routeCardLine(plan, checks: checks)).font(.subheadline).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            Image(systemName: "figure.roll").font(.title).foregroundStyle(.white)
                .frame(width: 56, height: 56).background(Color.blue, in: Circle())
                .accessibilityHidden(true)
        }
        .padding(16)
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(Color.blue, lineWidth: 2))
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("routeCard")
    }

    private func routeCardLine(_ plan: RoutePlan, checks: Int) -> String {
        var parts: [String] = []
        let crossings = plan.crossings + plan.sharedSurfaceTransitions
        parts.append(crossings == 1 ? "1 crossing" : "\(crossings) crossings")
        if checks > 0 { parts.append(checks == 1 ? "1 thing to check" : "\(checks) things to check") }
        parts.append("Preview only")
        return parts.joined(separator: " · ")
    }

    /// Every failure says what to change, at the explanation.
    @ViewBuilder private func failureActions(_ e: RoutePlanningError) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            switch e {
            case .outsideCoverage(let w), .tooFarFromNetwork(let w, _), .noEligibleFootpathNearby(let w, _):
                recoveryButton(for: w ?? .destination)
            case .noRoute(_, let o, let d):
                if o { recoveryButton(for: .origin) }
                if d || !o { recoveryButton(for: .destination) }
            case .sameLocation:
                recoveryButton(for: .destination)
            case .startNotAvailable:
                Text(TripCopy.chooseStart).font(.footnote)
            default:
                EmptyView()
            }
            if TripCopy.canRetry(e) {
                actionButton(TripCopy.tryAgain, "arrow.clockwise") { planner.plan(snapshot) }
            }
        }
    }

    private func recoveryButton(for end: EndpointEnd) -> some View {
        end == .destination
            ? actionButton(TripCopy.changeDestination, "mappin.and.ellipse") { planner.changeDestination() }
            : actionButton(TripCopy.changeStart, "figure.roll") { planner.beginEditing(.start) }
    }

    private func actionButton(_ title: String, _ icon: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: icon).font(.body.weight(.semibold)).frame(minHeight: 44)
        }
        .buttonStyle(.bordered).buttonBorderShape(.capsule)
    }

    private func card<C: View>(@ViewBuilder _ content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 10, content: content)
            .padding(14).frame(maxWidth: .infinity, alignment: .leading)
            .background(.background.secondary, in: RoundedRectangle(cornerRadius: 14))
    }

    private func announce(_ phase: TripPlanner.Phase) {
        switch phase {
        case .preview(let plan):
            let g = RouteSummary.groups(for: plan, origin: planner.plannedOrigin, destinationName: planner.destination?.name ?? "")
            AccessibilityNotification.Announcement(RouteSummary.announcement(for: plan, groups: g)).post()
        case .failed(let e):
            AccessibilityNotification.Announcement(TripCopy.planningFailure(e)).post()
        default: break
        }
    }
}

/// One list row in Apple Maps' style: a round coloured icon, a title and a grey detail line.
struct PanelRow: View {
    var icon: String
    var colour: Color
    var title: String
    var detail: String?
    var caption: String? = nil
    var selected = false
    var busy = false
    var chevron = false

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon).font(.system(size: 15, weight: .semibold)).foregroundStyle(.white)
                .frame(width: 32, height: 32).background(colour, in: Circle())
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                if let caption { Text(caption).font(.caption).foregroundStyle(.secondary) }
                Text(title).font(.body.weight(.medium)).foregroundStyle(.primary).lineLimit(2)
                if let detail {
                    Text(detail).font(.subheadline).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
            if busy { ProgressView() }
            if selected { Image(systemName: "checkmark").font(.body.weight(.semibold)).foregroundStyle(.blue) }
            if chevron { Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary) }
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
        .frame(maxWidth: .infinity, minHeight: 52, alignment: .leading)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}
