import Foundation

/// Reads `maply-route-preview/1` (atlas/ROUTING_API.md) into app types. Strict about the safety
/// fields: any reply that is not an unverified preview with navigation off is refused.
enum RoutePreviewDecoding {
    static let apiVersion = "maply-route-preview/1"

    /// What an error reply means. `snapshotChanged` is handled by the client, not shown.
    enum Failure: Error, Equatable {
        case snapshotChanged
        case planning(RoutePlanningError)
    }

    private static var decoder: JSONDecoder {
        let d = JSONDecoder(); d.keyDecodingStrategy = .convertFromSnakeCase; return d
    }

    // MARK: Request

    static func requestBody(_ r: RoutePlanRequest, expectedGraphVersion: String?) throws -> Data {
        struct Point: Encodable { var lat: Double; var lng: Double }
        struct Body: Encodable { var origin: Point; var destination: Point; var expected_graph_version: String? }
        func p(_ c: GeoCoordinate) -> Point { Point(lat: c.latitude, lng: c.longitude) }
        return try JSONEncoder().encode(Body(origin: p(r.origin.coordinate), destination: p(r.destination.coordinate),
                                             expected_graph_version: expectedGraphVersion))
    }

    // MARK: Route preview

    static func plan(_ data: Data) throws -> RoutePlan {
        let d: PreviewDTO
        do { d = try decoder.decode(PreviewDTO.self, from: data) } catch { throw RoutePlanningError.invalidResponse }
        guard d.apiVersion == apiVersion, d.status == "unverified_preview", d.navigationAllowed == false else {
            throw RoutePlanningError.invalidResponse
        }
        let s = d.summary
        return RoutePlan(
            routeMetres: s.routeM, streetMetres: s.streetM, connectorMetres: s.connectorM ?? 0, endpointGapMetres: s.endpointGapM ?? 0,
            evidence: s.evidenceM.model, designation: s.designationM?.model ?? DesignationMetres(),
            imagery: s.imageryAgeM?.model ?? ImageryAgeMetres(), imageryMonths: s.imageryMonths?.model,
            crossings: s.crossings?.count ?? 0, sharedSurfaceTransitions: s.crossings?.sharedSurfaceTransitions ?? 0,
            connectors: s.connectors?.count ?? 0, observations: s.observations?.count ?? 0,
            flaggedObservations: s.observations?.flaggedUncertain ?? 0,
            slope: d.slopeObservations?.model ?? SlopeObservations(),
            warnings: d.warnings.enumerated().map { i, w in w.model(i) }, warningsTruncated: d.warningsTruncated ?? false,
            legs: d.legs.map(\.model),
            origin: d.endpoints?.origin.model, destination: d.endpoints?.destination.model,
            limits: d.limits ?? [],
            release: ReleaseInfo(id: d.release?.id, publishedAt: d.release?.publishedAt, graphVersion: d.graphVersion,
                                 asOfDate: d.asOfDate, policyVersion: d.policyVersion))
    }

    // MARK: Coverage

    static func coverage(_ data: Data) throws -> RouteCoverage {
        let d: CoverageDTO
        do { d = try decoder.decode(CoverageDTO.self, from: data) } catch { throw RoutePlanningError.invalidResponse }
        guard d.apiVersion == apiVersion, d.navigationAllowed == false else { throw RoutePlanningError.invalidResponse }
        let limits = d.limits ?? []
        return RouteCoverage(
            areaName: d.area.name ?? "the surveyed area", approximate: d.area.approximate ?? true,
            polygons: d.area.geometry.polygons,
            limitMessages: Dictionary(limits.map { ($0.code, $0.message) }, uniquingKeysWith: { a, _ in a }),
            limitOrder: limits.map(\.code), graphVersion: d.snapshot?.graphVersion,
            release: ReleaseInfo(id: d.release?.id, publishedAt: d.release?.publishedAt, graphVersion: d.snapshot?.graphVersion,
                                 asOfDate: d.snapshot?.asOfDate, policyVersion: d.policy?.version),
            imageryFrom: d.survey?.imageryFrom, imageryTo: d.survey?.imageryTo, snapMaxMetres: d.snapMaxM)
    }

    // MARK: Errors

