import Foundation
import simd

/// Must match `GroundVertex` in Shaders.metal (16 bytes).
struct GroundVertex {
    var x: Float, z: Float      // metres from the tile's north-west corner, Mercator scale
    var kind: Float
    var u: Float                // across a ribbon: -1...1
}

/// Must match `PropVertex` in Shaders.metal (20 bytes).
struct PropVertex {
    var x: Float, y: Float, z: Float
    var nx: Int8, ny: Int8, nz: Int8, material: Int8
    var r: UInt8, g: UInt8, b: UInt8, a: UInt8
}

/// Shader switch values. Keep in step with `groundFragment`.
enum GroundKind {
    static let lawn: Float = 0, park: Float = 1, wood: Float = 2, sand: Float = 3, plaza: Float = 4, shore: Float = 5
    static let water: Float = 6, roadEdge: Float = 7, road: Float = 8, path: Float = 9, rail: Float = 10, pitch: Float = 11
    static let shadow: Float = 12
}

/// Everything on the ground is flat, so it is painted in this order with the depth test off.
enum GroundLayer: Int, CaseIterable {
    case landuse, cover, wood, shore, water, plaza, rail, paths, roadEdge, road, shadow
}

struct NamedSegment { var a: SIMD2<Float>, b: SIMD2<Float>, name: Int32 }

/// CPU side of one tile, ready to upload.
struct TileMesh {
    var groundVertices: [GroundVertex] = []
    var groundIndices: [UInt32] = []
    var layers: [Range<Int>] = []             // index ranges, by GroundLayer
    var propVertices: [PropVertex] = []
    var propIndices: [UInt32] = []
    var streets: [NamedSegment] = []
    var streetNames: [String] = []
}

/// Sun direction for shading and for the painted building shadows. Sydney's sun is to the north.
let sunDirection = simd_normalize(SIMD3<Float>(-0.42, 0.74, -0.52))

/// Turns one vector tile into stylised meshes. Runs on a background queue.
struct TileMesher {
    /// The bent world needs ground vertices no further apart than this.
    private static let maxEdge: Float = 48
    private static let wanted: Set<String> = ["building", "landcover", "landuse", "park", "transportation",
                                              "transportation_name", "water", "waterway"]
    private let id: TileID
    private let toMetres: Float       // tile units to Mercator metres
    private let ground: Float         // Mercator metres to real metres
    private let tileSize = Float(TileID.size)

    private var gv = [[GroundVertex]](repeating: [], count: GroundLayer.allCases.count)
    private var gi = [[UInt32]](repeating: [], count: GroundLayer.allCases.count)
    private var pv: [PropVertex] = [], pi: [UInt32] = []
    private var streets: [NamedSegment] = [], names: [String] = [], nameIndex: [String: Int32] = [:]
    /// 8 m cells already taken by roads or buildings, so trees do not grow through them.
    private var occupied: [Bool]
    private let cells: Int
    private var greens: [(polygon: Polygon, spacing: Float, keep: UInt32)] = []
    private var waters: [(polygon: Polygon, lo: SIMD2<Float>, hi: SIMD2<Float>)] = []

    static func build(data: Data, id: TileID) -> TileMesh {
        var m = TileMesher(id: id)
        return m.run(MVTTile(data: data, only: wanted))
    }

    private init(id: TileID) {
        self.id = id
        let lat = Mercator.unproject(id.centre).latitude
        ground = Float(Mercator.groundScale(latitude: lat))
        toMetres = Float(TileID.size) / 4096
        cells = Int(Float(TileID.size) / 8) + 1
        occupied = [Bool](repeating: false, count: cells * cells)
    }

    private mutating func run(_ tile: MVTTile) -> TileMesh {
        if let l = tile.layers["landuse"] { landuse(l) }
        if let l = tile.layers["landcover"] { landcover(l) }
        if let l = tile.layers["park"] { park(l) }
        if let l = tile.layers["water"] { water(l) }
        if let l = tile.layers["waterway"] { waterway(l) }
        if let l = tile.layers["transportation"] { transportation(l) }
        if let l = tile.layers["building"] { buildings(l) }
        if let l = tile.layers["transportation_name"] { streetNames(l) }
        trees()

        var mesh = TileMesh()
        for layer in GroundLayer.allCases {
            let i = layer.rawValue, base = UInt32(mesh.groundVertices.count), start = mesh.groundIndices.count
            mesh.groundVertices += gv[i]
            mesh.groundIndices += gi[i].map { $0 + base }
            mesh.layers.append(start..<mesh.groundIndices.count)
        }
        mesh.propVertices = pv; mesh.propIndices = pi
        mesh.streets = streets; mesh.streetNames = names
        return mesh
    }

    // MARK: Areas

