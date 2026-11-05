#if DEBUG
import Foundation

/// Debug-only layout data for checking the planning screens without the route service.
/// A straight line cut into legs with made-up facts. Every plan it returns is marked `isSample`,
/// and the summary says so at the top.
struct SampleRoutePlanningClient: RoutePlanningClient {
    func coverage() async throws -> RouteCoverage? { nil }

    func plan(_ request: RoutePlanRequest) async throws -> RoutePlan {
        try await Task.sleep(for: .milliseconds(400))
        let a = request.origin.coordinate, b = request.destination.coordinate
        let total = a.distance(to: b)
        func at(_ k: Double) -> GeoCoordinate {
            GeoCoordinate(latitude: a.latitude + (b.latitude - a.latitude) * k, longitude: a.longitude + (b.longitude - a.longitude) * k)
        }
        let gap = total * 0.03, first = total * 0.45, second = total * 0.49
        let legs = [
            RouteLeg(id: 0, kind: .endpointGap, coordinates: [at(0), at(0.03)], lengthMetres: gap, gapEnd: .origin),
            RouteLeg(id: 1, kind: .footpath, coordinates: [at(0.03), at(0.48)], lengthMetres: first, streetName: "Sample Street",
                     facing: "east", designation: .ordinary, evidence: EvidenceMetres(observed: first),
                     imagery: ImageryAgeMetres(withinThreeYears: first), imageryMonths: MonthRange(from: "2023-02", to: "2024-03"),
                     observations: 12, flaggedUncertain: 0),
            RouteLeg(id: 2, kind: .crossing, coordinates: [at(0.48)], streetName: "Example Lane",
                     nearby: NearbyCrossingObservations(kerbRampPresent: 1)),
            RouteLeg(id: 3, kind: .footpath, coordinates: [at(0.48), at(0.97)], lengthMetres: second, streetName: "Placeholder Road",
                     facing: "south", designation: .ordinary,
                     evidence: EvidenceMetres(observed: second * 0.5, uncertain: second * 0.2, unknown: second * 0.3),
                     imagery: ImageryAgeMetres(olderThanThreeYears: second * 0.7, noObservation: second * 0.3),
                     imageryMonths: MonthRange(from: "2019-06", to: "2019-06"), observations: 8, flaggedUncertain: 2),
            RouteLeg(id: 4, kind: .endpointGap, coordinates: [at(0.97), at(1)], lengthMetres: gap, gapEnd: .destination),
        ]
        let warnings = [
            ServiceWarning(id: 0, code: "crossing_unverified", severity: .prominent, leg: 2, at: at(0.48), observedOn: nil,
                           message: "Crossing Example Lane: the crossing point, kerb ramps and kerb heights are not known."),
            ServiceWarning(id: 1, code: "blocking_permanence_unknown", severity: .prominent, leg: 3, at: at(0.6), observedOn: "2019-06",
                           message: "A barrier blocked the path when imaged. Whether it is fixed or temporary is not recorded."),
            ServiceWarning(id: 2, code: "flagged_observation", severity: .info, leg: 3, at: at(0.7), observedOn: "2019-06",
                           message: "Observation flagged for re-checking; treat as uncertain."),
            ServiceWarning(id: 3, code: "endpoint_gap_not_assessed", severity: .info, leg: 0, at: at(0), observedOn: nil,
                           message: "The \(Int(gap)) m between the origin and the footpath is not assessed."),
        ]
        return RoutePlan(
            routeMetres: first + second, streetMetres: first + second, connectorMetres: 0, endpointGapMetres: gap * 2,
            evidence: EvidenceMetres(observed: first + second * 0.5, uncertain: second * 0.2, unknown: second * 0.3),
            designation: DesignationMetres(ordinary: first + second),
            imagery: ImageryAgeMetres(withinThreeYears: first, olderThanThreeYears: second * 0.7, noObservation: second * 0.3),
            imageryMonths: MonthRange(from: "2019-06", to: "2024-03"), crossings: 1, sharedSurfaceTransitions: 0, connectors: 0,
            observations: 20, flaggedObservations: 2,
            slope: SlopeObservations(entries: [.init(feature: .footpath, axis: .longitudinal, category: .moderate, count: 1)],
                                     observationsWithoutSlope: 19),
            warnings: warnings, legs: legs,
            origin: MatchedEndpoint(gapMetres: gap, streetName: "Sample Street", facing: "east", sideAmbiguous: false),
            destination: MatchedEndpoint(gapMetres: gap, streetName: "Placeholder Road", facing: "south", sideAmbiguous: false),
            limits: ["preview_only", "crossings_inferred_at_junctions", "width_not_measured", "slope_categorical_only"],
            release: ReleaseInfo(id: nil, publishedAt: nil, graphVersion: "sample", asOfDate: nil, policyVersion: "sample"),
            isSample: true)
    }
}
#endif