    static func failure(status: Int, data: Data) -> Failure {
        struct Envelope: Decodable { struct E: Decodable { var code: String; var details: Details? }; var error: E }
        struct Details: Decodable {
            var which: String?; var maxM: Double?; var reason: String?
            var originInMainNetwork: Bool?; var destinationInMainNetwork: Bool?
            var excludedReasons: [String]?
        }
        // Rate limited, by the service or by the host in front of it: an existing state, "busy".
        if status == 429 { return .planning(.busy) }
        guard let e = try? decoder.decode(Envelope.self, from: data).error else {
            // No contract body: a gateway or host page, not the service itself.
            return .planning([502, 503, 504].contains(status) ? .unavailable : .serviceError)
        }
        let which: EndpointEnd? = e.details?.which == "origin" ? .origin : e.details?.which == "destination" ? .destination : nil
        switch e.code {
        case "outside_coverage": return .planning(.outsideCoverage(which))
        case "too_far_from_network": return .planning(.tooFarFromNetwork(which, maxMetres: e.details?.maxM))
        case "no_eligible_footpath_nearby":
            let reasons: [ExclusionReason] = (e.details?.excludedReasons ?? []).map {
                switch $0 {
                case "fixed_barrier": .fixedBarrier
                case "fixed_barrier_flagged": .fixedBarrierFlagged
                case "footpath_absent": .footpathAbsent
                case "footpath_ends": .footpathEnds
                default: .other
                }
            }
            return .planning(.noEligibleFootpathNearby(which, reasons: reasons))
        case "same_location": return .planning(.sameLocation)
        case "no_route":
            let reason: RoutePlanningError.NoRouteReason = switch e.details?.reason {
            case "disconnected": .disconnected
            case "blocked_by_exclusions": .blockedByExclusions
            default: .other
            }
            return .planning(.noRoute(reason, originIsolated: e.details?.originInMainNetwork == false,
                                      destinationIsolated: e.details?.destinationInMainNetwork == false))
        case "snapshot_changed": return .snapshotChanged
        case "busy": return .planning(.busy)
        case "data_unavailable": return .planning(.loading)
        case "route_timeout": return .planning(.timedOut)
        default: return .planning(.serviceError)
        }
    }
}

// MARK: Wire types

private struct ReleaseDTO: Decodable { var id: Int?; var publishedAt: String? }
private struct MonthsDTO: Decodable {
    var from: String?; var to: String?
    var model: MonthRange? { from.flatMap { f in to.map { MonthRange(from: f, to: $0) } } }
}
private struct EvidenceDTO: Decodable {
    var observedM: Double; var uncertainM: Double; var unknownM: Double
    var model: EvidenceMetres { EvidenceMetres(observed: observedM, uncertain: uncertainM, unknown: unknownM) }
}
private struct ImageryDTO: Decodable {
    var within3yM: Double?; var olderThan3yM: Double?; var undatedM: Double?; var noObservationM: Double?

    /// Keys with a digit ("within_3y_m") come out of snake-case conversion in more than one
    /// spelling, so they are matched without regard to case rather than silently read as zero.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        func value(_ name: String) throws -> Double? {
            guard let k = c.allKeys.first(where: { $0.stringValue.lowercased() == name }) else { return nil }
            return try c.decode(Double.self, forKey: k)
        }
        within3yM = try value("within3ym"); olderThan3yM = try value("olderthan3ym")
        undatedM = try value("undatedm"); noObservationM = try value("noobservationm")
    }
    var model: ImageryAgeMetres {
        ImageryAgeMetres(withinThreeYears: within3yM ?? 0, olderThanThreeYears: olderThan3yM ?? 0,
                         undated: undatedM ?? 0, noObservation: noObservationM ?? 0)
    }
}
private struct DesignationDTO: Decodable {
    var ordinaryM: Double?; var designatedPedestrianM: Double?; var designatedSharedM: Double?; var ambiguousM: Double?
    var model: DesignationMetres {
        DesignationMetres(ordinary: ordinaryM ?? 0, pedestrian: designatedPedestrianM ?? 0, shared: designatedSharedM ?? 0, ambiguous: ambiguousM ?? 0)
    }
}

