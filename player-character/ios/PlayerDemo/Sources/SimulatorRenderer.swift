#if targetEnvironment(simulator)
import MetalKit

/// Preview only. The iOS Simulator SDK does not expose the Metal 4 API, so this draws the same scene
/// with the same shaders through the classic API. Ships nowhere: it is compiled out of device builds.
final class SimulatorRenderer: NSObject, MTKViewDelegate {
    private static let drawStride = 256
    private let scene: CharacterScene, queue: MTLCommandQueue
    private let characterPSO: MTLRenderPipelineState, groundPSO: MTLRenderPipelineState
    private let depthState: MTLDepthStencilState, sampler: MTLSamplerState
    private let vertices: MTLBuffer, indices: MTLBuffer, groundVerts: MTLBuffer, groundIdx: MTLBuffer
    private let textures: [MTLTexture]
    private let frames = DispatchSemaphore(value: 3)
    private var slot = 0
    private var frameBuffers: [MTLBuffer] = [], paletteBuffers: [MTLBuffer] = []
    private var last = CACurrentMediaTime()

    init?(view: MTKView, scene: CharacterScene) throws {
        guard let device = view.device, let q = device.makeCommandQueue(), let lib = device.makeDefaultLibrary() else { return nil }
        self.scene = scene; queue = q
        view.sampleCount = 4; view.depthStencilPixelFormat = .depth32Float
        func pso(_ v: String, _ f: String) throws -> MTLRenderPipelineState {
            let d = MTLRenderPipelineDescriptor()
            d.vertexFunction = lib.makeFunction(name: v); d.fragmentFunction = lib.makeFunction(name: f)
            d.rasterSampleCount = 4; d.colorAttachments[0].pixelFormat = view.colorPixelFormat
            d.depthAttachmentPixelFormat = .depth32Float
            return try device.makeRenderPipelineState(descriptor: d)
        }
        characterPSO = try pso("characterVertex", "characterFragment"); groundPSO = try pso("groundVertex", "groundFragment")
        let dd = MTLDepthStencilDescriptor(); dd.depthCompareFunction = .less; dd.isDepthWriteEnabled = true
        depthState = device.makeDepthStencilState(descriptor: dd)!
        let sd = MTLSamplerDescriptor(); sd.minFilter = .linear; sd.magFilter = .linear; sd.mipFilter = .linear
        sd.sAddressMode = .repeat; sd.tAddressMode = .repeat
        sampler = device.makeSamplerState(descriptor: sd)!
        func buf<T>(_ a: [T]) -> MTLBuffer { device.makeBuffer(bytes: a, length: MemoryLayout<T>.stride * a.count, options: .storageModeShared)! }
        let asset = scene.asset
        vertices = buf(asset.vertices); indices = buf(asset.indices)
        groundVerts = buf(scene.ground.map { [$0.x, $0.y] }.flatMap { $0 }); groundIdx = buf(scene.groundIndices)
        for _ in 0..<3 {
            frameBuffers.append(device.makeBuffer(length: 4096, options: .storageModeShared)!)
            paletteBuffers.append(device.makeBuffer(length: 64 * asset.parents.count, options: .storageModeShared)!)
        }
        let loader = MTKTextureLoader(device: device)
        textures = try asset.imageData.map { try loader.newTexture(data: $0, options: [.SRGB: true, .generateMipmaps: true, .textureStorageMode: MTLStorageMode.private.rawValue]) }
        super.init()
    }

    func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) {}

    func draw(in view: MTKView) {
        guard let rp = view.currentRenderPassDescriptor, let drawable = view.currentDrawable else { return }
        frames.wait()
        slot = (slot + 1) % 3
        let now = CACurrentMediaTime(); let dt = Float(min(now - last, 0.1)); last = now
        scene.update(dt: dt)
        let fb = frameBuffers[slot], pb = paletteBuffers[slot]
        var fu = scene.frameUniforms(aspect: Float(view.drawableSize.width / view.drawableSize.height))
        memcpy(fb.contents(), &fu, MemoryLayout<FrameUniforms>.stride)
        scene.animator.palette.withUnsafeBytes { memcpy(pb.contents(), $0.baseAddress!, $0.count) }
        rp.colorAttachments[0].clearColor = MTLClearColor(red: 0.930, green: 0.860, blue: 0.760, alpha: 1)
        let cb = queue.makeCommandBuffer()!
        let e = cb.makeRenderCommandEncoder(descriptor: rp)!
        e.setDepthStencilState(depthState); e.setFrontFacing(.counterClockwise)
        e.setVertexBuffer(pb, offset: 0, index: 1); e.setVertexBuffer(fb, offset: 0, index: 2); e.setFragmentBuffer(fb, offset: 0, index: 2)
        e.setFragmentSamplerState(sampler, index: 0)
        e.setRenderPipelineState(groundPSO); e.setVertexBuffer(groundVerts, offset: 0, index: 0)
        e.drawIndexedPrimitives(type: .triangle, indexCount: scene.groundIndices.count, indexType: .uint32, indexBuffer: groundIdx, indexBufferOffset: 0)
        e.setCullMode(.back); e.setRenderPipelineState(characterPSO); e.setVertexBuffer(vertices, offset: 0, index: 0)
        var offset = Self.drawStride
        for d in scene.asset.draws where scene.isVisible(d.part) {
            var du = DrawUniforms(uvOffset: d.isFace ? scene.faceOffset : .zero)
            memcpy(fb.contents() + offset, &du, MemoryLayout<DrawUniforms>.stride)
            e.setVertexBuffer(fb, offset: offset, index: 3)
            e.setFragmentTexture(textures[scene.textureIndex(d)], index: 0)
            e.drawIndexedPrimitives(type: .triangle, indexCount: d.indexCount, indexType: .uint32, indexBuffer: indices, indexBufferOffset: d.indexOffset)
            offset += Self.drawStride
        }
        e.endEncoding()
        cb.addCompletedHandler { [frames] _ in frames.signal() }
        cb.present(drawable); cb.commit()
    }
}
#endif
