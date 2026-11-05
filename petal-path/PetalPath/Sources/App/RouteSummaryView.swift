import SwiftUI

private let warningAmber = Color(red: 0.62, green: 0.36, blue: 0.00)

/// A label and a value side by side, stacked instead at accessibility text sizes so neither is
/// broken mid-word.
struct AdaptiveRow<Leading: View, Trailing: View>: View {
    @Environment(\.dynamicTypeSize) private var size
    var alignment: VerticalAlignment = .center
    @ViewBuilder var leading: Leading
    @ViewBuilder var trailing: Trailing

    var body: some View {
        if size.isAccessibilitySize {
            VStack(alignment: .leading, spacing: 2) { leading; trailing }
                .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            HStack(alignment: alignment) { leading; Spacer(minLength: 8); trailing }
        }
    }
}

/// The route preview. The trip first (where from, how far, what's known at a glance, the map),
/// then what needs checking, then reference detail. The map is an overview; everything it shows
/// is also in words.
struct RouteSummaryView: View {
    let plan: RoutePlan
    let origin: TripEndpoint?
    let destination: Place
    let coverage: RouteCoverage?
    let coverageState: TripPlanner.CoverageState
    let startChanged: Bool
    var resultFocus: AccessibilityFocusState<Bool>.Binding
    let planAgain: () -> Void
    let retryCoverage: () -> Void
    @State private var mapOpen = false

    private var groups: [WarningGroup] {
        RouteSummary.groups(for: plan, origin: origin, destinationName: destination.name)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if plan.isSample {
                Label(TripCopy.sampleBanner, systemImage: "hammer.fill")
                    .font(.system(.subheadline, design: .rounded, weight: .bold))
                    .foregroundStyle(.white).padding(12).frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color(red: 0.55, green: 0.10, blue: 0.20), in: RoundedRectangle(cornerRadius: 14))
                    .accessibilityIdentifier("sampleBanner")
            }

            if startChanged {
                VStack(alignment: .leading, spacing: 6) {
                    Label(TripCopy.startChanged, systemImage: "arrow.triangle.2.circlepath")
                        .font(.system(.callout, design: .rounded, weight: .semibold)).foregroundStyle(ink)
                        .fixedSize(horizontal: false, vertical: true)
                    Button(TripCopy.planAgain, action: planAgain)
                        .buttonStyle(.borderedProminent).tint(olive).frame(minHeight: 44)
                        .accessibilityIdentifier("planAgain")
                }
                .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                .background(warningAmber.opacity(0.14), in: RoundedRectangle(cornerRadius: 14))
            }

            VStack(alignment: .leading, spacing: 4) {
                Text(TripCopy.headline(plan.routeMetres))
                    .font(.system(.title2, design: .rounded, weight: .bold)).foregroundStyle(ink)
                    .accessibilityAddTraits(.isHeader)
                Text(TripCopy.previewLine)
                    .font(.system(.subheadline, design: .rounded, weight: .semibold)).foregroundStyle(ink.opacity(0.85))
                if let origin {
                    Text(TripCopy.plannedFrom(origin) + ".")
                        .font(.system(.footnote, design: .rounded, weight: origin.source == .simulatedCharacter ? .bold : .regular))
                        .foregroundStyle(ink.opacity(0.85))
                        .fixedSize(horizontal: false, vertical: true)
                }
                Text(TripCopy.measuredNote(plan))
                    .font(.system(.footnote, design: .rounded)).foregroundStyle(ink.opacity(0.8))
                    .fixedSize(horizontal: false, vertical: true)
            }
            .accessibilityElement(children: .combine)
            .accessibilityFocused(resultFocus)
            .accessibilityIdentifier("routeHeadline")

            section(TripCopy.glanceTitle) {
                ForEach(RouteSummary.facts(plan), id: \.label) { f in
                    AdaptiveRow {
                        Text(f.label)
                    } trailing: {
                        Text(f.value).monospacedDigit().fontWeight(.semibold)
                    }
                    .font(.system(.callout, design: .rounded)).foregroundStyle(ink)
                    .accessibilityElement(children: .combine)
                }
            }
            .accessibilityIdentifier("atAGlance")

