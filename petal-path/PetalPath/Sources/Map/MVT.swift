import Foundation
import simd

/// Mapbox Vector Tile reader. A hand-written protobuf walk over only the fields the mesher needs.
struct MVTTile {
    var layers: [String: MVTLayer] = [:]

    /// `only` skips the heavy layers (poi alone is over half of a CBD tile) without decoding them.
    init(data: Data, only wanted: Set<String>) {
        data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            var tile = PB(raw)
            while let (field, wire) = tile.next() {
                guard field == 3, wire == 2 else { tile.skip(wire); continue }
                let bytes = tile.bytes()
                guard let name = Self.layerName(bytes), wanted.contains(name) else { continue }
                layers[name] = MVTLayer(bytes, name: name)
            }
        }
    }

    private static func layerName(_ b: UnsafeRawBufferPointer) -> String? {
        var p = PB(b)
        while let (field, wire) = p.next() {
            if field == 1, wire == 2 { return String(decoding: p.bytes(), as: UTF8.self) }
            p.skip(wire)
        }
        return nil
    }
}

enum MVTValue: Equatable {
    case string(String), number(Double), bool(Bool)
    var string: String? { if case .string(let s) = self { return s }; return nil }
    var number: Double? { if case .number(let n) = self { return n }; return nil }
    var bool: Bool? { if case .bool(let b) = self { return b }; return nil }
}

struct MVTFeature {
    enum Kind: UInt64 { case unknown = 0, point, line, polygon }
    var kind: Kind = .unknown
    var tags: [UInt32] = []       // key, value, key, value...
    var geometry: [UInt32] = []
}

struct MVTLayer {
    var name: String
    var extent: Float = 4096
    var keys: [String] = []
    var values: [MVTValue] = []
    var features: [MVTFeature] = []

    init(_ bytes: UnsafeRawBufferPointer, name: String) {
        self.name = name
        var p = PB(bytes)
        while let (field, wire) = p.next() {
            switch (field, wire) {
            case (2, 2): features.append(Self.feature(p.bytes()))
            case (3, 2): keys.append(String(decoding: p.bytes(), as: UTF8.self))
            case (4, 2): values.append(Self.value(p.bytes()))
            case (5, 0): extent = Float(p.varint())
            default: p.skip(wire)
            }
        }
    }

    func tag(_ f: MVTFeature, _ key: String) -> MVTValue? {
        guard let k = keys.firstIndex(of: key) else { return nil }
        var i = 0
        while i + 1 < f.tags.count {
            if f.tags[i] == UInt32(k) { return values[Int(f.tags[i + 1])] }
            i += 2
        }
        return nil
    }

    private static func feature(_ b: UnsafeRawBufferPointer) -> MVTFeature {
        var f = MVTFeature(), p = PB(b)
        while let (field, wire) = p.next() {
            switch (field, wire) {
            case (2, 2): f.tags = p.packed()
            case (3, 0): f.kind = MVTFeature.Kind(rawValue: p.varint()) ?? .unknown
            case (4, 2): f.geometry = p.packed()
            default: p.skip(wire)
            }
        }
        return f
    }

    private static func value(_ b: UnsafeRawBufferPointer) -> MVTValue {
        var p = PB(b)
        while let (field, wire) = p.next() {
            switch (field, wire) {
            case (1, 2): return .string(String(decoding: p.bytes(), as: UTF8.self))
            case (2, 5): return .number(Double(Float(bitPattern: p.fixed32())))
            case (3, 1): return .number(Double(bitPattern: p.fixed64()))
            case (4, 0), (5, 0): return .number(Double(Int64(truncatingIfNeeded: p.varint())))
            case (6, 0): return .number(Double(PB.zigzag(p.varint())))
            case (7, 0): return .bool(p.varint() != 0)
            default: p.skip(wire)
            }
        }
        return .number(0)
    }

    /// Decodes the command stream into rings or lines, multiplied by `scale`.
    func paths(_ f: MVTFeature, scale: Float) -> [[SIMD2<Float>]] {
        var out: [[SIMD2<Float>]] = [], cur: [SIMD2<Float>] = []
        var x: Int32 = 0, y: Int32 = 0, i = 0
        let g = f.geometry
        while i < g.count {
            let cmd = g[i] & 7, n = Int(g[i] >> 3); i += 1
            guard cmd == 1 || cmd == 2 else { continue }   // 7 is ClosePath: rings are implicit
            for _ in 0..<n where i + 1 < g.count {
                x &+= Int32(truncatingIfNeeded: PB.zigzag(UInt64(g[i])))
                y &+= Int32(truncatingIfNeeded: PB.zigzag(UInt64(g[i + 1])))
                i += 2
                if cmd == 1, !cur.isEmpty { out.append(cur); cur = [] }
                cur.append(SIMD2(Float(x), Float(y)) * scale)
            }
        }
        if !cur.isEmpty { out.append(cur) }
        return out
    }

    /// Groups rings into polygons. The MVT spec makes exterior rings positive-area in tile
    /// coordinates (y down) and holes negative.
    func polygons(_ f: MVTFeature, scale: Float) -> [Polygon] {
        var out: [Polygon] = []
        for ring in paths(f, scale: scale) {
            let a = signedArea(ring)
            if a > 0 { out.append(Polygon(rings: [ring])) }
            else if a < 0, !out.isEmpty { out[out.count - 1].rings.append(ring) }
        }
        return out
    }
}

struct Polygon {
    var rings: [[SIMD2<Float>]]    // first is the exterior
    var area: Float { rings.reduce(0) { $0 + signedArea($1) } }
}

func signedArea(_ r: [SIMD2<Float>]) -> Float {
    var a: Float = 0
    var j = r.count - 1
    for i in 0..<r.count { a += r[j].x * r[i].y - r[i].x * r[j].y; j = i }
    return a / 2
}

/// Cursor over a protobuf message.
struct PB {
    private let b: UnsafeRawBufferPointer
    private var i = 0
    init(_ b: UnsafeRawBufferPointer) { self.b = b }

    static func zigzag(_ v: UInt64) -> Int64 { Int64(v >> 1) ^ -Int64(v & 1) }

    mutating func varint() -> UInt64 {
        var v: UInt64 = 0, s: UInt64 = 0
        while i < b.count {
            let c = b[i]; i += 1
            v |= UInt64(c & 0x7F) << s; s += 7
            if c < 0x80 || s > 63 { break }
        }
        return v
    }
    mutating func next() -> (field: Int, wire: Int)? {
        guard i < b.count else { return nil }
        let k = varint()
        return (Int(k >> 3), Int(k & 7))
    }
    mutating func bytes() -> UnsafeRawBufferPointer {
        let n = Int(varint()), end = min(i + n, b.count)
        defer { i = end }
        return UnsafeRawBufferPointer(rebasing: b[i..<end])
    }
    mutating func packed() -> [UInt32] {
        var p = PB(bytes()), out: [UInt32] = []
        out.reserveCapacity(p.b.count)
        while p.i < p.b.count { out.append(UInt32(truncatingIfNeeded: p.varint())) }
        return out
    }
    mutating func fixed32() -> UInt32 { defer { i += 4 }; return b.loadUnaligned(fromByteOffset: i, as: UInt32.self) }
    mutating func fixed64() -> UInt64 { defer { i += 8 }; return b.loadUnaligned(fromByteOffset: i, as: UInt64.self) }
    mutating func skip(_ wire: Int) {
        switch wire {
        case 0: _ = varint()
        case 1: i += 8
        case 2: _ = bytes()
        case 5: i += 4
        default: i = b.count
        }
    }
}
