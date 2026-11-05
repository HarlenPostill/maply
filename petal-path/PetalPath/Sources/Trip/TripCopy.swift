import Foundation

/// Every safety-relevant sentence the app writes for the trip screens, in one place so tests can
/// check it. Rules: never call a route safe, accessible, step-free, verified or navigable. Unknown
/// stays unknown. Slope counts are tags in survey photos, never slopes, lengths or gradients. The
/// route service's own warning messages are shown as details under these headings.
enum TripCopy {
    static let title = "Plan a trip"
    static let previewNotice = "Planning preview only. Petal Path doesn't check routes for wheelchair travel and gives no turn-by-turn guidance. Check conditions yourself before you go."
    /// The same meaning in one line, for accessibility text sizes. "More" shows the full notice.
    static let previewNoticeShort = "Preview only. Not checked for wheelchair travel."
    static let myLocation = "My location"
    static let characterPosition = "Character's position"
    static let characterDetail = "Simulated. This isn't where you are."
    static let searchForStart = "Search for a start"
    static let appleMapsCredit = "Places from Apple Maps"
    static let noResults = "No places found. Try another name or address."
    static let sendsNote = "Planning sends the start and destination to Maply's route service. The service doesn't store them, and nothing is saved on this device."
    static let planButton = "Plan route preview"
    static let planning = "Planning a route preview…"
    static let chooseStart = "Choose where to start."
    static let searchHint = "Type at least two letters."

    static func searchPrompt(_ f: TripPlanner.Field) -> String {
        f == .destination ? "Search for a place or address" : "Search for a start"
    }

    static func myLocationDetail(_ s: MyLocationStatus) -> String {
        switch s {
        case .off: "Turn on “Use my location” to start from where you are."
        case .finding: "Finding you…"
        case .rough(let a): "Only accurate to about \(distance(a)). Waiting for a closer fix."
        case .stale: "Your last location is over 30 seconds old. Waiting for a new one."
        case .ready(let f): "Accurate to about \(distance(f.horizontalAccuracy))."
        }
    }

    static func searchFailure(_ e: PlaceSearchError) -> String {
        switch e {
        case .busy: "Search is busy. Try again in a moment."
        case .offline: "Search needs an internet connection."
        case .notFound: "Couldn't find that place."
        }
    }

    // MARK: Coverage

    static let coverageEdge = "Shaded: outside the surveyed area."
    static func outsideArea(_ which: EndpointEnd, area: String?) -> String {
        let place = which == .origin ? "The start" : "The destination"
        return "\(place) is outside the surveyed area\(area.map { " (\($0))" } ?? ""), so no route can be planned to it."
    }
    static let coverageApproximate = "The surveyed area is approximate: roughly the City of Sydney council area."

    // MARK: Planning failures

    private static func end(_ w: EndpointEnd?, start: String = "the start", destination: String = "the destination", either: String = "one end of the trip") -> String {
        switch w { case .origin: start; case .destination: destination; case nil: either }
    }

    static func planningFailure(_ e: RoutePlanningError) -> String {
        switch e {
        case .notConnected:
            "Route planning isn't connected in this version of Petal Path yet."
        case .outsideCoverage(let w):
            "\(end(w, start: "The start", destination: "The destination", either: "Part of this trip")) is outside the surveyed area. The survey covers roughly the City of Sydney council area only."
        case .tooFarFromNetwork(let w, let max):
            "There's no surveyed street within \(distance(max ?? 40)) of \(end(w)). Try a place closer to a street."
        case .noEligibleFootpathNearby(let w, let reasons):
            "The footpath next to \(end(w)) is left out of route planning." + (reasons.isEmpty ? "" : " " + reasons.map(exclusion).joined(separator: " "))
        case .sameLocation:
            "The start and destination are the same place."
        case .noRoute(let reason, let o, let d):
            switch reason {
            case .blockedByExclusions:
                "The survey has no connected route between these places once streets with a known missing footpath, fixed barriers or steps are left out."
            case .disconnected where o || d:
                "\(o && d ? "Both places are" : o ? "The start is" : "The destination is") on a small stretch of surveyed footpath, such as an isolated plaza, that doesn't join the rest of the survey's network. That's a gap in the survey data, not a finding about the place itself."
            default:
                "The survey has no connected route between these places."
            }
        case .busy: "The route service is busy. Try again in a moment."
        case .loading: "The route service is still loading its data. Try again in about 30 seconds."
        case .timedOut: "Planning took too long. Try two places closer together."
        case .unavailable: "Couldn't reach the route service. Check your connection and try again."
        case .serviceError: "The route service couldn't plan this trip."
        case .invalidResponse: "The route service sent a reply Petal Path couldn't use, so nothing is shown."
        case .startNotAvailable: "Your start isn't available any more. Choose a start again."
        }
    }

