"""Player character builder. Run inside Blender. All dimensions are millimetres read straight
off the model sheet (character is 1000 mm tall, faces -Y, +X is the character's left)."""
import bpy, bmesh, math, os, random
from mathutils import Vector
ROOT = "/Users/harlenpostill/.t3/projects/maply/player-character"
SCN = "Player"
MM = 0.001
TAU = math.tau

def smooth(a, b, x):
    t = max(0, min(1, (x - a) / (b - a))) if a != b else float(x > a)
    return t * t * (3 - 2 * t)
def lerp(a, b, t): return a + (b - a) * t

class Part:
    """Accumulates geometry. Faces carry per-corner UVs, material slot and shading mode."""
    def __init__(s, name, mats):
        s.name, s.mats, s.v, s.f, s.w = name, mats, [], [], []
    def vert(s, p, w=None):
        s.v.append(tuple(p)); s.w.append(w); return len(s.v) - 1
    def face(s, idx, uv, mat=0, flat=False):
        s.f.append((tuple(idx), tuple(uv), mat, flat))
    def loft(s, rings, uvfn, mat=0, flat=False, closed=True):
        """rings: list of equal-length vert index lists. uvfn(ringIndex, u01)->(u,v)."""
        n = len(rings[0])
        for i in range(len(rings) - 1):
            a, b = rings[i], rings[i + 1]
            for j in range(n if closed else n - 1):
                k = (j + 1) % n
                u0, u1 = j / n, (j + 1) / n
                s.face((a[j], a[k], b[k], b[j]),
                       (uvfn(i, u0), uvfn(i, u1), uvfn(i + 1, u1), uvfn(i + 1, u0)), mat, flat)
    def cap(s, ring, centre, uvfn, mat=0, flat=False, reverse=False):
        n = len(ring)
        for j in range(n):
            a, b = ring[j], ring[(j + 1) % n]
            c = uvfn(.5); uv = (uvfn(j / n), uvfn((j + 1) / n), (c[0], c[1] - .04))
            s.face((a, b, centre) if not reverse else (b, a, centre), uv, mat, flat)

# ----------------------------------------------------------------- joints (mm)
J = dict(
    hips=(0, 0, 245), spine=(0, 0, 295), chest=(0, 0, 385), neck=(0, 0, 472), head=(0, 0, 522), head_end=(0, 0, 800),
    clav=(38, 0, 468), shoulder=(88, 0, 482), elbow=(150, 3, 408), wrist=(205, 6, 338), handtip=(250, 10, 282),
    thigh=(66, 0, 232), knee=(65, -4, 148), ankle=(65, 2, 62), ball=(78, -62, 16), toe=(80, -118, 16),
)
S_, E_, W_ = (Vector(J[k]) for k in ('shoulder', 'elbow', 'wrist'))
H_ = Vector((232, 9, 306))   # hand ball centre

def side(name, s): return name + ('.L' if s > 0 else '.R')

# ----------------------------------------------------------------- skin weights
def norm4(d):
    d = {k: v for k, v in d.items() if v > 0.005}
    top = sorted(d.items(), key=lambda kv: -kv[1])[:4]
    t = sum(v for _, v in top)
    return {k: v / t for k, v in top}

def chain(x, stops):
    """stops: [(x, bone)...] ascending. Returns linear blend between neighbours."""
    if x <= stops[0][0]: return {stops[0][1]: 1}
    for (a, ba), (b, bb) in zip(stops, stops[1:]):
        if x <= b:
            t = smooth(a, b, x)
            return norm4({ba: 1 - t, bb: t}) if ba != bb else {ba: 1}
    return {stops[-1][1]: 1}

def w_torso(p):
    return chain(p[2], [(255, 'hips'), (310, 'hips'), (350, 'spine'), (405, 'spine'), (445, 'chest'),
                        (478, 'chest'), (505, 'neck'), (525, 'neck'), (545, 'head')])
def w_head(p): return {'head': 1}
def w_arm(s):
    def f(p):
        v = Vector(p) * 1.0
        v.x *= s
        d = W_ - S_
        t = (v - S_).dot(d) / d.length_squared
        return chain(t, [(-.25, 'chest'), (-.05, side('shoulder', s)), (.02, side('shoulder', s)), (.14, side('upperarm', s)),
                         (.40, side('upperarm', s)), (.60, side('forearm', s)), (.86, side('forearm', s)), (1.02, side('hand', s))])
    return f
def w_leg(s):
    return lambda p: chain(p[2], [(52, side('foot', s)), (76, side('shin', s)), (128, side('shin', s)), (168, side('thigh', s)),
                                  (205, side('thigh', s)), (245, 'hips')])
def w_shoe(s):
    def f(p):
        t = smooth(30, 75, -p[1])
        d = {side('foot', s): 1 - t, side('toe', s): t}
        return norm4(d)
    return f
def w_shorts(p):
    th = smooth(258, 205, p[2])
    l = smooth(-34, 34, p[0])
    return norm4({'hips': 1 - th * .58, 'thigh.L': th * .58 * l, 'thigh.R': th * .58 * (1 - l)})

# ----------------------------------------------------------------- body
def ellipse_ring(P, cx, cy, z, a, b, n, w, start=0.0, e=2.0):
    r = []
    for j in range(n):
        t = start + j / n * TAU
        st, ct = math.sin(t), math.cos(t)
        x = abs(st) ** (2 / e) * math.copysign(1, st) * a
        y = abs(ct) ** (2 / e) * math.copysign(1, ct) * b
        p = (cx + x, cy - y, z)
        r.append(P.vert(p, w(p)))
    return r

def arm_frame(s):
    d = (W_ - S_).normalized()
    u = d.cross(Vector((0, 1, 0))).normalized()
    v = u.cross(d).normalized()
    return d, u, v
def arm_ring(P, s, centre, r, n, wf, ry=None):
    d, u, v = arm_frame(s)
    ring = []
    for j in range(n):
        t = j / n * TAU
        q = centre + u * math.cos(t) * r + v * math.sin(t) * (ry or r)
        q = Vector((q.x * s, q.y, q.z))
        ring.append(P.vert(q, wf(q)))
    return ring[::-1] if s > 0 else ring