private struct SummaryDTO: Decodable {
    struct Crossings: Decodable { var count: Int; var sharedSurfaceTransitions: Int? }
    struct Count: Decodable { var count: Int }
    struct Observations: Decodable { var count: Int; var flaggedUncertain: Int? }
    var routeM: Double; var streetM: Double; var connectorM: Double?; var endpointGapM: Double?
    var evidenceM: EvidenceDTO; var designationM: DesignationDTO?; var imageryAgeM: ImageryDTO?
    var imageryMonths: MonthsDTO?; var crossings: Crossings?; var connectors: Count?; var observations: Observations?
}

private struct EndpointDTO: Decodable {
    var gapM: Double; var streetName: String?; var facing: String?; var sideAmbiguous: Bool?
    var model: MatchedEndpoint { MatchedEndpoint(gapMetres: gapM, streetName: streetName, facing: facing, sideAmbiguous: sideAmbiguous ?? false) }
}
private struct EndpointsDTO: Decodable { var origin: EndpointDTO; var destination: EndpointDTO }

private struct WarningDTO: Decodable {
    var code: String; var severity: String; var leg: Int?; var at: [Double]?; var observedOn: String?; var message: String
    func model(_ i: Int) -> ServiceWarning {
        // Anything not marked info is treated as prominent, so an unfamiliar severity is never played down.
        ServiceWarning(id: i, code: code, severity: severity == "info" ? .info : .prominent, leg: leg,
                       at: at.flatMap(lngLat), observedOn: observedOn, message: message)
    }
}

private struct AnyKey: CodingKey {
    var stringValue: String; var intValue: Int? { nil }
    init(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { nil }
}

private func lngLat(_ a: [Double]) -> GeoCoordinate? { a.count >= 2 ? GeoCoordinate(latitude: a[1], longitude: a[0]) : nil }

/// GeoJSON Point, LineString, Polygon or MultiPolygon.
private struct GeometryDTO: Decodable {
    var points: [GeoCoordinate] = []
    var polygons: [[[GeoCoordinate]]] = []

    enum CodingKeys: String, CodingKey { case type, coordinates }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        switch try c.decode(String.self, forKey: .type) {
        case "Point": points = lngLat(try c.decode([Double].self, forKey: .coordinates)).map { [$0] } ?? []
        case "LineString": points = try c.decode([[Double]].self, forKey: .coordinates).compactMap(lngLat)
        case "Polygon": polygons = [try c.decode([[[Double]]].self, forKey: .coordinates).map { $0.compactMap(lngLat) }]
        case "MultiPolygon": polygons = try c.decode([[[[Double]]]].self, forKey: .coordinates).map { $0.map { $0.compactMap(lngLat) } }
        default: break
        }
    }
}

private struct LegDTO: Decodable {
    struct Nearby: Decodable {
        var kerbRampPresent: Int?; var kerbRampAbsent: Int?; var kerbRampRaisedLipOrStep: Int?
        var crossingUnmodifiedKerb: Int?; var crossingSteps: Int?; var crossingLevelOrRamp: Int?
    }
    var index: Int; var kind: String; var geometry: GeometryDTO?; var lengthM: Double?
    var streetName: String?; var crossedStreet: String?; var facing: String?; var designation: String?
    var evidenceM: EvidenceDTO?; var imageryAgeM: ImageryDTO?; var imageryMonths: MonthsDTO?
    var observations: Int?; var flaggedUncertain: Int?; var which: String?; var nearbyObservations: Nearby?

    var model: RouteLeg {
        let kind: RouteLeg.Kind = switch self.kind {
        case "footpath_side": .footpath
        case "shared_surface_transition": .sharedSurfaceTransition
        case "connector": .connector
        case "endpoint_gap": .endpointGap
        default: .crossing     // an unfamiliar junction step is shown as an unchecked crossing
        }
        let designation: Designation? = switch self.designation {
        case "ordinary": .ordinary
        case "designated_pedestrian": .pedestrian
        case "designated_shared": .shared
        case nil: nil
        default: .ambiguous
        }
        return RouteLeg(
            id: index, kind: kind, coordinates: geometry?.points ?? [], lengthMetres: lengthM,
            streetName: streetName ?? crossedStreet, facing: facing, designation: designation,
            evidence: evidenceM?.model, imagery: imageryAgeM?.model, imageryMonths: imageryMonths?.model,
            observations: observations, flaggedUncertain: flaggedUncertain,
            gapEnd: which == "origin" ? .origin : which == "destination" ? .destination : nil,
            nearby: nearbyObservations.map {
                NearbyCrossingObservations(kerbRampPresent: $0.kerbRampPresent ?? 0, kerbRampAbsent: $0.kerbRampAbsent ?? 0,
                                           kerbRampLipOrStep: $0.kerbRampRaisedLipOrStep ?? 0,
                                           crossingUnmodifiedKerb: $0.crossingUnmodifiedKerb ?? 0,
                                           crossingSteps: $0.crossingSteps ?? 0, crossingLevelOrRamp: $0.crossingLevelOrRamp ?? 0)
            })
    }
}

