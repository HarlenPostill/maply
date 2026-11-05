"""Authors the clips on RIG_Player. Legs are driven by the ctrl_foot IK controls, keys stay editable.
Walk is in place; ground speed in metres/second is stored on the rig as custom prop walk_speed."""
import bpy, math
from mathutils import Quaternion, Vector
ob = bpy.data.objects['RIG_Player']
PB = ob.pose.bones
MM = .001
AX = Vector((1, 0, 0)); AY = Vector((0, 1, 0)); AZ = Vector((0, 0, 1))
Rx = lambda d: Quaternion(AX, math.radians(d))
Ry = lambda d: Quaternion(AY, math.radians(d))
Rz = lambda d: Quaternion(AZ, math.radians(d))
c2 = lambda p: math.cos(math.tau * p)
s2 = lambda p: math.sin(math.tau * p)
def ease(a, b, x):
    t = max(0, min(1, (x - a) / (b - a))); return t * t * (3 - 2 * t)
def lerp(a, b, t): return a + (b - a) * t

def Q(bone, qw):
    """world-axis rotation -> pose-space quaternion for this bone (parents assumed near rest)"""
    r = PB[bone].bone.matrix_local.to_3x3().to_quaternion()
    return r.inverted() @ qw @ r
def L(bone, v):
    return PB[bone].bone.matrix_local.to_3x3().inverted() @ Vector(v)

ROT_BONES = ['hips', 'spine', 'chest', 'neck', 'head'] + [n + s for n in ('shoulder', 'upperarm', 'forearm', 'hand', 'toe', 'ctrl_foot') for s in ('.L', '.R')]
LOC_BONES = ['hips', 'ctrl_foot.L', 'ctrl_foot.R']

def clear():
    for pb in PB:
        pb.rotation_quaternion = (1, 0, 0, 0); pb.location = (0, 0, 0); pb.scale = (1, 1, 1)

def key(f, pose):
    """pose: dict bone -> dict(q=world quat, t=world mm offset)"""
    for b in ROT_BONES:
        d = pose.get(b, {})
        PB[b].rotation_quaternion = Q(b, d.get('q', Quaternion()))
        PB[b].keyframe_insert('rotation_quaternion', frame=f, group=b)
    for b in LOC_BONES:
        d = pose.get(b, {})
        PB[b].location = L(b, Vector(d.get('t', (0, 0, 0))) * MM)
        PB[b].keyframe_insert('location', frame=f, group=b)

def arms(p, swingL, swingR, bendL, bendR, lower=7):
    """swing: + = hand back. Keeps the sheet's A-pose, arms clear the wide vest."""
    p['upperarm.L'] = dict(q=Rx(swingL) @ Ry(lower))
    p['upperarm.R'] = dict(q=Rx(swingR) @ Ry(-lower))
    p['forearm.L'] = dict(q=Rx(-bendL))
    p['forearm.R'] = dict(q=Rx(-bendR))

# ------------------------------------------------------------ walk
PERIOD = 20                # frames @30fps
STANCE = .58
S = 60.                    # mm, half stride
LIFT = 38.
HEEL = (73., -62.); BALL = (-64., -62.)   # relative to ankle: (y, z)
RY_ = lambda v, deg: (v[0] * math.cos(math.radians(deg)) - v[1] * math.sin(math.radians(deg)),
                      v[0] * math.sin(math.radians(deg)) + v[1] * math.cos(math.radians(deg)))
def foot(p):
    p %= 1
    if p < STANCE:
        k = p / STANCE
        yf = lerp(-S, S, k); lift = 0
        pitch = lerp(9, 0, ease(0, .17, k)) if k < .17 else lerp(0, -27, ease(.70, 1, k))
    else:
        k = (p - STANCE) / (1 - STANCE)
        yf = lerp(S, -S, ease(0, 1, k))
        lift = math.sin(math.pi * k) ** 1.5 * LIFT
        pitch = lerp(-27, -6, ease(0, .4, k)) if k < .4 else lerp(-6, 9, ease(.4, 1, k))
    # roll about the heel (toe up) or the ball (heel up), that is what stops the foot skating
    pivot = HEEL if pitch > 0 else BALL
    ry, rz = RY_(pivot, -pitch)
    ay = yf + pivot[0] - ry
    az = lift - rz
    return (ay - 0, az - 62.), pitch

