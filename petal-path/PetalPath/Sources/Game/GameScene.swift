import Foundation
import Combine
import Metal
import simd

/// UI <-> game state.
final class GameState: ObservableObject {
    private static let outfitKey = "outfit"
    private static let locationKey = "useMyLocation"

    @Published var outfit = Outfit() { didSet { saveOutfit() } }
    @Published var settingsOpen = false
    @Published var fastWalk = false
    /// Off is the fake location in Sydney, walked by joystick. On follows the device.
    @Published var useMyLocation = false { didSet { UserDefaults.standard.set(useMyLocation, forKey: Self.locationKey) } }
    @Published var locationBlocked = false
    /// The joystick has walked away from the real position.
    @Published var wandering = false
    @Published var backend = "…"
    @Published var fps = 0
    @Published var street: String?
    @Published var mapNotice: String?
    @Published var error: String?
    /// Trip planning reads these. `deviceFix` is the raw device reading and is nil unless following the
    /// device. The character is "simulated" whenever she is not standing on the real position.
    @Published var deviceFix: LocationFix?
    @Published var characterSimulated = true
    var characterCoordinate = GeoCoordinate.circularQuay

    /// Joystick, screen space: x right, y up, length 0...1. Read every frame, so not published.
    var stick = SIMD2<Float>(0, 0)
    var cameraYaw: Float = 0, cameraPitch: Float = 0.95, cameraDistance: Float = 300
    var returnToFix = false
    /// Trip planning asks for a new device reading; a still device may otherwise send none.
    var wantsFreshFix = false
    static let lowestPitch: Float = 0.06
    var start = GeoCoordinate.circularQuay

    init() {
        useMyLocation = UserDefaults.standard.bool(forKey: Self.locationKey)
        if let d = UserDefaults.standard.data(forKey: Self.outfitKey),
           let o = try? JSONDecoder().decode(Outfit.self, from: d) { outfit = o }
        #if DEBUG
        // Test hooks, for the Mac Catalyst run of the real Metal 4 path where there is no touch input.
        setvbuf(stdout, nil, _IOLBF, 0)      // keep logs when stdout is a file
        let env = ProcessInfo.processInfo.environment
        if env["PP_OUTFIT"] == "girl" {
            outfit = Outfit(hair: .bob, top: .argyle, bottoms: .dress, shoeStyle: .maryJanes, shoes: .brown)
        }
        if let p = env["PP_START"]?.split(separator: ",").compactMap({ Double($0) }), p.count == 2 {
            start = GeoCoordinate(latitude: p[0], longitude: p[1])
        }
        if let y = env["PP_YAW"].flatMap(Float.init) { cameraYaw = y }
        if let p = env["PP_PITCH"].flatMap(Float.init) { cameraPitch = p }
        if env["PP_FAST"] != nil { fastWalk = true }
        if env["PP_SETTINGS"] != nil { settingsOpen = true }
        #endif
    }

    private func saveOutfit() {
        if let d = try? JSONEncoder().encode(outfit) { UserDefaults.standard.set(d, forKey: Self.outfitKey) }
    }
}

/// Must match `FrameUniforms` in Shaders.metal.
struct FrameUniforms {
    var viewProj: simd_float4x4
    var playerModel: simd_float4x4
    var eye: SIMD4<Float>
    var sunDir: SIMD4<Float>
    var scroll: SIMD4<Float>       // xy pattern offset in metres, z time, w town height 0...1
    var world: SIMD4<Float>        // x curvature, y fog start, z fog end, w player scale
    var playerClip: SIMD4<Float>   // xy pixels, z depth, w x-ray radius in pixels
    var skyRight: SIMD4<Float>     // camera ray at the screen's right, top and centre
    var skyUp: SIMD4<Float>
    var skyForward: SIMD4<Float>
}

