import Foundation
import simd

/// Minimal glTF 2.0 binary reader: exactly what a skinned game character needs, nothing else.
struct GLTFRoot: Decodable {
    struct Node: Decodable {
        var name: String?; var children: [Int]?; var mesh: Int?; var skin: Int?
        var translation: [Float]?; var rotation: [Float]?; var scale: [Float]?
    }
    struct Primitive: Decodable { var attributes: [String: Int]; var indices: Int; var material: Int? }
    struct Mesh: Decodable { var name: String?; var primitives: [Primitive] }
    struct Accessor: Decodable { var bufferView: Int; var byteOffset: Int?; var componentType: Int; var count: Int; var type: String }
    struct BufferView: Decodable { var byteOffset: Int?; var byteLength: Int; var byteStride: Int? }
    struct Skin: Decodable { var joints: [Int]; var inverseBindMatrices: Int }
    struct Sampler: Decodable { var input: Int; var output: Int }
    struct Target: Decodable { var node: Int; var path: String }
    struct Channel: Decodable { var sampler: Int; var target: Target }
    struct Animation: Decodable { var name: String?; var samplers: [Sampler]; var channels: [Channel] }
    struct TexRef: Decodable { var index: Int }
    struct PBR: Decodable { var baseColorTexture: TexRef? }
    struct Material: Decodable { var name: String?; var pbrMetallicRoughness: PBR? }
    struct Texture: Decodable { var source: Int }
    struct Image: Decodable { var bufferView: Int }
    var nodes: [Node]; var meshes: [Mesh]; var accessors: [Accessor]; var bufferViews: [BufferView]
    var skins: [Skin]; var animations: [Animation]; var materials: [Material]
    var textures: [Texture]; var images: [Image]
}

/// GPU vertex. Must stay in step with `Vertex` in Shaders.metal (56 bytes).
struct Vertex {
    var px: Float, py: Float, pz: Float
    var nx: Float, ny: Float, nz: Float
    var u: Float, v: Float
    var j0: UInt16, j1: UInt16, j2: UInt16, j3: UInt16
    var w0: Float, w1: Float, w2: Float, w3: Float
}

struct DrawItem {
    var part: String          // SK_Hair, SK_Top ... the swap points for customisation
    var material: Int
    var isFace: Bool          // face primitive gets the expression UV offset
    var indexOffset: Int      // bytes
    var indexCount: Int
}

struct Track {
    var joint: Int
    var translations: [SIMD3<Float>] = []
    var rotations: [simd_quatf] = []
}

struct Clip {
    var name: String
    var duration: Float
    var frames: Int
    var tracks: [Track]
}

final class CharacterAsset {
    var vertices: [Vertex] = []
    var indices: [UInt32] = []
    var draws: [DrawItem] = []
    var inverseBind: [simd_float4x4] = []
    var parents: [Int] = []                      // parent joint, -1 for skeleton root
    var rootTransform = matrix_identity_float4x4 // transform of anything above the root joint
    var restT: [SIMD3<Float>] = []
    var restR: [simd_quatf] = []
    var restS: [SIMD3<Float>] = []
    var jointOrder: [Int] = []                   // parents always before children
    var jointNames: [String] = []
    var clips: [String: Clip] = [:]
    var imageData: [Data] = []                   // indexed by material, then wardrobe textures
    var materialNames: [String] = []
    var wardrobeTextures: [String: Int] = [:]    // T_Top_Daisy -> index into imageData

    enum LoadError: Error { case badFile }

