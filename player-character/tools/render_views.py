"""Orthographic turnaround matching the model sheet, for side-by-side review."""
import bpy, math, os
from mathutils import Vector
ROOT = "/Users/harlenpostill/.t3/projects/maply/player-character"
scn = bpy.data.scenes['Player']
def ensure(name, data):
    o = bpy.data.objects.get(name)
    if o: return o
    o = bpy.data.objects.new(name, data); scn.collection.objects.link(o); return o
cam = ensure('ReviewCam', bpy.data.cameras.new('ReviewCam'))
cam.data.type = 'ORTHO'; cam.data.ortho_scale = 1.08
sun = ensure('ReviewSun', bpy.data.lights.new('ReviewSun', 'SUN'))
sun.data.energy = 1.35; sun.data.angle = math.radians(25)
sun.rotation_euler = (math.radians(55), 0, math.radians(-25))
w = bpy.data.worlds.get('Review') or bpy.data.worlds.new('Review')
w.use_nodes = True
bg = next(n for n in w.node_tree.nodes if n.type == 'BACKGROUND')
bg.inputs[0].default_value = (0.96, 0.92, 0.85, 1); bg.inputs[1].default_value = .72
scn.world = w
scn.camera = cam
scn.render.engine = 'BLENDER_EEVEE'
scn.render.resolution_x, scn.render.resolution_y = 560, 640
scn.view_settings.view_transform = 'Standard'; scn.view_settings.look = 'None'
scn.render.film_transparent = True
out = '%s/renders' % ROOT
tag = globals().get('TAG', 'view')
views = dict(front=0, q34=-35, side=90, back=180) 
for name, az in views.items():
    a = math.radians(az)
    d = Vector((math.sin(a), -math.cos(a), 0))
    cam.location = d * 3 + Vector((0, 0, .5))
    cam.rotation_euler = (math.pi / 2, 0, a)
    scn.render.filepath = f'{out}/{tag}_{name}.png'
    bpy.ops.render.render(write_still=True, scene=scn.name)
print('rendered')