/// Must match `DrawUniforms` in Shaders.metal.
struct DrawUniforms {
    var uvOffset = SIMD2<Float>(0, 0)
    var origin = SIMD2<Float>(0, 0)
    var scale: Float = 1
    var pad: Float = 0
    var pad2 = SIMD2<Float>(0, 0)
}

/// One piece of map to draw. Built once a frame and consumed by either renderer.
struct MapDraw {
    enum Pass { case ground, shadow, prop }
    var pass: Pass
    var vertices: MTLBuffer, indices: MTLBuffer
    var indexOffset: Int, indexCount: Int
    var uniforms: DrawUniforms
}

/// In-place walk covers this much ground (metres/second at 1 m tall): 2 x half-stride / stance time,
/// set in player-character/tools/animate.py. Moving the world at this speed keeps the planted foot locked.
let walkSpeed: Float = 2 * 0.060 / (0.58 * 20 / 30)

/// Renderer-agnostic game: both Metal backends draw this.
final class GameScene {
    /// The character is 1 m tall in the asset and the map is real size. Like a Pokémon Go avatar she
    /// is hugely over scale, so a few city blocks fit around her on a phone. Her foot-locked walking
    /// speed scales with her, to about 9 m/s of real ground.
    static let playerScale: Float = 30
    static let curvature: Float = 0.00030
    static let fogEnd: Float = 720
    /// Linear colour of the sky at the horizon. `skyColour` in Shaders.metal must match, it is the fog.
    static let sky = SIMD3<Double>(0.620, 0.800, 0.930)

    let asset: CharacterAsset
    let animator: Animator
    let state: GameState
    private(set) var location: LocationSource
    private let joystick: JoystickLocationSource
    private let gps: GPSLocationSource
    private var gpsRunning = false
    let streamer: MapStreamer
    private let baseVertices: MTLBuffer, baseIndices: MTLBuffer, baseIndexCount: Int

    private var facing: Float = 0          // radians about up, 0 faces the camera's default south
    private var speed: Float = 0
    private var time: Float = 0
    private var idleTimer: Float = 7, blinkTimer: Float = 2.5, blinkHold: Float = 0
    private var framing: Float = 0         // 0 follow camera, 1 dressing room close-up
    private var streetTimer: Float = 0
    private var pattern = PatternAnchor()
    #if DEBUG
    private let autoWalk = ProcessInfo.processInfo.environment["PP_AUTOWALK"].flatMap(Float.init)
    private let dumpPosition = ProcessInfo.processInfo.environment["PP_LOG_POSITION"] != nil
    private var logTimer: Float = 0
    #endif

    private var walkAtScale: Float { walkSpeed * Self.playerScale }

    init(state: GameState, device: MTLDevice) throws {
        self.state = state
        asset = try CharacterAsset(url: Bundle.main.url(forResource: "player", withExtension: "glb")!)
        animator = Animator(asset: asset)
        joystick = JoystickLocationSource(start: state.start)
        gps = GPSLocationSource(start: state.start)
        location = joystick
        streamer = MapStreamer(device: device)

        // Lawn under everything, recentred on the player every frame. Fine enough to bend.
        var v = [GroundVertex](), idx = [UInt32]()
        let n = 80, half: Float = 800
        for z in 0...n { for x in 0...n {
            v.append(GroundVertex(x: (Float(x) / Float(n) - 0.5) * 2 * half, z: (Float(z) / Float(n) - 0.5) * 2 * half, kind: GroundKind.lawn, u: 0))
        } }
        for z in 0..<n { for x in 0..<n {
            let a = UInt32(z * (n + 1) + x), b = a + 1, c = a + UInt32(n + 1), d = c + 1
            idx += [a, c, b, b, c, d]
        } }
        baseVertices = device.makeBuffer(bytes: v, length: MemoryLayout<GroundVertex>.stride * v.count, options: .storageModeShared)!
        baseIndices = device.makeBuffer(bytes: idx, length: 4 * idx.count, options: .storageModeShared)!
        baseIndexCount = idx.count
    }

