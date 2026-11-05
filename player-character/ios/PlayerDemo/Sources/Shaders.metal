#include <metal_stdlib>
using namespace metal;

struct Vertex {               // 56 bytes, matches Vertex in GLB.swift
    packed_float3 position;
    packed_float3 normal;
    packed_float2 uv;
    packed_ushort4 joints;
    packed_float4 weights;
};

struct FrameUniforms {
    float4x4 viewProj;
    float4 eye;
    float4 sunDir;            // xyz, world space
    float4 scroll;            // ground offset in metres, xy = world x,z
};

struct DrawUniforms {
    float2 uvOffset;
    float2 pad;
};

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
    float4 world = skin * float4(float3(v.position), 1.0);
    CharacterOut o;
    o.position = frame.viewProj * world;
    o.world = world.xyz;
    o.normal = float3x3(skin[0].xyz, skin[1].xyz, skin[2].xyz) * float3(v.normal);
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
    float3 world;
};

// The little world curves away from the player, a soft horizon instead of a hard edge.
vertex GroundOut groundVertex(uint vid [[vertex_id]],
                              constant packed_float2 *grid [[buffer(0)]],
                              constant FrameUniforms &frame [[buffer(2)]])
{
    float2 p = float2(grid[vid]);
    float y = -dot(p, p) * 0.018;
    GroundOut o;
    o.world = float3(p.x, y, p.y);
    o.position = frame.viewProj * float4(o.world, 1.0);
    return o;
}

static float hash21(float2 p) {
    p = fract(p * float2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}

fragment float4 groundFragment(GroundOut in [[stage_in]],
                               constant FrameUniforms &frame [[buffer(2)]])
{
    float2 w = in.world.xz + frame.scroll.xy;
    // lawn tiles, big enough to read movement, soft enough to stay out of the way
    float2 tile = floor(w / 0.6);
    float checker = fmod(tile.x + tile.y, 2.0);
    float3 a = float3(0.470, 0.545, 0.285), b = float3(0.520, 0.590, 0.320);
    float3 c = mix(a, b, checker);
    // scattered tufts, these show speed best
    float2 cell = floor(w / 0.22);
    float2 f = fract(w / 0.22) - 0.5;
    float h = hash21(cell);
    float2 o = float2(hash21(cell + 3.1), hash21(cell + 7.7)) - 0.5;
    float d = length((f - o * 0.5) * float2(1.0, 1.6));
    float tuft = step(0.72, h) * (1.0 - smoothstep(0.09, 0.15, d));
    c = mix(c, float3(0.70, 0.70, 0.40), tuft * 0.55);
    // contact shadow under the character
    float2 s = in.world.xz / float2(0.26, 0.20);
    float shadow = 1.0 - smoothstep(0.25, 1.0, length(s));
    c *= 1.0 - shadow * 0.34;
    // fade into the sky colour
    float fog = smoothstep(2.6, 7.0, length(in.world.xz));
    c = mix(c, float3(0.930, 0.860, 0.760), fog);
    return float4(c, 1.0);
}
