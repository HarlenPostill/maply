#!/usr/bin/env python3
"""Generates every texture for the player character. Run: python3 tools/make_textures.py
All art is procedural so outfit/hair recolours are one-line edits."""
import os, numpy as np
from PIL import Image, ImageDraw, ImageFilter
OUT = os.path.join(os.path.dirname(__file__), '..', 'textures')
rng = np.random.default_rng(7)
def hexc(h): h=h.lstrip('#'); return np.array([int(h[i:i+2],16) for i in (0,2,4)],float)

def noise(h, w, beta=1.6, seed=0):
    """Tileable fractal noise in 0..1."""
    r=np.random.default_rng(seed); f=np.fft.fft2(r.normal(size=(h,w)))
    fy=np.fft.fftfreq(h)[:,None]*h; fx=np.fft.fftfreq(w)[None,:]*w
    k=np.sqrt(fx*fx+fy*fy); k[0,0]=1
    n=np.real(np.fft.ifft2(f/k**beta)); n=(n-n.mean())/n.std()
    return np.clip(n*0.22+0.5,0,1)

def save(arr,name,bits=None):
    Image.fromarray(np.clip(arr,0,255).astype('uint8')).save(os.path.join(OUT,name),optimize=True)

def felt(h,w,col,dark,seed,amt=1.0):
    """Soft felt / suede mottling, the look of the sheet's hair and shorts."""
    n=noise(h,w,1.3,seed)*.65+noise(h,w,.55,seed+1)*.35
    t=np.clip((n-.5)*amt*2.2+.5,0,1)[...,None]
    return col*(1-t)+dark*t

SK='#f6c497'; 
# ------------------------------------------------------------------ skin
skin=felt(128,128,hexc(SK),hexc('#f0b98b'),1,.5); save(skin,'T_Skin.png')

# ------------------------------------------------------------------ hair
hair=felt(512,512,hexc('#8a5639'),hexc('#744730'),2,.6); save(hair,'T_Hair.png')

# ------------------------------------------------------------------ bottoms
b=felt(512,512,hexc('#6f4a30'),hexc('#54361f'),3,.9)
# hem stitch across the bottom of the shorts (v=0 end -> image bottom)
b[-34:-28,:]=b[-34:-28,:]*.78
save(b,'T_Bottoms.png')

# ------------------------------------------------------------------ shoes & socks (shared sheet)
def shoes(name,c1,c2):
    S=512; sh=felt(S,S,hexc(c1),hexc(c2),4,.9)
    # bottom band of the sheet is the sole / sock area
    sole=felt(S,S,hexc('#f0e4d0'),hexc('#d9c6a9'),5,.5)
    sh[360:430]=sole[360:430]
    sock=felt(S,S,hexc('#fdf3e2'),hexc('#efdfc6'),6,.4)
    rib=(np.sin(np.linspace(0,2*np.pi*16,S))*.5+.5)[None,:,None]
    sock=sock*(.93+.07*rib)
    sh[440:]=sock[440:]
    # seam stitch on the upper, a lighter dashed line round the vamp
    sh[300:306,:]=sh[300:306,:]*1.22
    save(sh,name)
shoes('T_Shoes.png','#7a4c33','#573522')               # sheet brown, the default
for n,c1,c2 in (('Terracotta','#bb6540','#924a2a'),('Olive','#7b8049','#5a6033'),
                ('Mustard','#cf9f42','#a67c2c'),('Blue','#73899a','#54697a')):
    shoes(f'T_Shoes_{n}.png',c1,c2)

# ------------------------------------------------------------------ top (vest + sleeves + collar)
T=1024
# row ranges in image space (top = 0). The builder maps its UVs to these too.
R_TORSO=(0,640); R_COLLAR=(660,780); R_SLEEVE=(800,1024)
Z_TOP,Z_BOT=490.,262.            # mm covered by the torso band
def z2row(z): return R_TORSO[0]+(Z_TOP-z)/(Z_TOP-Z_BOT)*(R_TORSO[1]-R_TORSO[0])
def r_sheet(z): return z2row(z)
def argyle(top):
    # hand drawn pattern at 4x, then downsampled so edges stay soft, like the sheet
    SS=4; mask=Image.new('L',(T*SS,640*SS),0); d=ImageDraw.Draw(mask)
    REPEATS=10; cell=T*SS/REPEATS
    def r(z): return z2row(z)*SS
    zc=392.; hz=29.; hw=cell*.46
    for i in range(-1,REPEATS+1):
        cx=(i+.5)*cell            # u=.5 (front centre) lands mid-diamond when repeats is even? see below
        cx=i*cell
        d.polygon([(cx-hw,r(zc)),(cx,r(zc+hz)),(cx+hw,r(zc)),(cx,r(zc-hz))],fill=255)
    for zrow in (440.,342.):
        n=REPEATS*3; dash=T*SS/n
        for i in range(n+1):
            x=i*dash
            d.rounded_rectangle([x+dash*.14,r(zrow+4.5),x+dash*.86,r(zrow-4.5)],radius=int(r(0)-r(3)),fill=255)
    m=mask.resize((T,640),Image.LANCZOS).filter(ImageFilter.GaussianBlur(.7))
    m=np.array(m,float)[...,None]/255
    wool=felt(640,T,hexc('#dfbf95'),hexc('#c9a67c'),8,.8)
    top[:640]=top[:640]*(1-m)+wool*m

