#include <metal_stdlib>
using namespace metal;

struct Vertex {               // 56 bytes, matches Vertex in GLB.swift
    packed_float3 position;
    packed_float3 normal;
    packed_float2 uv;
    packed_ushort4 joints;
    packed_float4 weights;
};

struct GroundVertex {         // 16 bytes, matches TileMesher.swift
    packed_float2 p;
    float kind;
    float u;
};

struct PropVertex {           // 20 bytes, matches TileMesher.swift
    packed_float3 p;
    packed_char4 n;           // xyz normal, w material: 1 is a wall with windows
    packed_uchar4 c;
};

struct FrameUniforms {
    float4x4 viewProj;
    float4x4 playerModel;
    float4 eye;
    float4 sunDir;            // xyz, world space
    float4 scroll;            // xy pattern offset in metres, z time, w town height 0...1
    float4 world;             // x curvature, y fog start, z fog end, w player scale
    float4 playerClip;        // xy pixels, z depth, w x-ray radius in pixels
    float4 skyRight;          // camera ray at the screen's right, top and centre
    float4 skyUp;
    float4 skyForward;
};

struct DrawUniforms {
    float2 uvOffset;
    float2 origin;            // tile corner relative to the player, metres
    float scale;              // Mercator metres to real metres
    float pad;
    float2 pad2;
};

// Sky at the horizon, and the fog. GameScene.sky must match.
constant float3 skyColour = float3(0.620, 0.800, 0.930);

// The world rolls away from the player like a log, for a soft horizon instead of a hard edge.
static float bend(float2 p, float y, constant FrameUniforms &frame) { return y - dot(p, p) * frame.world.x; }

static float fogAmount(float2 p, constant FrameUniforms &frame) { return smoothstep(frame.world.y, frame.world.z, length(p)); }

// Past the horizon the bent ground faces away from the eye. Flat ground has no depth test, so
// without this the far side of the world is painted over the near side, underside showing.
static bool pastHorizon(float2 p, constant FrameUniforms &frame) {
    float k = frame.world.x;
    float3 onGround = float3(p.x, -dot(p, p) * k, p.y);
    return dot(float3(2.0 * k * p.x, 1.0, 2.0 * k * p.y), frame.eye.xyz - onGround) < 0.0;
}

static float hash21(float2 p) {
    p = fract(p * float2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}

// ---------------------------------------------------------------- sky
struct SkyOut {
    float4 position [[position]];
    float2 ndc;
};

vertex SkyOut skyVertex(uint vid [[vertex_id]])
{
    float2 p = float2((vid << 1) & 2, vid & 2) * 2.0 - 1.0;
    SkyOut o;
    o.position = float4(p, 1.0, 1.0);
    o.ndc = p;
    return o;
}

static float noise2(float2 p) {
    float2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash21(i), hash21(i + float2(1, 0)), f.x),
               mix(hash21(i + float2(0, 1)), hash21(i + float2(1, 1)), f.x), f.y);
}

static float clouds(float2 p) {
    float sum = 0.0, amp = 0.5;
    for (int i = 0; i < 4; i++) {
        sum += noise2(p) * amp;
        p = p * 2.03 + float2(13.1, 7.7);
        amp *= 0.5;
    }
    return sum;
}

// Painted clouds on a soft blue dome. Below the horizon is pure fog colour, and most of the screen
// is there and covered by the map, so it returns before any noise is worked out.
fragment float4 skyFragment(SkyOut in [[stage_in]],
                            constant FrameUniforms &frame [[buffer(2)]])
{
    float3 d = normalize(frame.skyForward.xyz + frame.skyRight.xyz * in.ndc.x + frame.skyUp.xyz * in.ndc.y);
    if (d.y <= 0.0) return float4(skyColour, 1.0);

    float3 zenith = float3(0.170, 0.430, 0.800);
    float3 c = mix(skyColour, zenith, pow(saturate(d.y * 1.5), 0.65));
    float sun = saturate(dot(d, frame.sunDir.xyz));
    c += float3(1.0, 0.86, 0.62) * (pow(sun, 900.0) * 1.2 + pow(sun, 40.0) * 0.12);

    // Hard-edged puffs with a shaded underside, closer to a painted backdrop than to real weather.
    float2 q = d.xz / (d.y + 0.30) * 2.3 + float2(frame.scroll.z * 0.012, frame.scroll.z * 0.004);
    float density = clouds(q);
    float shape = smoothstep(0.545, 0.565, density);
    float lit = saturate((density - clouds(q + frame.sunDir.xz * 0.11)) * 7.0 + 0.62);
    lit = floor(lit * 3.0 + 0.5) / 3.0;
    float3 cloud = mix(float3(0.660, 0.730, 0.880), float3(1.0, 0.980, 0.945), lit);
    return float4(mix(c, cloud, shape * smoothstep(0.012, 0.09, d.y)), 1.0);
}

