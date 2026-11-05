import Foundation
import simd

enum Move: String, CaseIterable, Identifiable {
    case idle = "Idle", walk = "Walk", look = "Look", wave = "Wave"
    var id: String { rawValue }
    var clip: String { ["Idle": "Idle", "Walk": "Walk", "Look": "Idle_Look", "Wave": "Idle_Wave"][rawValue]! }
}

/// Plays clips with crossfades and produces the joint palette for the vertex shader.
final class Animator {
    private let a: CharacterAsset
    private var cur: Clip, prev: Clip?
    private var curT: Float = 0, prevT: Float = 0
    private var blend: Float = 1, fade: Float = 0.25
    private var T: [SIMD3<Float>], R: [simd_quatf], pT: [SIMD3<Float>], pR: [simd_quatf]
    private var global: [simd_float4x4]
    private(set) var palette: [simd_float4x4]
    /// Clip finished a pass this update.
    private(set) var looped = false

    init(asset: CharacterAsset) {
        a = asset
        cur = asset.clips["Idle"]!
        T = asset.restT; R = asset.restR; pT = T; pR = R
        global = Array(repeating: matrix_identity_float4x4, count: asset.parents.count)
        palette = global
    }

    var currentClip: String { cur.name }

    func play(_ name: String, fade f: Float = 0.25) {
        guard let c = a.clips[name], c.name != cur.name else { return }
        prev = cur; prevT = curT; cur = c; curT = 0; blend = 0; fade = f
    }

    private func sample(_ c: Clip, _ time: Float, _ t: inout [SIMD3<Float>], _ r: inout [simd_quatf]) {
        let f = time / max(c.duration, 1e-4) * Float(c.frames - 1)
        let i0 = min(Int(f), c.frames - 2), k = f - Float(i0), i1 = i0 + 1
        for tr in c.tracks {
            if !tr.translations.isEmpty { t[tr.joint] = mix(tr.translations[i0], tr.translations[i1], t: k) }
            if !tr.rotations.isEmpty { r[tr.joint] = simd_slerp(tr.rotations[i0], tr.rotations[i1], k) }
        }
    }

    func update(dt: Float) {
        curT += dt
        looped = curT >= cur.duration
        if looped { curT = curT.truncatingRemainder(dividingBy: cur.duration) }
        sample(cur, curT, &T, &R)
        var outT = T, outR = R
        if let p = prev {
            blend = min(1, blend + dt / fade)
            prevT = (prevT + dt).truncatingRemainder(dividingBy: p.duration)
            sample(p, prevT, &pT, &pR)
            let w = blend * blend * (3 - 2 * blend)
            for j in 0..<T.count {
                outT[j] = mix(pT[j], T[j], t: w)
                outR[j] = simd_slerp(pR[j], R[j], w)
            }
            if blend >= 1 { prev = nil }
        }
        for j in a.jointOrder {
            let local = makeTRS(outT[j], outR[j], a.restS[j])
            let p = a.parents[j]
            global[j] = (p < 0 ? a.rootTransform : global[p]) * local
            palette[j] = global[j] * a.inverseBind[j]
        }
    }
}