def daisies(top):
    """Two staggered rows of daisies. The band is stretched (1.42 px/mm across, 2.8 px/mm down),
    so everything is drawn in mm and mapped per axis to stay round on the body."""
    SS=4; W,H=T*SS,640*SS
    px=T*SS/720.; pz=lambda z:r_sheet(z)*SS
    white=Image.new('L',(W,H),0); gold=Image.new('L',(W,H),0)
    dw,dg=ImageDraw.Draw(white),ImageDraw.Draw(gold)
    def blob(d,cx,cz,rx,rz,rot):
        pts=[]
        for k in range(20):
            t=k/20*2*np.pi; x,y=np.cos(t)*rx,np.sin(t)*rz
            c,s_=np.cos(rot),np.sin(rot)
            mx,mz=x*c-y*s_,x*s_+y*c
            pts.append(((cx+mx)*px,pz(cz+mz)))
        d.polygon(pts,fill=255)
    def flower(cx,cz,sz):
        for k in range(6):
            a=k/6*2*np.pi
            for wrap in (-720,0,720):
                blob(dw,cx+wrap+np.cos(a)*sz*.62,cz+np.sin(a)*sz*.62,sz*.46,sz*.30,a)
        for wrap in (-720,0,720): blob(dg,cx+wrap,cz,sz*.30,sz*.30,0)
    N=6; step=720/N
    for i in range(N):                       # u=.5 (front centre) gets a flower on the upper row
        flower(360+i*step,428,24)
        flower(360+(i+.5)*step,352,24)
    for m,col in ((white,('#fbf1dd','#ecdcc0')),(gold,('#e0ad45','#c8922f'))):
        a=np.array(m.resize((T,640),Image.LANCZOS).filter(ImageFilter.GaussianBlur(.7)),float)[...,None]/255
        top[:640]=top[:640]*(1-a)+felt(640,T,hexc(col[0]),hexc(col[1]),8,.6)*a

def make_top(name,c1,c2,pattern):
    top=np.zeros((T,T,3))
    top[:]=felt(T,T,hexc(c1),hexc(c2),7,1.1)
    # knit: very fine horizontal rows on the vest
    row=(np.sin(np.arange(T)*2*np.pi/7)*.5+.5)[:,None,None]; top*=.965+.035*row
    pattern(top)
    # hem band: fleck cream knit with vertical rib
    hem_row=int(z2row(306))
    hem=felt(640,T,hexc('#f1e0c2'),hexc('#c79f73'),9,1.9)
    fleck=(noise(640,T,.15,10)>.56)[...,None]
    hem=hem*(1-fleck*np.array([.14,.2,.28]))
    ribs=(np.sin(np.arange(T)*2*np.pi*90/T)*.5+.5)[None,:,None]
    hem=hem*(.9+.1*ribs)
    top[hem_row:640]=hem[hem_row:640]
    top[hem_row-3:hem_row+1]*=.72   # shadow where vest meets the hem
    top[640:]=top[639]
    # collar
    def ribbed(h,w,count,seed,c1='#f3e1c3',c2='#dcc199',depth=.13):
        f=felt(h,w,hexc(c1),hexc(c2),seed,.7)
        rb=(np.sin(np.arange(w)*2*np.pi*count/w)*.5+.5)[None,:,None]
        return f*(1-depth+depth*rb)
    top[R_COLLAR[0]-8:R_COLLAR[1]+8]=ribbed(R_COLLAR[1]-R_COLLAR[0]+16,T,40,11)
    # sleeves: chunky rib, stitched row near the cuff
    sl=ribbed(T-R_SLEEVE[0]+8,T,15,12,'#f4dfbd','#d8b98f',.2)
    top[R_SLEEVE[0]-8:]=sl
    save(top,name)


