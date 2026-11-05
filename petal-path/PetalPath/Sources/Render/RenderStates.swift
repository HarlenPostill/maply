import Metal

/// Depth, stencil and sampler states, identical on both backends.
struct RenderStates {
    let paint: MTLDepthStencilState     // flat ground, drawn in order: no depth at all
    let shadow: MTLDepthStencilState    // painted shadows: first touch of a pixel wins
    let solid: MTLDepthStencilState     // buildings, trees, character
    let sampler: MTLSamplerState

    init(device: MTLDevice) {
        let p = MTLDepthStencilDescriptor()
        p.depthCompareFunction = .always; p.isDepthWriteEnabled = false
        paint = device.makeDepthStencilState(descriptor: p)!

        let s = MTLDepthStencilDescriptor()
        s.depthCompareFunction = .always; s.isDepthWriteEnabled = false
        let st = MTLStencilDescriptor()
        st.stencilCompareFunction = .equal; st.depthStencilPassOperation = .incrementClamp
        s.frontFaceStencil = st; s.backFaceStencil = st
        shadow = device.makeDepthStencilState(descriptor: s)!

        let d = MTLDepthStencilDescriptor()
        d.depthCompareFunction = .less; d.isDepthWriteEnabled = true
        solid = device.makeDepthStencilState(descriptor: d)!

        let sd = MTLSamplerDescriptor()
        sd.minFilter = .linear; sd.magFilter = .linear; sd.mipFilter = .linear
        sd.sAddressMode = .repeat; sd.tAddressMode = .repeat; sd.maxAnisotropy = 8
        sampler = device.makeSamplerState(descriptor: sd)!
    }
}