def arm_pt(t):
    # gently follows shoulder -> elbow -> wrist
    if t < .5: return S_.lerp(E_, t * 2)
    return E_.lerp(W_, (t - .5) * 2)

def build_body():
    P = Part('SK_Body', ['M_Face', 'M_Skin'])
    skinuv = lambda i, u: (u, min(.98, i * .1))
    # ---- head
    cx, cy, cz, rx, ry, rz = 0, 0, 725, 212, 215, 210
    lon, lats = 20, [72, 54, 36, 18, 0, -18, -36, -54, -72]
    e = 2.25
    def hp(lat, th):
        la = math.radians(lat)
        c, s = abs(math.cos(la)) ** (2 / e), abs(math.sin(la)) ** (2 / e) * math.copysign(1, lat)
        return (rx * c * math.sin(th), -ry * c * math.cos(th), cz + rz * s)
    rings = [[P.vert(hp(l, j / lon * TAU), {'head': 1}) for j in range(lon)] for l in lats]
    top, bot = P.vert((0, 0, cz + rz), {'head': 1}), P.vert((0, 0, cz - rz), {'head': 1})
    def fuv(p):  # planar front projection into atlas cell 0 (top-left)
        return ((p[0] + 235) / 470 * .5, 1 - (970 - p[2]) / 470 * .5)
    def add_head_face(idx):
        ps = [P.v[i] for i in idx]
        front = sum(q[1] for q in ps) / len(ps) < 0
        uv = [fuv(q) if front else ((q[0] + 300) / 600, (q[2] - 500) / 500) for q in ps]
        P.face(idx, uv, 0 if front else 1)
    for a, b in zip(rings, rings[1:]):
        for j in range(lon):
            k = (j + 1) % lon
            add_head_face((a[j], a[k], b[k], b[j]))
    for j in range(lon):
        k = (j + 1) % lon
        add_head_face((top, rings[0][k], rings[0][j]) if False else (rings[0][j], top, rings[0][k]))
        add_head_face((rings[-1][k], bot, rings[-1][j]))
    # ---- neck + torso (lives under clothes so outfits can be swapped)
    T = [(545, 44, 44), (505, 50, 48), (478, 72, 58), (420, 96, 84), (350, 116, 112), (300, 126, 122), (262, 128, 108), (238, 112, 84)]
    rs = [ellipse_ring(P, 0, 0, z, a, b, 10, w_torso) for z, a, b in T]
    P.loft(rs, skinuv, 1)
    c0 = P.vert((0, 0, 228), {'hips': 1})
    P.cap(rs[-1], c0, lambda u: (u, .9), 1)
    # ---- arms + hands, one loft each so hands stay attached when sleeves are removed
    for s in (1, -1):
        wf = w_arm(s)
        n = 10
        rr = []
        for t, r in [(.02, 22), (.14, 33), (.42, 36), (.5, 36), (.58, 35), (.8, 33), (.98, 30)]:
            rr.append(arm_ring(P, s, arm_pt(t), r, n, wf))
        d, _, _ = arm_frame(s)
        for k, r in [(-.82, .55), (-.48, .88), (-.05, 1), (.42, .90), (.78, .62)]:
            rr.append(arm_ring(P, s, H_ + d * (k * 60), r * 60, n, wf))
        P.loft(rr, skinuv, 1)
        tip = H_ + d * 60
        tip = (tip.x * s, tip.y, tip.z)
        P.cap(rr[-1], P.vert(tip, wf(tip)), lambda u: (u, .95), 1)
    # ---- legs + simple bare foot
    for s in (1, -1):
        wf, cxl = w_leg(s), s * J['thigh'][0]
        L = [(222, 38), (190, 40), (168, 41), (150, 42), (132, 41), (100, 40), (70, 39), (54, 38)]
        rs = [ellipse_ring(P, cxl, 0, z, r, r, 10, wf) for z, r in L]
        for (z, a, b, cy_) in [(36, 40, 56, -8), (14, 44, 66, -10)]:
            rs.append(ellipse_ring(P, cxl + s * 6, cy_, z, a, b, 10, w_shoe(s)))
        P.loft(rs, skinuv, 1)
        q = (cxl + s * 6, -10, 8)
        P.cap(rs[-1], P.vert(q, w_shoe(s)(q)), lambda u: (u, .95), 1)
        q = (cxl, 0, 228)
        P.cap(rs[0], P.vert(q, wf(q)), lambda u: (u, 0), 1, reverse=True)
    return P

# ----------------------------------------------------------------- hair
def head_D(z):
    k = max(.04, 1 - ((z - 725) / 210) ** 2)
    return 213 * math.sqrt(k)
def dome(th_deg, z):
    """horizontal distance of the hair volume at azimuth th and height z"""
    t = math.radians(th_deg)
    # rounder towards the forehead, flatter over the crown and back
    az = abs(((th_deg + 180) % 360) - 180)
    ex = lerp(2.0, 2.6, smooth(25, 100, az))
    k = max(.02, 1 - abs((z - 735) / 265) ** ex) ** (1 / ex)
    a, b = 272 * k, 295 * k
    return a * b / math.hypot(b * math.sin(t), a * math.cos(t)), 10 * math.cos(t) * -1 * -1
def pol(th, D, z):
    t = math.radians(th)
    return (D * math.sin(t), -D * math.cos(t), z)

# hairline, read off the sheet. (azimuth, z, distance); None distance = sit on the head surface + offset
S = None
RIM = [
    (-3, 790, 26), (24, 826, 11), (52, 806, 11), (76, 745, 9), (90, 672, 9), (97, 596, 9),
    (106, 590, 238), (113, 655, 352), (122, 612, 258), (133, 572, 330), (146, 592, 262), (159, 524, 296),
    (170, 566, 242), (181, 520, 240), (192, 566, 244), (202, 526, 302), (214, 590, 264), (228, 568, 336),
    (239, 612, 258), (248, 652, 354), (255, 590, 238), (263, 596, 9), (270, 672, 9), (282, 735, 9),
    (304, 792, 11), (330, 846, 11), (351, 884, 7),
]
# entries with D<30 mean "offset from head surface"
RINGC = {0: 930, 22.5: 906, 45: 882, 67.5: 835, 90: 800, 112.5: 748, 135: 716, 157.5: 700, 180: 695,
         202.5: 700, 225: 716, 247.5: 748, 270: 800, 292.5: 838, 315: 892, 337.5: 930}
