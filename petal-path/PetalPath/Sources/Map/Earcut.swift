import Foundation
import simd

/// Ear-clipping triangulator for polygons with holes, after Mapbox's earcut (ISC licence).
/// Survives the self-touching rings that real map data contains.
struct Earcut {
    /// Returns triangle indices into the concatenation of `rings`. The first ring is the exterior.
    static func triangulate(_ rings: [[SIMD2<Float>]]) -> [UInt32] {
        var e = Earcut()
        return e.run(rings)
    }

    private struct Node {
        var i: Int32, x: Double, y: Double
        var prev: Int32 = -1, next: Int32 = -1, prevZ: Int32 = -1, nextZ: Int32 = -1
        var z: Int32 = 0
        var steiner = false
    }

    private var n: [Node] = []
    private var tris: [UInt32] = []
    private var minX = 0.0, minY = 0.0, inv = 0.0

    private mutating func run(_ rings: [[SIMD2<Float>]]) -> [UInt32] {
        guard let outer = rings.first, outer.count >= 3 else { return [] }
        let total = rings.reduce(0) { $0 + $1.count }
        n.reserveCapacity(total + rings.count * 2)
        tris.reserveCapacity((total + rings.count * 2) * 3)
        var base = 0
        var head = linkedList(outer, base: 0, clockwise: true)
        base = outer.count
        guard head >= 0, n[Int(head)].next != n[Int(head)].prev else { return [] }
        if rings.count > 1 { head = eliminateHoles(rings, firstBase: base, outer: head) }
        if total > 80 {
            var a = SIMD2<Float>(repeating: .infinity), b = -a
            for p in outer { a = simd_min(a, p); b = simd_max(b, p) }
            minX = Double(a.x); minY = Double(a.y)
            let size = Double(max(b.x - a.x, b.y - a.y))
            inv = size != 0 ? 32767 / size : 0
        }
        earcutLinked(head, 0)
        return tris
    }

    // ---- linked list
    @inline(__always) private func x(_ a: Int32) -> Double { n[Int(a)].x }
    @inline(__always) private func y(_ a: Int32) -> Double { n[Int(a)].y }
    @inline(__always) private func next(_ a: Int32) -> Int32 { n[Int(a)].next }
    @inline(__always) private func prev(_ a: Int32) -> Int32 { n[Int(a)].prev }

    private mutating func linkedList(_ pts: [SIMD2<Float>], base: Int, clockwise: Bool) -> Int32 {
        var sum = 0.0, j = pts.count - 1
        for i in 0..<pts.count {
            sum += Double(pts[j].x - pts[i].x) * Double(pts[i].y + pts[j].y)
            j = i
        }
        var last: Int32 = -1
        if clockwise == (sum > 0) {
            for i in 0..<pts.count { last = insert(base + i, pts[i], last) }
        } else {
            for i in stride(from: pts.count - 1, through: 0, by: -1) { last = insert(base + i, pts[i], last) }
        }
        if last >= 0, equal(last, next(last)) { let l = next(last); remove(last); last = l }
        return last
    }

    private mutating func insert(_ i: Int, _ p: SIMD2<Float>, _ last: Int32) -> Int32 {
        let id = Int32(n.count)
        n.append(Node(i: Int32(i), x: Double(p.x), y: Double(p.y)))
        if last < 0 {
            n[Int(id)].prev = id; n[Int(id)].next = id
        } else {
            let after = next(last)
            n[Int(id)].next = after; n[Int(id)].prev = last
            n[Int(after)].prev = id; n[Int(last)].next = id
        }
        return id
    }

    private mutating func remove(_ p: Int32) {
        let node = n[Int(p)]
        n[Int(node.next)].prev = node.prev
        n[Int(node.prev)].next = node.next
        if node.prevZ >= 0 { n[Int(node.prevZ)].nextZ = node.nextZ }
        if node.nextZ >= 0 { n[Int(node.nextZ)].prevZ = node.prevZ }
    }

    @inline(__always) private func equal(_ a: Int32, _ b: Int32) -> Bool { x(a) == x(b) && y(a) == y(b) }
    @inline(__always) private func area(_ p: Int32, _ q: Int32, _ r: Int32) -> Double {
        (y(q) - y(p)) * (x(r) - x(q)) - (x(q) - x(p)) * (y(r) - y(q))
    }

    private mutating func filterPoints(_ start: Int32, _ endIn: Int32 = -1) -> Int32 {
        guard start >= 0 else { return start }
        var end = endIn >= 0 ? endIn : start
        var p = start, again = false
        repeat {
            again = false
            if !n[Int(p)].steiner, equal(p, next(p)) || area(prev(p), p, next(p)) == 0 {
                remove(p)
                p = prev(p); end = p
                if p == next(p) { break }
                again = true
            } else {
                p = next(p)
            }
        } while again || p != end
        return end
    }