    init(url: URL) throws {
        let d = try Data(contentsOf: url)
        func u32(_ o: Int) -> Int { Int(d.withUnsafeBytes { $0.loadUnaligned(fromByteOffset: o, as: UInt32.self) }) }
        guard d.count > 28, u32(0) == 0x46546C67 else { throw LoadError.badFile }
        let jsonLen = u32(12)
        let g = try JSONDecoder().decode(GLTFRoot.self, from: d.subdata(in: 20..<(20 + jsonLen)))
        let binStart = 20 + jsonLen + 8
        let comps = ["SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16]

        func raw(_ i: Int) -> (start: Int, count: Int, comps: Int, type: Int, stride: Int) {
            let a = g.accessors[i], v = g.bufferViews[a.bufferView]
            let n = comps[a.type]!
            let size = [5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4][a.componentType]!
            return (binStart + (v.byteOffset ?? 0) + (a.byteOffset ?? 0), a.count, n, a.componentType, v.byteStride ?? (n * size))
        }
        func floats(_ i: Int) -> [Float] {
            let r = raw(i); var out = [Float](); out.reserveCapacity(r.count * r.comps)
            d.withUnsafeBytes { p in
                for e in 0..<r.count {
                    let base = r.start + e * r.stride
                    for c in 0..<r.comps {
                        switch r.type {
                        case 5126: out.append(p.loadUnaligned(fromByteOffset: base + c * 4, as: Float.self))
                        case 5123: out.append(Float(p.loadUnaligned(fromByteOffset: base + c * 2, as: UInt16.self)) / 65535)
                        default:   out.append(Float(p.load(fromByteOffset: base + c, as: UInt8.self)) / 255)
                        }
                    }
                }
            }
            return out
        }
        func ints(_ i: Int) -> [UInt32] {
            let r = raw(i); var out = [UInt32](); out.reserveCapacity(r.count * r.comps)
            d.withUnsafeBytes { p in
                for e in 0..<r.count {
                    let base = r.start + e * r.stride
                    for c in 0..<r.comps {
                        switch r.type {
                        case 5125: out.append(p.loadUnaligned(fromByteOffset: base + c * 4, as: UInt32.self))
                        case 5123: out.append(UInt32(p.loadUnaligned(fromByteOffset: base + c * 2, as: UInt16.self)))
                        default:   out.append(UInt32(p.load(fromByteOffset: base + c, as: UInt8.self)))
                        }
                    }
                }
            }
            return out
        }

        // ---- skeleton
        let skin = g.skins[0]
        let jointIndex = Dictionary(uniqueKeysWithValues: skin.joints.enumerated().map { ($1, $0) })
        var nodeParent = [Int: Int]()
        for (i, n) in g.nodes.enumerated() { n.children?.forEach { nodeParent[$0] = i } }
        func matrix(_ n: GLTFRoot.Node) -> simd_float4x4 {
            makeTRS(n.translation.map { SIMD3($0[0], $0[1], $0[2]) } ?? .zero,
                    n.rotation.map { simd_quatf(ix: $0[0], iy: $0[1], iz: $0[2], r: $0[3]) } ?? simd_quatf(ix: 0, iy: 0, iz: 0, r: 1),
                    n.scale.map { SIMD3($0[0], $0[1], $0[2]) } ?? .one)
        }
        for node in skin.joints {
            let n = g.nodes[node]
            jointNames.append(n.name ?? "joint")
            parents.append(nodeParent[node].flatMap { jointIndex[$0] } ?? -1)
            restT.append(n.translation.map { SIMD3($0[0], $0[1], $0[2]) } ?? .zero)
            restR.append(n.rotation.map { simd_quatf(ix: $0[0], iy: $0[1], iz: $0[2], r: $0[3]) } ?? simd_quatf(ix: 0, iy: 0, iz: 0, r: 1))
            restS.append(n.scale.map { SIMD3($0[0], $0[1], $0[2]) } ?? .one)
            if parents.last == -1 {
                var m = matrix_identity_float4x4, c = nodeParent[node]
                while let p = c { m = matrix(g.nodes[p]) * m; c = nodeParent[p] }
                rootTransform = m
            }
        }
        var placed = Set<Int>()
        while jointOrder.count < parents.count {
            for j in 0..<parents.count where !placed.contains(j) && (parents[j] < 0 || placed.contains(parents[j])) {
                jointOrder.append(j); placed.insert(j)
            }
        }
        let ibm = floats(skin.inverseBindMatrices)
        for j in 0..<skin.joints.count {
            let m = Array(ibm[(j * 16)..<(j * 16 + 16)])
            inverseBind.append(simd_float4x4(SIMD4(m[0], m[1], m[2], m[3]), SIMD4(m[4], m[5], m[6], m[7]),
                                             SIMD4(m[8], m[9], m[10], m[11]), SIMD4(m[12], m[13], m[14], m[15])))
        }

        // ---- geometry: everything shares one vertex and one index buffer
        for node in g.nodes where node.mesh != nil {
            let mesh = g.meshes[node.mesh!]
            for prim in mesh.primitives {
                let pos = floats(prim.attributes["POSITION"]!), nor = floats(prim.attributes["NORMAL"]!)
                let uv = floats(prim.attributes["TEXCOORD_0"]!), jo = ints(prim.attributes["JOINTS_0"]!)
                let we = floats(prim.attributes["WEIGHTS_0"]!), idx = ints(prim.indices)
                let base = UInt32(vertices.count)
                for i in 0..<(pos.count / 3) {
                    vertices.append(Vertex(px: pos[i * 3], py: pos[i * 3 + 1], pz: pos[i * 3 + 2],
                                           nx: nor[i * 3], ny: nor[i * 3 + 1], nz: nor[i * 3 + 2],
                                           u: uv[i * 2], v: uv[i * 2 + 1],
                                           j0: UInt16(jo[i * 4]), j1: UInt16(jo[i * 4 + 1]), j2: UInt16(jo[i * 4 + 2]), j3: UInt16(jo[i * 4 + 3]),
                                           w0: we[i * 4], w1: we[i * 4 + 1], w2: we[i * 4 + 2], w3: we[i * 4 + 3]))
                }
                let mat = prim.material ?? 0
                draws.append(DrawItem(part: node.name ?? mesh.name ?? "", material: mat,
                                      isFace: g.materials[mat].name == "M_Face",
                                      indexOffset: indices.count * 4, indexCount: idx.count))
                indices.append(contentsOf: idx.map { $0 + base })
            }
        }

        // ---- textures, in material order
        for m in g.materials {
            materialNames.append(m.name ?? "")
            let view = g.bufferViews[g.images[g.textures[m.pbrMetallicRoughness!.baseColorTexture!.index].source].bufferView]
            let s = binStart + (view.byteOffset ?? 0)
            imageData.append(d.subdata(in: s..<(s + view.byteLength)))
        }

        // ---- wardrobe textures: same UVs as the mesh they dress, so swapping needs no new geometry
        for name in Wardrobe.allTextures {
            guard let url = Bundle.main.url(forResource: name, withExtension: "png") else { continue }
            wardrobeTextures[name] = imageData.count
            imageData.append(try Data(contentsOf: url))
        }

        // ---- clips
        for a in g.animations {
            var byJoint = [Int: Track](); var duration: Float = 0; var frames = 0
            for c in a.channels {
                guard let j = jointIndex[c.target.node], c.target.path != "scale" else { continue }
                let s = a.samplers[c.sampler]
                let t = floats(s.input); let v = floats(s.output)
                duration = max(duration, t.last ?? 0); frames = max(frames, t.count)
                var tr = byJoint[j] ?? Track(joint: j)
                if c.target.path == "translation" {
                    tr.translations = stride(from: 0, to: v.count, by: 3).map { SIMD3(v[$0], v[$0 + 1], v[$0 + 2]) }
                } else if c.target.path == "rotation" {
                    tr.rotations = stride(from: 0, to: v.count, by: 4).map { simd_quatf(ix: v[$0], iy: v[$0 + 1], iz: v[$0 + 2], r: v[$0 + 3]) }
                }
                byJoint[j] = tr
            }
            let name = a.name ?? "clip"
            clips[name] = Clip(name: name, duration: duration, frames: frames, tracks: Array(byJoint.values))
        }
    }
}

func makeTRS(_ t: SIMD3<Float>, _ r: simd_quatf, _ s: SIMD3<Float>) -> simd_float4x4 {
    var m = simd_float4x4(r)
    m.columns.0 *= s.x; m.columns.1 *= s.y; m.columns.2 *= s.z
    m.columns.3 = SIMD4(t, 1)
    return m
}
