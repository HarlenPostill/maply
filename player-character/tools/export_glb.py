"""Exports player.glb (deform skeleton only, IK baked per frame) and saves player.blend."""
import bpy, json, os
ROOT = "/Users/harlenpostill/.t3/projects/maply/player-character"
scn = bpy.data.scenes['Player']
vl = scn.view_layers[0]
for o in vl.objects: o.select_set(False, view_layer=vl)
ALT = ['SK_Hair_Bunches', 'SK_Hair_Bob', 'SK_Skirt', 'SK_Dress', 'SK_Tights', 'SK_Shoes_MaryJane']
names = ['RIG_Player', 'SK_Body', 'SK_Hair', 'SK_Top', 'SK_Bottoms', 'SK_Socks', 'SK_Shoes'] + ALT
for n in ALT: bpy.data.objects[n].hide_set(False, view_layer=vl)
for n in names: bpy.data.objects[n].select_set(True, view_layer=vl)
vl.objects.active = bpy.data.objects['RIG_Player']
os.makedirs(ROOT + '/export', exist_ok=True)
# Plain call on purpose: wrapping this in temp_override crashes Blender 5.2.2 during sampling.
r = bpy.ops.export_scene.gltf(filepath=ROOT + '/export/player.glb', export_format='GLB', use_selection=True,
    export_animations=True, export_animation_mode='ACTIONS', export_force_sampling=True, export_frame_step=1,
    export_def_bones=True, export_skins=True, export_morph=False, export_yup=True,
    export_texcoords=True, export_normals=True, export_materials='EXPORT', export_image_format='AUTO',
    export_all_influences=False, export_optimize_animation_size=False)
for n in ALT: bpy.data.objects[n].hide_set(True, view_layer=vl)
print(r, os.path.getsize(ROOT + '/export/player.glb'))
bpy.ops.wm.save_as_mainfile(filepath=ROOT + '/player.blend')
print(bpy.data.filepath)
