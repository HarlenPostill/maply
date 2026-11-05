#if targetEnvironment(simulator)
import MetalKit

/// Preview only. The iOS Simulator SDK does not expose the Metal 4 API, so this draws the same scene
/// with the same shaders through the classic API. Ships nowhere: it is compiled out of device builds.
final class SimulatorRenderer: NSObject, MTKViewDelegate {
    private static let drawStride = 256
    private static let frameBufferSize = 256 * 1024
    private let scene: GameScene, queue: MTLCommandQueue
    private let characterPSO: MTLRenderPipelineState, groundPSO: MTLRenderPipelineState
    private let shadowPSO: MTLRenderPipelineState, propPSO: MTLRenderPipelineState
    private let states: RenderStates
    private let vertices: MTLBuffer, indices: MTLBuffer
    private let textures: [MTLTexture]
    private let frames = DispatchSemaphore(value: 3)
    private var slot = 0
    private var frameBuffers: [MTLBuffer] = [], paletteBuffers: [MTLBuffer] = []
    private var last = CACurrentMediaTime()
    private var fpsFrames = 0, fpsStart = CACurrentMediaTime()

    init?(view: MTKView, scene: GameScene) throws {
        guard let device = view.device, let q = device.makeCommandQueue(), let lib = device.makeDefaultLibrary() else { return nil }
        self.scene = scene; queue = q
        view.sampleCount = 4; view.depthStencilPixelFormat = .depth32Float_stencil8
        func pso(_ v: String, _ f: String, blended: Bool = false) throws -> MTLRenderPipelineState {
            let d = MTLRenderPipelineDescriptor()
            d.vertexFunction = lib.makeFunction(name: v); d.fragmentFunction = lib.makeFunction(name: f)
            d.rasterSampleCount = 4; d.colorAttachments[0].pixelFormat = view.colorPixelFormat
            d.depthAttachmentPixelFormat = .depth32Float_stencil8
            d.stencilAttachmentPixelFormat = .depth32Float_stencil8
            if blended {
                d.colorAttachments[0].isBlendingEnabled = true
                d.colorAttachments[0].sourceRGBBlendFactor = .sourceAlpha
                d.colorAttachments[0].destinationRGBBlendFactor = .oneMinusSourceAlpha
                d.colorAttachments[0].sourceAlphaBlendFactor = .one
                d.colorAttachments[0].destinationAlphaBlendFactor = .oneMinusSourceAlpha
            }
            return try device.makeRenderPipelineState(descriptor: d)
        }
        characterPSO = try pso("characterVertex", "characterFragment"); groundPSO = try pso("groundVertex", "groundFragment")
        shadowPSO = try pso("groundVertex", "groundFragment", blended: true); propPSO = try pso("propVertex", "propFragment")
        states = RenderStates(device: device)
        func buf<T>(_ a: [T]) -> MTLBuffer { device.makeBuffer(bytes: a, length: MemoryLayout<T>.stride * a.count, options: .storageModeShared)! }
        let asset = scene.asset
        vertices = buf(asset.vertices); indices = buf(asset.indices)
        for _ in 0..<3 {
            frameBuffers.append(device.makeBuffer(length: Self.frameBufferSize, options: .storageModeShared)!)
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
        _ = scene.streamer.takeResidencyChanges()    // classic Metal tracks residency for us
        fpsFrames += 1
        if now - fpsStart >= 1 {
            scene.state.fps = Int(Double(fpsFrames) / (now - fpsStart) + 0.5)
            fpsFrames = 0; fpsStart = now
        }
        let fb = frameBuffers[slot], pb = paletteBuffers[slot]
        var fu = scene.frameUniforms(width: Float(view.drawableSize.width), height: Float(view.drawableSize.height))
        memcpy(fb.contents(), &fu, MemoryLayout<FrameUniforms>.stride)
        scene.animator.palette.withUnsafeBytes { memcpy(pb.contents(), $0.baseAddress!, $0.count) }
        let sky = GameScene.sky
        rp.colorAttachments[0].clearColor = MTLClearColor(red: sky.x, green: sky.y, blue: sky.z, alpha: 1)
        let cb = queue.makeCommandBuffer()!
        let e = cb.makeRenderCommandEncoder(descriptor: rp)!
        e.setFrontFacing(.counterClockwise)
        e.setVertexBuffer(pb, offset: 0, index: 1); e.setVertexBuffer(fb, offset: 0, index: 2); e.setFragmentBuffer(fb, offset: 0, index: 2)
        e.setFragmentSamplerState(states.sampler, index: 0)

        var offset = Self.drawStride
        func writeUniforms(_ du: DrawUniforms) {
            var u = du
            memcpy(fb.contents() + offset, &u, MemoryLayout<DrawUniforms>.stride)
            e.setVertexBuffer(fb, offset: offset, index: 3)
            offset += Self.drawStride
        }

        var bound: MapDraw.Pass?
        for d in scene.mapDraws() {
            if bound != d.pass {
                bound = d.pass
                switch d.pass {
                case .ground: e.setCullMode(.none); e.setRenderPipelineState(groundPSO); e.setDepthStencilState(states.paint)
                case .shadow: e.setCullMode(.none); e.setRenderPipelineState(shadowPSO); e.setDepthStencilState(states.shadow)
                case .prop:   e.setCullMode(.back); e.setRenderPipelineState(propPSO); e.setDepthStencilState(states.solid)
                }
            }
            writeUniforms(d.uniforms)
            e.setVertexBuffer(d.vertices, offset: 0, index: 0)
            e.drawIndexedPrimitives(type: .triangle, indexCount: d.indexCount, indexType: .uint32, indexBuffer: d.indices, indexBufferOffset: d.indexOffset)
        }

        e.setCullMode(.back); e.setDepthStencilState(states.solid)
        e.setRenderPipelineState(characterPSO); e.setVertexBuffer(vertices, offset: 0, index: 0)
        for d in scene.asset.draws where scene.isVisible(d.part) {
            writeUniforms(DrawUniforms(uvOffset: d.isFace ? scene.faceOffset : .zero))
            e.setFragmentTexture(textures[scene.textureIndex(d)], index: 0)
            e.drawIndexedPrimitives(type: .triangle, indexCount: d.indexCount, indexType: .uint32, indexBuffer: indices, indexBufferOffset: d.indexOffset)
        }
        e.endEncoding()
        cb.addCompletedHandler { [frames] _ in frames.signal() }
        cb.present(drawable); cb.commit()
    }
}
#endif