    private mutating func landuse(_ l: MVTLayer) {
        let scale = l.scale(toMetres)
        for f in l.features where f.kind == .polygon {
            let kind: Float
            switch l.tag(f, "class")?.string {
            case "commercial", "retail", "railway", "garages": kind = GroundKind.plaza
            case "pitch", "playground", "stadium", "school", "kindergarten", "university", "college", "hospital": kind = GroundKind.pitch
            default: continue     // suburb and neighbourhood blanket the whole tile
            }
            for p in l.polygons(f, scale: scale) { fill(p, .landuse, kind) }
        }
    }

    private mutating func landcover(_ l: MVTLayer) {
        let scale = l.scale(toMetres)
        for f in l.features where f.kind == .polygon {
            switch l.tag(f, "class")?.string {
            case "wood", "forest":
                for p in l.polygons(f, scale: scale) { fill(p, .wood, GroundKind.wood); greens.append((p, 20, 215)) }
            case "grass", "wetland", "farmland":
                for p in l.polygons(f, scale: scale) { fill(p, .cover, GroundKind.park); greens.append((p, 27, 150)) }
            case "sand", "beach":
                for p in l.polygons(f, scale: scale) { fill(p, .cover, GroundKind.sand) }
            default: continue
            }
        }
    }

    private mutating func park(_ l: MVTLayer) {
        let scale = l.scale(toMetres)
        for f in l.features where f.kind == .polygon {
            for p in l.polygons(f, scale: scale) { fill(p, .cover, GroundKind.park) }
        }
    }

    private mutating func water(_ l: MVTLayer) {
        let scale = l.scale(toMetres)
        for f in l.features where f.kind == .polygon {
            for p in l.polygons(f, scale: scale) {
                fill(p, .water, GroundKind.water)
                let (lo, hi) = bounds(p.rings[0])
                waters.append((p, lo, hi))
                // a soft pale bank, broken where the polygon is only cut by the tile edge
                for ring in p.rings { outline(ring, halfWidth: 4.5 / ground, .shore, GroundKind.shore) }
            }
        }
    }

    private mutating func waterway(_ l: MVTLayer) {
        let scale = l.scale(toMetres)
        for f in l.features where f.kind == .line {
            if l.tag(f, "brunnel")?.string == "tunnel" { continue }
            let river = l.tag(f, "class")?.string == "river"
            for line in l.paths(f, scale: scale) {
                ribbon(line, halfWidth: (river ? 12 : 4) / ground, .water, GroundKind.water)
            }
        }
    }

    // MARK: Transport

    /// Drawn wider than life so the street pattern reads at the game's zoom and fits the over-scale
    /// player. Width in real metres.
    private static func roadWidth(_ cls: String) -> (width: Float, rank: Int)? {
        switch cls {
        case "motorway", "trunk": return (30, 7)
        case "primary": return (27, 6)
        case "secondary": return (24, 5)
        case "tertiary": return (21, 4)
        case "minor": return (18, 3)
        case "busway": return (14, 3)
        case "service": return (11, 2)
        case "raceway": return (11, 2)
        default: return nil
        }
    }

    private mutating func transportation(_ l: MVTLayer) {
        let scale = l.scale(toMetres)
        var roads: [(rank: Int, width: Float, line: [SIMD2<Float>])] = []
        for f in l.features {
            guard var cls = l.tag(f, "class")?.string else { continue }
            if cls.hasSuffix("_construction") { cls = String(cls.dropLast("_construction".count)) }
            if l.tag(f, "brunnel")?.string == "tunnel" { continue }
            if (l.tag(f, "layer")?.number ?? 0) < 0 || (l.tag(f, "level")?.number ?? 0) < 0 { continue }
            if l.tag(f, "indoor")?.number == 1 || l.tag(f, "indoor")?.bool == true { continue }
            if cls == "ferry" { continue }

            if f.kind == .polygon {           // squares, wharves and bridge decks
                for p in l.polygons(f, scale: scale) { fill(p, .plaza, GroundKind.plaza) }
                continue
            }
            guard f.kind == .line else { continue }
            for line in l.paths(f, scale: scale) {
                if cls == "rail" || cls == "transit" {
                    ribbon(line, halfWidth: 3.2 / ground, .rail, GroundKind.rail)
                } else if cls == "path" || cls == "track" {
                    ribbon(line, halfWidth: (cls == "track" ? 2.8 : 2.0) / ground, .paths, GroundKind.path)
                    occupy(line, spread: 0)
                } else if cls == "pier" {
                    ribbon(line, halfWidth: 3.5 / ground, .plaza, GroundKind.plaza)
                } else if let spec = Self.roadWidth(cls) {
                    roads.append((spec.rank, spec.width, line))
                    occupy(line, spread: 1)
                }
            }
        }
        // All kerbs first, then all surfaces from small to large, so junctions join cleanly.
        roads.sort { $0.rank < $1.rank }
        for r in roads {
            let half = r.width / 2 / ground
            ribbon(r.line, halfWidth: half + 2.2 / ground, .roadEdge, GroundKind.roadEdge)
            ribbon(r.line, halfWidth: half, .road, GroundKind.road)
        }
    }