// ---------------------------------------------------------------- character
struct CharacterOut {
    float4 position [[position]];
    float3 normal;
    float3 world;
    float2 uv;
};

vertex CharacterOut characterVertex(uint vid [[vertex_id]],
                                    constant Vertex *verts [[buffer(0)]],
                                    constant float4x4 *palette [[buffer(1)]],
                                    constant FrameUniforms &frame [[buffer(2)]],
                                    constant DrawUniforms &draw [[buffer(3)]])
{
    const Vertex v = verts[vid];
    float4 w = float4(v.weights);
    float4x4 skin = palette[v.joints[0]] * w.x + palette[v.joints[1]] * w.y
                  + palette[v.joints[2]] * w.z + palette[v.joints[3]] * w.w;
    float4x4 m = frame.playerModel * skin;
    float4 world = m * float4(float3(v.position), 1.0);
    CharacterOut o;
    o.position = frame.viewProj * world;
    o.world = world.xyz;
    o.normal = float3x3(m[0].xyz, m[1].xyz, m[2].xyz) * float3(v.normal);
    o.uv = float2(v.uv) + draw.uvOffset;
    return o;
}

// Soft, cosy lighting: wrapped sun plus a warm hemisphere, nothing metallic or glossy.
fragment float4 characterFragment(CharacterOut in [[stage_in]],
                                  constant FrameUniforms &frame [[buffer(2)]],
                                  texture2d<float> albedo [[texture(0)]],
                                  sampler s [[sampler(0)]])
{
    float3 n = normalize(in.normal);
    float3 l = frame.sunDir.xyz;
    float wrap = saturate(dot(n, l) * 0.5 + 0.5);
    wrap *= wrap;
    float3 sky = float3(0.74, 0.72, 0.70), ground = float3(0.60, 0.53, 0.47);
    float3 hemi = mix(ground, sky, n.y * 0.5 + 0.5);
    float3 light = hemi + float3(0.48, 0.44, 0.38) * wrap;
    float3 c = albedo.sample(s, in.uv).rgb * light;
    float3 v = normalize(frame.eye.xyz - in.world);
    float rim = pow(1.0 - saturate(dot(n, v)), 3.0) * saturate(n.y * 0.5 + 0.5);
    return float4(c + float3(0.10, 0.09, 0.07) * rim, 1.0);
}

// ---------------------------------------------------------------- ground
struct GroundOut {
    float4 position [[position]];
    float2 local;             // metres from the player
    float kind;
    float u;
};

vertex GroundOut groundVertex(uint vid [[vertex_id]],
                              constant GroundVertex *verts [[buffer(0)]],
                              constant FrameUniforms &frame [[buffer(2)]],
                              constant DrawUniforms &draw [[buffer(3)]])
{
    const GroundVertex v = verts[vid];
    float2 p = draw.origin + float2(v.p) * draw.scale;
    GroundOut o;
    o.local = p;
    o.kind = v.kind;
    o.u = v.u;
    o.position = frame.viewProj * float4(p.x, bend(p, 0.0, frame), p.y, 1.0);
    return o;
}