    static func exclusion(_ r: ExclusionReason) -> String {
        switch r {
        case .fixedBarrier: "A survey photo showed a fixed barrier or steps there."
        case .fixedBarrierFlagged: "A survey photo showed a fixed barrier or steps there. That photo is flagged for re-checking and may be wrong, but the footpath stays left out until it's re-checked."
        case .footpathAbsent: "A survey photo showed no footpath there."
        case .footpathEnds: "A survey photo showed the footpath ending there."
        case .other: "The service gave a reason this version can't show."
        }
    }

    /// Whether a failure is worth trying again as it stands.
    static func canRetry(_ e: RoutePlanningError) -> Bool {
        switch e { case .busy, .loading, .timedOut, .unavailable, .serviceError: true; default: false }
    }

    // MARK: Summary

    static let sampleBanner = "Sample layout data for testing. This is not a route."
    static let previewLine = "Not checked for wheelchair travel. Navigation is off for this route."
    static func headline(_ metres: Double) -> String { "Route preview · \(distance(metres))" }

    static func measuredNote(_ p: RoutePlan) -> String {
        var unmeasured: [String] = []
        if p.endpointGapMetres > 0 { unmeasured.append("the \(distance(p.endpointGapMetres)) at the ends") }
        let junctions = p.crossings + p.sharedSurfaceTransitions
        if junctions > 0 { unmeasured.append(count(junctions, "crossing")) }
        return unmeasured.isEmpty ? "Measured along the footpath and any inferred links."
            : "Measured along the footpath and any inferred links. It leaves out \(unmeasured.joined(separator: " and ")), which aren't measured or assessed."
    }

    static let warningsTitle = "What needs checking"
    static let glanceTitle = "At a glance"
    static let overviewTitle = "Overview"
    static let overviewHint = "The sections below describe the whole route in words."
    static let overviewOffset = "Footpaths are drawn a few metres to their side of the street to show which side. That isn't where the footpath was surveyed."
    static let evidenceTitle = "What the survey saw"
    static let slopeTitle = "Slope tags in survey photos"
    static let slopeCaveat = "These count slope tags on features in survey photos, not slopes. One slope can be tagged in several photos, and no tag gives a gradient or a length."
    static let imageryTitle = "Imagery age"
    static let stepsTitle = "Along the way"
    static let limitsTitle = "What this data can't tell you"
    static let widthNotMeasured = "Footpath width isn't in the data."

    static func evidence(_ e: Evidence) -> String {
        switch e {
        case .observed: "Observed in survey"
        case .uncertain: "Flagged for re-checking"
        case .unknown: "Not surveyed"
        }
    }

    /// Map legend: a whole leg is drawn as its least-known part.
    static func legend(_ e: Evidence) -> String {
        switch e {
        case .observed: "Observed along the whole stretch"
        case .uncertain: "Partly flagged for re-checking"
        case .unknown: "Partly or wholly not surveyed"
        }
    }
    static let legendMixed = "Mixed: partly not surveyed or flagged. Where along the stretch isn't known."
    static let legendInferred = "Inferred link or unassessed gap"
    static let legendCrossing = "Crossing not checked, numbered as in “Along the way”"
    static let mixedRule = "The service reports how much of each stretch was observed, not where. A stretch with any unsurveyed or flagged part is drawn as mixed along its whole length."
    static let enlargeMap = "Open larger map"
    static let mapTitle = "Route map"

    static func inferredLinks(_ m: Double, _ n: Int) -> String { "Inferred \(n == 1 ? "link" : "links") between streets: \(distance(m))" }