    private mutating func streetNames(_ l: MVTLayer) {
        let scale = l.scale(toMetres)
        for f in l.features where f.kind == .line {
            guard let name = l.tag(f, "name:en")?.string ?? l.tag(f, "name")?.string, !name.isEmpty else { continue }
            if l.tag(f, "class")?.string == "ferry" { continue }
            let index: Int32
            if let i = nameIndex[name] { index = i } else {
                index = Int32(names.count); names.append(name); nameIndex[name] = index
            }
            for line in l.paths(f, scale: scale) where line.count > 1 {
                for i in 1..<line.count { streets.append(NamedSegment(a: line[i - 1], b: line[i], name: index)) }
            }
        }
    }

    private mutating func occupy(_ line: [SIMD2<Float>], spread: Int) {
        for i in 1..<max(line.count, 1) {
            let a = line[i - 1], b = line[i], n = Int(simd_length(b - a) / 6) + 1
            for s in 0...n { mark(a + (b - a) * Float(s) / Float(n), spread: spread) }
        }
    }
    private mutating func mark(_ p: SIMD2<Float>, spread: Int = 1) {
        let c = SIMD2<Int>(Int((p.x / 8).rounded(.down)), Int((p.y / 8).rounded(.down)))
        // Tiles carry a buffer zone, so points can sit outside the grid entirely.
        let lo = simd_max(c &- spread, .zero), hi = simd_min(c &+ spread, SIMD2(repeating: cells - 1))
        guard lo.x <= hi.x, lo.y <= hi.y else { return }
        for y in lo.y...hi.y {
            for x in lo.x...hi.x { occupied[y * cells + x] = true }
        }
    }
    private func isOccupied(_ p: SIMD2<Float>) -> Bool {
        let x = Int(p.x / 8), y = Int(p.y / 8)
        return x >= 0 && y >= 0 && x < cells && y < cells && occupied[y * cells + x]
    }

    // MARK: Ground tessellation

    private mutating func fill(_ p: Polygon, _ layer: GroundLayer, _ kind: Float) {
        let tri = Earcut.triangulate(p.rings)
        guard !tri.isEmpty else { return }
        let pts = p.rings.flatMap { $0 }, i = layer.rawValue
        let base = UInt32(gv[i].count)
        gv[i].reserveCapacity(gv[i].count + pts.count)
        for q in pts { gv[i].append(GroundVertex(x: q.x, z: q.y, kind: kind, u: 0)) }
        let limit = Self.maxEdge * Self.maxEdge
        var t = 0
        while t + 2 < tri.count {
            let ia = tri[t], ib = tri[t + 1], ic = tri[t + 2]; t += 3
            let a = pts[Int(ia)], b = pts[Int(ib)], c = pts[Int(ic)]
            if simd_distance_squared(a, b) > limit || simd_distance_squared(b, c) > limit || simd_distance_squared(c, a) > limit {
                split(a, b, c, i, kind)
            } else {
                gi[i] += [base + ia, base + ib, base + ic]
            }
        }
    }

    /// Longest-edge bisection. Every edge over the limit is halved at its exact midpoint whichever
    /// triangle reaches it, so neighbours stay crack-free.
    private mutating func split(_ a: SIMD2<Float>, _ b: SIMD2<Float>, _ c: SIMD2<Float>, _ layer: Int, _ kind: Float) {
        let ab = simd_distance_squared(a, b), bc = simd_distance_squared(b, c), ca = simd_distance_squared(c, a)
        let longest = max(ab, bc, ca)
        if longest <= Self.maxEdge * Self.maxEdge {
            let base = UInt32(gv[layer].count)
            for q in [a, b, c] { gv[layer].append(GroundVertex(x: q.x, z: q.y, kind: kind, u: 0)) }
            gi[layer] += [base, base + 1, base + 2]
        } else if longest == ab {
            let m = (a + b) / 2; split(a, m, c, layer, kind); split(m, b, c, layer, kind)
        } else if longest == bc {
            let m = (b + c) / 2; split(a, b, m, layer, kind); split(a, m, c, layer, kind)
        } else {
            let m = (c + a) / 2; split(a, b, m, layer, kind); split(m, b, c, layer, kind)
        }
    }

