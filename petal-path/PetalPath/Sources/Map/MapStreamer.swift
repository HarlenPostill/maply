import Metal
import simd
import QuartzCore

/// One uploaded tile. Both renderers draw these.
final class GPUTile {
    let id: TileID
    let origin: SIMD2<Double>
    let groundVertices: MTLBuffer?, groundIndices: MTLBuffer?, propVertices: MTLBuffer?, propIndices: MTLBuffer?
    let layers: [Range<Int>], propIndexCount: Int
    let streets: [NamedSegment], streetNames: [String]

    init(id: TileID, mesh m: TileMesh, device: MTLDevice) {
        func buf<T>(_ a: [T]) -> MTLBuffer? {
            a.isEmpty ? nil : device.makeBuffer(bytes: a, length: MemoryLayout<T>.stride * a.count, options: .storageModeShared)
        }
        self.id = id; origin = id.origin
        groundVertices = buf(m.groundVertices); groundIndices = buf(m.groundIndices)
        propVertices = buf(m.propVertices); propIndices = buf(m.propIndices)
        layers = m.layers; propIndexCount = m.propIndices.count
        streets = m.streets; streetNames = m.streetNames
    }

    var buffers: [MTLBuffer] { [groundVertices, groundIndices, propVertices, propIndices].compactMap { $0 } }
}

/// Keeps the tiles around the player loaded. Called on the main thread; networking and meshing
/// happen in the background and land back here.
final class MapStreamer {
    /// Fog hides the world at about 720 m, so a tile starts loading well before it can be seen.
    static let loadRadius = 900.0
    static let unloadRadius = 1900.0

    private let device: MTLDevice
    private let source = TileSource()
    private let meshQueue = DispatchQueue(label: "petalpath.mesh", qos: .userInitiated)
    private var inFlight = Set<TileID>()
    private var retryAfter: [TileID: CFTimeInterval] = [:]
    private(set) var tiles: [TileID: GPUTile] = [:]

    /// Residency bookkeeping for Metal 4. The classic path ignores both lists.
    private var added: [MTLBuffer] = [], removed: [MTLBuffer] = []
    private(set) var failing = false

    init(device: MTLDevice) { self.device = device }

    var isLoading: Bool { !inFlight.isEmpty }

    func takeResidencyChanges() -> (added: [MTLBuffer], removed: [MTLBuffer]) {
        defer { added = []; removed = [] }
        return (added, removed)
    }

    func update(centre m: SIMD2<Double>) {
        let scale = Mercator.groundScale(latitude: Mercator.unproject(m).latitude)
        let load = Self.loadRadius / scale, unload = Self.unloadRadius / scale
        let c = TileID(containing: m)
        var wanted: [(TileID, Double)] = []
        for y in (c.y - 2)...(c.y + 2) {
            for x in (c.x - 2)...(c.x + 2) {
                guard x >= 0, y >= 0, x < 1 << TileID.zoom, y < 1 << TileID.zoom else { continue }
                let id = TileID(x: x, y: y), d = id.distanceSquared(to: m)
                if d <= load * load { wanted.append((id, d)) }
            }
        }
        wanted.sort { $0.1 < $1.1 }
        let now = CACurrentMediaTime()
        for (id, _) in wanted where tiles[id] == nil && !inFlight.contains(id) {
            if inFlight.count >= TileSource.maxParallel { break }
            if let t = retryAfter[id], t > now { continue }
            start(id)
        }
        for (id, t) in tiles where id.distanceSquared(to: m) > unload * unload {
            removed += t.buffers
            tiles[id] = nil
            #if DEBUG
            print("[map] unload \(id)")
            #endif
        }
    }

    private func start(_ id: TileID) {
        inFlight.insert(id)
        source.fetch(id) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure:
                DispatchQueue.main.async {
                    self.inFlight.remove(id)
                    self.retryAfter[id] = CACurrentMediaTime() + 8
                    self.failing = true
                }
            case .success(let data):
                self.meshQueue.async {
                    let t0 = CACurrentMediaTime()
                    let mesh = data.isEmpty ? TileMesh(layers: GroundLayer.allCases.map { _ in 0..<0 }) : TileMesher.build(data: data, id: id)
                    let tile = GPUTile(id: id, mesh: mesh, device: self.device)
                    #if DEBUG
                    print("[map] \(id): \(data.count / 1024) KB, \(mesh.groundIndices.count / 3) ground + \(mesh.propIndices.count / 3) prop tris in \(Int((CACurrentMediaTime() - t0) * 1000)) ms")
                    #endif
                    DispatchQueue.main.async {
                        self.inFlight.remove(id)
                        self.failing = false
                        self.tiles[id] = tile
                        self.added += tile.buffers
                    }
                }
            }
        }
    }

    /// Nearest named street to a point, within `limit` real metres.
    func street(near m: SIMD2<Double>, limit: Float, groundScale: Float) -> String? {
        guard let t = tiles[TileID(containing: m)] else { return nil }
        let p = SIMD2<Float>(m - t.origin)
        var best = limit / groundScale, name: Int32 = -1
        best *= best
        for s in t.streets {
            let ab = s.b - s.a, l = simd_length_squared(ab)
            let k = l > 0 ? simd_clamp(simd_dot(p - s.a, ab) / l, 0, 1) : 0
            let d = simd_distance_squared(p, s.a + ab * k)
            if d < best { best = d; name = s.name }
        }
        return name >= 0 ? t.streetNames[Int(name)] : nil
    }
}
