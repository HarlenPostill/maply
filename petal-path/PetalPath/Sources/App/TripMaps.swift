import SwiftUI
import MapKit

/// How each kind of evidence is drawn. Colour is never the only cue: each has its own line pattern,
/// and the legend and the written sections say the same in words.
extension Evidence {
    var colour: Color {
        switch self {
        case .observed: Color(red: 0.13, green: 0.42, blue: 0.27)
        case .uncertain: Color(red: 0.72, green: 0.42, blue: 0.00)
        case .unknown: Color(red: 0.42, green: 0.36, blue: 0.56)
        }
    }
    var dash: [CGFloat] {
        switch self {
        case .observed: []
        case .uncertain: [10, 6]
        case .unknown: [2, 6]
        }
    }
}

let inferredColour = Color(red: 0.35, green: 0.35, blue: 0.38)
let inferredDash: [CGFloat] = [4, 4]
/// Routine unchecked crossings: dark and neutral, not alarm red, and unlike the destination pin.
let crossingColour = ink

/// One drawn stroke of a leg: a base line plus, for a mixed leg, a sparse overlay of its
/// least-known pattern. The overlay is spread along the whole leg because where the gap lies is
/// not known; the legend and the text say so.
struct LegStroke {
    var colour: Color, dash: [CGFloat], width: CGFloat
    var overlay: (colour: Color, dash: [CGFloat])?

    init(_ l: RouteLeg) {
        switch l.kind {
        case .footpath:
            switch (l.evidence ?? EvidenceMetres(unknown: 1)).style {
            case .whole(let e): colour = e.colour; dash = e.dash; width = 5; overlay = nil
            case .mixed(let least):
                // Mostly a line, with a few small marks of the least-known kind spread along it.
                colour = Evidence.observed.colour; dash = []; width = 5
                overlay = (least.colour, least == .unknown ? [1, 16] : [5, 16])
            }
        default:
            colour = inferredColour; dash = inferredDash; width = 3; overlay = nil
        }
    }
}

/// Frames points with room around them. Pins draw upwards, so the top margin is larger, and the
/// bottom keeps markers off Apple's logo and Legal link, which the terms say must stay clear.
func mapFraming(_ points: [GeoCoordinate], minimumMetres: Double = 400) -> MapCameraPosition {
    guard let first = points.first else { return .automatic }
    var rect = MKMapRect(origin: MKMapPoint(first.clCoordinate), size: MKMapSize(width: 0, height: 0))
    for c in points.dropFirst() {
        rect = rect.union(MKMapRect(origin: MKMapPoint(c.clCoordinate), size: MKMapSize(width: 0, height: 0)))
    }
    let minimum = minimumMetres * MKMapPointsPerMeterAtLatitude(first.latitude)
    let w = max(rect.width, minimum), h = max(rect.height, minimum)
    let x = rect.midX - w / 2, y = rect.midY - h / 2
    return .rect(MKMapRect(x: x - w * 0.2, y: y - h * 0.35, width: w * 1.4, height: h * 1.75))
}

/// Shades everything outside the surveyed area, so its edge never looks like a route.
struct CoverageShade: MapContent {
    var coverage: RouteCoverage?
    var body: some MapContent {
        if let coverage, let polygon = Self.outside(coverage) {
            MapPolygon(polygon).foregroundStyle(ink.opacity(0.18)).stroke(ink.opacity(0.35), lineWidth: 1)
        }
    }

    static func outside(_ c: RouteCoverage) -> MKPolygon? {
        let rings = c.polygons.compactMap(\.first).filter { $0.count > 2 }
        let all = rings.flatMap { $0 }
        guard let minLat = all.map(\.latitude).min(), let maxLat = all.map(\.latitude).max(),
              let minLon = all.map(\.longitude).min(), let maxLon = all.map(\.longitude).max() else { return nil }
        let pad = 0.5
        var frame = [CLLocationCoordinate2D(latitude: minLat - pad, longitude: minLon - pad), CLLocationCoordinate2D(latitude: minLat - pad, longitude: maxLon + pad),
                     CLLocationCoordinate2D(latitude: maxLat + pad, longitude: maxLon + pad), CLLocationCoordinate2D(latitude: maxLat + pad, longitude: minLon - pad)]
        let holes = rings.map { r -> MKPolygon in var cs = r.map(\.clCoordinate); return MKPolygon(coordinates: &cs, count: cs.count) }
        return MKPolygon(coordinates: &frame, count: frame.count, interiorPolygons: holes)
    }
}