// The demo's lawn at game scale: big soft tiles to read movement, tufts to show speed.
static float3 lawn(float2 w) {
    float2 tile = floor(w / 18.0);
    float3 c = mix(float3(0.470, 0.545, 0.285), float3(0.505, 0.580, 0.310), fmod(abs(tile.x + tile.y), 2.0));
    float2 cell = floor(w / 7.0);
    float2 f = fract(w / 7.0) - 0.5;
    float h = hash21(cell);
    float2 o = float2(hash21(cell + 3.1), hash21(cell + 7.7)) - 0.5;
    float d = length((f - o * 0.5) * float2(1.0, 1.6));
    return mix(c, float3(0.70, 0.70, 0.40), step(0.72, h) * (1.0 - smoothstep(0.09, 0.15, d)) * 0.55);
}

// Scattered dots, for flowers, pebbles and speckle.
static float dots(float2 w, float size, float density, float radius) {
    float2 cell = floor(w / size);
    float2 f = fract(w / size) - 0.5;
    float2 o = float2(hash21(cell + 1.7), hash21(cell + 5.3)) - 0.5;
    return step(1.0 - density, hash21(cell)) * (1.0 - smoothstep(radius * 0.7, radius, length(f - o * 0.6)));
}

static float3 pavers(float2 w, float size, float3 a, float3 b, float3 grout) {
    float2 cell = floor(w / size);
    float2 f = abs(fract(w / size) - 0.5);
    float3 c = mix(a, b, hash21(cell));
    return mix(c, grout, smoothstep(0.44, 0.49, max(f.x, f.y)) * 0.6);
}

fragment float4 groundFragment(GroundOut in [[stage_in]],
                               constant FrameUniforms &frame [[buffer(2)]])
{
    if (pastHorizon(in.local, frame)) discard_fragment();
    float2 w = in.local + frame.scroll.xy;
    int kind = int(in.kind + 0.5);
    float3 c;
    switch (kind) {
    case 1: {   // park
        c = lawn(w) * float3(0.90, 1.04, 0.86);
        c = mix(c, float3(0.96, 0.92, 0.72), dots(w, 8.0, 0.10, 0.09));
        c = mix(c, float3(0.93, 0.66, 0.68), dots(w + 11.0, 9.5, 0.07, 0.08));
        break; }
    case 2:     // wood
        c = mix(float3(0.345, 0.440, 0.245), float3(0.385, 0.475, 0.265), hash21(floor(w / 11.0)));
        break;
    case 3:     // sand
    case 5:     // bank
        c = mix(float3(0.900, 0.815, 0.635), float3(0.800, 0.700, 0.520), dots(w, 3.5, 0.3, 0.2));
        break;
    case 4:     // plaza
        c = pavers(w, 8.0, float3(0.800, 0.745, 0.650), float3(0.770, 0.715, 0.620), float3(0.690, 0.630, 0.540));
        break;
    case 6: {   // water: drifting pale ribbons on deep teal
        float t = frame.scroll.z;
        float a = sin(w.x * 0.028 + w.y * 0.018 + t * 0.5) + sin(w.y * 0.044 - t * 0.35);
        float wave = fract((w.y + a * 16.0) / 38.0);
        float band = smoothstep(0.55, 0.62, wave) * (1.0 - smoothstep(0.62, 0.72, wave));
        c = mix(float3(0.450, 0.700, 0.720), float3(0.520, 0.760, 0.760), a * 0.25 + 0.5);
        c = mix(c, float3(0.740, 0.880, 0.840), band * 0.55);
        break; }
    case 7:     // kerb
        c = float3(0.640, 0.530, 0.400);
        break;
    case 8:     // road
        c = pavers(w, 6.5, float3(0.930, 0.860, 0.690), float3(0.905, 0.830, 0.655), float3(0.830, 0.745, 0.570));
        break;
    case 9:     // footpath
        c = mix(float3(0.840, 0.725, 0.530), float3(0.770, 0.650, 0.465), dots(w, 2.4, 0.3, 0.25));
        break;
    case 10:    // rail bed
        c = mix(float3(0.620, 0.565, 0.500), float3(0.500, 0.445, 0.390), step(0.5, fract((w.x + w.y) / 6.0)));
        break;
    case 11:    // pitches and playgrounds
        c = lawn(w) * float3(1.06, 1.08, 0.92);
        break;
    case 12: {  // painted shadow, stencilled so overlaps do not double up
        float fade = (1.0 - fogAmount(in.local, frame)) * frame.scroll.w;
        return float4(0.120, 0.110, 0.200, 0.34 * fade);
    }
    default:
        c = lawn(w);
        break;
    }
    // The lawn keeps the demo's linear values. Everything else was picked by eye in sRGB.
    if (kind >= 2 && kind <= 10) c = pow(c, 2.2);
    // contact shadow under the character
    float2 s = in.local / (float2(0.26, 0.20) * frame.world.w);
    c *= 1.0 - (1.0 - smoothstep(0.25, 1.0, length(s))) * 0.34;
    return float4(mix(c, skyColour, fogAmount(in.local, frame)), 1.0);
}