    static func designation(_ d: DesignationMetres) -> [String] {
        var s: [String] = []
        if d.pedestrian > 0 { s.append("On a designated pedestrian street: \(distance(d.pedestrian))") }
        if d.shared > 0 { s.append("In a shared zone, where people and vehicles share the street: \(distance(d.shared))") }
        if d.ambiguous > 0 { s.append("Street type unclear in map data: \(distance(d.ambiguous))") }
        return s
    }

    static func observations(_ n: Int, flagged: Int) -> String {
        "\(count(n, "survey photo")) along the route" + (flagged > 0 ? ", \(flagged) flagged for re-checking." : ".")
    }

    // MARK: Warnings

    /// A plain heading for each warning code. The service's message follows as the detail.
    static func warningHeading(_ code: String) -> String {
        switch code {
        case "crossing_unverified": "Crossing not checked"
        case "shared_surface_transition_unverified": "Crossing a shared street, not checked"
        case "connector_inferred": "Inferred link between streets"
        case "footpath_unknown": "Footpath not surveyed"
        case "steep_slope_observed": "Steep slope in a survey photo"
        case "temporary_obstruction_observed": "Temporary obstruction in a survey photo"
        case "blocking_permanence_unknown": "Blockage in a survey photo, may be fixed or temporary"
        case "flagged_observation": "Survey photo flagged for re-checking"
        case "narrowed_passage": "Narrowed footpath in a survey photo"
        case "footpath_interrupted": "Footpath interrupted in a survey photo"
        case "missing_surface_observed": "Missing footpath surface in a survey photo"
        case "driveway_lip_or_channel": "Driveway lip or channel in a survey photo"
        case "endpoint_side_ambiguous": "Not clear which side of the street"
        case "endpoint_gap_not_assessed": "Gap to the footpath not assessed"
        case "hazard_observed": "Hazard in a survey photo"
        case "surface_concern": "Surface concern in a survey photo"
        case "driveway_dips": "Driveway dips across the footpath"
        case "driveway_tilts_sideways": "Driveway tilts the footpath sideways"
        case "driveway_interrupted": "Driveway interrupts the footpath"
        case "no_separate_footpath": "No separate footpath"
        case "slope_not_recorded": "Slope not recorded in survey photos"
        case "simulated_start": "Simulated start"
        case "entrance_not_checked": "Entrance not checked"
        default: "Survey note"
        }
    }

    static let simulatedStart = "This preview starts from the character's simulated position, not your real location."
    static func entranceNotChecked(_ place: String) -> String { "Entrance access at \(place) isn't checked. Entrances aren't in the data." }
    static let warningsTruncated = "The service sent the first 300 warnings only. There are more along this route."

    static func slopeNotRecorded(_ s: SlopeObservations, observations: Int) -> String {
        observations == 0 ? "No survey photo along this route, so slope is unknown."
            : "\(s.observationsWithoutSlope) of \(count(observations, "survey photo")) along this route have no slope tag. Slope there is unknown, not level."
    }

    static func photoDate(_ yyyymm: String?) -> String? {
        guard let yyyymm, let m = month(yyyymm) else { return nil }
        return "Photo from \(m)"
    }

    // MARK: Slope

    static func slopeEntry(_ e: SlopeObservations.Entry) -> String {
        let feature = switch e.feature {
        case .footpath: "Footpath"
        case .driveway: "Driveway"
        case .kerbRamp: "Kerb ramp"
        case .crossing: "Crossing"
        }
        let axis = switch e.axis {
        case .slope: ""
        case .longitudinal: ", along the way"
        case .cross: ", side to side"
        }
        return "\(category(e.category)): \(feature.lowercased())\(axis)"
    }

    static func category(_ c: SlopeObservations.Category) -> String {
        switch c {
        case .apparentlyLevel: "Looked level"
        case .gentle: "Gentle"
        case .moderate: "Moderate"
        case .steep: "Steep"
        }
    }

    static func tagCount(_ n: Int) -> String { count(n, "tag") }
    static let slopeUnreadFeatures = "The service sent slope tags this version can't show."

    // MARK: Imagery