/// A small Apple map that shows search results and the chosen ends. Apple's terms require search
/// results to appear with an Apple map, so this sits beside them, with its legal label left clear.
struct PlaceMap: View {
    var region: MKCoordinateRegion
    var start: TripEndpoint?
    var destination: Place?
    var coverage: RouteCoverage?
    var height: CGFloat = 190

    /// Refit when an end moves by more than about 10 m, not on every GPS jitter.
    private var fitKey: String {
        func r(_ c: GeoCoordinate?) -> String { c.map { String(format: "%.4f,%.4f", $0.latitude, $0.longitude) } ?? "-" }
        return "\(r(start?.coordinate))|\(r(destination?.coordinate))|\(coverage == nil)"
    }

    var body: some View {
        // Not draggable: it sits in a scrolling sheet, where a draggable map steals the scroll.
        let ends = [start?.coordinate, destination?.coordinate].compactMap { $0 }
        Map(initialPosition: ends.isEmpty ? .region(region) : mapFraming(ends), interactionModes: []) {
            CoverageShade(coverage: coverage)
            if let start {
                Marker(start.label, systemImage: "figure.roll", coordinate: start.coordinate.clCoordinate).tint(olive)
            }
            if let destination {
                Marker(destination.name, coordinate: destination.coordinate.clCoordinate).tint(.red)
            }
        }
        .mapStyle(.standard(pointsOfInterest: .excludingAll))
        .id(fitKey)
        .frame(height: height)
        .clipShape(RoundedRectangle(cornerRadius: 16))
    }
}

/// The whole route on an Apple map. Small and still inside the sheet, or large and interactive in
/// its own sheet. The written sections carry every fact it shows.
struct RouteOverviewMap: View {
    var plan: RoutePlan
    var origin: TripEndpoint?
    var destination: Place?
    var interactive = false

    private var lines: [RouteLeg] { plan.legs.filter { $0.coordinates.count > 1 } }
    private var junctions: [RouteLeg] {
        plan.legs.filter { ($0.kind == .crossing || $0.kind == .sharedSurfaceTransition) && !$0.coordinates.isEmpty }
    }

    var body: some View {
        let points = plan.legs.flatMap(\.coordinates) + [origin?.coordinate, destination?.coordinate].compactMap { $0 }
        let numbers = plan.junctionNumbers
        Map(initialPosition: mapFraming(points), interactionModes: interactive ? .all : []) {
            ForEach(lines) { l in
                MapPolyline(coordinates: l.coordinates.map(\.clCoordinate))
                    .stroke(.white, style: StrokeStyle(lineWidth: LegStroke(l).width + 4, lineCap: .round, lineJoin: .round))
            }
            ForEach(lines) { l in
                let s = LegStroke(l)
                MapPolyline(coordinates: l.coordinates.map(\.clCoordinate))
                    .stroke(s.colour, style: StrokeStyle(lineWidth: s.width, lineCap: .round, lineJoin: .round, dash: s.dash))
            }
            ForEach(lines.filter { LegStroke($0).overlay != nil }) { l in
                let o = LegStroke(l).overlay!
                MapPolyline(coordinates: l.coordinates.map(\.clCoordinate))
                    .stroke(o.colour, style: StrokeStyle(lineWidth: 4, lineCap: .round, lineJoin: .round, dash: o.dash))
            }
            ForEach(junctions) { l in
                Annotation(TripCopy.legTitle(l, number: numbers[l.id]), coordinate: l.coordinates[0].clCoordinate) {
                    Text(numbers[l.id].map(String.init) ?? "!")
                        .font(.system(size: 11, weight: .heavy, design: .rounded)).monospacedDigit()
                        .foregroundStyle(.white).frame(minWidth: 20, minHeight: 20)
                        .background(crossingColour, in: Circle())
                        .overlay(Circle().stroke(.white, lineWidth: 2))
                }
                .annotationTitles(.hidden)
            }
            if let origin {
                Marker(origin.label, systemImage: "figure.roll", coordinate: origin.coordinate.clCoordinate).tint(olive)
            }
            if let destination {
                Marker(destination.name, coordinate: destination.coordinate.clCoordinate).tint(.red)
            }
        }
        .mapStyle(.standard(emphasis: .muted, pointsOfInterest: .excludingAll))
        .mapControls {
            MapScaleView()
            if interactive { MapCompass(); MapPitchToggle() }
        }
    }
}

