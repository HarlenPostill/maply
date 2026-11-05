import bpy, math
from mathutils import Vector
ROOT = "/Users/harlenpostill/.t3/projects/maply/player-character"
scn = bpy.data.scenes['Player']; ob = bpy.data.objects['RIG_Player']
CLIP = globals().get('CLIP', 'Walk'); FRAMES = globals().get('FRAMES', list(range(0, 20, 2))); AZ = globals().get('AZ', 90)
scn.render.resolution_x, scn.render.resolution_y = 300, 340
ob.animation_data.action = bpy.data.actions[CLIP]
try: ob.animation_data.action_slot = bpy.data.actions[CLIP].slots[0]
except Exception as e: print(e)
cam = bpy.data.objects['ReviewCam']
a = math.radians(AZ); d = Vector((math.sin(a), -math.cos(a), 0))
cam.location = d * 3 + Vector((0, 0, .5)); cam.rotation_euler = (math.pi / 2, 0, a)
for f in FRAMES:
    scn.frame_set(f)
    scn.render.filepath = f'{ROOT}/renders/clip_{CLIP}_{AZ}_{f:03d}.png'
    bpy.ops.render.render(write_still=True, scene=scn.name)
print('ok')