    /// Buffers that never change, for the Metal 4 residency set.
    var staticBuffers: [MTLBuffer] { [baseVertices, baseIndices] }

    func update(dt: Float) {
        time += dt
        syncLocationMode()
        framing += ((state.settingsOpen ? 1 : 0) - framing) * min(1, dt * 7)
        var stick = state.settingsOpen ? SIMD2<Float>(0, 0) : state.stick
        #if DEBUG
        if let a = autoWalk, !state.settingsOpen { stick = SIMD2(sin(a), cos(a)) }
        #endif
        let amount = min(1, simd_length(stick))

        // Screen up walks away from the camera. World x is east, z is south.
        var desired = SIMD2<Float>(0, 0)
        if amount > 0.12 {
            let s = simd_normalize(stick), yaw = state.cameraYaw
            let right = SIMD2(cos(yaw), -sin(yaw)), forward = SIMD2(-sin(yaw), -cos(yaw))
            let move = right * s.x + forward * s.y      // (east, south)
            desired = SIMD2(move.x, -move.y) * walkAtScale * amount * (state.fastWalk ? 5 : 1)
        }
        location.steer(desired)
        location.tick(dt: dt)

        let v = location.velocity
        speed = simd_length(v)
        if speed > 0.15 {
            let target = atan2(v.x, -v.y)
            var delta = (target - facing).truncatingRemainder(dividingBy: 2 * .pi)
            if delta > .pi { delta -= 2 * .pi } else if delta < -.pi { delta += 2 * .pi }
            facing += delta * min(1, dt * 11)
        }

        // Idle and Walk blend through the animator's crossfade.
        if speed > 0.25 {
            animator.play("Walk", fade: 0.18)
            animator.rate = min(2.4, max(0.55, speed / walkAtScale))
            idleTimer = Float.random(in: 6...10)
        } else {
            if animator.currentClip == "Walk" { animator.play("Idle", fade: 0.22) }
            animator.rate = 1
            idleTimer -= dt
            if idleTimer <= 0 { animator.play("Idle_Look", fade: 0.4); idleTimer = 12 + Float.random(in: 0...6) }
        }
        animator.update(dt: dt)
        if animator.looped, animator.currentClip == "Idle_Look" { animator.play("Idle", fade: 0.4) }

        blinkTimer -= dt
        if blinkTimer <= 0 { blinkHold = 0.12; blinkTimer = Float.random(in: 2...5) }
        blinkHold = max(0, blinkHold - dt)

        let at = Mercator.project(location.coordinate)
        streamer.update(centre: at)

        streetTimer -= dt
        if streetTimer <= 0 {
            streetTimer = 0.4
            let g = Float(Mercator.groundScale(latitude: location.coordinate.latitude))
            let name = streamer.street(near: at, limit: 28, groundScale: g)
            let wandering = gpsRunning && gps.isWandering
            let notice = gpsRunning && gps.status == .locating ? "Finding you…"
                : gpsRunning && gps.status == .failed ? "Can't find your location yet…"
                : streamer.failing && streamer.tiles.isEmpty ? "Can't reach the map. Retrying…"
                : streamer.tiles.isEmpty ? "Loading the map…" : nil
            let fix = gpsRunning ? gps.latestFix : nil
            let simulated = !(gpsRunning && location === gps && !gps.isWandering)
            let character = location.coordinate
            DispatchQueue.main.async { [state] in
                if state.street != name { state.street = name }
                if state.mapNotice != notice { state.mapNotice = notice }
                if state.wandering != wandering { state.wandering = wandering }
                if state.deviceFix != fix { state.deviceFix = fix }
                if state.characterSimulated != simulated { state.characterSimulated = simulated }
                state.characterCoordinate = character
            }
        }
        #if DEBUG
        logTimer -= dt
        if dumpPosition, logTimer <= 0 {
            logTimer = 2
            print("[pos] \(location.coordinate.latitude),\(location.coordinate.longitude) \(state.street ?? "-")")
        }
        #endif
    }