            section(TripCopy.overviewTitle) {
                // No tap target over the map: Apple's Legal link inside it must stay usable.
                RouteOverviewMap(plan: plan, origin: origin, destination: destination)
                    .frame(height: 240)
                    .clipShape(RoundedRectangle(cornerRadius: 16))
                    .accessibilityElement(children: .contain)
                    .accessibilityLabel("Route overview map")
                    .accessibilityHint(TripCopy.overviewHint)
                Button { mapOpen = true } label: {
                    Label(TripCopy.enlargeMap, systemImage: "arrow.up.left.and.arrow.down.right")
                        .font(.system(.subheadline, design: .rounded, weight: .semibold)).frame(minHeight: 44)
                }
                .tint(olive)
                .accessibilityHint("A map you can move and zoom, with the same key.")
                .accessibilityIdentifier("openRouteMap")
                RouteLegend(showsCrossings: plan.crossings + plan.sharedSurfaceTransitions > 0)
                Text(TripCopy.mixedRule + " " + TripCopy.overviewOffset)
                    .font(.system(.caption, design: .rounded)).foregroundStyle(ink.opacity(0.8))
                    .fixedSize(horizontal: false, vertical: true)
            }

            section(TripCopy.warningsTitle) {
                ForEach(groups) { g in WarningGroupView(group: g) }
            }
            .accessibilityIdentifier("routeWarnings")

            section(TripCopy.evidenceTitle) {
                evidenceBar
                ForEach(Evidence.allCases, id: \.self) { e in
                    valueRow(swatch: LineSwatch(evidence: e), TripCopy.evidence(e), TripCopy.distance(plan.evidence[e]))
                }
                if plan.connectorMetres > 0 {
                    valueRow(swatch: LineSwatch(colour: inferredColour, dash: inferredDash, width: 3),
                             TripCopy.inferredLinks(plan.connectorMetres, plan.connectors), nil)
                }
                ForEach(TripCopy.designation(plan.designation), id: \.self) { line in
                    Text(line).font(.system(.callout, design: .rounded)).foregroundStyle(ink).fixedSize(horizontal: false, vertical: true)
                }
                Text(TripCopy.observations(plan.observations, flagged: plan.flaggedObservations))
                    .font(.system(.footnote, design: .rounded)).foregroundStyle(ink.opacity(0.8))
                if plan.limits.contains("width_not_measured") {
                    Text(TripCopy.widthNotMeasured).font(.system(.footnote, design: .rounded)).foregroundStyle(ink.opacity(0.8))
                }
            }

            section(TripCopy.slopeTitle) {
                ForEach(plan.slope.entries) { e in
                    AdaptiveRow {
                        Text(TripCopy.slopeEntry(e)).fontWeight(e.category == .steep ? .bold : .regular)
                    } trailing: {
                        Text(TripCopy.tagCount(e.count)).monospacedDigit()
                    }
                    .font(.system(.callout, design: .rounded)).foregroundStyle(ink)
                    .accessibilityElement(children: .combine)
                }
                Text(TripCopy.slopeNotRecorded(plan.slope, observations: plan.observations))
                    .font(.system(.callout, design: .rounded)).foregroundStyle(ink)
                    .fixedSize(horizontal: false, vertical: true)
                if plan.slope.hasUnreadFeatures {
                    Text(TripCopy.slopeUnreadFeatures).font(.system(.footnote, design: .rounded, weight: .semibold)).foregroundStyle(ink)
                }
                Text(TripCopy.slopeCaveat).font(.system(.footnote, design: .rounded)).foregroundStyle(ink.opacity(0.8))
                    .fixedSize(horizontal: false, vertical: true)
            }
            .accessibilityIdentifier("slopeSection")