/// `slope_observations`: feature → axis → category → count, plus two plain fields.
private struct SlopeDTO: Decodable {
    var model = SlopeObservations()

    struct Key: CodingKey {
        var stringValue: String; var intValue: Int? { nil }
        init(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }
    init(from decoder: Decoder) throws {
        // Keys arrive camel-cased by the decoder's strategy.
        let features: [String: SlopeObservations.Feature] = ["footpath": .footpath, "driveway": .driveway, "kerbRamp": .kerbRamp, "crossing": .crossing]
        let axes: [String: SlopeObservations.Axis] = ["slope": .slope, "longitudinal": .longitudinal, "cross": .cross]
        // Nested dictionary keys may or may not be converted by the key strategy, so accept both.
        let categories: [String: SlopeObservations.Category] = ["apparentlyLevel": .apparentlyLevel, "apparently_level": .apparentlyLevel,
                                                                "gentle": .gentle, "moderate": .moderate, "steep": .steep]
        let c = try decoder.container(keyedBy: Key.self)
        // A slope count under a feature this app doesn't know yet must not vanish silently.
        let known = Set(features.keys).union(["observationsWithoutSlope", "unit"])
        if c.allKeys.contains(where: { !known.contains($0.stringValue) }) { model.hasUnreadFeatures = true }
        model.observationsWithoutSlope = try c.decodeIfPresent(Int.self, forKey: Key(stringValue: "observationsWithoutSlope")) ?? 0
        for (name, feature) in features {
            guard let byAxis = try? c.decode([String: [String: Int]].self, forKey: Key(stringValue: name)) else { continue }
            for (axisName, counts) in byAxis {
                guard let axis = axes[axisName] else { model.hasUnreadFeatures = true; continue }
                for (catName, n) in counts where n > 0 {
                    guard let cat = categories[catName] else { model.hasUnreadFeatures = true; continue }
                    model.entries.append(.init(feature: feature, axis: axis, category: cat, count: n))
                }
            }
        }
        let fOrder = SlopeObservations.Feature.allCases, aOrder = SlopeObservations.Axis.allCases, cOrder = SlopeObservations.Category.allCases
        model.entries.sort {
            (cOrder.firstIndex(of: $0.category)!, fOrder.firstIndex(of: $0.feature)!, aOrder.firstIndex(of: $0.axis)!)
                > (cOrder.firstIndex(of: $1.category)!, fOrder.firstIndex(of: $1.feature)!, aOrder.firstIndex(of: $1.axis)!)
        }
    }
}

private struct PreviewDTO: Decodable {
    var apiVersion: String; var status: String; var navigationAllowed: Bool
    var release: ReleaseDTO?; var graphVersion: String?; var policyVersion: String?; var asOfDate: String?
    var endpoints: EndpointsDTO?; var summary: SummaryDTO; var slopeObservations: SlopeDTO?
    var warnings: [WarningDTO]; var warningsTruncated: Bool?; var legs: [LegDTO]; var limits: [String]?
}

private struct CoverageDTO: Decodable {
    struct Area: Decodable { var name: String?; var approximate: Bool?; var geometry: GeometryDTO }
    struct Snapshot: Decodable { var graphVersion: String?; var asOfDate: String? }
    struct Survey: Decodable { var imageryFrom: String?; var imageryTo: String? }
    struct Policy: Decodable { var version: String? }
    struct Limit: Decodable { var code: String; var message: String }
    var apiVersion: String; var release: ReleaseDTO?; var snapshot: Snapshot?; var area: Area
    var survey: Survey?; var policy: Policy?; var limits: [Limit]?; var snapMaxM: Double?; var navigationAllowed: Bool
}