    /// Stays in Sydney until the first real fix arrives, so there is no jump to nowhere while searching.
    private func syncLocationMode() {
        if state.useMyLocation {
            if !gpsRunning { gps.begin(); gpsRunning = true }
            if gps.status == .denied {
                gps.stop(); gpsRunning = false
                DispatchQueue.main.async { [state] in state.useMyLocation = false; state.locationBlocked = true }
            } else if gps.hasFix, location !== gps {
                location = gps
            }
            if state.returnToFix { state.returnToFix = false; gps.returnToFix() }
            if state.wantsFreshFix { state.wantsFreshFix = false; gps.requestFreshFix() }
        } else if gpsRunning {
            gps.stop(); gpsRunning = false
            joystick.teleport(to: state.start)
            location = joystick
        }
    }

    /// Face atlas is 2x2: neutral, blink / happy, surprised.
    var faceOffset: SIMD2<Float> { blinkHold > 0 ? SIMD2(0.5, 0) : .zero }

    func isVisible(_ part: String) -> Bool {
        if Wardrobe.slottedParts.contains(part) { return state.outfit.meshes.contains(part) }
        return true
    }

    /// Texture for a draw: the outfit's override when it has one, otherwise the mesh's own material.
    func textureIndex(_ d: DrawItem) -> Int {
        if let name = state.outfit.textureOverride(part: d.part), let i = asset.wardrobeTextures[name] { return i }
        return d.material
    }

    func frameUniforms(width: Float, height: Float) -> FrameUniforms {
        let s = Self.playerScale, f = framing * framing * (3 - 2 * framing)
        // In the dressing room the camera swings round to her front and frames her above the sheet,
        // and the town sinks into the ground so nothing stands between them.
        var towardFront = (facing - state.cameraYaw).truncatingRemainder(dividingBy: 2 * .pi)
        if towardFront > .pi { towardFront -= 2 * .pi } else if towardFront < -.pi { towardFront += 2 * .pi }
        let yaw = state.cameraYaw + towardFront * f
        let pitch = state.cameraPitch + (0.16 - state.cameraPitch) * f

        // As the camera drops towards the horizon it pulls in on a shorter arm, tilts up to the sky,
        // and the world flattens so the view does not end a stone's throw away.
        let low = 1 - simd_smoothstep(0.12, 0.6, pitch)
        let arm = state.cameraDistance * (1 - 0.62 * low)
        let distance = arm + (Self.playerScale * 3.2 - arm) * f
        let target = SIMD3<Float>(0, s * 0.42 - s * 0.62 * f, 0)
        let cp = cos(pitch), sp = sin(pitch)
        let eye = target + SIMD3(sin(yaw) * cp, sp, cos(yaw) * cp) * distance
        let aim = target + SIMD3(0, low * (1 - f) * distance * 0.2, 0)
        let curve = Self.curvature * (1 - 0.8 * low)
        let fovY: Float = 0.80, aspect = width / height
        let viewProj = perspective(fovY: fovY, aspect: aspect, near: 2, far: 2200) * lookAt(eye, aim, SIMD3(0, 1, 0))

        let forward = simd_normalize(aim - eye), right = simd_normalize(simd_cross(forward, SIMD3(0, 1, 0)))
        let up = simd_cross(right, forward), halfHeight = tan(fovY / 2)

        let model = makeTRS(.zero, simd_quatf(angle: facing, axis: SIMD3(0, 1, 0)), SIMD3(repeating: s))

        let m = Mercator.project(location.coordinate)
        let g = Mercator.groundScale(latitude: location.coordinate.latitude)
        let drift = pattern.offset(at: m, groundScale: g)
        let horizon = Self.horizonDistance(eye: eye, curvature: curve)
        let fogEnd = min(Self.fogEnd, horizon * 0.92)

        let chest = viewProj * SIMD4<Float>(0, s * 0.5, 0, 1)
        let ndc = chest / chest.w
        let radius = 0.85 * s / simd_length(eye - SIMD3(0, s * 0.5, 0)) / tan(fovY / 2) * height / 2
        return FrameUniforms(viewProj: viewProj, playerModel: model, eye: SIMD4(eye, 1), sunDir: SIMD4(sunDirection, 0),
                             scroll: SIMD4(drift.x, drift.y, time, 1 - f),
                             world: SIMD4(curve, fogEnd * 0.55, fogEnd, s),
                             playerClip: SIMD4((ndc.x * 0.5 + 0.5) * width, (0.5 - ndc.y * 0.5) * height, ndc.z, radius),
                             skyRight: SIMD4(right * halfHeight * aspect, 0), skyUp: SIMD4(up * halfHeight, 0),
                             skyForward: SIMD4(forward, 0))
    }

