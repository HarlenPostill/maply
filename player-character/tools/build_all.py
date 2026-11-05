"""One command rebuild:
   /Applications/Blender.app/Contents/MacOS/Blender -b --factory-startup -P tools/build_all.py"""
import bpy
# factory-startup content must not reach the export
for o in list(bpy.data.objects): bpy.data.objects.remove(o, do_unlink=True)
for c in list(bpy.data.collections): bpy.data.collections.remove(c)
T = "/Users/harlenpostill/.t3/projects/maply/player-character/tools/"
for f in ('build_character.py', 'animate.py', 'export_glb.py'):
    exec(compile(open(T + f).read(), T + f, 'exec'), {'__name__': '__main__'})