    private mutating func ribbon(_ raw: [SIMD2<Float>], halfWidth hw: Float, _ layer: GroundLayer, _ kind: Float) {
        var pts: [SIMD2<Float>] = []
        pts.reserveCapacity(raw.count)
        for p in raw {
            guard let last = pts.last else { pts.append(p); continue }
            let d = simd_distance(last, p)
            if d < 0.01 { continue }
            let n = Int(d / Self.maxEdge)
            if n > 0 { for s in 1...n { pts.append(last + (p - last) * Float(s) / Float(n + 1)) } }
            pts.append(p)
        }
        guard pts.count > 1 else { return }
        let i = layer.rawValue
        var dirs: [SIMD2<Float>] = []
        dirs.reserveCapacity(pts.count - 1)
        for s in 1..<pts.count { dirs.append(simd_normalize(pts[s] - pts[s - 1])) }

        for s in 0..<dirs.count {
            let n = SIMD2(-dirs[s].y, dirs[s].x) * hw, a = pts[s], b = pts[s + 1]
            let base = UInt32(gv[i].count)
            gv[i].append(GroundVertex(x: a.x + n.x, z: a.y + n.y, kind: kind, u: 1))
            gv[i].append(GroundVertex(x: a.x - n.x, z: a.y - n.y, kind: kind, u: -1))
            gv[i].append(GroundVertex(x: b.x + n.x, z: b.y + n.y, kind: kind, u: 1))
            gv[i].append(GroundVertex(x: b.x - n.x, z: b.y - n.y, kind: kind, u: -1))
            gi[i] += [base, base + 1, base + 2, base + 2, base + 1, base + 3]
        }
        // rounded joins on the outside of each bend, and rounded ends
        for s in 1..<max(dirs.count, 1) {
            let d0 = dirs[s - 1], d1 = dirs[s], c = d0.x * d1.y - d0.y * d1.x
            if abs(c) < 0.02 && simd_dot(d0, d1) > 0 { continue }
            let side: Float = c > 0 ? -1 : 1
            let a0 = atan2(d0.x * side, -d0.y * side)
            var sweep = atan2(d1.x * side, -d1.y * side) - a0
            if sweep > .pi { sweep -= 2 * .pi } else if sweep < -.pi { sweep += 2 * .pi }
            fan(pts[s], from: a0, sweep: sweep, hw, i, kind, rimU: side)
        }
        let start = dirs[0], end = dirs[dirs.count - 1]
        fan(pts[0], from: atan2(start.x, -start.y), sweep: .pi, hw, i, kind, rimU: 1)
        fan(pts[pts.count - 1], from: atan2(-end.x, end.y), sweep: .pi, hw, i, kind, rimU: 1)
    }

    private mutating func fan(_ c: SIMD2<Float>, from a0: Float, sweep: Float, _ r: Float, _ i: Int, _ kind: Float, rimU: Float) {
        let steps = max(1, Int(abs(sweep) / (.pi / 4) + 0.5))
        let base = UInt32(gv[i].count)
        gv[i].append(GroundVertex(x: c.x, z: c.y, kind: kind, u: 0))
        for s in 0...steps {
            let a = a0 + sweep * Float(s) / Float(steps)
            gv[i].append(GroundVertex(x: c.x + cos(a) * r, z: c.y + sin(a) * r, kind: kind, u: rimU))
            if s > 0 { gi[i] += [base, base + UInt32(s), base + UInt32(s) + 1] }
        }
    }

    /// Ribbon along a closed ring, skipping edges outside the tile (those are the clip line).
    private mutating func outline(_ ring: [SIMD2<Float>], halfWidth: Float, _ layer: GroundLayer, _ kind: Float) {
        var run: [SIMD2<Float>] = []
        func outside(_ a: SIMD2<Float>, _ b: SIMD2<Float>) -> Bool {
            (a.x <= 0 && b.x <= 0) || (a.y <= 0 && b.y <= 0) || (a.x >= tileSize && b.x >= tileSize) || (a.y >= tileSize && b.y >= tileSize)
        }
        for i in 0..<ring.count {
            let a = ring[i], b = ring[(i + 1) % ring.count]
            if outside(a, b) {
                if run.count > 1 { ribbon(run, halfWidth: halfWidth, layer, kind) }
                run = []
            } else {
                if run.isEmpty { run.append(a) }
                run.append(b)
            }
        }
        if run.count > 1 { ribbon(run, halfWidth: halfWidth, layer, kind) }
    }

    // MARK: Buildings

    private static let walls: [SIMD3<UInt8>] = [
        SIMD3(242, 224, 190), SIMD3(238, 190, 150), SIMD3(214, 146, 114), SIMD3(176, 190, 146),
        SIMD3(160, 186, 196), SIMD3(240, 208, 138), SIMD3(226, 172, 164), SIMD3(232, 216, 196)
    ]
    private static let roofs: [SIMD3<UInt8>] = [
        SIMD3(178, 96, 76), SIMD3(112, 134, 150), SIMD3(120, 124, 72), SIMD3(196, 150, 70), SIMD3(150, 92, 92)
    ]

    /// Real heights would hide the player and block the camera, so towers are squashed into toy-town
    /// proportions, staying taller than their neighbours: 6 m becomes 15 m and 200 m becomes 61 m.
    static func toyHeight(_ h: Float) -> Float { 6 + 3.9 * sqrt(max(h, 0)) }

    /// One polygon from the building layer, before it is known which others it belongs with.
    private struct Footprint {
        var polygon: Polygon, top: Float, raw: Float, area: Float
        var lo: SIMD2<Float>, hi: SIMD2<Float>
        var crossing: UInt32?      // set when it straddles a tile edge
    }

