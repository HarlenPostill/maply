#!/usr/bin/env python3
"""Reads player.glb and checks structure + baked animation against values dumped from Blender."""
import json, struct, sys, numpy as np
P='/Users/harlenpostill/.t3/projects/maply/player-character/export/player.glb'
d=open(P,'rb').read(); jl=struct.unpack('<I',d[12:16])[0]; G=json.loads(d[20:20+jl]); B=d[28+jl:]
def acc(i):
    a=G['accessors'][i]; v=G['bufferViews'][a['bufferView']]
    n={'SCALAR':1,'VEC2':2,'VEC3':3,'VEC4':4,'MAT4':16}[a['type']]; t={5126:'<f4',5123:'<u2',5125:'<u4',5121:'u1'}[a['componentType']]
    o=v.get('byteOffset',0)+a.get('byteOffset',0)
    return np.frombuffer(B,t,a['count']*n,o).reshape(a['count'],n) if n>1 else np.frombuffer(B,t,a['count'],o)
S=G['skins'][0]; names=[G['nodes'][j]['name'] for j in S['joints']]
print('joints',len(names),names)
print('skins',len(G['skins']),'meshes',[(m['name'],len(m['primitives'])) for m in G['meshes']])
print('materials',[m['name'] for m in G['materials']],'images',len(G['images']))
tri=sum(G['accessors'][p['indices']]['count']//3 for m in G['meshes'] for p in m['primitives']); vc=sum(G['accessors'][p['attributes']['POSITION']]['count'] for m in G['meshes'] for p in m['primitives'])
print('tris',tri,'gpu verts',vc)
for a in G['animations']:
    t=acc(a['samplers'][0]['input']); print(a['name'],'channels',len(a['channels']),'t',round(float(t[0]),3),round(float(t[-1]),3),'keys',len(t))
# global joint matrices at a frame
idx={j:i for i,j in enumerate(S['joints'])}
parent={c:i for i,n in enumerate(G['nodes']) for c in n.get('children',[])}
def qm(q):
    x,y,z,w=q; return np.array([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],[2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],[2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])
def local(n,ov):
    N=G['nodes'][n]; T=np.array(ov.get((n,'translation'),N.get('translation',[0,0,0])));R=np.array(ov.get((n,'rotation'),N.get('rotation',[0,0,0,1])));Sc=np.array(ov.get((n,'scale'),N.get('scale',[1,1,1])))
    m=np.eye(4);m[:3,:3]=qm(R)*Sc;m[:3,3]=T;return m
def glob(n,ov):
    m=local(n,ov)
    while n in parent: n=parent[n]; m=local(n,ov)@m
    return m
def pose(clip,k):
    a=next(x for x in G['animations'] if x['name']==clip); ov={}
    for c in a['channels']:
        s=a['samplers'][c['sampler']]; ov[(c['target']['node'],c['target']['path'])]=acc(s['output'])[k]
    return ov
ref=json.load(open(sys.argv[1])) if len(sys.argv)>1 else None
if ref:
    worst=0
    for clip,frames in ref.items():
        for f,bones in frames.items():
            ov=pose(clip,int(f))
            for b,h in bones.items():
                j=next(i for i,n in enumerate(G['nodes']) if n.get('name')==b)
                p=glob(j,ov)[:3,3]; e=np.linalg.norm(p-np.array(h)); worst=max(worst,e)
    print('worst joint error vs Blender mm',round(worst*1000,3))
    ifoot=[]
    a='Walk'; 
    n=next(i for i,x in enumerate(G['nodes']) if x.get('name')=='toe.L')