    /// How far from the player the bent world drops out of sight, on the side facing away from the
    /// camera, where it comes soonest. The tangent point of a ray from the eye to y = -k d².
    /// A lower or closer camera brings this in, so the fog has to follow it.
    static func horizonDistance(eye: SIMD3<Float>, curvature k: Float = curvature) -> Float {
        let a = simd_length(SIMD2(eye.x, eye.z))
        return -a + sqrt(a * a + max(eye.y, 0) / k)
    }

    /// Painter's order: every tile's layer 0, then every tile's layer 1, and so on. Tiles carry a
    /// buffer zone that overlaps their neighbours, and this keeps a road on top of the next tile's lawn.
    func mapDraws() -> [MapDraw] {
        let at = Mercator.project(location.coordinate)
        let g = Mercator.groundScale(latitude: location.coordinate.latitude)
        var out: [MapDraw] = []
        out.reserveCapacity(8 + streamer.tiles.count * 12)
        out.append(MapDraw(pass: .ground, vertices: baseVertices, indices: baseIndices, indexOffset: 0, indexCount: baseIndexCount, uniforms: DrawUniforms()))
        let tiles = Array(streamer.tiles.values)
        let placed = tiles.map { t in (t, DrawUniforms(origin: SIMD2<Float>((t.origin - at) * g), scale: Float(g))) }
        for layer in GroundLayer.allCases {
            for (t, u) in placed {
                let r = t.layers[layer.rawValue]
                guard !r.isEmpty, let v = t.groundVertices, let i = t.groundIndices else { continue }
                out.append(MapDraw(pass: layer == .shadow ? .shadow : .ground, vertices: v, indices: i,
                                   indexOffset: r.lowerBound * 4, indexCount: r.count, uniforms: u))
            }
        }
        for (t, u) in placed {
            guard let v = t.propVertices, let i = t.propIndices else { continue }
            out.append(MapDraw(pass: .prop, vertices: v, indices: i, indexOffset: 0, indexCount: t.propIndexCount, uniforms: u))
        }
        return out
    }
}

func lookAt(_ eye: SIMD3<Float>, _ c: SIMD3<Float>, _ up: SIMD3<Float>) -> simd_float4x4 {
    let f = normalize(c - eye), s = normalize(cross(f, up)), u = cross(s, f)
    return simd_float4x4(SIMD4(s.x, u.x, -f.x, 0), SIMD4(s.y, u.y, -f.y, 0), SIMD4(s.z, u.z, -f.z, 0),
                         SIMD4(-dot(s, eye), -dot(u, eye), dot(f, eye), 1))
}
/// Right-handed, Metal clip space (z 0...1).
func perspective(fovY: Float, aspect: Float, near: Float, far: Float) -> simd_float4x4 {
    let y = 1 / tan(fovY * 0.5), x = y / aspect, z = far / (near - far)
    return simd_float4x4(SIMD4(x, 0, 0, 0), SIMD4(0, y, 0, 0), SIMD4(0, 0, z, -1), SIMD4(0, 0, z * near, 0))
}
