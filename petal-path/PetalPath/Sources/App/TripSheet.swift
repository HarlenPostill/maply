import SwiftUI
import MapKit

/// Choose a start and a destination, then ask the route service for a planning preview.
struct TripSheet: View {
    @ObservedObject var planner: TripPlanner
    @ObservedObject var state: GameState
    @FocusState private var searchFocused: Bool
    @AccessibilityFocusState private var resultFocused: Bool
    @Environment(\.dynamicTypeSize) private var typeSize
    @State private var noticeExpanded = false

    private var snapshot: TripLocationSnapshot { state.tripLocation }

    var body: some View {
        ScrollViewReader { scroll in
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    AdaptiveRow {
                        Text(TripCopy.title).font(.system(.title3, design: .rounded, weight: .bold)).foregroundStyle(ink)
                            .accessibilityAddTraits(.isHeader)
                    } trailing: {
                        Button("Done") { planner.close() }
                            .font(.system(.body, design: .rounded, weight: .semibold)).tint(olive).frame(minHeight: 44)
                            .accessibilityIdentifier("tripDone")
                    }
                    notice

                    if let field = planner.editing {
                        searchPanel(field)
                    } else {
                        endpoints
                        // Room above, so scrolling to the result clears the sheet's grabber.
                        planArea.padding(.top, 12).id("result")
                    }
                }
                .padding(18)
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
        .onChange(of: state.tripLocation) { _, s in planner.positionsChanged(s) }
        // The trip screens use the game's fixed light palette. Without this a dark system appearance
        // gives a dark Apple map inside a cream sheet, and the dark crossing markers vanish on it.
        .environment(\.colorScheme, .light)
        .task {
            // Ask for a current reading on open, and again while the last one is stale.
            if state.useMyLocation { state.wantsFreshFix = true }
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(8))
                if case .stale = planner.myLocationStatus(state.tripLocation) { state.wantsFreshFix = true }
            }
        }
    }

    /// Full at ordinary sizes. At accessibility sizes, and while searching (so results fit above the
    /// keyboard), the same meaning in one line, with "More".
    @ViewBuilder private var notice: some View {
        let compact = typeSize.isAccessibilitySize || planner.editing != nil
        let short = compact && !noticeExpanded
        VStack(alignment: .leading, spacing: 4) {
            Label(short ? TripCopy.previewNoticeShort : TripCopy.previewNotice, systemImage: "info.circle.fill")
                .font(.system(.footnote, design: .rounded, weight: .medium)).foregroundStyle(ink)
                .fixedSize(horizontal: false, vertical: true)
            if compact {
                Button(noticeExpanded ? TripCopy.showLess : TripCopy.showMore) { noticeExpanded.toggle() }
                    .font(.system(.footnote, design: .rounded, weight: .semibold)).tint(olive).frame(minHeight: 44)
                    .accessibilityLabel(noticeExpanded ? "Show less about planning previews" : "More about planning previews")
            }
        }
        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
        .background(olive.opacity(0.14), in: RoundedRectangle(cornerRadius: 14))
        .accessibilityIdentifier("previewNotice")
    }

    // MARK: Start and destination

    private var endpoints: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("From").font(.system(.subheadline, design: .rounded, weight: .bold)).foregroundStyle(ink.opacity(0.75))
                .accessibilityAddTraits(.isHeader)
            TimelineView(.periodic(from: .now, by: 5)) { _ in originOptions }

            Text("To").font(.system(.subheadline, design: .rounded, weight: .bold)).foregroundStyle(ink.opacity(0.75))
                .padding(.top, 4).accessibilityAddTraits(.isHeader)
            if let d = planner.destination {
                AdaptiveRow(alignment: .top) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(d.name).font(.system(.body, design: .rounded, weight: .semibold))
                        if let a = d.address { Text(a).font(.system(.footnote, design: .rounded)).foregroundStyle(ink.opacity(0.75)) }
                    }
                    .foregroundStyle(ink).accessibilityElement(children: .combine)
                } trailing: {
                    Button("Change") { planner.changeDestination() }
                        .font(.system(.subheadline, design: .rounded, weight: .semibold)).tint(olive)
                        .frame(minHeight: 44).accessibilityLabel(TripCopy.changeDestination)
                        .accessibilityIdentifier("changeDestination")
                }
                if let outside = planner.outsideCoverage(origin: planner.originEndpoint(snapshot)) {
                    VStack(alignment: .leading, spacing: 4) {
                        Label(TripCopy.outsideArea(outside, area: nil), systemImage: "map")
                            .font(.system(.callout, design: .rounded, weight: .semibold)).foregroundStyle(ink)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(TripCopy.coverageApproximate).font(.system(.footnote, design: .rounded)).foregroundStyle(ink.opacity(0.8))
                        recoveryButton(for: outside)
                    }
                    .accessibilityIdentifier("outsideCoverage")
                }
                // Apple's terms: a place from Apple Maps is shown with an Apple map. The preview has its own.
                if case .preview = planner.phase {} else {
                    PlaceMap(region: planner.searchRegion, start: planner.originEndpoint(snapshot), destination: d, coverage: planner.coverage)
                        .accessibilityLabel("Map of the start and destination")
                    Text(TripCopy.appleMapsCredit + (planner.coverage == nil ? "" : ". " + TripCopy.coverageEdge))
                        .font(.system(.caption, design: .rounded)).foregroundStyle(ink.opacity(0.7))
                }
            } else {
                Button { planner.changeDestination() } label: {
                    Label("Choose a destination", systemImage: "magnifyingglass")
                        .font(.system(.body, design: .rounded, weight: .semibold))
                        .frame(maxWidth: .infinity, minHeight: 48)
                }
                .buttonStyle(.borderedProminent).tint(olive)
                .accessibilityIdentifier("chooseDestination")
            }
        }
    }

    private var originOptions: some View {
        let s = snapshot, my = planner.myLocationStatus(s)
        return VStack(alignment: .leading, spacing: 6) {
            OriginRow(title: TripCopy.myLocation, detail: TripCopy.myLocationDetail(my), icon: "location.fill",
                      selected: planner.originChoice == .myLocation, enabled: my.fix != nil) {
                planner.chooseOrigin(.myLocation, s)
            }
            .accessibilityIdentifier("originMyLocation")
            if my == .off {
                Button { state.useMyLocation = true } label: {
                    Label(TripCopy.turnOnLocation, systemImage: "location")
                        .font(.system(.footnote, design: .rounded, weight: .semibold)).frame(minHeight: 44)
                }
                .tint(olive).padding(.leading, 10)
                .accessibilityHint("Your character follows where you are, and My location can be a start.")
                .accessibilityIdentifier("turnOnLocation")
            }
            if planner.characterAvailable(s) {
                OriginRow(title: TripCopy.characterPosition, detail: TripCopy.characterDetail, icon: "figure.walk",
                          selected: planner.originChoice == .character, enabled: true) {
                    planner.chooseOrigin(.character, s)
                }
                .accessibilityIdentifier("originCharacter")
            }
            OriginRow(title: planner.startPlace?.name ?? TripCopy.searchForStart,
                      detail: planner.startPlace?.address ?? (planner.startPlace == nil ? "Plan from somewhere else." : nil),
                      icon: "magnifyingglass", selected: planner.originChoice == .place, enabled: true) {
                planner.chooseOrigin(.place, s)
            }
            .accessibilityIdentifier("originPlace")
            if planner.startPlace != nil {
                Button("Search for a different start") { planner.beginEditing(.start) }
                    .font(.system(.footnote, design: .rounded, weight: .semibold)).tint(olive).frame(minHeight: 44)
            }
        }
    }

    // MARK: Planning

    @ViewBuilder private var planArea: some View {
        if let d = planner.destination {
            switch planner.phase {
            case .choosing:
                VStack(alignment: .leading, spacing: 8) {
                    if planner.originChoice == nil {
                        Text(TripCopy.chooseStart).font(.system(.callout, design: .rounded, weight: .semibold)).foregroundStyle(ink)
                    }
                    Button { planner.plan(snapshot) } label: {
                        Text(TripCopy.planButton).font(.system(.body, design: .rounded, weight: .bold)).frame(maxWidth: .infinity, minHeight: 48)
                    }
                    .buttonStyle(.borderedProminent).tint(olive)
                    .disabled(!planner.canPlan(snapshot) || planner.outsideCoverage(origin: planner.originEndpoint(snapshot)) != nil)
                    .accessibilityIdentifier("planRoute")
                    Text(TripCopy.sendsNote).font(.system(.caption, design: .rounded)).foregroundStyle(ink.opacity(0.7))
                        .fixedSize(horizontal: false, vertical: true)
                }
            case .planning:
                HStack(spacing: 10) { ProgressView(); Text(TripCopy.planning) }
                    .font(.system(.callout, design: .rounded)).foregroundStyle(ink)
                    .accessibilityElement(children: .combine)
            case .failed(let e):
                VStack(alignment: .leading, spacing: 10) {
                    Label(TripCopy.planningFailure(e), systemImage: "exclamationmark.bubble.fill")
                        .font(.system(.callout, design: .rounded, weight: .semibold)).foregroundStyle(ink)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityFocused($resultFocused)
                        .accessibilityIdentifier("planningFailure")
                    failureActions(e)
                }
            case .preview(let plan):
                RouteSummaryView(plan: plan, origin: planner.plannedOrigin, destination: d, coverage: planner.coverage,
                                 coverageState: planner.coverageState, startChanged: planner.previewStartChanged(snapshot),
                                 resultFocus: $resultFocused, planAgain: { planner.plan(snapshot) },
                                 retryCoverage: { planner.loadCoverage() })
            }
        }
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
                Text(TripCopy.chooseStart).font(.system(.footnote, design: .rounded)).foregroundStyle(ink)
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
            Label(title, systemImage: icon).font(.system(.body, design: .rounded, weight: .semibold)).frame(minHeight: 44)
        }
        .buttonStyle(.bordered).tint(olive)
    }

    // MARK: Search

    private func searchPanel(_ field: TripPlanner.Field) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            AdaptiveRow {
                Text(field == .destination ? "Choose a destination" : "Choose a start")
                    .font(.system(.headline, design: .rounded, weight: .bold)).foregroundStyle(ink)
                    .accessibilityAddTraits(.isHeader)
            } trailing: {
                if field == .start || planner.destination != nil {
                    Button("Cancel") { planner.endEditing() }
                        .font(.system(.body, design: .rounded)).tint(olive).frame(minHeight: 44)
                        .accessibilityIdentifier("cancelSearch")
                }
            }
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(ink.opacity(0.6)).accessibilityHidden(true)
                TextField(TripCopy.searchPrompt(field), text: $planner.query)
                    .font(.system(.body, design: .rounded))
                    .textInputAutocapitalization(.words).autocorrectionDisabled()
                    .submitLabel(.search).focused($searchFocused)
                    .onSubmit { searchFocused = false }
                    .accessibilityIdentifier("tripSearchField")
                if !planner.query.isEmpty {
                    Button { planner.query = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(ink.opacity(0.5)) }
                        .frame(minWidth: 44, minHeight: 44).accessibilityLabel("Clear search")
                }
            }
            .padding(.horizontal, 12).frame(minHeight: 48)
            .background(Color.white.opacity(0.8), in: RoundedRectangle(cornerRadius: 14))

            // Apple's terms: search results are shown with an Apple map, so it sits above them.
            PlaceMap(region: planner.searchRegion, start: nil, destination: nil, coverage: planner.coverage, height: 100)
                .accessibilityLabel("Map of the search area")
            Text(TripCopy.appleMapsCredit).font(.system(.caption, design: .rounded)).foregroundStyle(ink.opacity(0.7))

            searchStatus

            ForEach(planner.suggestions) { s in
                Button { Task { await planner.select(s) } } label: {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(s.title).font(.system(.body, design: .rounded, weight: .semibold))
                            if !s.subtitle.isEmpty { Text(s.subtitle).font(.system(.footnote, design: .rounded)).foregroundStyle(ink.opacity(0.75)) }
                        }
                        Spacer()
                        if planner.resolving == s.id { ProgressView() }
                    }
                    .foregroundStyle(ink).frame(maxWidth: .infinity, minHeight: 44, alignment: .leading).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(planner.resolving != nil)
                .accessibilityElement(children: .combine)
                Divider().overlay(ink.opacity(0.1))
            }
        }
        .onAppear { searchFocused = true }
    }

    @ViewBuilder private var searchStatus: some View {
        let q = planner.query.trimmingCharacters(in: .whitespaces)
        switch planner.searchStatus {
        case .idle:
            if q.count < 2 { hint(TripCopy.searchHint) }
        case .searching:
            HStack(spacing: 8) { ProgressView(); Text("Searching…") }.font(.system(.footnote, design: .rounded)).foregroundStyle(ink)
                .accessibilityElement(children: .combine)
        case .done:
            if planner.suggestions.isEmpty { hint(TripCopy.noResults) }
        case .failed(let e):
            VStack(alignment: .leading, spacing: 4) {
                hint(TripCopy.searchFailure(e))
                if e != .notFound { actionButton(TripCopy.tryAgain, "arrow.clockwise") { planner.retrySearch() } }
            }
        }
    }

    private func hint(_ s: String) -> some View {
        Text(s).font(.system(.footnote, design: .rounded)).foregroundStyle(ink.opacity(0.8))
            .accessibilityIdentifier("searchStatus")
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

/// Presses show, but a disabled row is not faded by the system: its explanation must stay readable.
private struct OriginRowStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.opacity(configuration.isPressed ? 0.75 : 1)
    }
}

/// One choice of start: a radio row with the reason it can or can't be used.
struct OriginRow: View {
    let title: String; let detail: String?; let icon: String
    let selected: Bool; let enabled: Bool; let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: selected ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(enabled ? olive : ink.opacity(0.35)).font(.title3)
                    .accessibilityHidden(true)          // the selected trait says this
                VStack(alignment: .leading, spacing: 2) {
                    Label(title, systemImage: icon).font(.system(.body, design: .rounded, weight: .semibold))
                        .foregroundStyle(enabled ? ink : ink.opacity(0.6))
                    if let detail {
                        // Stays readable when the row is off: it says why.
                        Text(detail).font(.system(.footnote, design: .rounded)).foregroundStyle(ink.opacity(0.85))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(10).frame(maxWidth: .infinity, minHeight: 48, alignment: .leading)
            .background(selected ? olive.opacity(0.14) : Color.white.opacity(0.6), in: RoundedRectangle(cornerRadius: 14))
            .contentShape(Rectangle())
        }
        .buttonStyle(OriginRowStyle())
        .disabled(!enabled)
        // Combine keeps the button's own traits, so "selected" and "dimmed" are read; the radio
        // image is hidden above, so its symbol name isn't.
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}
