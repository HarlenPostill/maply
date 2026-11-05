import Foundation
import simd
import Combine

enum Expression: String, CaseIterable, Identifiable {
    case auto = "Auto", happy = "Happy", surprised = "Wow"
    var id: String { rawValue }
}

/// UI <-> scene state.
final class DemoState: ObservableObject {
    @Published var move: Move = .idle
    @Published var expression: Expression = .auto
    @Published var outfit = Outfit()
    @Published var backend = "…"
    @Published var error: String?
    init() {
        #if DEBUG
        // Test hook: DEMO_OUTFIT=girl DEMO_MOVE=Walk starts straight in that state, for running the
        // real Metal 4 path on a Mac where there is no touch input to drive the wardrobe.
        let env = ProcessInfo.processInfo.environment
        if env["DEMO_OUTFIT"] == "girl" {
            outfit = Outfit(hair: .bob, top: .argyle, bottoms: .dress, shoeStyle: .maryJanes, shoes: .brown)
        }
        if let m = env["DEMO_MOVE"], let move = Move(rawValue: m) { self.move = move }
        if let y = env["DEMO_YAW"].flatMap(Float.init) { yaw = y }
        #endif
    }

    var wardrobeOpen = true        // taller panel: the camera makes room so the shoes stay visible
    var yaw: Float = -0.42, pitch: Float = 0.12, distance: Float = 3.9
}

struct FrameUniforms {
    var viewProj: simd_float4x4
    var eye: SIMD4<Float>
    var sunDir: SIMD4<Float>
    var scroll: SIMD4<Float>
}
struct DrawUniforms { var uvOffset: SIMD2<Float>; var pad = SIMD2<Float>(0, 0) }

/// In-place walk covers this much ground (metres/second): 2 x half-stride / stance time,
/// set in tools/animate.py. Scrolling the ground at this speed keeps the planted foot locked.
let walkSpeed: Float = 2 * 0.060 / (0.58 * 20 / 30)

/// Renderer-agnostic game state: both Metal backends draw this.
final class CharacterScene {
    let asset: CharacterAsset
    let animator: Animator
    let state: DemoState
    let ground: [SIMD2<Float>]
    let groundIndices: [UInt32]
    private var scrollZ: Float = 0
    private var speed: Float = 0
    private var idleTimer: Float = 7, blinkTimer: Float = 2.5, blinkHold: Float = 0
    private var lastMove: Move = .idle
    private var waveHappy: Float = 0
    private var framing: Float = 1

    init(state: DemoState) throws {
        self.state = state
        asset = try CharacterAsset(url: Bundle.main.url(forResource: "player", withExtension: "glb")!)
        animator = Animator(asset: asset)
        var g = [SIMD2<Float>](), idx = [UInt32]()
        let n = 48, size: Float = 9
        for z in 0...n { for x in 0...n { g.append(SIMD2(Float(x) / Float(n) - 0.5, Float(z) / Float(n) - 0.5) * 2 * size) } }
        for z in 0..<n { for x in 0..<n {
            let a = UInt32(z * (n + 1) + x), b = a + 1, c = a + UInt32(n + 1), d = c + 1
            idx += [a, c, b, b, c, d]
        } }
        ground = g; groundIndices = idx
    }

    func update(dt: Float) {
        framing += ((state.wardrobeOpen ? 1 : 0) - framing) * min(1, dt * 8)
        let move = state.move
        if move != lastMove { animator.play(move.clip); lastMove = move; idleTimer = Float.random(in: 6...10) }
        animator.update(dt: dt)
        // one-shot gestures hand back to the breathing idle
        if (move == .look || move == .wave), animator.looped {
            DispatchQueue.main.async { self.state.move = .idle }
        }
        if move == .idle {
            idleTimer -= dt
            if idleTimer <= 0 { animator.play("Idle_Look", fade: 0.4); idleTimer = 12 + Float.random(in: 0...6) }
            if animator.looped && animator.currentClip == "Idle_Look" { animator.play("Idle", fade: 0.4) }
        }
        let target: Float = move == .walk ? walkSpeed : 0
        speed += (target - speed) * min(1, dt * 7)
        scrollZ = (scrollZ - speed * dt).truncatingRemainder(dividingBy: 240)
        // blinking
        blinkTimer -= dt
        if blinkTimer <= 0 { blinkHold = 0.12; blinkTimer = Float.random(in: 2...5) }
        blinkHold = max(0, blinkHold - dt)
        waveHappy = move == .wave ? 1 : 0
    }

    /// Face atlas is 2x2: neutral, blink / happy, surprised.
    var faceOffset: SIMD2<Float> {
        switch state.expression {
        case .happy: return SIMD2(0, 0.5)
        case .surprised: return SIMD2(0.5, 0.5)
        case .auto:
            if waveHappy > 0 { return SIMD2(0, 0.5) }
            return blinkHold > 0 ? SIMD2(0.5, 0) : .zero
        }
    }

    func isVisible(_ part: String) -> Bool {
        if Wardrobe.slottedParts.contains(part) { return state.outfit.meshes.contains(part) }
        return true
    }

    /// Texture for a draw: the outfit's override when it has one, otherwise the mesh's own material.
    func textureIndex(_ d: DrawItem) -> Int {
        if let name = state.outfit.textureOverride(part: d.part), let i = asset.wardrobeTextures[name] { return i }
        return d.material
    }

    func frameUniforms(aspect: Float) -> FrameUniforms {
        let target = SIMD3<Float>(0, 0.32 - 0.25 * framing, 0)   // sits the character above the control panel
        let cp = cos(state.pitch), sp = sin(state.pitch)
        let eye = target + SIMD3(sin(state.yaw) * cp, sp, cos(state.yaw) * cp) * (state.distance + 1.25 * framing)
        let view = lookAt(eye, target, SIMD3(0, 1, 0))
        let fovY: Float = 0.50
        let proj = perspective(fovY: fovY, aspect: aspect, near: 0.05, far: 40)
        return FrameUniforms(viewProj: proj * view, eye: SIMD4(eye, 1),
                             sunDir: SIMD4(normalize(SIMD3<Float>(-0.35, 0.75, 0.60)), 0),
                             scroll: SIMD4(0, scrollZ, 0, 0))
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
