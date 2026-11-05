import XCTest
import simd
@testable import PetalPath

final class MapTests: XCTestCase {
    func testMercatorRoundTrip() {
        let c = GeoCoordinate.circularQuay
        let back = Mercator.unproject(Mercator.project(c))
        XCTAssertEqual(back.latitude, c.latitude, accuracy: 1e-9)
        XCTAssertEqual(back.longitude, c.longitude, accuracy: 1e-9)
    }

    /// Slippy-map tile numbers for Circular Quay, worked out independently.
    func testTileContainingCircularQuay() {
        let id = TileID(containing: Mercator.project(.circularQuay))
        XCTAssertEqual(id, TileID(x: 15073, y: 9831))
    }

    func testSquareWithHoleTriangulatesToItsArea() {
        let outer: [SIMD2<Float>] = [SIMD2(0, 0), SIMD2(10, 0), SIMD2(10, 10), SIMD2(0, 10)]
        let hole: [SIMD2<Float>] = [SIMD2(4, 4), SIMD2(4, 6), SIMD2(6, 6), SIMD2(6, 4)]
        XCTAssertEqual(triangulatedArea([outer, hole]), 96, accuracy: 0.001)
    }

    func testConcaveLargeRingUsesZOrderPathAndKeepsArea() {
        // A comb with more than 80 points, which switches on the hashed ear test.
        var ring: [SIMD2<Float>] = []
        for i in 0..<45 {
            ring.append(SIMD2(Float(i) * 2, 0)); ring.append(SIMD2(Float(i) * 2 + 1, 5))
        }
        ring.append(SIMD2(90, 20)); ring.append(SIMD2(-2, 20))
        XCTAssertEqual(triangulatedArea([ring]), abs(signedArea(ring)), accuracy: 0.01)
    }

    func testVectorTileGeometryDecodes() {
        // Layer "building", one polygon: a 10 by 10 square with its exterior ring positive.
        let g: [UInt8] = [
            0x09, 0x00, 0x00,                   // MoveTo(0, 0)
            0x1A, 0x14, 0x00, 0x00, 0x14, 0x13, 0x00,  // LineTo x3: (+10,0) (0,+10) (-10,0)
            0x0F                                // ClosePath
        ]
        let feature: [UInt8] = [0x18, 0x03, 0x22, UInt8(g.count)] + g
        let name = Array("building".utf8)
        var layer: [UInt8] = [0x0A, UInt8(name.count)] + name
        layer += [0x12, UInt8(feature.count)] + feature
        layer += [0x28, 0x80, 0x20]             // extent 4096
        let tile = MVTTile(data: Data([0x1A, UInt8(layer.count)] + layer), only: ["building"])
        let l = tile.layers["building"]!
        XCTAssertEqual(l.extent, 4096)
        XCTAssertEqual(l.features.count, 1)
        let p = l.polygons(l.features[0], scale: 1)
        XCTAssertEqual(p.count, 1)
        XCTAssertEqual(p[0].area, 100, accuracy: 0.001)
    }

    func testJoystickSourceMovesNorthWhenSteeredNorth() {
        let s = JoystickLocationSource(start: .circularQuay)
        s.steer(SIMD2(0, 10))
        for _ in 0..<120 { s.tick(dt: 1.0 / 60) }
        XCTAssertGreaterThan(s.coordinate.latitude, GeoCoordinate.circularQuay.latitude)
        XCTAssertEqual(s.coordinate.longitude, GeoCoordinate.circularQuay.longitude, accuracy: 1e-9)
    }

    /// The conveyor-belt bug: a spot on the ground must keep its pattern as the player walks away.
    func testGroundPatternStaysPutWhileWalking() {
        var anchor = PatternAnchor()
        let spot = Mercator.project(.circularQuay) + SIMD2(120, 80)
        func patternAtSpot(_ player: GeoCoordinate) -> SIMD2<Float> {
            let m = Mercator.project(player), g = Mercator.groundScale(latitude: player.latitude)
            return SIMD2<Float>((spot - m) * g) + anchor.offset(at: m, groundScale: g)
        }
        let start = patternAtSpot(.circularQuay)
        for metres in stride(from: 10.0, through: 500, by: 10) {
            let c = GeoCoordinate(latitude: GeoCoordinate.circularQuay.latitude - metres / 111_320,
                                  longitude: GeoCoordinate.circularQuay.longitude + metres / 92_000)
            XCTAssertEqual(simd_distance(patternAtSpot(c), start), 0, accuracy: 0.05, "after \(metres) m")
        }
    }

    func testHorizonComesCloserWithALowerCamera() {
        let high = GameScene.horizonDistance(eye: SIMD3(0, 245, 175))
        let low = GameScene.horizonDistance(eye: SIMD3(0, 144, 263))
        XCTAssertEqual(high, 745, accuracy: 3)
        XCTAssertEqual(low, 478, accuracy: 3)
    }

    func testBunchesIsGoneAndItsMeshStaysHidden() {
        XCTAssertFalse(HairStyle.allCases.map(\.rawValue).contains("Bunches"))
        XCTAssertTrue(Wardrobe.slottedParts.contains("SK_Hair_Bunches"))
        XCTAssertFalse(Outfit().meshes.contains("SK_Hair_Bunches"))
    }

    func testOutfitSavedWithBunchesKeepsTheRestOfTheLook() throws {
        let json = #"{"hair":"Bunches","top":"Daisy","bottoms":"Skirt","shoeStyle":"Mary Janes","shoes":"Blue"}"#
        let o = try JSONDecoder().decode(Outfit.self, from: Data(json.utf8))
        XCTAssertEqual(o, Outfit(hair: .short, top: .daisy, bottoms: .skirt, shoeStyle: .maryJanes, shoes: .blue))
    }

    func testGPSFirstFixPlacesThenEasesAndIgnoresJitter() {
        let g = GPSLocationSource(start: .circularQuay)
        let home = GeoCoordinate(latitude: 51.5007, longitude: -0.1246)
        g.receive(home, accuracy: 10)
        XCTAssertTrue(g.hasFix)
        XCTAssertEqual(g.coordinate, home)

        // 2 m with 10 m accuracy is noise
        g.receive(GeoCoordinate(latitude: 51.50072, longitude: -0.1246), accuracy: 10)
        g.tick(dt: 1.0 / 60)
        XCTAssertEqual(g.velocity, .zero)

        // 30 m north is a walk: she heads north and arrives
        let there = GeoCoordinate(latitude: 51.50097, longitude: -0.1246)
        g.receive(there, accuracy: 10)
        g.tick(dt: 1.0 / 60)
        XCTAssertGreaterThan(g.velocity.y, 1)
        for _ in 0..<600 { g.tick(dt: 1.0 / 60) }
        XCTAssertEqual(g.coordinate.latitude, there.latitude, accuracy: 1e-5)
        XCTAssertEqual(g.velocity, .zero)

        // a jump does not walk across town
        let far = GeoCoordinate(latitude: 51.52, longitude: -0.1246)
        g.receive(far, accuracy: 10)
        g.tick(dt: 1.0 / 60)
        XCTAssertEqual(g.coordinate.latitude, far.latitude, accuracy: 1e-6)
    }

    private func triangulatedArea(_ rings: [[SIMD2<Float>]]) -> Float {
        let pts = rings.flatMap { $0 }, t = Earcut.triangulate(rings)
        var total: Float = 0
        for i in stride(from: 0, to: t.count, by: 3) {
            let a = pts[Int(t[i])], b = pts[Int(t[i + 1])], c = pts[Int(t[i + 2])]
            total += abs((b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)) / 2
        }
        return total
    }
}
