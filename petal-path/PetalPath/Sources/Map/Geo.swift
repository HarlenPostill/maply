import Foundation
import simd

struct GeoCoordinate: Equatable {
    var latitude: Double, longitude: Double
    /// Alfred Street at Circular Quay, where the harbour, the CBD and the Botanic Garden all meet.
    static let circularQuay = GeoCoordinate(latitude: -33.8619, longitude: 151.2107)
}

/// Web Mercator in metres from the map's north-west corner: x east, y south.
enum Mercator {
    static let circumference = 40_075_016.685_578_49

    static func project(_ c: GeoCoordinate) -> SIMD2<Double> {
        let lat = c.latitude * .pi / 180
        return SIMD2((c.longitude + 180) / 360, (1 - asinh(tan(lat)) / .pi) / 2) * circumference
    }

    static func unproject(_ m: SIMD2<Double>) -> GeoCoordinate {
        let n = m / circumference
        return GeoCoordinate(latitude: atan(sinh(.pi * (1 - 2 * n.y))) * 180 / .pi, longitude: n.x * 360 - 180)
    }

    /// Mercator stretches by 1/cos(latitude). Multiply a Mercator length by this to get real metres.
    static func groundScale(latitude: Double) -> Double { cos(latitude * .pi / 180) }
}

/// Keeps ground patterns glued to the world as the player moves.
///
/// Mercator metres have to become real metres for the patterns, but the factor changes with latitude.
/// Multiplying a position of 24 million metres by the player's own factor slides everything by metres
/// per metre walked, so the factor is held still and only rebased after a long trip or a teleport.
struct PatternAnchor {
    private static let wrap = 65_536.0
    private(set) var scale: Double?

    mutating func offset(at m: SIMD2<Double>, groundScale g: Double) -> SIMD2<Float> {
        if scale == nil || abs(scale! - g) > 0.003 { scale = g }
        let p = m * scale!
        return SIMD2(Float(p.x.truncatingRemainder(dividingBy: Self.wrap)), Float(p.y.truncatingRemainder(dividingBy: Self.wrap)))
    }
}

struct TileID: Hashable, CustomStringConvertible {
    /// OpenMapTiles stops at z14: about 2 km square at Sydney's latitude.
    static let zoom = 14
    static let size = Mercator.circumference / Double(1 << zoom)
    var x: Int, y: Int

    init(x: Int, y: Int) { self.x = x; self.y = y }
    init(containing m: SIMD2<Double>) { x = Int(floor(m.x / Self.size)); y = Int(floor(m.y / Self.size)) }

    var origin: SIMD2<Double> { SIMD2(Double(x), Double(y)) * Self.size }
    var centre: SIMD2<Double> { origin + SIMD2(repeating: Self.size / 2) }
    var description: String { "\(Self.zoom)/\(x)/\(y)" }

    /// Squared distance from a point to the nearest edge of this tile.
    func distanceSquared(to m: SIMD2<Double>) -> Double {
        let d = simd_abs(m - centre) - SIMD2(repeating: Self.size / 2)
        let o = simd_max(d, .zero)
        return simd_length_squared(o)
    }
}