    /// OpenStreetMap maps a tower on a podium as overlapping parts, so anything that truly overlaps is
    /// one building: it gets one colour and one height variation, and its parts read as steps.
    private mutating func buildings(_ l: MVTLayer) {
        let scale = l.scale(toMetres)
        let tile = SIMD2<Float>(repeating: tileSize)
        var prints: [Footprint] = []
        for f in l.features where f.kind == .polygon {
            if l.tag(f, "hide_3d")?.bool == true { continue }
            let top = Self.toyHeight(Float(l.tag(f, "render_height")?.number ?? 6))
            let raw = Float(l.tag(f, "render_min_height")?.number ?? 0)
            for p in l.polygons(f, scale: scale) {
                let area = p.area * ground * ground
                guard area > 8, let ring = p.rings.first else { continue }
                let (lo, hi) = bounds(ring)
                // Wholly in the buffer zone means the neighbouring tile owns it.
                guard hi.x > 0, hi.y > 0, lo.x < tile.x, lo.y < tile.y else { continue }
                prints.append(Footprint(polygon: p, top: top, raw: raw, area: area, lo: lo, hi: hi,
                                        crossing: Self.crossingKey(ring, tile: id, tileSize: tileSize)))
            }
        }

        var parent = Array(prints.indices)
        func root(_ i: Int) -> Int {
            var r = i
            while parent[r] != r { parent[r] = parent[parent[r]]; r = parent[r] }
            return r
        }
        var cells: [SIMD2<Int32>: [Int]] = [:]
        for (i, b) in prints.enumerated() {
            let c0 = SIMD2<Int32>(b.lo / 64), c1 = SIMD2<Int32>(b.hi / 64)
            for y in c0.y...c1.y { for x in c0.x...c1.x { cells[SIMD2(x, y), default: []].append(i) } }
        }
        var joined = Set<SIMD2<Int>>()
        for list in cells.values where list.count > 1 {
            for (n, a) in list.enumerated() {
                for b in list[(n + 1)...] where root(a) != root(b) {
                    let pair = SIMD2(min(a, b), max(a, b))
                    guard !joined.contains(pair) else { continue }
                    joined.insert(pair)
                    let x = prints[a], y = prints[b]
                    guard x.lo.x < y.hi.x, y.lo.x < x.hi.x, x.lo.y < y.hi.y, y.lo.y < x.hi.y else { continue }
                    if Self.overlaps(x.polygon.rings[0], y.polygon.rings[0]) { parent[root(a)] = root(b) }
                }
            }
        }

        // A straddling group is named by where it crosses the tile edge, the one thing both tiles agree on.
        var named: [Int: UInt32] = [:], fallback: [Int: UInt32] = [:], members: [Int: Int] = [:]
        for (i, b) in prints.enumerated() {
            let r = root(i)
            members[r, default: 0] += 1
            if let k = b.crossing { named[r] = min(named[r] ?? .max, k) }
            let c = (b.polygon.rings[0].reduce(SIMD2<Float>.zero, +) / Float(b.polygon.rings[0].count)).rounded(.down)
            fallback[r] = min(fallback[r] ?? .max, mix32(UInt32(truncatingIfNeeded: (id.x + Int(c.x)) &* 73856093 ^ (id.y + Int(c.y)) &* 19349663)))
        }

        for (i, b) in prints.enumerated() {
            let r = root(i), hash = named[r] ?? fallback[r]!
            let wall = Self.walls[Int(hash % UInt32(Self.walls.count))]
            let roof = Self.roofs[Int((hash >> 8) % UInt32(Self.roofs.count))]
            // Slightly varied heights stop neighbouring blocks merging into one slab.
            let lift = 1 + Float(hash >> 24) / 255 * 0.12
            let top = b.top * lift
            let bottom: Float = b.raw > 1 ? min(Self.toyHeight(b.raw) * lift, top - 1) : -2
            for ring in b.polygon.rings { walls(ring, bottom: bottom, top: top, colour: wall) }
            // A pitched roof would poke through its other parts, or differ between two tiles.
            self.roof(b.polygon, top: top, area: b.area, colour: roof, pitched: members[r] == 1 && b.crossing == nil)
            shadow(b.polygon.rings[0], height: b.raw > 1 ? 0 : top)
            for ring in b.polygon.rings { occupyArea(ring) }
        }
    }

