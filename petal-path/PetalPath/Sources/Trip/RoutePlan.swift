import Foundation

// What the route service said, in app terms. Built by `RoutePreviewDecoding` from
// `maply-route-preview/1` (atlas/ROUTING_API.md). Nothing here is calculated by the app.

/// Every route is a planning preview. There is deliberately no other case: nothing in the app can
/// describe a route as checked, verified or ready to navigate.
enum RouteReadiness: Equatable {
    case planningPreview
}

/// How much the survey knows about a stretch.
enum Evidence: Equatable, CaseIterable {
    /// Seen in survey imagery and not flagged.
    case observed
    /// Seen, but flagged in review for re-checking.
    case uncertain
    /// No observation reaches it. Never treated as known.
    case unknown
}

/// Metres by evidence. For the route summary these partition the street metres.
struct EvidenceMetres: Equatable {
    var observed: Double = 0, uncertain: Double = 0, unknown: Double = 0
    var total: Double { observed + uncertain + unknown }
    subscript(_ e: Evidence) -> Double {
        switch e { case .observed: observed; case .uncertain: uncertain; case .unknown: unknown }
    }
    /// The least-known evidence present.
    var leastKnown: Evidence { unknown > 0 ? .unknown : uncertain > 0 ? .uncertain : .observed }

    /// How a whole leg is drawn. The service gives evidence per leg, not where along it, so a leg
    /// with more than one kind is "mixed": never drawn as all-observed, and never as all-unknown
    /// when part of it was observed. There is no threshold: any mix is mixed.
    var style: LegEvidenceStyle {
        let present = Evidence.allCases.filter { self[$0] > 0 }
        switch present.count {
        case 0: return .whole(.unknown)
        case 1: return .whole(present[0])
        default: return .mixed(leastKnown: leastKnown)
        }
    }
}

enum LegEvidenceStyle: Equatable {
    /// All of the leg has this evidence.
    case whole(Evidence)
    /// Partly this, partly better known. Where along the leg is not known.
    case mixed(leastKnown: Evidence)
}

/// Metres by the age of the newest imagery covering them. Separate from uncertainty.
struct ImageryAgeMetres: Equatable {
    var withinThreeYears: Double = 0, olderThanThreeYears: Double = 0, undated: Double = 0, noObservation: Double = 0
}

/// Imagery months as "YYYY-MM".
struct MonthRange: Equatable { var from: String; var to: String }

enum Designation: Equatable {
    case ordinary, pedestrian, shared, ambiguous
}

struct DesignationMetres: Equatable {
    var ordinary: Double = 0, pedestrian: Double = 0, shared: Double = 0, ambiguous: Double = 0
}

/// Camera observations near a junction that mention a ramp or crossing. Context only: a camera
/// position cannot place a ramp at a particular kerb, so these never say the crossing is usable.
struct NearbyCrossingObservations: Equatable {
    var kerbRampPresent = 0, kerbRampAbsent = 0, kerbRampLipOrStep = 0
    var crossingUnmodifiedKerb = 0, crossingSteps = 0, crossingLevelOrRamp = 0
    var total: Int { kerbRampPresent + kerbRampAbsent + kerbRampLipOrStep + crossingUnmodifiedKerb + crossingSteps + crossingLevelOrRamp }
}

struct RouteLeg: Equatable, Identifiable {
    enum Kind: Equatable {
        case footpath
        /// Crossing a street at a junction. Never checked.
        case crossing
        /// Moving across a designated pedestrian street or shared zone at a junction. Never checked.
        case sharedSurfaceTransition
        /// A link the atlas inferred between streets.
        case connector
        /// Between a requested point and the matched footpath. Never assessed.
        case endpointGap
    }
    var id: Int
    var kind: Kind
    /// A line, or one point for a crossing.
    var coordinates: [GeoCoordinate]
    /// Nil for crossings, whose length is not measured.
    var lengthMetres: Double?
    /// The footpath's street, or the street crossed.
    var streetName: String?
    /// Compass side of the street, such as "north".
    var facing: String?
    var designation: Designation?
    var evidence: EvidenceMetres?
    var imagery: ImageryAgeMetres?
    var imageryMonths: MonthRange?
    var observations: Int?
    var flaggedUncertain: Int?
    var gapEnd: EndpointEnd?
    var nearby: NearbyCrossingObservations?
}

enum EndpointEnd: Equatable { case origin, destination }

/// Where the service joined a requested point to the footpath network.
struct MatchedEndpoint: Equatable {
    var gapMetres: Double
    var streetName: String?
    var facing: String?
    var sideAmbiguous: Bool
}

/// One warning from the service, shown in its own words.
struct ServiceWarning: Equatable, Identifiable {
    enum Severity: Equatable { case prominent, info }
    var id: Int
    var code: String
    var severity: Severity
    var leg: Int?
    var at: GeoCoordinate?
    /// "YYYY-MM" of the imagery it came from.
    var observedOn: String?
    var message: String
}