TIER = {90: (338, 802), 270: (340, 806), 112.5: (272, 735), 247.5: (272, 735), 135: (268, 690), 225: (270, 690)}

def stitch(P, a, b, ang_a, ang_b):
    """Triangulate between two cyclic rings of different size, marching by azimuth."""
    tris = []
    i, j, na, nb = 0, 0, len(a), len(b)
    A = lambda k: ang_a[k % na] + 360 * (k // na)
    B = lambda k: ang_b[k % nb] + 360 * (k // nb)
    while i < na or j < nb:
        if j >= nb or (i < na and A(i + 1) <= B(j + 1)):
            tris.append((a[i % na], a[(i + 1) % na], b[j % nb])); i += 1
        else:
            tris.append((b[j % nb], a[i % na], b[(j + 1) % nb])); j += 1
    return tris

def box_uv(ps):
    n = (Vector(ps[1]) - Vector(ps[0])).cross(Vector(ps[2]) - Vector(ps[0]))
    ax = max(range(3), key=lambda i: abs(n[i]))
    u, v = [(1, 2), (0, 2), (0, 1)][ax]
    off = ax * .31
    return [(q[u] / 420 + off, q[v] / 420 + off) for q in ps]

def ring_z(ring, th):
    keys = sorted(ring)
    th %= 360
    for a, b in zip(keys, keys[1:] + [keys[0] + 360]):
        if a <= th <= b:
            t = (th - a) / (b - a)
            return lerp(ring[a], ring[b % 360], t)
    return ring[keys[0]]

def build_hair(name='SK_Hair', mats=('M_Hair',), RIM=RIM, RINGC=RINGC, TIER=TIER):
    rnd = random.Random(11)
    P = Part(name, list(mats))
    W = {'head': 1}
    jit = lambda a: rnd.uniform(-a, a)
    rim, rim_a = [], []
    for th, z, D in RIM:
        d = head_D(z) + D if D < 30 else D
        rim.append(P.vert(pol(th, d, z), W)); rim_a.append(th)
    under = []
    for (th, z, D), _ in zip(RIM, rim):
        zu = z + (30 if D < 30 else 55)
        under.append(P.vert(pol(th, head_D(zu) - 14, zu), W))
    ringC, angC = [], []
    for th in sorted(RINGC):
        z = RINGC[th]; d = dome(th, z)[0]
        if th in TIER: d, z = TIER[th]
        p = pol(th, d, z)
        ringC.append(P.vert((p[0] + jit(5), p[1] + jit(5) + 10, p[2] + jit(5)), W)); angC.append(th)
    ringB, angB = [], []
    for k in range(10):
        th = 9 + k * 36
        z = 735 + 265 * math.cos(math.radians(31))
        p = pol(th, dome(th, z)[0], z)
        ringB.append(P.vert((p[0] + jit(7), p[1] + jit(7) + 8, p[2] + jit(6)), W)); angB.append(th)
    ringM, angM = [], []
    for k in range(16):
        th = 11.25 + k * 22.5
        z = lerp(ring_z(RINGC, th), 945, .55)
        p = pol(th, dome(th, z)[0] * 1.035, z)
        ringM.append(P.vert((p[0] + jit(7), p[1] + jit(7) + 10, p[2] + jit(7)), W)); angM.append(th)
    crown = P.vert((-6, 18, 1000), W)  # sheet: peak of the hair
    tris = []
    tris += stitch(P, rim, ringC, rim_a, angC)
    tris += stitch(P, ringC, ringM, angC, angM)
    tris += stitch(P, ringM, ringB, angM, angB)
    for k in range(10):
        tris.append((ringB[k], ringB[(k + 1) % 10], crown))
    n = len(rim)
    for k in range(n):
        m = (k + 1) % n
        tris.append((rim[m], rim[k], under[k]))
        tris.append((rim[m], under[k], under[m]))
    for t in tris:
        P.face(t, box_uv([P.v[i] for i in t]), 0, True)
    return P

# Girl's style: rounded bangs that stop above the eyes, soft ends, two high bunches.
# Bunches stay high so the raised arm in Idle_Wave passes underneath them.
RIM_G = [
    (0, 804, 26), (14, 824, 18), (28, 800, 24), (42, 818, 16), (56, 778, 18), (70, 760, 9), (80, 700, 9), (92, 622, 12),
    (104, 640, 9), (116, 590, 262), (130, 612, 256), (145, 556, 272), (158, 592, 252), (170, 540, 248), (181, 576, 240),
    (192, 538, 248), (204, 592, 252), (217, 556, 272), (232, 612, 256), (246, 590, 262), (258, 640, 9), (268, 622, 12),
    (280, 700, 9), (290, 760, 9), (304, 778, 18), (318, 818, 16), (332, 800, 24), (346, 824, 18),
]
RINGC_G = {0: 926, 22.5: 918, 45: 900, 67.5: 846, 90: 792, 112.5: 746, 135: 716, 157.5: 700, 180: 695,
           202.5: 700, 225: 716, 247.5: 746, 270: 792, 292.5: 846, 315: 900, 337.5: 918}

def ring_at(P, c, tang, r, n, jag=0.0):
    t = Vector(tang).normalized()
    u = t.cross(Vector((0, 1, 0))).normalized()
    v = u.cross(t).normalized()
    out = []
    for j in range(n):
        a = j / n * TAU
        q = Vector(c) + u * math.cos(a) * r + v * math.sin(a) * r + t * (jag if j % 2 else -jag)
        out.append(P.vert(q, {'head': 1}))
    return out

def build_hair_bunches():
    P = build_hair('SK_Hair_Bunches', ('M_Hair', 'M_HairTie'), RIM_G, RINGC_G, {})
    n = 8
    for s in (1, -1):
        path = [((250, 24, 806), (1, 0, -.05), 26, 0), ((304, 28, 800), (1, 0, -.12), 27, 0), ((330, 30, 784), (1, 0, -.6), 50, 0),
                ((354, 32, 738), (.5, 0, -1), 60, 0), ((368, 36, 688), (.2, 0, -1), 48, 6), ((372, 38, 648), (.1, 0, -1), 25, 16)]
        rings = [ring_at(P, (c[0] * s, c[1], c[2]), (d[0] * s, d[1], d[2]), r, n, jag) for c, d, r, jag in path]
        n0 = len(P.f)
        P.loft(rings, lambda i, u: (u, i * .17))
        tip = P.vert((374 * s, 40, 606), {'head': 1})
        P.cap(rings[-1], tip, lambda u: (u, .9))
        P.cap(rings[0], P.vert((236 * s, 24, 806), {'head': 1}), lambda u: (u, 0), reverse=True)
        # box UVs like the shell, triangulated so the creases fall per facet
        fresh = P.f[n0:]; del P.f[n0:]
        for idx, _, _, _ in fresh:
            for t in ([idx] if len(idx) == 3 else [(idx[0], idx[1], idx[2]), (idx[0], idx[2], idx[3])]):
                P.face(t, box_uv([P.v[i] for i in t]), 0, True)
        # hair tie, a short band round the root of the bunch
        tie = [ring_at(P, ((x) * s, 26, z), (s, 0, -.12), r, 10) for x, z, r in ((288, 803, 28), (293, 803, 36), (307, 801, 36), (312, 800, 28))]
        P.loft(tie, lambda i, u: (u, i * .3), mat=1)
    return P

# Wavy chin-length bob, two layers like the sheet. Unlike the spiky styles this is a smooth grid:
# Upper layer: side-swept fringe, then seven rounded petal lobes with flicked ends round the sides and back.
# Lower layer: a separate curtain under it, rounded lobes that curl in at the jaw.
BOB_FRONT = [(-90, 642, 12), (-80, 690, 9), (-72, 760, 9), (-58, 812, 10), (-40, 838, 10), (-24, 856, 10), (-12, 860, 8),
             (-4, 788, 24), (10, 832, 13), (30, 866, 10), (46, 848, 10), (60, 812, 10), (72, 760, 9), (80, 690, 9), (90, 642, 12)]

def scallop(x):
    """0 at a cusp, 1 at the middle of a lobe. Rounded lobes meeting in sharp cusps."""
    return abs(math.cos(math.pi * x)) ** .55

def bob_rim(th):
    """(z, distance from axis) of the upper layer's edge at azimuth th, -180..180"""
    az = abs(th)
    if az <= 90:
        for (a, za, oa), (b, zb, ob) in zip(BOB_FRONT, BOB_FRONT[1:]):
            if a <= th <= b:
                t = (th - a) / (b - a)
                z, off = lerp(za, zb, t), lerp(oa, ob, t)
                return z, head_D(z) + off
    l = scallop((az - 96) / 28)
    flare = lerp(328, 314, smooth(120, 150, az))
    z, d = lerp(672, 620, l), lerp(296, flare, l)
    return z, lerp(head_D(642) + 12, d, smooth(88, 104, az))

def flower(P, centre, normal, R, mat):
    # felt daisy, purely surface: six puffy petals and a gold middle
    n = Vector(normal).normalized()
    u = n.cross(Vector((0, 0, 1))).normalized(); v = n.cross(u).normalized()
    c = Vector(centre)
    W = {'head': 1}
    def dome(o, ra, rb, h, rot, uvc):
        ring, seg = [], 8
        for j in range(seg):
            t = j / seg * TAU
            a, b = math.cos(t) * ra, math.sin(t) * rb
            cr, sr = math.cos(rot), math.sin(rot)
            ring.append(P.vert(o + u * (a * cr - b * sr) + v * (a * sr + b * cr), W))
        top = P.vert(o + n * h, W)
        for j in range(seg):
            k = (j + 1) % seg
            q = lambda m: (uvc + math.cos(m / seg * TAU) * .1, .5 + math.sin(m / seg * TAU) * .2)
            P.face((ring[j], ring[k], top), (q(j), q(j + 1), (uvc, .5)), mat)
    for k in range(6):
        a = k / 6 * TAU
        dome(c + (u * math.cos(a) + v * math.sin(a)) * R * .60 + n * 2, R * .46, R * .40, 20, a, .25)
    dome(c + n * 9, R * .32, R * .32, 15, 0, .75)

def build_hair_bob():
    P = Part('SK_Hair_Bob', ['M_Hair', 'M_Flower'])
    W = {'head': 1}
    # keep every hairline control point, fill the rest evenly
    ths = sorted(set([a for a, _, _ in BOB_FRONT] + [-180 + k * 7.5 for k in range(48)]))
    ths = [t for t in ths if t < 180]
    n = len(ths)
    tris = []
    crown = P.vert((-4, 14, 1000), W)
    rings = []
    for f in (.30, .58, .82, 1.0):
        ring = []
        for th in ths:
            z, d = bob_rim(th)
            a = f * math.pi / 2
            zi = z + (998 - z) * math.cos(a)
            di = d * math.sin(a)
            if f < 1:                       # volume never dips under the dome, so no skin shows through
                di = max(di, dome(th, max(zi, 735))[0] * (.86 if abs(th) < 95 else .9))
            ring.append(P.vert(pol(th, di, zi), W))
        rings.append(ring)
    for k in range(n):
        tris.append((rings[0][k], rings[0][(k + 1) % n], crown))
    for a, b in zip(rings, rings[1:]):
        for k in range(n):
            m = (k + 1) % n
            tris += [(a[k], b[k], b[m]), (a[k], b[m], a[m])]
    rim = rings[-1]
    under = []
    curl = []                               # a small roll under the edge softens the flicks
    for th in ths:
        z, d = bob_rim(th)
        curl.append(P.vert(pol(th, d - (26 if abs(th) > 92 else 5), z + (16 if abs(th) > 92 else 10)), W))
        zu = z + 52
        under.append(P.vert(pol(th, head_D(zu) - 14, zu), W))
    for k in range(n):
        m = (k + 1) % n
        tris += [(rim[m], rim[k], curl[k]), (rim[m], curl[k], curl[m]), (curl[m], curl[k], under[k]), (curl[m], under[k], under[m])]
    # lower layer, an open curtain from jaw round the back to the other jaw
    steps = 30
    low = [[] for _ in range(5)]
    for k in range(steps + 1):
        th = 80 + k * 200 / steps
        e = smooth(0, 30, min(th - 80, 280 - th))
        l = scallop((th - 92) / 22)
        zb = lerp(556, 508, l)
        spec = [(705, 250), (615, lerp(head_D(615) + 6, 296, e)), (574, lerp(head_D(574) + 8, lerp(298, 318, l), e)),
                (zb, lerp(198, lerp(266, 290, l), e)), (zb + 34, lerp(150, 226, e))]
        for r, (z, d) in zip(low, spec):
            r.append(P.vert(pol(th, d, z), W))
    first_low = len(tris)
    for a, b in zip(low, low[1:]):
        for k in range(steps):
            tris += [(a[k], a[k + 1], b[k + 1]), (a[k], b[k + 1], b[k])]
    for t in tris:
        P.face(t, box_uv([P.v[i] for i in t]), 0, True)
    P.outward = range(first_low, len(tris))
    th, z = -52, 836
    d = bob_rim(th)
    p = pol(th, 238, z)
    flower(P, (p[0], p[1], p[2]), (p[0], p[1], (p[2] - 735) * .8), 64, 1)
    return P

# ----------------------------------------------------------------- top
ROW = lambda r: 1 - r / 1024
def build_top():
    P = Part('SK_Top', ['M_Top'])
    def torso_v(z): return ROW((490 - z) / (490 - 262) * 640)
    rings_def = [(494, 84, 70), (476, 104, 82), (430, 113, 98), (350, 131, 128), (308, 142, 142), (306, 147, 147), (262, 148, 147), (260, 128, 120)]
    n = 16
    rs = [ellipse_ring(P, 0, 0, z, a, b, n, w_torso, start=math.pi, e=2.15) for z, a, b in rings_def]
    zs = [r[0] for r in rings_def]
    P.loft(rs, lambda i, u: (u, torso_v(zs[i])))
    # turtleneck collar
    C = [(488, 88, 74), (503, 90, 77), (520, 80, 68), (525, 66, 58), (508, 55, 48)]
    rs = [ellipse_ring(P, 0, 0, z, a, b, n, w_torso, start=math.pi) for z, a, b in C]
    P.loft(rs, lambda i, u: (u * 2, ROW(662 + i / (len(C) - 1) * 116)))
    # sleeves
    def slv(s):
        wf = w_arm(s)
        rows = [(.02, 26), (.12, 40), (.40, 50), (.5, 51), (.60, 52), (.86, 54), (1.0, 58), (.96, 41)]
        rr = [arm_ring(P, s, arm_pt(t), r, 10, wf) for t, r in rows]
        P.loft(rr, lambda i, u: (u, ROW(804 + min(i, 6) / 6 * 214)))
    slv(1); slv(-1)
    return P

# ----------------------------------------------------------------- shorts
def build_bottoms():
    P = Part('SK_Bottoms', ['M_Bottoms'])
    n = 16
    defs = [(280, 134, 100), (262, 144, 106), (235, 147, 106), (205, 147, 100)]
    rs = [ellipse_ring(P, 0, 0, z, a, b, n, w_shorts, e=2.3) for z, a, b in defs]
    P_uv = lambda i, u: (u, .95 - i * .17)
    P.loft(rs, P_uv)
    cr = rs[-1]
    c1 = P.vert((0, -40, 205), w_shorts((0, 0, 205)))
    c2 = P.vert((0, 40, 205), w_shorts((0, 0, 205)))
    # ring index 0 is front centre, 4 = +X, 8 = back, 12 = -X
    for s in (1, -1):
        arc = [cr[i % n] for i in (range(0, 9) if s > 0 else range(8, 17))]
        tube = arc + ([c2, c1] if s > 0 else [c1, c2])
        cx = s * 72
        wf = w_shorts
        def target(vi, a, b, z, ins=0):
            x, y, _ = P.v[vi]
            t = math.atan2(x - cx, -y)
            p = (cx + math.sin(t) * (a - ins), -math.cos(t) * (b - ins), z)
            return P.vert(p, wf(p))
        low = [target(v, 71, 86, 170) for v in tube]
        lip = [target(v, 71, 86, 182, ins=20) for v in tube]
        P.loft([tube, low, lip], lambda i, u: (u, .44 - i * .22))
    return P

def w_skirt(p):
    # stays with the hips at the waist, the hem follows the legs a little so they never poke through
    th = smooth(250, 168, p[2]) * .42
    l = smooth(-70, 70, p[0])
    return norm4({'hips': 1 - th, 'thigh.L': th * l, 'thigh.R': th * (1 - l)})

def build_skirt():
    P = Part('SK_Skirt', ['M_Skirt'])
    n = 24
    R = [(284, 132, 98, 0), (264, 141, 105, 0), (236, 150, 118, 3), (200, 166, 134, 7), (168, 180, 146, 10), (178, 156, 122, 0)]
    rings = []
    for z, a, b, pleat in R:
        ring = []
        for j in range(n):
            t = j / n * TAU
            k = pleat if j % 2 == 0 else -pleat       # box pleats: every other vertex stands proud
            p = (math.sin(t) * (a + k), -math.cos(t) * (b + k), z)
            ring.append(P.vert(p, w_skirt(p)))
        rings.append(ring)
    P.loft(rings, lambda i, u: (u, 1 - min(i, 4) * .225), flat=False)
    return P

def w_dress(p):
    if p[2] > 262: return w_torso(p)
    th = smooth(262, 184, p[2]) * .40
    l = smooth(-80, 80, p[0])
    return norm4({'hips': 1 - th, 'thigh.L': th * l, 'thigh.R': th * (1 - l)})

def build_dress():
    P = Part('SK_Dress', ['M_Dress'])
    v_of = lambda z: ROW((494 - z) / (494 - 184) * 640)
    R = [(494, 84, 70), (476, 104, 82), (430, 116, 102), (360, 136, 128), (300, 154, 148), (250, 168, 160), (205, 180, 172), (192, 182, 174), (184, 178, 170), (190, 156, 146)]
    n = 20
    rs = [ellipse_ring(P, 0, 0, z, a, b, n, w_dress, start=math.pi, e=2.1) for z, a, b in R]
    zs = [r[0] for r in R]
    P.loft(rs, lambda i, u: (u, v_of(max(zs[i], 184.5))))
    # roll neck, chunkier than the sweater's
    C = [(486, 88, 76), (500, 106, 94), (518, 104, 90), (532, 84, 72), (528, 62, 54), (508, 54, 46)]
    rs = [ellipse_ring(P, 0, 0, z, a, b, 16, w_torso, start=math.pi) for z, a, b in C]
    P.loft(rs, lambda i, u: (u * 2, ROW(656 + i / (len(C) - 1) * 126)))
    for s in (1, -1):
        wf = w_arm(s)
        rows = [(.02, 26), (.12, 42), (.40, 52), (.5, 54), (.60, 55), (.84, 58), (.97, 56), (1.0, 48), (.96, 41)]
        rr = [arm_ring(P, s, arm_pt(t), r, 10, wf) for t, r in rows]
        P.loft(rr, lambda i, u: (u, ROW(800 + i / 8 * 218)))
    return P

def build_tights():
    P = Part('SK_Tights', ['M_Dress'])
    for s in (1, -1):
        wf, cx = w_leg(s), s * 65
        L = [(216, 44), (190, 45), (168, 45), (150, 45), (132, 45), (100, 44), (72, 43), (56, 42)]
        rs = [ellipse_ring(P, cx, 0, z, r, r, 10, wf) for z, r in L]
        P.loft(rs, lambda i, u: (u, ROW(668 + i * 14)))
    return P

def build_mary_janes():
    P = Part('SK_Shoes_MaryJane', ['M_Shoes'])
    def base(s, n, g, sh):
        out = []
        for j in range(n):
            t = j / n * TAU
            st, ct = math.sin(t), math.cos(t)
            e = 2.15
            out.append((s * 80 + abs(st) ** (2 / e) * math.copysign(1, st) * (68 * sh + g),
                        -21 - abs(ct) ** (2 / e) * math.copysign(1, ct) * (98 * sh + g)))
        return out
    for s in (1, -1):
        n = 14
        wf = w_shoe(s)
        def ring(pts, zf):
            r = []
            for j, (x, y) in enumerate(pts):
                p = (x, y, zf(j) if callable(zf) else zf); r.append(P.vert(p, wf(p)))
            return r
        sole = [ring(base(s, n, -2, 1), 0), ring(base(s, n, 0, 1), 4), ring(base(s, n, 1, 1), 22), ring(base(s, n, -3, .975), 24)]
        P.loft(sole, lambda i, u: (u, 1 - (362 + i * 22) / 512), flat=True)
        P.cap(sole[0], P.vert((s * 80, -21, 0), wf((s * 80, -21, 0))), lambda u: (u, .25), reverse=True, flat=True)
        # low cut: the plan outline closes over the toes, then opens into an oval round the foot
        cx, cy, ca, cb = s * 72, -12, 48, 72
        ct = lambda j: math.cos(j / n * TAU)
        oval = [(cx + math.sin(j / n * TAU) * ca, cy - ct(j) * cb) for j in range(n)]
        rim_z = lambda j: 50 + 16 * max(0, ct(j)) ** 2 + 10 * max(0, -ct(j)) ** 2
        mid = [(lerp(x, ox, .42), lerp(y, oy, .42)) for (x, y), (ox, oy) in zip(base(s, n, -3, .975), oval)]
        up = [sole[-1], ring(mid, lambda j: lerp(24, rim_z(j), .78) + 3), ring(oval, rim_z)]
        lip = ring([(lerp(x, cx, .10), lerp(y, cy, .10)) for x, y in oval], lambda j: rim_z(j) - 9)
        up.append(lip)
        P.loft(up, lambda i, u: (u, 1 - (292 - i * 80) / 512))
        # cream sock across the top of the foot, seen through the opening
        ank = [(s * 65 + math.sin(j / n * TAU) * 44, 2 - math.cos(j / n * TAU) * 44) for j in range(n)]
        ANKZ = 80
        foot = [lip, ring([(lerp(x, ax, .55), lerp(y, ay, .55)) for (x, y), (ax, ay) in zip([(lerp(x, cx, .10), lerp(y, cy, .10)) for x, y in oval], ank)], 69), ring(ank, ANKZ)]
        P.loft(foot, lambda i, u: (u, .13 - i * .04))
        # the strap
        arc = []
        for k in range(7):
            f = math.radians(-90 + k * 30)
            r4 = []
            for hx, hz, dy in ((50, 24, -10), (54, 28, -10), (54, 28, 10), (50, 24, 10)):
                p = (cx + math.sin(f) * hx, -52 + dy, 48 + math.cos(f) * hz)
                r4.append(P.vert(p, wf(p)))
            arc.append(r4)
        P.loft(arc, lambda i, u: (u, 1 - (120 + i * 20) / 512))
    return P

# ----------------------------------------------------------------- socks & shoes
def build_socks():
    P = Part('SK_Socks', ['M_Shoes'])
    for s in (1, -1):
        wf, cx = w_leg(s), s * 65
        L = [(116, 42), (128, 47), (121, 48), (92, 46), (64, 45)]
        rs = [ellipse_ring(P, cx, 0, z, r, r, 10, wf) for z, r in L]
        P.loft(rs, lambda i, u: (u * 2, .135 - i * .03))
    return P

def build_shoes():
    P = Part('SK_Shoes', ['M_Shoes'])
    def outline(s, n, grow=0.0, shrink=1.0):
        out = []
        for j in range(n):
            t = j / n * TAU
            st, ct = math.sin(t), math.cos(t)
            e = 2.15
            x = abs(st) ** (2 / e) * math.copysign(1, st) * (68 * shrink + grow)
            y = abs(ct) ** (2 / e) * math.copysign(1, ct) * (98 * shrink + grow)
            out.append((s * 80 + x, -21 - y))
        return out, None
    for s in (1, -1):
        n = 12
        wf = w_shoe(s)
        def ring(pts, z):
            r = []
            for x, y in pts:
                p = (x, y, z); r.append(P.vert(p, wf(p)))
            return r
        base = lambda g, sh: outline(s, n, g, sh)[0]
        sole = [ring(base(-2, 1), 0), ring(base(0, 1), 4), ring(base(1, 1), 22), ring(base(-3, .975), 24)]
        SV = lambda i, u: (u, ROW(362 + i * 24) * 2 - 1 if False else 1 - (362 + i * 22) / 512)
        P.loft(sole, SV, flat=True)
        ctr = P.vert((s * 80, -21, 0), wf((s * 80, -21, 0)))
        P.cap(sole[0], ctr, lambda u: (u, .25), reverse=True, flat=True)
        # upper: morph the plan outline into a circle round the ankle
        ank = [(s * 65 + math.sin(j / n * TAU) * 49, 2 - math.cos(j / n * TAU) * 52) for j in range(n)]
        up = []
        for t, z in [(0, 24), (.30, 52), (.62, 68), (.88, 77), (1, 82)]:
            bb = base(-3, .975)
            # keep the toe box full, it stays low and round for longer than the heel
            pts = []
            for j, ((x, y), (ax, ay)) in enumerate(zip(bb, ank)):
                tt = t ** (1.45 if y < -30 else 1.0)
                pts.append((lerp(x, ax, tt), lerp(y, ay, tt)))
            up.append(sole[-1] if t == 0 else ring(pts, z))
        up.append(ring([(s * 65 + math.sin(j / n * TAU) * 44, 2 - math.cos(j / n * TAU) * 46) for j in range(n)], 66))
        P.loft(up, lambda i, u: (u, 1 - (292 - i * 56) / 512 if i < 5 else .95))
    return P

# ----------------------------------------------------------------- materials
def get_mat(name, tex):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    b = nt.nodes.new('ShaderNodeBsdfPrincipled')
    t = nt.nodes.new('ShaderNodeTexImage')
    p = os.path.join(ROOT, 'textures', tex)
    img = bpy.data.images.get(tex) or bpy.data.images.load(p)
    img.filepath = p; img.reload()
    img.colorspace_settings.name = 'sRGB'
    t.image = img
    nt.links.new(t.outputs['Color'], b.inputs['Base Color'])
    nt.links.new(b.outputs['BSDF'], out.inputs['Surface'])
    b.inputs['Roughness'].default_value = 1.0
    b.inputs['Specular IOR Level'].default_value = 0.1
    t.location = (-320, 0); out.location = (320, 0)
    return m
MATS = dict(M_Face='T_Face.png', M_Skin='T_Skin.png', M_Hair='T_Hair.png', M_Top='T_Top.png', M_Bottoms='T_Bottoms.png', M_Shoes='T_Shoes.png',
            M_Skirt='T_Skirt.png', M_HairTie='T_HairTie.png', M_Flower='T_Flower.png', M_Dress='T_Dress.png')
# Alternate items share the skeleton but start hidden, the default outfit is the model sheet.
ALT_ITEMS = ('SK_Hair_Bunches', 'SK_Hair_Bob', 'SK_Skirt', 'SK_Dress', 'SK_Tights', 'SK_Shoes_MaryJane')

def realize(P, coll, arm):
    me = bpy.data.meshes.new(P.name)
    verts = [Vector(v) * MM for v in P.v]
    me.from_pydata([tuple(v) for v in verts], [], [f[0] for f in P.f])
    me.update()
    uv = me.uv_layers.new(name='UVMap')
    for poly, f in zip(me.polygons, P.f):
        poly.material_index = f[2]
        poly.use_smooth = not f[3]
        for li, c in zip(poly.loop_indices, f[1]):
            uv.data[li].uv = c
    # consistent outward normals; UVs live on loops so flipping must carry them, bmesh does
    bm = bmesh.new(); bm.from_mesh(me)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    if hasattr(P, 'outward'):
        bm.faces.ensure_lookup_table()
        group = [bm.faces[i] for i in P.outward]
        for f in group: f.normal_update()
        # judged on the wide upper rings of the shell, which clearly face away from the axis
        top = group[:len(group) // 2]
        if sum(f.normal.dot(f.calc_center_median().to_2d().to_3d()) for f in top) < 0:
            bmesh.ops.reverse_faces(bm, faces=group)
    if P.name.startswith('SK_Hair'):        # soft planes with crisp spikes, like the sheet
        crease = 72 if P.name == 'SK_Hair_Bob' else 46   # the bob is all soft waves
        for f in bm.faces: f.smooth = True
        for e in bm.edges:
            if e.is_manifold and e.calc_face_angle(0) > math.radians(crease): e.smooth = False
    bm.to_mesh(me); bm.free()
    for mn in P.mats: me.materials.append(bpy.data.materials[mn])
    ob = bpy.data.objects.new(P.name, me)
    coll.objects.link(ob)
    ob.parent = arm
    groups = {}
    for i, w in enumerate(P.w):
        for b, val in (w or {}).items():
            g = groups.get(b) or ob.vertex_groups.new(name=b)
            groups[b] = g
            g.add([i], val, 'REPLACE')
    mod = ob.modifiers.new('Armature', 'ARMATURE'); mod.object = arm
    return ob

# ----------------------------------------------------------------- armature
def build_armature(coll):
    a = bpy.data.armatures.new('Player')
    ob = bpy.data.objects.new('RIG_Player', a)
    coll.objects.link(ob)
    wins = bpy.context.window_manager.windows
    scn = bpy.data.scenes[SCN]
    if wins: wins[0].scene = scn
    vl = scn.view_layers[0]
    vl.objects.active = ob
    ob.select_set(True, view_layer=vl)
    ov = dict(scene=scn, view_layer=vl, active_object=ob, object=ob, selected_objects=[ob])
    if wins: ov['window'] = wins[0]
    with bpy.context.temp_override(**ov):
        bpy.ops.object.mode_set(mode='EDIT')
        eb = a.edit_bones
        def B(name, h, t, parent=None, conn=False, deform=True, roll=None):
            b = eb.new(name)
            b.head = Vector(h) * MM; b.tail = Vector(t) * MM
            b.use_deform = deform
            if parent: b.parent = eb[parent]; b.use_connect = conn
            return b
        B('root', (0, 0, 0), (0, -120, 0), deform=False)
        B('hips', J['hips'], J['spine'], 'root')
        B('spine', J['spine'], J['chest'], 'hips', True)
        B('chest', J['chest'], J['neck'], 'spine', True)
        B('neck', J['neck'], J['head'], 'chest', True)
        B('head', J['head'], J['head_end'], 'neck', True)
        for s in (1, -1):
            m = lambda k: (J[k][0] * s, J[k][1], J[k][2])
            B(side('shoulder', s), m('clav'), m('shoulder'), 'chest')
            B(side('upperarm', s), m('shoulder'), m('elbow'), side('shoulder', s), True)
            B(side('forearm', s), m('elbow'), m('wrist'), side('upperarm', s), True)
            B(side('hand', s), m('wrist'), m('handtip'), side('forearm', s), True)
            B(side('thigh', s), m('thigh'), m('knee'), 'hips')
            B(side('shin', s), m('knee'), m('ankle'), side('thigh', s), True)
            B(side('foot', s), m('ankle'), m('ball'), side('shin', s), True)
            B(side('toe', s), m('ball'), m('toe'), side('foot', s), True)
            # animator controls, not exported
            an = m('ankle'); pole = (an[0], -320, 150)
            cf = B(side('ctrl_foot', s), an, m('ball'), 'root', deform=False)
            cf.roll = eb[side('foot', s)].roll
            B(side('ctrl_pole', s), pole, (pole[0], pole[1], pole[2] + 40), 'root', deform=False)
        bpy.ops.armature.calculate_roll(type='GLOBAL_POS_Y') if False else None
        bpy.ops.object.mode_set(mode='OBJECT')
    # IK on the legs; everything else stays FK
    for s in (1, -1):
        pb = ob.pose.bones[side('shin', s)]
        c = pb.constraints.new('IK')
        c.target = ob; c.subtarget = side('ctrl_foot', s)
        c.pole_target = ob; c.pole_subtarget = side('ctrl_pole', s)
        c.chain_count = 2
        th = ob.data.bones[side('thigh', s)]; sh = ob.data.bones[side('shin', s)]
        pole = ob.data.bones[side('ctrl_pole', s)].head_local
        pn = (sh.tail_local - th.head_local).cross(pole - th.head_local)
        proj = pn.cross(th.y_axis if False else (th.tail_local - th.head_local).normalized())
        xa = th.matrix_local.to_3x3().col[0]
        ang = xa.angle(proj)
        if xa.cross(proj).dot(th.tail_local - th.head_local) > 0: ang = -ang
        c.pole_angle = ang
        f = ob.pose.bones[side('foot', s)]
        c = f.constraints.new('COPY_ROTATION')
        c.target = ob; c.subtarget = side('ctrl_foot', s)
        # solved foot should keep the control's orientation relative to rest
        c.target_space = 'LOCAL_OWNER_ORIENT' if False else 'WORLD'; c.owner_space = 'WORLD'
    col = a.collections.new('Deform'); ctl = a.collections.new('Controls')
    for b in a.bones:
        (col if b.use_deform else ctl).assign(b)
    for pb in ob.pose.bones: pb.rotation_mode = 'QUATERNION'
    return ob

# ----------------------------------------------------------------- go
def main():
    wins = bpy.context.window_manager.windows
    if not wins and SCN not in bpy.data.scenes:   # headless, factory scene becomes ours
        bpy.context.scene.name = SCN
    scn = bpy.data.scenes.get(SCN)
    if scn:
        for o in list(scn.collection.all_objects):
            bpy.data.objects.remove(o, do_unlink=True)
        for c in list(scn.collection.children):
            if c.name != 'Character':
                bpy.data.collections.remove(c)
    else:
        scn = bpy.data.scenes.new(SCN)
    if wins: wins[0].scene = scn
    scn.render.fps = 30
    scn.unit_settings.system = 'METRIC'
    for me in [m for m in bpy.data.meshes if m.users == 0]: bpy.data.meshes.remove(me)
    for a in [a for a in bpy.data.armatures if a.users == 0]: bpy.data.armatures.remove(a)
    col = scn.collection.children.get('Character') or bpy.data.collections.new('Character')
    if col.name not in scn.collection.children: scn.collection.children.link(col)
    for m, t in MATS.items(): get_mat(m, t)
    arm = build_armature(col)
    stats = {}
    for fn in (build_body, build_hair, build_top, build_bottoms, build_socks, build_shoes, build_hair_bunches, build_hair_bob, build_skirt, build_dress, build_tights, build_mary_janes):
        P = fn(); ob = realize(P, col, arm)
        stats[ob.name] = sum(len(p.vertices) - 2 for p in ob.data.polygons)
    scn.view_layers[0].update()
    stats['default outfit'] = sum(v for k, v in stats.items() if k not in ALT_ITEMS)
    for n in ALT_ITEMS: bpy.data.objects[n].hide_set(True, view_layer=scn.view_layers[0])
    print(stats, 'height', max(v[2] for o in col.objects if o.type == 'MESH' for v in [ (o.matrix_world @ x.co) for x in o.data.vertices]))
main()
