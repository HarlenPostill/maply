"""Review renders of a wardrobe combination. Set OUTFIT = dict(hair=, top=, shoes=, skirt=) before exec."""
import bpy
ROOT = "/Users/harlenpostill/.t3/projects/maply/player-character"
O = globals().get('OUTFIT', {})
scn = bpy.data.scenes['Player']; vl = scn.view_layers[0]
def show(n, on): 
    ob = bpy.data.objects[n]; ob.hide_set(not on, view_layer=vl); ob.hide_render = not on
hair = O.get('hair', 'SK_Hair'); dress = O.get('dress', False); skirt = O.get('skirt', False); mj = O.get('mj', False)
for n in ('SK_Hair', 'SK_Hair_Bunches', 'SK_Hair_Bob'): show(n, n == hair)
show('SK_Bottoms', not skirt and not dress); show('SK_Skirt', skirt and not dress)
show('SK_Top', not dress); show('SK_Dress', dress); show('SK_Socks', not dress); show('SK_Tights', dress)
show('SK_Shoes', not mj); show('SK_Shoes_MaryJane', mj)
def tex(mat, file):
    n = next(n for n in bpy.data.materials[mat].node_tree.nodes if n.type == 'TEX_IMAGE')
    img = bpy.data.images.get(file) or bpy.data.images.load(f'{ROOT}/textures/{file}')
    n.image = img
tex('M_Top', O.get('top', 'T_Top.png')); tex('M_Shoes', O.get('shoes', 'T_Shoes.png'))