/// Slope tags on features in the route's survey observations. These count tags, not slopes and not
/// lengths: one slope can be tagged in several observations, and none gives a gradient.
struct SlopeObservations: Equatable {
    enum Feature: String, CaseIterable { case footpath, driveway, kerbRamp, crossing }
    enum Axis: String, CaseIterable { case slope, longitudinal, cross }
    enum Category: String, CaseIterable { case apparentlyLevel, gentle, moderate, steep }

    struct Entry: Equatable, Identifiable {
        var id: String { "\(feature)-\(axis)-\(category)" }
        var feature: Feature, axis: Axis, category: Category, count: Int
    }

    /// Only the non-zero counts.
    var entries: [Entry] = []
    /// Camera observations on the route with no slope tag at all. Never counted as level.
    var observationsWithoutSlope: Int = 0
    /// The service sent slope counts for a feature this version can't show.
    var hasUnreadFeatures = false

    func count(_ c: Category) -> Int { entries.filter { $0.category == c }.reduce(0) { $0 + $1.count } }
}

struct ReleaseInfo: Equatable {
    var id: Int?
    var publishedAt: String?
    var graphVersion: String?
    var asOfDate: String?
    var policyVersion: String?
}

struct RoutePlan: Equatable {
    var readiness: RouteReadiness = .planningPreview
    /// Footpath plus inferred links. Excludes endpoint gaps and crossings, which are not measured.
    var routeMetres: Double
    var streetMetres: Double
    var connectorMetres: Double
    var endpointGapMetres: Double
    var evidence: EvidenceMetres
    var designation: DesignationMetres
    var imagery: ImageryAgeMetres
    var imageryMonths: MonthRange?
    var crossings: Int
    var sharedSurfaceTransitions: Int
    var connectors: Int
    var observations: Int
    var flaggedObservations: Int
    var slope: SlopeObservations
    var warnings: [ServiceWarning]
    var warningsTruncated = false
    var legs: [RouteLeg]
    var origin: MatchedEndpoint?
    var destination: MatchedEndpoint?
    /// Limitation codes that apply. Their wording comes from the coverage answer.
    var limits: [String]
    var release: ReleaseInfo
    /// Debug layout data. Never comes from the route service.
    var isSample = false

    /// Crossings and shared-surface transitions numbered 1, 2, 3 in route order, by leg id, so the
    /// map markers and the written list refer to the same one.
    var junctionNumbers: [Int: Int] {
        var n = 0, out: [Int: Int] = [:]
        for l in legs where l.kind == .crossing || l.kind == .sharedSurfaceTransition { n += 1; out[l.id] = n }
        return out
    }
}

/// The surveyed area and the service's standing limits.
struct RouteCoverage: Equatable {
    var areaName: String
    var approximate: Bool
    /// Polygons, each an outer ring then holes.
    var polygons: [[[GeoCoordinate]]]
    var limitMessages: [String: String]
    var limitOrder: [String]
    var graphVersion: String?
    var release: ReleaseInfo
    var imageryFrom: String?
    var imageryTo: String?
    var snapMaxMetres: Double?

    /// Even-odd test against every ring. The service's boundary is strict, so this only lets the app
    /// say "outside" before asking; the service still decides.
    func contains(_ c: GeoCoordinate) -> Bool {
        polygons.contains { rings in
            rings.reduce(false) { inside, ring in Self.inRing(c, ring) ? !inside : inside }
        }
    }

    private static func inRing(_ p: GeoCoordinate, _ ring: [GeoCoordinate]) -> Bool {
        guard ring.count > 2 else { return false }
        var inside = false, j = ring.count - 1
        for i in ring.indices {
            let a = ring[i], b = ring[j]
            if (a.latitude > p.latitude) != (b.latitude > p.latitude),
               p.longitude < (b.longitude - a.longitude) * (p.latitude - a.latitude) / (b.latitude - a.latitude) + a.longitude {
                inside.toggle()
            }
            j = i
        }
        return inside
    }
}

/// Why the service left a footpath out.
enum ExclusionReason: Equatable {
    /// A survey photo showed a fixed barrier or steps.
    case fixedBarrier
    /// The same, but that photo is flagged for re-checking. It is still left out until re-checked.
    case fixedBarrierFlagged
    /// A survey photo showed no footpath on an ordinary street.
    case footpathAbsent
    /// A survey photo showed the footpath ending.
    case footpathEnds
    case other
}

enum RoutePlanningError: Error, Equatable {
    /// No route service is configured in this build.
    case notConnected
    case outsideCoverage(EndpointEnd?)
    /// No surveyed street close enough to join the point to.
    case tooFarFromNetwork(EndpointEnd?, maxMetres: Double?)
    /// The footpath near the point is left out by the policy, for the reasons the service gave.
    case noEligibleFootpathNearby(EndpointEnd?, reasons: [ExclusionReason])
    case sameLocation
    enum NoRouteReason: Equatable { case disconnected, blockedByExclusions, other }
    case noRoute(NoRouteReason, originIsolated: Bool, destinationIsolated: Bool)
    case busy
    case loading
    case timedOut
    case unavailable
    /// The service refused the request or failed in a way the app can't explain.
    case serviceError
    /// The reply didn't follow the contract, or wasn't a navigation-off preview.
    case invalidResponse
    /// The start is no longer valid, for example the device fix went stale.
    case startNotAvailable
}