    // ---- the clipping loop
    private mutating func earcutLinked(_ start: Int32, _ pass: Int) {
        guard start >= 0 else { return }
        var ear = start
        if pass == 0, inv != 0 { indexCurve(ear) }
        var stop = ear
        while prev(ear) != next(ear) {
            let p = prev(ear), nx = next(ear)
            if inv != 0 ? isEarHashed(ear) : isEar(ear) {
                tris.append(UInt32(n[Int(p)].i)); tris.append(UInt32(n[Int(ear)].i)); tris.append(UInt32(n[Int(nx)].i))
                remove(ear)
                ear = next(nx); stop = ear
                continue
            }
            ear = nx
            if ear == stop {
                switch pass {
                case 0: earcutLinked(filterPoints(ear), 1)
                case 1: earcutLinked(cureLocalIntersections(filterPoints(ear)), 2)
                default: splitEarcut(ear)
                }
                break
            }
        }
    }

    @inline(__always) private func inTriangle(_ ax: Double, _ ay: Double, _ bx: Double, _ by: Double, _ cx: Double, _ cy: Double, _ px: Double, _ py: Double) -> Bool {
        (cx - px) * (ay - py) >= (ax - px) * (cy - py) &&
        (ax - px) * (by - py) >= (bx - px) * (ay - py) &&
        (bx - px) * (cy - py) >= (cx - px) * (by - py)
    }
    @inline(__always) private func inTriangleExceptFirst(_ ax: Double, _ ay: Double, _ bx: Double, _ by: Double, _ cx: Double, _ cy: Double, _ px: Double, _ py: Double) -> Bool {
        !(ax == px && ay == py) && inTriangle(ax, ay, bx, by, cx, cy, px, py)
    }

    private func isEar(_ ear: Int32) -> Bool {
        let a = prev(ear), b = ear, c = next(ear)
        if area(a, b, c) >= 0 { return false }
        let ax = x(a), bx = x(b), cx = x(c), ay = y(a), by = y(b), cy = y(c)
        let x0 = min(ax, bx, cx), y0 = min(ay, by, cy), x1 = max(ax, bx, cx), y1 = max(ay, by, cy)
        var p = next(c)
        while p != a {
            let px = x(p), py = y(p)
            if px >= x0, px <= x1, py >= y0, py <= y1,
               inTriangleExceptFirst(ax, ay, bx, by, cx, cy, px, py), area(prev(p), p, next(p)) >= 0 { return false }
            p = next(p)
        }
        return true
    }

    private func isEarHashed(_ ear: Int32) -> Bool {
        let a = prev(ear), b = ear, c = next(ear)
        if area(a, b, c) >= 0 { return false }
        let ax = x(a), bx = x(b), cx = x(c), ay = y(a), by = y(b), cy = y(c)
        let x0 = min(ax, bx, cx), y0 = min(ay, by, cy), x1 = max(ax, bx, cx), y1 = max(ay, by, cy)
        let minZ = zOrder(x0, y0), maxZ = zOrder(x1, y1)
        var p = n[Int(ear)].prevZ, q = n[Int(ear)].nextZ
        func hit(_ o: Int32) -> Bool {
            let px = x(o), py = y(o)
            return px >= x0 && px <= x1 && py >= y0 && py <= y1 && o != a && o != c &&
                inTriangleExceptFirst(ax, ay, bx, by, cx, cy, px, py) && area(prev(o), o, next(o)) >= 0
        }
        while p >= 0, n[Int(p)].z >= minZ, q >= 0, n[Int(q)].z <= maxZ {
            if hit(p) || hit(q) { return false }
            p = n[Int(p)].prevZ; q = n[Int(q)].nextZ
        }
        while p >= 0, n[Int(p)].z >= minZ { if hit(p) { return false }; p = n[Int(p)].prevZ }
        while q >= 0, n[Int(q)].z <= maxZ { if hit(q) { return false }; q = n[Int(q)].nextZ }
        return true
    }

    private mutating func cureLocalIntersections(_ startIn: Int32) -> Int32 {
        var start = startIn, p = start
        repeat {
            let a = prev(p), b = next(next(p))
            if !equal(a, b), intersects(a, p, next(p), b), locallyInside(a, b), locallyInside(b, a) {
                tris.append(UInt32(n[Int(a)].i)); tris.append(UInt32(n[Int(p)].i)); tris.append(UInt32(n[Int(b)].i))
                let after = next(p)
                remove(p); remove(after)
                p = b; start = b
            }
            p = next(p)
        } while p != start
        return filterPoints(p)
    }