    static func imageryRange(_ r: MonthRange?) -> String {
        guard let r, let from = month(r.from), let to = month(r.to) else { return "No dated survey photos along this route." }
        return from == to ? "Survey photos from \(from)." : "Survey photos from \(from) to \(to)."
    }

    static func imageryRows(_ i: ImageryAgeMetres) -> [(String, Double)] {
        [("Photo under 3 years old", i.withinThreeYears), ("Photo over 3 years old", i.olderThanThreeYears),
         ("Photo has no date", i.undated), ("No photo", i.noObservation)].filter { $0.1 > 0 }
    }

    // MARK: Legs

    static func legTitle(_ l: RouteLeg) -> String {
        switch l.kind {
        case .footpath: legPlace(l) ?? "Unnamed street"
        case .crossing: "Crossing of \(l.streetName ?? "a street")"
        case .sharedSurfaceTransition: "Crossing of \(l.streetName ?? "a shared street")"
        case .connector: "Inferred link"
        case .endpointGap: l.gapEnd == .origin ? "From the start to the footpath" : "From the footpath to the destination"
        }
    }

    static func legDetail(_ l: RouteLeg) -> String {
        var parts: [String] = []
        switch l.kind {
        case .footpath:
            if let f = l.facing { parts.append("\(f) side") }
            switch l.designation {
            case .pedestrian: parts.append("designated pedestrian street")
            case .shared: parts.append("shared zone")
            case .ambiguous: parts.append("street type unclear")
            default: break
            }
            if let e = l.evidence {
                parts += Evidence.allCases.filter { e[$0] > 0 }.map { "\(evidence($0).lowercased()) \(distance(e[$0]))" }
            }
        case .crossing, .sharedSurfaceTransition:
            parts.append("not checked: crossing point, kerb ramps and kerb heights aren't known")
        case .connector:
            parts.append("joins streets that stop short of each other; not surveyed")
        case .endpointGap:
            parts.append("straight line, not assessed")
        }
        return parts.joined(separator: " · ").capitalisingFirst
    }

    /// Context near a crossing, never a verdict on it.
    static func nearby(_ n: NearbyCrossingObservations) -> String? {
        guard n.total > 0 else { return nil }
        var seen: [String] = []
        if n.kerbRampPresent > 0 { seen.append("a kerb ramp (\(n.kerbRampPresent))") }
        if n.kerbRampAbsent > 0 { seen.append("no kerb ramp (\(n.kerbRampAbsent))") }
        if n.kerbRampLipOrStep > 0 { seen.append("a ramp with a lip or step (\(n.kerbRampLipOrStep))") }
        if n.crossingUnmodifiedKerb > 0 { seen.append("an unmodified kerb (\(n.crossingUnmodifiedKerb))") }
        if n.crossingSteps > 0 { seen.append("steps (\(n.crossingSteps))") }
        if n.crossingLevelOrRamp > 0 { seen.append("a level or ramped crossing (\(n.crossingLevelOrRamp))") }
        return "Nearby survey photos showed \(seen.joined(separator: ", ")). They may not be at this crossing."
    }

    /// The numbered title used in the list and on the map, such as "2. Crossing of Hunter Street".
    static func legTitle(_ l: RouteLeg, number: Int?) -> String { number.map { "\($0). \(legTitle(l))" } ?? legTitle(l) }

    /// A leg's street, or a plain name for an unnamed designated street.
    static func legPlace(_ l: RouteLeg) -> String? {
        if let s = l.streetName { return s }
        guard l.kind == .footpath else { return nil }
        switch l.designation { case .pedestrian: return "Pedestrian street"; case .shared: return "Shared zone"; default: return nil }
    }

    /// For VoiceOver: the same facts as the leg row, with commas instead of separators.
    static func legAccessibility(_ l: RouteLeg, number: Int?) -> String {
        [legTitle(l, number: number), l.lengthMetres.map(distance), legDetail(l).replacingOccurrences(of: " · ", with: ", "), l.nearby.flatMap(nearby)]
            .compactMap { $0 }.joined(separator: ", ")
    }

    static func gapNotAssessed(_ end: EndpointEnd, _ m: Double) -> String {
        end == .origin ? "The \(distance(m)) between the start and the footpath isn't assessed."
            : "The \(distance(m)) between the footpath and the destination isn't assessed."
    }