make_top('T_Top.png','#7c7443','#5c5730',argyle)
make_top('T_Top_Daisy.png','#c9774d','#a95e3a',daisies)

# ------------------------------------------------------------------ skirt: tan plaid, pleats are
# the geometry's job, this only darkens the folds so they read from a distance
Sk=512
sk=felt(Sk,Sk,hexc('#bd9560'),hexc('#a37c49'),13,.7)
line=np.zeros((Sk,Sk))
for k in range(0,Sk,64): line[:,k:k+7]=1; line[k+18:k+25,:]=1
sk=sk*(1-line[...,None]*.20)+hexc('#6b4730')*line[...,None]*.20
thin=np.zeros((Sk,Sk))
for k in range(32,Sk,64): thin[:,k:k+3]=1; thin[k+18:k+21,:]=1
sk=sk*(1-thin[...,None]*.35)+hexc('#f3e4c8')*thin[...,None]*.35
fold=(np.cos(np.arange(Sk)*2*np.pi*12/Sk)*.5+.5)[None,:,None]
sk=sk*(.84+.16*fold)
sk[-30:-24]=sk[-30:-24]*.72                    # hem stitch
save(sk,'T_Skirt.png')
save(felt(64,64,hexc('#d67f52'),hexc('#b9663c'),14,.5),'T_HairTie.png')

# ------------------------------------------------------------------ flower clip: left cream, right gold
fl=felt(128,128,hexc('#fbf0da'),hexc('#ecdabb'),15,.6)
fl[:,64:]=felt(128,64,hexc('#e3b04a'),hexc('#c8922f'),16,.7)
save(fl,'T_Flower.png')

# ------------------------------------------------------------------ pinafore dress
def make_dress():
    Z0,Z1=494.,184.
    row=lambda z:(Z0-z)/(Z0-Z1)*640
    D=felt(T,T,hexc('#7a6c3f'),hexc('#5c5230'),17,1.1)
    D*=(.965+.035*(np.sin(np.arange(T)*2*np.pi/7)*.5+.5)[:,None,None])
    SS=4; m=Image.new('L',(T*SS,640*SS),0); d=ImageDraw.Draw(m); R=lambda z:row(z)*SS
    def diamonds(z,hz,hwf,count):
        c=T*SS/count
        for i in range(count+1):
            cx=i*c
            d.polygon([(cx-c*hwf,R(z)),(cx,R(z+hz)),(cx+c*hwf,R(z)),(cx,R(z-hz))],fill=255)
    def dots(z,r,count,off=.5):
        c=T*SS/count
        for i in range(count+1):
            cx=(i+off)*c
            d.ellipse([cx-c*.07,R(z+r),cx+c*.07,R(z-r)],fill=255)
    diamonds(392,25,.30,10); dots(392,6,10)
    for i in range(21):
        x=i*T*SS/20; d.rounded_rectangle([x+T*SS/20*.2,R(442),x+T*SS/20*.8,R(432)],radius=8,fill=255)
    diamonds(240,15,.26,16); dots(240,5,16)
    a=np.array(m.resize((T,640),Image.LANCZOS).filter(ImageFilter.GaussianBlur(.7)),float)[...,None]/255
    D[:640]=D[:640]*(1-a)+felt(640,T,hexc('#d9ba8c'),hexc('#bf9d70'),8,.8)*a
    # fleck cream band, then the dark trim at the very hem
    band0,band1,hem=int(row(222)),int(row(203)),640
    fleck=felt(640,T,hexc('#efdfc2'),hexc('#c7a077'),9,1.8)
    D[band0:band1]=fleck[band0:band1]
    D[band1:hem]=felt(640,T,hexc('#5b5130'),hexc('#463e22'),18,.8)[band1:hem]
    D[640:]=D[639]
    def ribbed(h,w,count,seed,c1,c2,depth):
        f=felt(h,w,hexc(c1),hexc(c2),seed,.7)
        return f*(1-depth+depth*(np.sin(np.arange(w)*2*np.pi*count/w)*.5+.5)[None,:,None])
    D[652:788]=ribbed(136,T,36,11,'#f3e1c3','#dcc199',.14)
    D[792:]=ribbed(T-792,T,18,12,'#f6e2c2','#dcc097',.2)
    save(D,'T_Dress.png')
make_dress()

# ------------------------------------------------------------------ face atlas, 2x2 cells of 512
FACE=512; SS=4
INK='#4a2d20'
# The cell covers x -235..235 mm, z 500..970 mm
def F(x,z): return ((x+235)/470*FACE*SS,(970-z)/470*FACE*SS)
def L(mm): return mm/470*FACE*SS
def base(seed=20):
    f=Image.new('RGB',(FACE*SS,FACE*SS),SK); return f