    /// True when the rings share real area. Touching along a wall, as terraces do, does not count.
    static func overlaps(_ a: [SIMD2<Float>], _ b: [SIMD2<Float>]) -> Bool {
        let (small, large) = abs(signedArea(a)) < abs(signedArea(b)) ? (a, b) : (b, a)
        let c = small.reduce(SIMD2<Float>.zero, +) / Float(small.count)
        func inside(_ q: SIMD2<Float>) -> Bool {
            var hit = false
            var j = large.count - 1
            for i in 0..<large.count {
                let p = large[i], o = large[j]
                if (p.y > q.y) != (o.y > q.y), q.x < (o.x - p.x) * (q.y - p.y) / (o.y - p.y) + p.x { hit.toggle() }
                j = i
            }
            return hit
        }
        if inside(c) { return true }
        // Step each corner 2 m inwards, clear of any shared wall.
        return small.contains { v in
            let d = c - v, l = simd_length(d)
            return l > 2 && inside(v + d / l * 2)
        }
    }

    /// Identifies a ring by where it crosses its tile's edges, in whole-map units, so the same building
    /// gets the same key from the tiles on either side. Nil when it crosses no edge.
    static func crossingKey(_ ring: [SIMD2<Float>], tile: TileID, tileSize: Float) -> UInt32? {
        var best: UInt32?
        func consider(_ line: Int, _ axis: Int, _ along: Float) {
            let k = mix32(UInt32(truncatingIfNeeded: line &* 40503 ^ axis &* 7919 ^ Int((along / 4).rounded()) &* 2654435761))
            best = min(best ?? .max, k)
        }
        for i in 0..<ring.count {
            let a = ring[i], b = ring[(i + 1) % ring.count]
            for (edge, line) in [(Float(0), tile.x), (tileSize, tile.x + 1)] where (a.x - edge) * (b.x - edge) < 0 {
                let y = a.y + (b.y - a.y) * (edge - a.x) / (b.x - a.x)
                if y >= -40, y <= tileSize + 40 { consider(line, 0, y + Float(tile.y % 1024) * tileSize) }
            }
            for (edge, line) in [(Float(0), tile.y), (tileSize, tile.y + 1)] where (a.y - edge) * (b.y - edge) < 0 {
                let x = a.x + (b.x - a.x) * (edge - a.y) / (b.y - a.y)
                if x >= -40, x <= tileSize + 40 { consider(line, 1, x + Float(tile.x % 1024) * tileSize) }
            }
        }
        return best
    }

    private mutating func occupyArea(_ r: [SIMD2<Float>]) {
        let (lo, hi) = bounds(r)
        var y = lo.y
        while y <= hi.y {
            var x = lo.x
            while x <= hi.x { mark(SIMD2(x, y)); x += 8 }
            y += 8
        }
    }

    private mutating func walls(_ ring: [SIMD2<Float>], bottom: Float, top: Float, colour: SIMD3<UInt8>) {
        pv.reserveCapacity(pv.count + ring.count * 4)
        for i in 0..<ring.count {
            let a = ring[i], b = ring[(i + 1) % ring.count], d = b - a
            guard simd_length_squared(d) > 0.01 else { continue }
            let o = simd_normalize(SIMD2(d.y, -d.x))     // outward, for a positive-area ring
            let n = SIMD3(o.x, 0, o.y), base = UInt32(pv.count)
            pv.append(vertex(SIMD3(a.x, bottom, a.y), n, colour, 1))
            pv.append(vertex(SIMD3(a.x, top, a.y), n, colour, 1))
            pv.append(vertex(SIMD3(b.x, bottom, b.y), n, colour, 1))
            pv.append(vertex(SIMD3(b.x, top, b.y), n, colour, 1))
            pi += [base, base + 1, base + 2, base + 2, base + 1, base + 3]
        }
    }

    private mutating func roof(_ p: Polygon, top: Float, area: Float, colour: SIMD3<UInt8>, pitched: Bool) {
        let ring = p.rings[0]
        if pitched, p.rings.count == 1, area < 900, top < 30, isConvex(ring) {
            let c = ring.reduce(SIMD2<Float>.zero, +) / Float(ring.count)
            let rise = min(9, sqrt(area) * 0.3)
            if ring.count == 4 {
                hipRoof(ring, top: top, rise: rise, colour: colour)
            } else {
                for i in 0..<ring.count {
                    let a = ring[i], b = ring[(i + 1) % ring.count]
                    facet(SIMD3(a.x, top, a.y), SIMD3(c.x, top + rise, c.y), SIMD3(b.x, top, b.y), colour, up: true)
                }
            }
            return
        }
        let tri = Earcut.triangulate(p.rings)
        let pts = p.rings.flatMap { $0 }, base = UInt32(pv.count), up = SIMD3<Float>(0, 1, 0)
        for q in pts { pv.append(vertex(SIMD3(q.x, top, q.y), up, colour, 0)) }
        var t = 0
        while t + 2 < tri.count {
            let a = pts[Int(tri[t])], b = pts[Int(tri[t + 1])], c = pts[Int(tri[t + 2])]
            // Facing up means negative area in (x, z), which runs south.
            let cross = (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)
            if cross > 0 { pi += [base + tri[t], base + tri[t + 2], base + tri[t + 1]] }
            else { pi += [base + tri[t], base + tri[t + 1], base + tri[t + 2]] }
            t += 3
        }
    }