            section(TripCopy.imageryTitle) {
                Text(TripCopy.imageryRange(plan.imageryMonths)).font(.system(.callout, design: .rounded)).foregroundStyle(ink)
                ForEach(TripCopy.imageryRows(plan.imagery), id: \.0) { row in
                    valueRow(swatch: nil as LineSwatch?, row.0, TripCopy.distance(row.1))
                }
            }

            section(TripCopy.stepsTitle) {
                let numbers = plan.junctionNumbers
                ForEach(plan.legs) { l in legRow(l, number: numbers[l.id]) }
            }
            .accessibilityIdentifier("alongTheWay")

            limitsSection

            if let p = TripCopy.provenance(plan.release) {
                Text(p).font(.system(.caption, design: .rounded)).foregroundStyle(ink.opacity(0.7))
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .sheet(isPresented: $mapOpen) {
            RouteMapSheet(plan: plan, origin: origin, destination: destination)
                .presentationDetents([.large])
                .presentationBackground(cream)
        }
    }

    private var limitsSection: some View {
        let limits = RouteSummary.limitLines(plan, coverage: coverage)
        return section(TripCopy.limitsTitle) {
            if limits.source == .fallback {
                Text(coverageState == .loading ? TripCopy.limitsLoading : TripCopy.limitsUnavailable)
                    .font(.system(.footnote, design: .rounded, weight: .semibold)).foregroundStyle(ink)
                    .fixedSize(horizontal: false, vertical: true)
                if coverageState != .loading {
                    Button(TripCopy.tryAgain, action: retryCoverage).font(.system(.footnote, design: .rounded, weight: .semibold))
                        .tint(olive).frame(minHeight: 44).accessibilityLabel("Try loading the data limits again")
                }
            }
            ForEach(limits.lines, id: \.self) { line in
                Label {
                    Text(line).fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: "minus").foregroundStyle(ink.opacity(0.5)).accessibilityHidden(true)
                }
                .font(.system(.footnote, design: .rounded)).foregroundStyle(ink)
            }
            Text(TripCopy.limitsNote).font(.system(.caption, design: .rounded)).foregroundStyle(ink.opacity(0.7))
        }
        .accessibilityIdentifier("dataLimits")
    }

    private func section<C: View>(_ title: String, @ViewBuilder _ content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.system(.headline, design: .rounded, weight: .bold)).foregroundStyle(ink)
                .accessibilityAddTraits(.isHeader)
            content()
        }
        .padding(14).frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.6), in: RoundedRectangle(cornerRadius: 16))
    }

    private func valueRow<S: View>(swatch: S?, _ label: String, _ value: String?) -> some View {
        AdaptiveRow {
            HStack(spacing: 8) { if let swatch { swatch }; Text(label).fixedSize(horizontal: false, vertical: true) }
        } trailing: {
            if let value { Text(value).monospacedDigit() }
        }
        .font(.system(.callout, design: .rounded)).foregroundStyle(ink)
        .accessibilityElement(children: .combine)
    }

    /// Proportional, with the gaps between pieces taken out first so it never overflows.
    private var evidenceBar: some View {
        GeometryReader { g in
            let parts = Evidence.allCases.filter { plan.evidence[$0] > 0 }
            let gap: CGFloat = 2, usable = max(0, g.size.width - gap * CGFloat(max(parts.count - 1, 0)))
            HStack(spacing: gap) {
                ForEach(parts, id: \.self) { e in
                    Rectangle().fill(e.colour.opacity(e == .observed ? 1 : 0.75))
                        .overlay(e == .observed ? nil : stripes)
                        .frame(width: usable * plan.evidence[e] / max(plan.evidence.total, 1))
                }
            }
        }
        .frame(height: 14).clipShape(Capsule())
        .accessibilityHidden(true)
    }

    private var stripes: some View {
        Canvas { ctx, size in
            var p = Path()
            stride(from: -size.height, to: size.width, by: 7).forEach { x in
                p.move(to: CGPoint(x: x, y: size.height)); p.addLine(to: CGPoint(x: x + size.height, y: 0))
            }
            ctx.stroke(p, with: .color(.white.opacity(0.55)), lineWidth: 2)
        }
    }

    @ViewBuilder private func legIcon(_ l: RouteLeg, number: Int?) -> some View {
        switch l.kind {
        case .footpath:
            LineSwatch(LegStroke(l)).rotationEffect(.degrees(90)).frame(width: 14, height: 34)
        case .crossing, .sharedSurfaceTransition:
            Text(number.map(String.init) ?? "!").font(.system(size: 10, weight: .heavy, design: .rounded))
                .foregroundStyle(.white).frame(width: 18, height: 18).background(crossingColour, in: Circle())
        case .connector:
            Image(systemName: "link").font(.caption.weight(.bold)).foregroundStyle(inferredColour)
        case .endpointGap:
            Image(systemName: "arrow.left.and.right").font(.caption.weight(.bold)).foregroundStyle(inferredColour)
        }
    }

    private func legRow(_ l: RouteLeg, number: Int?) -> some View {
        HStack(alignment: .top, spacing: 10) {
            legIcon(l, number: number).frame(width: 20).padding(.top, 2).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                AdaptiveRow(alignment: .firstTextBaseline) {
                    Text(TripCopy.legTitle(l, number: number)).font(.system(.callout, design: .rounded, weight: .semibold))
                } trailing: {
                    if let m = l.lengthMetres { Text(TripCopy.distance(m)).font(.system(.callout, design: .rounded)).monospacedDigit() }
                }
                Text(TripCopy.legDetail(l)).font(.system(.footnote, design: .rounded)).foregroundStyle(ink.opacity(0.8))
                    .fixedSize(horizontal: false, vertical: true)
                if let n = l.nearby.flatMap(TripCopy.nearby) {
                    Text(n).font(.system(.footnote, design: .rounded)).foregroundStyle(ink.opacity(0.8))
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .foregroundStyle(ink)
        .padding(.vertical, 4)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(TripCopy.legAccessibility(l, number: number))
    }
}

/// One kind of warning. Shows the most important instance and folds the rest away, so routine
/// items never bury a blockage, while every instance stays one tap away.
struct WarningGroupView: View {
    let group: WarningGroup
    @State private var expanded = false

    private var prominent: Bool { group.severity == .prominent }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label {
                Text(TripCopy.groupHeading(group))
                    .font(.system(.callout, design: .rounded, weight: prominent ? .bold : .semibold))
                    .fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: prominent ? "exclamationmark.triangle.fill" : "info.circle")
                    .foregroundStyle(prominent ? warningAmber : ink.opacity(0.7))
            }
            .foregroundStyle(ink)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel((prominent ? "Warning: " : "") + TripCopy.groupHeading(group))
            .accessibilityAddTraits(.isHeader)

            ForEach(expanded ? group.items : Array(group.items.prefix(1))) { item in
                VStack(alignment: .leading, spacing: 1) {
                    Text(item.message).font(.system(.footnote, design: .rounded))
                    if let c = item.context { Text(c).font(.system(.caption, design: .rounded)).foregroundStyle(ink.opacity(0.7)) }
                }
                .foregroundStyle(ink).fixedSize(horizontal: false, vertical: true)
                .padding(.leading, 30)
                .accessibilityElement(children: .combine)
            }
            if group.items.count > 1 {
                Button(expanded ? "Show fewer" : "Show all \(group.items.count)") { expanded.toggle() }
                    .font(.system(.footnote, design: .rounded, weight: .semibold)).tint(olive)
                    .frame(minHeight: 44).padding(.leading, 30)
                    .accessibilityLabel("\(group.heading): all \(group.items.count)")
                    .accessibilityValue(expanded ? "Expanded" : "Collapsed")
                    .accessibilityHint(expanded ? "Shows only the first." : "Shows every one along the route.")
            }
        }
        .padding(.vertical, 2)
    }
}
