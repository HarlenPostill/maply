import Foundation

/// Warnings of one kind, with a plain heading and every instance the service sent.
struct WarningGroup: Identifiable, Equatable {
    struct Item: Equatable, Identifiable {
        var id: String
        /// The service's own words, or the app's for app-side facts.
        var message: String
        /// Where and when: the street and the photo month, when known.
        var context: String?
    }
    var id: String { code }
    var code: String
    var severity: ServiceWarning.Severity
    var heading: String
    /// A short total for the heading, such as "96 m in 9 places".
    var summary: String?
    var items: [Item]
}

/// Turns the service's facts into what the summary shows, in the order they matter. Pure, so tests
/// can check that nothing is dropped, softened or reordered by accident.
enum RouteSummary {
    /// Fixed order of concern. Lower comes first. A blockage near the destination must never sit
    /// below the routine crossings and unsurveyed stretches every route has.
    static func rank(_ code: String) -> Int {
        switch code {
        case "warnings_truncated", "simulated_start": 0
        case "blocking_permanence_unknown": 1
        case "temporary_obstruction_observed": 2
        case "narrowed_passage", "footpath_interrupted", "missing_surface_observed", "driveway_lip_or_channel": 3
        case "steep_slope_observed": 4
        case "flagged_observation": 6
        case "endpoint_side_ambiguous": 7
        case "footpath_unknown": 8
        case "connector_inferred": 9
        case "crossing_unverified", "shared_surface_transition_unverified": 10
        case "endpoint_gap_not_assessed": 20
        case "hazard_observed", "surface_concern", "driveway_dips", "driveway_tilts_sideways", "driveway_interrupted": 21
        case "no_separate_footpath": 22
        case "slope_not_recorded": 30
        case "entrance_not_checked": 31
        default: 5      // unfamiliar codes rank just after the known physical ones, never at the bottom
        }
    }