def walk_pose(p):
    P = {}
    bob = -8 - 5 * math.cos(2 * math.tau * (p - .07))
    P['hips'] = dict(t=(9 * s2(p), 0, bob), q=Rz(-7 * c2(p)) @ Ry(-3 * s2(p)))
    P['spine'] = dict(q=Rz(5 * c2(p)) @ Rx(3.5) @ Ry(1.6 * s2(p)))
    P['chest'] = dict(q=Rz(6 * c2(p)) @ Ry(1.6 * s2(p)))
    P['neck'] = dict(q=Rz(-2.5 * c2(p)))
    P['head'] = dict(q=Rz(-2 * c2(p)) @ Rx(-1.8 + 1.6 * math.cos(2 * math.tau * (p - .12))) @ Ry(-1.8 * s2(p - .08)))
    for s, off in (('.L', 0), ('.R', .5)):
        (y, z), pitch = foot(p + off)
        P['ctrl_foot' + s] = dict(t=(0, y, z), q=Rx(-pitch))
        P['toe' + s] = dict(q=Rx(min(0, pitch) * -1 * -1))
    d = .045                 # arms drag a touch behind the hips
    sL = 24 * c2(p - d); sR = -sL
    arms(P, sL, sR, 16 + 11 * max(0, -c2(p - d)), 16 + 11 * max(0, c2(p - d)))
    P['shoulder.L'] = dict(q=Rx(3 * c2(p)))
    P['shoulder.R'] = dict(q=Rx(-3 * c2(p)))
    P['hand.L'] = dict(q=Rx(-5 * c2(p - .1)))
    P['hand.R'] = dict(q=Rx(5 * c2(p - .1)))
    return P

# ------------------------------------------------------------ idles
def idle_base(p):
    """p = 0..1 over 3 seconds. Breathing, slow weight shift, moving hold."""
    P = {}
    b = c2(p)                # breath
    sh = s2(p)               # weight shift
    P['hips'] = dict(t=(4 * sh, 0, -10 - 1.8 * b), q=Ry(-.8 * sh))
    P['spine'] = dict(q=Rx(.8 - .9 * b) @ Ry(.5 * sh))
    P['chest'] = dict(q=Rx(-1.2 * b) @ Ry(.4 * sh))
    P['neck'] = dict(q=Rx(.7 * b))
    P['head'] = dict(q=Rx(.9 * s2(p + .15)) @ Ry(1.1 * s2(p + .3)))
    arms(P, 1.8 * s2(p + .1), -1.2 * s2(p + .2), 9 + 1.5 * b, 9 - 1.5 * b, lower=7 + 1.0 * b)
    P['shoulder.L'] = dict(q=Ry(1.3 * b))
    P['shoulder.R'] = dict(q=Ry(-1.3 * b))
    return P
def idle_pose(p): return idle_base(p)

def track(t, keys):
    """keys: [(time, value)...] smooth between."""
    if t <= keys[0][0]: return keys[0][1]
    for (a, va), (b, vb) in zip(keys, keys[1:]):
        if t <= b: return lerp(va, vb, ease(a, b, t))
    return keys[-1][1]

LOOK = 150
def look_pose(f):
    P = idle_base((f / LOOK * 2) % 1)
    t = f
    yaw = track(t, [(0, 0), (18, 0), (34, -36), (66, -36), (84, 32), (116, 32), (134, 0), (150, 0)])
    tilt = track(t, [(0, 0), (34, 3), (66, 2), (84, -3), (116, -2), (134, 0)])
    nod = track(t, [(0, 0), (34, -3), (66, -1), (84, 4), (116, 3), (134, 0)])
    P['head']['q'] = Rz(yaw * .68) @ Ry(tilt) @ Rx(nod) @ P['head']['q']
    P['neck']['q'] = Rz(yaw * .22) @ P['neck']['q']
    P['chest']['q'] = Rz(yaw * .10) @ P['chest']['q']
    return P