def cheeks(d,c='#ec9f7c'):
    pass
def paint(fn):
    im=base(); 
    # cheeks, soft
    ch=Image.new('L',im.size,0); dc=ImageDraw.Draw(ch)
    for sx in(-1,1):
        cx,cz=F(sx*156,640); dc.ellipse([cx-L(33),cz-L(25),cx+L(33),cz+L(25)],fill=215)
    ch=ch.filter(ImageFilter.GaussianBlur(L(6)))
    im.paste(Image.new('RGB',im.size,'#e5926e'),mask=ch)
    d=ImageDraw.Draw(im); fn(im,d)
    return im.resize((FACE,FACE),Image.LANCZOS).filter(ImageFilter.GaussianBlur(.5))
def nose(d):
    a=F(0,728); b=F(-24,668); c=F(24,668)
    d.polygon([F(-24,664),F(24,664),F(0,658)],fill='#c9754e')
    d.polygon([a,b,c],fill='#dd5a22')
def arc(d,cx,cz,rx,rz,a0,a1,w,fill=INK):
    x,y=F(cx,cz); box=[x-L(rx),y-L(rz),x+L(rx),y+L(rz)]
    d.arc(box,a0,a1,fill=fill,width=int(L(w)))
    import math
    for a in(a0,a1):
        px=x+L(rx-w/2)*math.cos(math.radians(a)); py=y+L(rz-w/2)*math.sin(math.radians(a))
        d.ellipse([px-L(w/2),py-L(w/2),px+L(w/2),py+L(w/2)],fill=fill)
def eye(d,cx,pupil=1.0,cz=742):
    # sclera, then pupil pushed right, thin ink line hugging the white on the left - as on the sheet
    rx,rz=50,64
    x,y=F(cx,cz)
    d.ellipse([x-L(rx+3),y-L(rz+3),x+L(rx+3),y+L(rz+3)],fill=SK)
    d.ellipse([x-L(rx),y-L(rz),x+L(rx),y+L(rz)],fill=INK)
    d.ellipse([x-L(rx-5),y-L(rz-5),x+L(rx-5),y+L(rz-5)],fill='#fffdf8')
    px,pz=F(cx+12,cz-1)
    d.ellipse([px-L(40*pupil),pz-L(60*pupil),px+L(40*pupil),pz+L(60*pupil)],fill=INK)
    # the ink rim only survives on the left, as on the sheet: trim the white so the pupil meets skin
    return
def smile(d):
    arc(d,0,650,56,36,28,152,6.5)
def neutral(im,d):
    eye(d,-112); eye(d,112); nose(d); smile(d)
def blink(im,d):
    for cx in(-112,112): arc(d,cx,770,52,48,28,152,8)
    nose(d); smile(d)
def happy(im,d):
    for cx in(-112,112): arc(d,cx,725,50,46,205,335,8)
    nose(d)
    # open mouth with tongue
    x,y=F(0,640)
    mask=Image.new('L',im.size,0); dm=ImageDraw.Draw(mask)
    dm.pieslice([x-L(52),y-L(62),x+L(52),y+L(62)],0,180,fill=255)
    fill=Image.new('RGB',im.size,'#a8412f'); tx,ty=F(0,592)
    ImageDraw.Draw(fill).ellipse([tx-L(34),ty-L(22),tx+L(34),ty+L(22)],fill='#ec8f7f')
    im.paste(fill,mask=mask)
    d.line([F(-52,640),F(52,640)],fill=INK,width=int(L(6)))
    arcb=[x-L(52),y-L(62),x+L(52),y+L(62)]; d.arc(arcb,0,180,fill=INK,width=int(L(6)))
def surprised(im,d):
    eye(d,-112,.82); eye(d,112,.82); nose(d)
    x,y=F(0,622); d.ellipse([x-L(23),y-L(29),x+L(23),y+L(29)],fill=INK)
    d.ellipse([x-L(18),y-L(24),x+L(18),y+L(24)],fill='#b34a38')
    tx,ty=F(0,610); d.ellipse([tx-L(12),ty-L(10),tx+L(12),ty+L(10)],fill='#e88f7e')
atlas=Image.new('RGB',(FACE*2,FACE*2),SK)
for (i,j),fn in {(0,0):neutral,(1,0):blink,(0,1):happy,(1,1):surprised}.items():
    atlas.paste(paint(fn),(i*FACE,j*FACE))
atlas.save(os.path.join(OUT,'T_Face.png'),optimize=True)
print('ok',sorted(os.listdir(OUT)))