    private mutating func splitEarcut(_ start: Int32) {
        var a = start
        repeat {
            var b = next(next(a))
            while b != prev(a) {
                if n[Int(a)].i != n[Int(b)].i, isValidDiagonal(a, b) {
                    var c = splitPolygon(a, b)
                    let f = filterPoints(a, next(a))
                    c = filterPoints(c, next(c))
                    earcutLinked(f, 0)
                    earcutLinked(c, 0)
                    return
                }
                b = next(b)
            }
            a = next(a)
        } while a != start
    }

    // ---- holes
    private mutating func eliminateHoles(_ rings: [[SIMD2<Float>]], firstBase: Int, outer: Int32) -> Int32 {
        var queue: [Int32] = [], base = firstBase
        for ring in rings.dropFirst() {
            let list = linkedList(ring, base: base, clockwise: false)
            base += ring.count
            guard list >= 0 else { continue }
            if list == next(list) { n[Int(list)].steiner = true }
            queue.append(leftmost(list))
        }
        queue.sort { x($0) < x($1) }
        var head = outer
        for h in queue { head = eliminateHole(h, head) }
        return head
    }

    private mutating func eliminateHole(_ hole: Int32, _ outer: Int32) -> Int32 {
        guard let bridge = findHoleBridge(hole, outer) else { return outer }
        let reverse = splitPolygon(bridge, hole)
        _ = filterPoints(reverse, next(reverse))
        return filterPoints(bridge, next(bridge))
    }

    private func findHoleBridge(_ hole: Int32, _ outer: Int32) -> Int32? {
        var p = outer, qx = -Double.infinity
        let hx = x(hole), hy = y(hole)
        var m: Int32 = -1
        if equal(hole, p) { return p }
        repeat {
            let nx = next(p)
            if equal(hole, nx) { return nx }
            if hy <= y(p), hy >= y(nx), y(nx) != y(p) {
                let cross = x(p) + (hy - y(p)) * (x(nx) - x(p)) / (y(nx) - y(p))
                if cross <= hx, cross > qx {
                    qx = cross
                    m = x(p) < x(nx) ? p : nx
                    if cross == hx { return m }
                }
            }
            p = nx
        } while p != outer
        guard m >= 0 else { return nil }
        let stop = m, mx = x(m), my = y(m)
        var tanMin = Double.infinity
        p = m
        repeat {
            let px = x(p), py = y(p)
            if hx >= px, px >= mx, hx != px,
               inTriangle(hy < my ? hx : qx, hy, mx, my, hy < my ? qx : hx, hy, px, py) {
                let t = abs(hy - py) / (hx - px)
                if locallyInside(p, hole),
                   t < tanMin || (t == tanMin && (px > x(m) || (px == x(m) && sectorContainsSector(m, p)))) {
                    m = p; tanMin = t
                }
            }
            p = next(p)
        } while p != stop
        return m
    }

    private func sectorContainsSector(_ m: Int32, _ p: Int32) -> Bool {
        area(prev(m), m, prev(p)) < 0 && area(next(p), m, next(m)) < 0
    }

    private func leftmost(_ start: Int32) -> Int32 {
        var p = start, l = start
        repeat {
            if x(p) < x(l) || (x(p) == x(l) && y(p) < y(l)) { l = p }
            p = next(p)
        } while p != start
        return l
    }

    // ---- z-order index: speeds up large coastlines from quadratic to near linear
    private func zOrder(_ px: Double, _ py: Double) -> Int32 {
        var a = UInt32((px - minX) * inv), b = UInt32((py - minY) * inv)
        a = (a | (a << 8)) & 0x00FF00FF; a = (a | (a << 4)) & 0x0F0F0F0F
        a = (a | (a << 2)) & 0x33333333; a = (a | (a << 1)) & 0x55555555
        b = (b | (b << 8)) & 0x00FF00FF; b = (b | (b << 4)) & 0x0F0F0F0F
        b = (b | (b << 2)) & 0x33333333; b = (b | (b << 1)) & 0x55555555
        return Int32(truncatingIfNeeded: a | (b << 1))
    }

    private mutating func indexCurve(_ start: Int32) {
        var p = start
        repeat {
            if n[Int(p)].z == 0 { n[Int(p)].z = zOrder(x(p), y(p)) }
            n[Int(p)].prevZ = prev(p); n[Int(p)].nextZ = next(p)
            p = next(p)
        } while p != start
        let tail = n[Int(p)].prevZ
        n[Int(tail)].nextZ = -1
        n[Int(p)].prevZ = -1
        sortLinked(p)
    }