    static func imageryShortRange(_ r: MonthRange?) -> String {
        guard let r, let from = month(r.from), let to = month(r.to) else { return "no dated photos" }
        return from == to ? from : "\(from) to \(to)"
    }

    // MARK: Start and result state

    static func plannedFrom(_ e: TripEndpoint) -> String {
        switch e.source {
        case .device(let a): "Planned from your location (accurate to about \(distance(a)))"
        case .simulatedCharacter: "Planned from the character's simulated position"
        case .searchedPlace: "Planned from \(e.label)"
        }
    }
    static let startChanged = "The start has changed since this preview was planned."
    static let planAgain = "Plan again"
    static let turnOnLocation = "Use my location"
    static let changeDestination = "Change destination"
    static let changeStart = "Change start"
    static let tryAgain = "Try again"
    static let showMore = "More"
    static let showLess = "Less"
    static func groupHeading(_ g: WarningGroup) -> String { g.summary.map { "\(g.heading) · \($0)" } ?? g.heading }

    // MARK: Limits

    static let limitsUnavailable = "The service's full list of data limits couldn't load. These are the main ones."
    static let limitsLoading = "Loading the service's list of data limits…"
    static let limitsNote = "These apply to the whole survey, not only this route."
    static let coreLimitCodes = ["preview_only", "crossings_inferred_at_junctions", "camera_position_not_feature_position",
                                 "width_not_measured", "slope_categorical_only", "imagery_age", "endpoint_gap_not_assessed"]

    /// The app's own words for a limit when the service's haven't loaded.
    static func fallbackLimit(_ code: String) -> String {
        switch code {
        case "preview_only": "Nothing in this route is checked. It's a planning preview, with navigation off."
        case "crossings_inferred_at_junctions": "Crossings are placed at street junctions. Where people actually cross, signals, kerb heights and widths aren't known."
        case "camera_position_not_feature_position": "Survey photos are placed where the camera stood, not at the footpath, ramp or kerb they show."
        case "width_not_measured": "Footpath width isn't in the data."
        case "slope_categorical_only": "Slopes are rough categories from a few photos. There are no gradients."
        case "imagery_age": "Some survey photos are old, and many have no date."
        case "endpoint_gap_not_assessed": "The stretch between each end and the footpath isn't assessed, and entrances aren't in the data."
        case "connectors_inferred": "Some links join streets that stop short of each other. They're inferred, not surveyed."
        case "no_footway_network": "Park, plaza and laneway paths, service roads and steps aren't mapped."
        case "no_grade_separation": "Bridges and tunnels aren't known, so streets that cross on the map are treated as meeting."
        case "approximate_boundary": "The edge of the surveyed area is approximate."
        default: "A further data limit applies that this version can't describe."
        }
    }

    static func provenance(_ r: ReleaseInfo) -> String? {
        var s: [String] = []
        if let id = r.id { s.append("Survey data release \(id)") }
        if let p = r.policyVersion { s.append("policy \(p)") }
        if let g = r.graphVersion { s.append("graph \(g)") }
        if let a = r.asOfDate { s.append("ages as of \(a)") }
        return s.isEmpty ? nil : s.joined(separator: " · ") + "."
    }

    // MARK: Formatting

    static func distance(_ m: Double) -> String {
        if m < 1 { return "under 1 m" }
        if m < 100 { return "\(Int(m.rounded())) m" }
        if m < 950 { return "\(Int((m / 10).rounded()) * 10) m" }
        return String(format: "%.1f km", m / 1000)
    }

    static func count(_ n: Int, _ noun: String) -> String { "\(n) \(noun)\(n == 1 ? "" : "s")" }

    /// "2024-03" → "Mar 2024".
    static func month(_ yyyymm: String) -> String? {
        let p = yyyymm.split(separator: "-")
        guard let y = p.first.flatMap({ Int($0) }) else { return nil }
        guard p.count > 1, let m = Int(p[1]), (1...12).contains(m) else { return "\(y)" }
        let f = DateFormatter(); f.locale = .current
        return "\(f.shortMonthSymbols[m - 1]) \(y)"
    }
}

private extension String {
    var capitalisingFirst: String { prefix(1).uppercased() + dropFirst() }
}