// ---------------------------------------------------------------- buildings and trees
struct PropOut {
    float4 position [[position]];
    float3 normal;
    float3 colour;
    float2 local;
    float height;             // metres above the ground, before the world bends
    float window;
};

vertex PropOut propVertex(uint vid [[vertex_id]],
                          constant PropVertex *verts [[buffer(0)]],
                          constant FrameUniforms &frame [[buffer(2)]],
                          constant DrawUniforms &draw [[buffer(3)]])
{
    const PropVertex v = verts[vid];
    float3 p = float3(v.p);
    float2 xz = draw.origin + p.xz * draw.scale;
    PropOut o;
    o.local = xz;
    o.height = p.y;
    o.normal = float3(float(v.n.x), float(v.n.y), float(v.n.z)) / 127.0;
    o.window = float(v.n.w);
    o.colour = float3(float(v.c.x), float(v.c.y), float(v.c.z)) / 255.0;
    o.colour = pow(o.colour, 2.2);
    o.position = frame.viewProj * float4(xz.x, bend(xz, (p.y + 2.0) * frame.scroll.w - 2.0, frame), xz.y, 1.0);
    return o;
}

fragment float4 propFragment(PropOut in [[stage_in]],
                             constant FrameUniforms &frame [[buffer(2)]])
{
    if (frame.scroll.w < 0.04) discard_fragment();    // the town has sunk for the wardrobe
    if (pastHorizon(in.local, frame)) discard_fragment();

    // A low camera sits among the buildings, so those right beside it dissolve too.
    constexpr float bayer[16] = { 0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5 };
    uint2 px = uint2(in.position.xy) % 4;
    float threshold = (bayer[px.y * 4 + px.x] + 0.5) / 16.0;
    float nearEye = 1.0 - smoothstep(45.0, 120.0, distance(float3(in.local.x, in.height, in.local.y), frame.eye.xyz));
    if (nearEye > threshold) discard_fragment();

    // Anything between the camera and the player melts away in a dither, so she is never lost.
    float2 fromPlayer = in.position.xy - frame.playerClip.xy;
    if (in.position.z < frame.playerClip.z) {
        float hole = 1.0 - smoothstep(0.55, 1.0, length(fromPlayer * float2(1.3, 0.8)) / frame.playerClip.w);
        if (hole * 0.92 > threshold) discard_fragment();
    }

    float3 n = normalize(in.normal);
    float3 c = in.colour;
    if (in.window > 0.5) {
        // windows: a grid along the wall, a band of kerbside trim at the foot
        float2 along = in.local + frame.scroll.xy;
        float s = abs(n.x) > abs(n.z) ? along.y : along.x;
        float2 g = float2(fract(s / 8.5), fract(in.height / 8.0));
        float pane = step(0.26, g.x) * step(g.x, 0.74) * step(0.30, g.y) * step(g.y, 0.76);
        float upper = step(4.0, in.height);
        c = mix(c, float3(0.285, 0.380, 0.430), pane * upper * 0.62);
        c = mix(c * 0.72, c, smoothstep(0.1, 3.5, in.height));
    }
    float wrap = saturate(dot(n, frame.sunDir.xyz) * 0.5 + 0.5);
    wrap *= wrap;
    float3 hemi = mix(float3(0.60, 0.53, 0.47), float3(0.74, 0.72, 0.70), n.y * 0.5 + 0.5);
    c *= hemi + float3(0.48, 0.44, 0.38) * wrap;
    return float4(mix(c, skyColour, fogAmount(in.local, frame)), 1.0);
}