    private mutating func sortLinked(_ listIn: Int32) {
        var list = listIn, size = 1, merges = 0
        repeat {
            var p = list, tail: Int32 = -1
            list = -1
            merges = 0
            while p >= 0 {
                merges += 1
                var q = p, pSize = 0
                for _ in 0..<size {
                    pSize += 1
                    q = n[Int(q)].nextZ
                    if q < 0 { break }
                }
                var qSize = size
                while pSize > 0 || (qSize > 0 && q >= 0) {
                    let e: Int32
                    if pSize != 0, qSize == 0 || q < 0 || n[Int(p)].z <= n[Int(q)].z {
                        e = p; p = n[Int(p)].nextZ; pSize -= 1
                    } else {
                        e = q; q = n[Int(q)].nextZ; qSize -= 1
                    }
                    if tail >= 0 { n[Int(tail)].nextZ = e } else { list = e }
                    n[Int(e)].prevZ = tail
                    tail = e
                }
                p = q
            }
            n[Int(tail)].nextZ = -1
            size *= 2
        } while merges > 1
    }

    // ---- diagonals
    private func isValidDiagonal(_ a: Int32, _ b: Int32) -> Bool {
        let bi = n[Int(b)].i
        guard n[Int(next(a))].i != bi, n[Int(prev(a))].i != bi, !intersectsPolygon(a, b) else { return false }
        if locallyInside(a, b), locallyInside(b, a), middleInside(a, b),
           area(prev(a), a, prev(b)) != 0 || area(a, prev(b), b) != 0 { return true }
        return equal(a, b) && area(prev(a), a, next(a)) > 0 && area(prev(b), b, next(b)) > 0
    }

    private func sign(_ v: Double) -> Int { v > 0 ? 1 : v < 0 ? -1 : 0 }
    private func onSegment(_ p: Int32, _ q: Int32, _ r: Int32) -> Bool {
        x(q) <= max(x(p), x(r)) && x(q) >= min(x(p), x(r)) && y(q) <= max(y(p), y(r)) && y(q) >= min(y(p), y(r))
    }
    private func intersects(_ p1: Int32, _ q1: Int32, _ p2: Int32, _ q2: Int32) -> Bool {
        let o1 = sign(area(p1, q1, p2)), o2 = sign(area(p1, q1, q2))
        let o3 = sign(area(p2, q2, p1)), o4 = sign(area(p2, q2, q1))
        if o1 != o2, o3 != o4 { return true }
        if o1 == 0, onSegment(p1, p2, q1) { return true }
        if o2 == 0, onSegment(p1, q2, q1) { return true }
        if o3 == 0, onSegment(p2, p1, q2) { return true }
        if o4 == 0, onSegment(p2, q1, q2) { return true }
        return false
    }
    private func intersectsPolygon(_ a: Int32, _ b: Int32) -> Bool {
        let ai = n[Int(a)].i, bi = n[Int(b)].i
        var p = a
        repeat {
            let pn = next(p), pi = n[Int(p)].i, ni = n[Int(pn)].i
            if pi != ai, ni != ai, pi != bi, ni != bi, intersects(p, pn, a, b) { return true }
            p = pn
        } while p != a
        return false
    }
    private func locallyInside(_ a: Int32, _ b: Int32) -> Bool {
        area(prev(a), a, next(a)) < 0
            ? area(a, b, next(a)) >= 0 && area(a, prev(a), b) >= 0
            : area(a, b, prev(a)) < 0 || area(a, next(a), b) < 0
    }
    private func middleInside(_ a: Int32, _ b: Int32) -> Bool {
        var p = a, inside = false
        let px = (x(a) + x(b)) / 2, py = (y(a) + y(b)) / 2
        repeat {
            let q = next(p)
            if (y(p) > py) != (y(q) > py), y(q) != y(p), px < (x(q) - x(p)) * (py - y(p)) / (y(q) - y(p)) + x(p) {
                inside.toggle()
            }
            p = q
        } while p != a
        return inside
    }
    private mutating func splitPolygon(_ a: Int32, _ b: Int32) -> Int32 {
        let a2 = Int32(n.count)
        n.append(Node(i: n[Int(a)].i, x: x(a), y: y(a)))
        let b2 = Int32(n.count)
        n.append(Node(i: n[Int(b)].i, x: x(b), y: y(b)))
        let an = next(a), bp = prev(b)
        n[Int(a)].next = b; n[Int(b)].prev = a
        n[Int(a2)].next = an; n[Int(an)].prev = a2
        n[Int(b2)].next = a2; n[Int(a2)].prev = b2
        n[Int(bp)].next = b2; n[Int(b2)].prev = bp
        return b2
    }
}