    private func isConvex(_ r: [SIMD2<Float>]) -> Bool {
        guard r.count >= 3, r.count <= 12 else { return false }
        for i in 0..<r.count {
            let a = r[i], b = r[(i + 1) % r.count], c = r[(i + 2) % r.count]
            if (b.x - a.x) * (c.y - b.y) - (b.y - a.y) * (c.x - b.x) < -0.5 { return false }
        }
        return true
    }

    private mutating func hipRoof(_ q: [SIMD2<Float>], top: Float, rise: Float, colour: SIMD3<UInt8>) {
        // ridge runs between the midpoints of the two short edges, pulled in for the hips
        let long = simd_distance(q[0], q[1]) >= simd_distance(q[1], q[2])
        let (a, b, c, d) = long ? (q[0], q[1], q[2], q[3]) : (q[1], q[2], q[3], q[0])
        let m1 = (b + c) / 2, m2 = (d + a) / 2, axis = m1 - m2
        let r1 = m1 - axis * 0.2, r2 = m2 + axis * 0.2
        let v = { (p: SIMD2<Float>, h: Float) in SIMD3<Float>(p.x, h, p.y) }
        let ta = v(a, top), tb = v(b, top), tc = v(c, top), td = v(d, top), ra = v(r1, top + rise), rb = v(r2, top + rise)
        facet(ta, tb, ra, colour, up: true); facet(ta, ra, rb, colour, up: true)
        facet(tc, td, rb, colour, up: true); facet(tc, rb, ra, colour, up: true)
        facet(tb, tc, ra, colour, up: true); facet(td, ta, rb, colour, up: true)
    }

    /// Flat-shaded triangle wound to face up, or away from `away`.
    private mutating func facet(_ a: SIMD3<Float>, _ b: SIMD3<Float>, _ c: SIMD3<Float>, _ colour: SIMD3<UInt8>,
                                up: Bool = false, away: SIMD3<Float>? = nil) {
        func real(_ p: SIMD3<Float>) -> SIMD3<Float> { SIMD3(p.x * ground, p.y, p.z * ground) }
        var n = simd_cross(real(b) - real(a), real(c) - real(a))
        guard simd_length_squared(n) > 1e-6 else { return }
        n = simd_normalize(n)
        var flip = false
        if up { flip = n.y < 0 }
        else if let away { flip = simd_dot(n, (a + b + c) / 3 - away) < 0 }
        if flip { n = -n }
        let base = UInt32(pv.count)
        pv.append(vertex(a, n, colour, 0))
        pv.append(vertex(flip ? c : b, n, colour, 0))
        pv.append(vertex(flip ? b : c, n, colour, 0))
        pi += [base, base + 1, base + 2]
    }

    private func vertex(_ p: SIMD3<Float>, _ n: SIMD3<Float>, _ c: SIMD3<UInt8>, _ material: Int8) -> PropVertex {
        PropVertex(x: p.x, y: p.y, z: p.z, nx: Int8(n.x * 127), ny: Int8(n.y * 127), nz: Int8(n.z * 127), material: material,
                   r: c.x, g: c.y, b: c.z, a: 255)
    }

    /// Paints the ground area swept by the footprint as the sun casts it. Only walls facing away
    /// from the sun need a quad. Overlaps are removed by the stencil test in the shadow pass.
    private mutating func shadow(_ ring: [SIMD2<Float>], height: Float) {
        guard height > 0 else { return }
        let reach = -SIMD2(sunDirection.x, sunDirection.z) / sunDirection.y * height * 0.55 / ground
        let i = GroundLayer.shadow.rawValue
        for k in 0..<ring.count {
            let a = ring[k], b = ring[(k + 1) % ring.count], d = b - a
            guard simd_dot(SIMD2(d.y, -d.x), reach) > 0 else { continue }
            split(a, b, b + reach, i, GroundKind.shadow)
            split(a, b + reach, a + reach, i, GroundKind.shadow)
        }
    }

    // MARK: Trees

    private mutating func trees() {
        var count = 0
        let budget = 3500
        for g in greens where count < budget {
            let p = g.polygon
            guard p.area * ground * ground > 500, let ring = p.rings.first else { continue }
            var (lo, hi) = bounds(ring)
            lo = simd_max(lo, .zero); hi = simd_min(hi, SIMD2(repeating: tileSize))
            let step = g.spacing / ground
            var y = (lo.y / step).rounded(.down) * step
            while y < hi.y {
                var x = (lo.x / step).rounded(.down) * step
                while x < hi.x {
                    let h = mix32(UInt32(truncatingIfNeeded: Int(x * 3) &* 73856093 ^ Int(y * 3) &* 19349663))
                    let at = SIMD2(x + Float(h & 255) / 255 * step, y + Float((h >> 8) & 255) / 255 * step)
                    x += step
                    guard (h >> 16) & 255 < g.keep, !isOccupied(at), contains(p, at), !inWater(at) else { continue }
                    tree(at, hash: h)
                    count += 1
                    if count >= budget { return }
                }
                y += step
            }
        }
    }