WAVE = 120
def wave_pose(f):
    P = idle_base((f / WAVE) % 1)
    up = track(f, [(0, 0), (16, 1), (98, 1), (118, 0), (120, 0)])
    w = math.sin((f - 16) / 82 * math.tau * 3.5) * ease(14, 22, f) * (1 - ease(90, 98, f))
    # character's right hand: forward and out, forearm up. Going forward keeps the hand in front of the
    # shoulder-length hair styles, where a sideways wave would pass through them.
    rest = (PB['upperarm.R'].bone.tail_local - PB['upperarm.R'].bone.head_local).normalized()
    fore = (PB['forearm.R'].bone.tail_local - PB['forearm.R'].bone.head_local).normalized()
    low = Ry(-7)
    aim = Vector((-.72, -.60, -.10)).normalized()
    full = rest.rotation_difference(aim)
    qu = low.slerp(full, up)
    F = (Vector((-.55, -.40, .73)) + Vector((-.45, .12, 0)) * w).normalized()
    qf = fore.rotation_difference(full.inverted() @ F)
    P['upperarm.R'] = dict(q=qu)
    P['forearm.R'] = dict(q=Rx(-9).slerp(qf, up))
    P['hand.R'] = dict(q=Ry(lerp(0, 14 * w, up)))
    P['shoulder.R'] = dict(q=Ry(lerp(0, 8, up)))
    P['spine']['q'] = Ry(lerp(0, 2.5, up)) @ P['spine']['q']
    P['chest']['q'] = Ry(lerp(0, 2.5, up)) @ P['chest']['q']
    P['head']['q'] = Ry(lerp(0, -5, up) + w * 1.2) @ Rz(lerp(0, -6, up)) @ P['head']['q']
    P['hips']['t'] = (P['hips']['t'][0] + lerp(0, 6, up), 0, P['hips']['t'][2] + 2.5 * abs(w) * up)
    return P

def make(name, length, fn, step, loop=True):
    a = bpy.data.actions.get(name)
    if a: bpy.data.actions.remove(a)
    clear()
    ob.animation_data_create()
    ob.animation_data.action = None
    a = bpy.data.actions.new(name); a.use_fake_user = True
    ob.animation_data.action = a
    frames = list(range(0, length + 1, step))
    if frames[-1] != length: frames.append(length)
    for f in frames: key(f, fn(f))
    if loop:   # pad past both ends so the auto handles at the loop point stay continuous
        key(-step, fn(length - step)); key(length + step, fn(step))
    a.use_frame_range = True; a.frame_start = 0; a.frame_end = length; a.use_cyclic = False
    return a

def main():
    acts = [make('Walk', PERIOD, lambda f: walk_pose(f / PERIOD), 1),
            make('Idle', 90, lambda f: idle_pose(f / 90), 6),
            make('Idle_Look', LOOK, look_pose, 6),
            make('Idle_Wave', WAVE, wave_pose, 3)]
    ob.animation_data.action = None
    for t in list(ob.animation_data.nla_tracks): ob.animation_data.nla_tracks.remove(t)
    for a in acts:
        t = ob.animation_data.nla_tracks.new(); t.name = 'CLIP_' + a.name; t.mute = True
        st = t.strips.new(a.name, 0, a)
    ob.animation_data.action = acts[0]
    bpy.context.scene.frame_start, bpy.context.scene.frame_end = 0, PERIOD
    ob['walk_speed'] = (2 * S * MM) / (STANCE * PERIOD / 30)
    print('walk speed m/s', ob['walk_speed'], [a.name for a in bpy.data.actions])
main()