    static func groups(for plan: RoutePlan, origin: TripEndpoint?, destinationName: String) -> [WarningGroup] {
        let legs = Dictionary(plan.legs.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        var list: [WarningGroup] = []

        if plan.warningsTruncated { list.append(single("warnings_truncated", .prominent, TripCopy.warningsTruncated)) }
        if origin?.source == .simulatedCharacter { list.append(single("simulated_start", .prominent, TripCopy.simulatedStart)) }

        var order: [String] = [], byCode: [String: WarningGroup] = [:], weight: [String: Double] = [:]
        for w in plan.warnings {
            let leg = w.leg.flatMap { legs[$0] }
            // The street is context only when the message doesn't already name it.
            let street = leg.flatMap { TripCopy.legPlace($0) }.flatMap { w.message.contains($0) ? nil : $0 }
            let context = [street, TripCopy.photoDate(w.observedOn)].compactMap { $0 }.joined(separator: " · ")
            let item = WarningGroup.Item(id: "\(w.id)", message: message(w, leg), context: context.isEmpty ? nil : context)
            if byCode[w.code] == nil {
                order.append(w.code)
                byCode[w.code] = WarningGroup(code: w.code, severity: w.severity, heading: TripCopy.warningHeading(w.code), items: [])
            }
            byCode[w.code]!.items.append(item)
            weight[item.id] = sortWeight(w.code, leg)
            if w.severity == .prominent { byCode[w.code]!.severity = .prominent }    // one prominent instance is enough
        }
        for code in order {
            var g = byCode[code]!
            // Largest first, so the first line shown is the one that matters most; ties keep route order.
            g.items = g.items.enumerated().sorted { a, b in
                let wa = weight[a.element.id] ?? 0, wb = weight[b.element.id] ?? 0
                return wa != wb ? wa > wb : a.offset < b.offset
            }.map(\.element)
            g.summary = summary(code, g.items.count, plan.warnings.filter { $0.code == code }.compactMap { $0.leg.flatMap { legs[$0] } })
            list.append(g)
        }

        // Few survey photos carry any slope tag. Said plainly, as information: slopes never change the route.
        if plan.slope.observationsWithoutSlope > 0 || plan.observations == 0 {
            list.append(single("slope_not_recorded", .info, TripCopy.slopeNotRecorded(plan.slope, observations: plan.observations)))
        }
        list.append(single("entrance_not_checked", .info, TripCopy.entranceNotChecked(destinationName)))

        let prominent = list.enumerated().filter { $0.element.severity == .prominent }
        let info = list.enumerated().filter { $0.element.severity == .info }
        func sorted(_ g: [(offset: Int, element: WarningGroup)]) -> [WarningGroup] {
            g.sorted { a, b in
                let ra = rank(a.element.code), rb = rank(b.element.code)
                return ra != rb ? ra < rb : a.offset < b.offset
            }.map(\.element)
        }
        return sorted(prominent) + sorted(info)
    }

    /// App wording where the app knows better words for the same fact; otherwise the service's.
    private static func message(_ w: ServiceWarning, _ leg: RouteLeg?) -> String {
        if w.code == "endpoint_gap_not_assessed", let leg, leg.kind == .endpointGap, let m = leg.lengthMetres, let end = leg.gapEnd {
            return TripCopy.gapNotAssessed(end, m)
        }
        return w.message
    }

    private static func sortWeight(_ code: String, _ leg: RouteLeg?) -> Double {
        guard let leg else { return 0 }
        switch code {
        case "footpath_unknown": return leg.evidence?.unknown ?? 0
        case "flagged_observation": return leg.evidence?.uncertain ?? 0
        default: return 0
        }
    }

    private static func summary(_ code: String, _ count: Int, _ legs: [RouteLeg]) -> String? {
        switch code {
        case "footpath_unknown":
            var seen = Set<Int>()
            let metres = legs.filter { seen.insert($0.id).inserted }.reduce(0) { $0 + ($1.evidence?.unknown ?? 0) }
            return metres > 0 ? "\(TripCopy.distance(metres)) in \(TripCopy.count(count, "place"))" : count > 1 ? TripCopy.count(count, "place") : nil
        case "crossing_unverified", "shared_surface_transition_unverified":
            return count > 1 ? TripCopy.count(count, "crossing") : nil
        default:
            return count > 1 ? "\(count) along the route" : nil
        }
    }

    private static func single(_ code: String, _ s: ServiceWarning.Severity, _ text: String) -> WarningGroup {
        WarningGroup(code: code, severity: s, heading: TripCopy.warningHeading(code), summary: nil, items: [.init(id: code, message: text, context: nil)])
    }

    // MARK: At a glance

    /// Facts, not a verdict: what was seen, what wasn't, and what was never checked.
    static func facts(_ p: RoutePlan) -> [(label: String, value: String)] {
        var f: [(String, String)] = [
            (TripCopy.evidence(.observed), TripCopy.distance(p.evidence.observed)),
            (TripCopy.evidence(.uncertain), TripCopy.distance(p.evidence.uncertain)),
            (TripCopy.evidence(.unknown), TripCopy.distance(p.evidence.unknown)),
        ]
        let junctions = p.crossings + p.sharedSurfaceTransitions
        f.append(("Crossings not checked", "\(junctions)"))
        if p.connectors > 0 { f.append(("Inferred links", "\(p.connectors), \(TripCopy.distance(p.connectorMetres))")) }
        f.append(("Slope tags", p.slope.entries.isEmpty ? "none on this route" : TripCopy.count(p.slope.entries.reduce(0) { $0 + $1.count }, "tag")))
        f.append(("Survey photos", TripCopy.imageryShortRange(p.imageryMonths)))
        return f
    }

    // MARK: Limits

    enum LimitsSource: Equatable { case service, fallback }

    /// The route's limits in the service's words when they have loaded, and the app's own words for
    /// the core limits when they haven't. A route never shows fewer limits because a request failed.
    static func limitLines(_ p: RoutePlan, coverage: RouteCoverage?) -> (lines: [String], source: LimitsSource) {
        let codes = p.limits.isEmpty ? TripCopy.coreLimitCodes : p.limits
        if let coverage, codes.allSatisfy({ coverage.limitMessages[$0] != nil }) {
            let order = coverage.limitOrder.filter(codes.contains)
            return (order.compactMap { coverage.limitMessages[$0] }, .service)
        }
        let all = Array(Set(codes).union(TripCopy.coreLimitCodes))
        let ordered = TripCopy.coreLimitCodes.filter(all.contains) + all.filter { !TripCopy.coreLimitCodes.contains($0) }.sorted()
        return (ordered.map { coverage?.limitMessages[$0] ?? TripCopy.fallbackLimit($0) }, .fallback)
    }

    /// Spoken once when a preview arrives.
    static func announcement(for plan: RoutePlan, groups: [WarningGroup]) -> String {
        let prominent = groups.filter { $0.severity == .prominent }.count
        return (plan.isSample ? TripCopy.sampleBanner + " " : "")
            + "\(TripCopy.headline(plan.routeMetres)). \(TripCopy.previewLine) \(TripCopy.count(prominent, "kind")) of warning to check."
    }
}