/// A short line drawn in a leg's pattern, for the legend and the list.
struct LineSwatch: View {
    var colour: Color
    var dash: [CGFloat]
    var width: CGFloat = 5
    var overlay: (colour: Color, dash: [CGFloat])? = nil
    var body: some View {
        Canvas { ctx, size in
            var p = Path(); p.move(to: CGPoint(x: 3, y: size.height / 2)); p.addLine(to: CGPoint(x: size.width - 3, y: size.height / 2))
            ctx.stroke(p, with: .color(colour), style: StrokeStyle(lineWidth: width, lineCap: .round, dash: dash))
            if let overlay { ctx.stroke(p, with: .color(overlay.colour), style: StrokeStyle(lineWidth: width, lineCap: .round, dash: overlay.dash)) }
        }
        .frame(width: 34, height: 14)
        .accessibilityHidden(true)
    }
}

extension LineSwatch {
    init(evidence: Evidence) { self.init(colour: evidence.colour, dash: evidence.dash) }
    init(_ s: LegStroke) { self.init(colour: s.colour, dash: s.dash, width: s.width, overlay: s.overlay) }
    static var mixed: LineSwatch {
        LineSwatch(colour: Evidence.observed.colour, dash: [], overlay: (Evidence.unknown.colour, [1, 10]))
    }
}

/// The legend, shared by the small and the large map.
struct RouteLegend: View {
    var showsCrossings: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            row(LineSwatch(evidence: .observed), TripCopy.evidence(.observed))
            row(LineSwatch(evidence: .uncertain), TripCopy.evidence(.uncertain))
            row(LineSwatch(evidence: .unknown), TripCopy.evidence(.unknown))
            row(.mixed, TripCopy.legendMixed)
            row(LineSwatch(colour: inferredColour, dash: inferredDash, width: 3), TripCopy.legendInferred)
            if showsCrossings {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("1").font(.system(size: 10, weight: .heavy, design: .rounded)).foregroundStyle(.white)
                        .frame(width: 18, height: 18).background(crossingColour, in: Circle()).frame(width: 34)
                    Text(TripCopy.legendCrossing).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .font(.system(.footnote, design: .rounded)).foregroundStyle(ink)
        .accessibilityHidden(true)     // the same facts are in the sections, in words
    }

    private func row(_ s: LineSwatch, _ t: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) { s; Text(t).fixedSize(horizontal: false, vertical: true) }
    }
}

/// The overview, large and interactive. Apple's logo and Legal link stay in their own corner.
struct RouteMapSheet: View {
    var plan: RoutePlan
    var origin: TripEndpoint?
    var destination: Place?
    @Environment(\.dismiss) private var dismiss
    @State private var legendShown = true

    var body: some View {
        VStack(spacing: 0) {
            AdaptiveRow {
                Text(TripCopy.mapTitle).font(.system(.title3, design: .rounded, weight: .bold)).foregroundStyle(ink)
                    .accessibilityAddTraits(.isHeader)
            } trailing: {
                Button("Done") { dismiss() }.font(.system(.body, design: .rounded, weight: .semibold)).tint(olive).frame(minHeight: 44)
            }
            .padding(.horizontal, 18).padding(.vertical, 10)
            RouteOverviewMap(plan: plan, origin: origin, destination: destination, interactive: true)
                .accessibilityLabel("Interactive route map")
                .accessibilityHint(TripCopy.overviewHint)
            DisclosureGroup(isExpanded: $legendShown) {
                VStack(alignment: .leading, spacing: 6) {
                    RouteLegend(showsCrossings: plan.crossings + plan.sharedSurfaceTransitions > 0)
                    Text(TripCopy.mixedRule).font(.system(.caption, design: .rounded)).foregroundStyle(ink.opacity(0.8))
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.top, 6)
            } label: {
                Text("Key").font(.system(.subheadline, design: .rounded, weight: .bold)).foregroundStyle(ink)
            }
            .tint(olive)
            .padding(.horizontal, 18).padding(.vertical, 10)
        }
        .background(cream)
        .environment(\.colorScheme, .light)      // same fixed palette as the trip sheet
    }
}