    private func bounds(_ ring: [SIMD2<Float>]) -> (lo: SIMD2<Float>, hi: SIMD2<Float>) {
        var lo = SIMD2<Float>(repeating: .infinity), hi = -lo
        for q in ring { lo = simd_min(lo, q); hi = simd_max(hi, q) }
        return (lo, hi)
    }

    private func inWater(_ q: SIMD2<Float>) -> Bool {
        waters.contains { q.x >= $0.lo.x && q.y >= $0.lo.y && q.x <= $0.hi.x && q.y <= $0.hi.y && contains($0.polygon, q) }
    }

    private func contains(_ p: Polygon, _ q: SIMD2<Float>) -> Bool {
        var inside = false
        for r in p.rings {
            var j = r.count - 1
            for i in 0..<r.count {
                let a = r[i], b = r[j]
                if (a.y > q.y) != (b.y > q.y), q.x < (b.x - a.x) * (q.y - a.y) / (b.y - a.y) + a.x { inside.toggle() }
                j = i
            }
        }
        return inside
    }

    private static let leaves: [SIMD3<UInt8>] = [SIMD3(116, 150, 78), SIMD3(92, 132, 76), SIMD3(142, 164, 84), SIMD3(170, 170, 92), SIMD3(226, 164, 170)]

    private mutating func tree(_ at: SIMD2<Float>, hash h: UInt32) {
        let size = 17 + Float((h >> 24) & 15) * 0.8, r = size * 0.42 / ground
        let turn = Float((h >> 4) & 63) / 63
        // one tree in twenty is blossom: this is Petal Path after all
        let leaf = (h >> 3) % 20 == 0 ? Self.leaves[4] : Self.leaves[Int((h >> 12) % 4)]
        let trunk = SIMD3<UInt8>(124, 90, 62), shade = SIMD3<UInt8>(UInt8(Float(leaf.x) * 0.82), UInt8(Float(leaf.y) * 0.82), UInt8(Float(leaf.z) * 0.82))
        let axis = { (y: Float) in SIMD3<Float>(at.x, y, at.y) }
        let ring = { (k: Int, radius: Float, y: Float) -> SIMD3<Float> in
            let a = (Float(k) + turn) * .pi / 3
            return SIMD3(at.x + cos(a) * radius, y, at.y + sin(a) * radius)
        }
        let t0: Float = 0, t1 = size * 0.34, w = r * 0.2
        for k in stride(from: 0, to: 6, by: 2) {
            let a0 = ring(k, w, t0), a1 = ring(k + 2, w, t0), b0 = ring(k, w, t1), b1 = ring(k + 2, w, t1)
            facet(a0, b0, a1, trunk, away: axis(t1 / 2)); facet(a1, b0, b1, trunk, away: axis(t1 / 2))
        }
        if h & 4 == 0 {   // round: two stacked gems
            let mid = size * 0.62, top = size
            for k in 0..<6 {
                facet(ring(k, r, mid), ring(k + 1, r, mid), axis(top), leaf, away: axis(mid))
                facet(ring(k, r, mid), ring(k + 1, r, mid), axis(t1 * 0.9), shade, away: axis(mid))
            }
        } else {          // pine: two stacked cones
            let y0 = size * 0.28, y1 = size * 0.62
            for k in 0..<6 {
                facet(ring(k, r, y0), ring(k + 1, r, y0), axis(y1 + size * 0.1), leaf, away: axis(y0 + size * 0.15))
                facet(ring(k, r * 0.72, y1 - size * 0.08), ring(k + 1, r * 0.72, y1 - size * 0.08), axis(size * 1.08), shade, away: axis(y1))
            }
        }
        // soft hexagon of shade
        let reach = -SIMD2(sunDirection.x, sunDirection.z) / sunDirection.y * size * 0.35 / ground
        let i = GroundLayer.shadow.rawValue, base = UInt32(gv[i].count), c = at + reach
        gv[i].append(GroundVertex(x: c.x, z: c.y, kind: GroundKind.shadow, u: 0))
        for k in 0..<6 {
            let a = Float(k) * .pi / 3
            gv[i].append(GroundVertex(x: c.x + cos(a) * r, z: c.y + sin(a) * r, kind: GroundKind.shadow, u: 1))
            gi[i] += [base, base + 1 + UInt32(k), base + 1 + UInt32((k + 1) % 6)]
        }
    }
}

private extension MVTLayer {
    func scale(_ toMetres: Float) -> Float { toMetres * 4096 / extent }
}

func mix32(_ v: UInt32) -> UInt32 {
    var h = v
    h ^= h >> 16; h = h &* 0x7feb352d
    h ^= h >> 15; h = h &* 0x846ca68b
    return h ^ (h >> 16)
}
