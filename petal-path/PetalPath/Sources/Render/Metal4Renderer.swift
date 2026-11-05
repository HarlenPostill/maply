#if !targetEnvironment(simulator)
import MetalKit

/// The real renderer. Uses the Metal 4 core API: MTL4CommandQueue, per-frame command allocators,
/// argument tables, residency sets and MTL4Compiler. Not exposed by the iOS Simulator SDK.
final class Metal4Renderer: NSObject, MTKViewDelegate {
    private static let framesInFlight = 3
    private static let drawStride = 256
    private static let frameBlock = 512          // FrameUniforms lives before the per-draw data
    private static let frameBufferSize = 256 * 1024
    private let device: MTLDevice
    private let scene: GameScene
    private let queue: any MTL4CommandQueue
    private let commandBuffer: any MTL4CommandBuffer
    private var allocators: [any MTL4CommandAllocator] = []
    private let table: any MTL4ArgumentTable
    private let characterPSO: MTLRenderPipelineState, groundPSO: MTLRenderPipelineState
    private let shadowPSO: MTLRenderPipelineState, propPSO: MTLRenderPipelineState, skyPSO: MTLRenderPipelineState
    private let paintState: MTLDepthStencilState, shadowState: MTLDepthStencilState, solidState: MTLDepthStencilState
    private let sampler: MTLSamplerState
    private let residency: any MTLResidencySet
    private let vertices: MTLBuffer, indices: MTLBuffer
    private var frameBuffers: [MTLBuffer] = [], paletteBuffers: [MTLBuffer] = []
    private var textures: [MTLTexture] = []
    private var msaa: MTLTexture?, depth: MTLTexture?
    private let event: MTLSharedEvent
    private var frameNumber: UInt64 = 0
    private var last = CACurrentMediaTime()
    private var fpsFrames = 0, fpsStart = CACurrentMediaTime()
    /// Metal 4 does not retain what a command buffer uses, so unloaded tiles wait out the frames in flight.
    private var retiring: [(frame: UInt64, buffers: [MTLBuffer])] = []

    init?(view: MTKView, scene: GameScene) throws {
        guard let device = view.device, device.supportsFamily(.metal3),
              device.supportsFamily(MTLGPUFamily(rawValue: 5002)!),   // MTLGPUFamilyMetal4
              let queue = device.makeMTL4CommandQueue(), let cb = device.makeCommandBuffer(),
              let library = device.makeDefaultLibrary() else { return nil }
        self.device = device; self.scene = scene; self.queue = queue; commandBuffer = cb
        let asset = scene.asset

        for _ in 0..<Self.framesInFlight { allocators.append(device.makeCommandAllocator()!) }

        let ta = MTL4ArgumentTableDescriptor()
        ta.maxBufferBindCount = 4; ta.maxTextureBindCount = 1; ta.maxSamplerStateBindCount = 1
        table = try device.makeArgumentTable(descriptor: ta)

        // pipelines
        let compiler = try device.makeCompiler(descriptor: MTL4CompilerDescriptor())
        func pso(_ vs: String, _ fs: String, blended: Bool = false) throws -> MTLRenderPipelineState {
            let v = MTL4LibraryFunctionDescriptor(); v.library = library; v.name = vs
            let f = MTL4LibraryFunctionDescriptor(); f.library = library; f.name = fs
            let d = MTL4RenderPipelineDescriptor()
            d.vertexFunctionDescriptor = v; d.fragmentFunctionDescriptor = f
            d.rasterSampleCount = 4
            d.colorAttachments[0].pixelFormat = view.colorPixelFormat
            if blended {
                d.colorAttachments[0].blendingState = .enabled
                d.colorAttachments[0].sourceRGBBlendFactor = .sourceAlpha
                d.colorAttachments[0].destinationRGBBlendFactor = .oneMinusSourceAlpha
                d.colorAttachments[0].sourceAlphaBlendFactor = .one
                d.colorAttachments[0].destinationAlphaBlendFactor = .oneMinusSourceAlpha
            }
            return try compiler.makeRenderPipelineState(descriptor: d)
        }
        characterPSO = try pso("characterVertex", "characterFragment")
        groundPSO = try pso("groundVertex", "groundFragment")
        shadowPSO = try pso("groundVertex", "groundFragment", blended: true)
        propPSO = try pso("propVertex", "propFragment")
        skyPSO = try pso("skyVertex", "skyFragment")

        let states = RenderStates(device: device)
        paintState = states.paint; shadowState = states.shadow; solidState = states.solid
        sampler = states.sampler

        // geometry
        func buf<T>(_ a: [T]) -> MTLBuffer { device.makeBuffer(bytes: a, length: MemoryLayout<T>.stride * a.count, options: .storageModeShared)! }
        vertices = buf(asset.vertices); indices = buf(asset.indices)
        for _ in 0..<Self.framesInFlight {
            frameBuffers.append(device.makeBuffer(length: Self.frameBufferSize, options: .storageModeShared)!)
            paletteBuffers.append(device.makeBuffer(length: 64 * asset.parents.count, options: .storageModeShared)!)
        }

        // textures
        let loader = MTKTextureLoader(device: device)
        textures = try asset.imageData.map {
            try loader.newTexture(data: $0, options: [.SRGB: true, .generateMipmaps: true, .textureStorageMode: MTLStorageMode.private.rawValue])
        }

        // Metal 4 has no implicit residency: everything the GPU touches is declared here.
        let rd = MTLResidencySetDescriptor(); rd.initialCapacity = 128
        residency = try device.makeResidencySet(descriptor: rd)
        for a in [vertices, indices] + scene.staticBuffers + frameBuffers + paletteBuffers { residency.addAllocation(a) }
        for t in textures { residency.addAllocation(t) }
        residency.commit()
        queue.addResidencySet(residency)
        queue.addResidencySet((view.layer as! CAMetalLayer).residencySet)
        event = device.makeSharedEvent()!
        super.init()
    }

    func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) {
        guard size.width > 0, size.height > 0 else { return }
        _ = event.wait(untilSignaledValue: frameNumber, timeoutMS: 500)
        let cd = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: view.colorPixelFormat, width: Int(size.width), height: Int(size.height), mipmapped: false)
        cd.textureType = .type2DMultisample; cd.sampleCount = 4; cd.usage = .renderTarget; cd.storageMode = .memoryless
        msaa = device.makeTexture(descriptor: cd)
        let dd = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .depth32Float_stencil8, width: Int(size.width), height: Int(size.height), mipmapped: false)
        dd.textureType = .type2DMultisample; dd.sampleCount = 4; dd.usage = .renderTarget; dd.storageMode = .memoryless
        depth = device.makeTexture(descriptor: dd)
        // Memoryless attachments have no backing store, so they stay out of the residency set.
    }

    /// Declares newly uploaded tiles and drops unloaded ones once no frame in flight can use them.
    private func syncResidency() {
        let changes = scene.streamer.takeResidencyChanges()
        var dirty = false
        for b in changes.added { residency.addAllocation(b); dirty = true }
        if !changes.removed.isEmpty { retiring.append((frameNumber, changes.removed)) }
        while let r = retiring.first, frameNumber >= r.frame + UInt64(Self.framesInFlight) + 1 {
            for b in r.buffers { residency.removeAllocation(b) }
            retiring.removeFirst()
            dirty = true
        }
        if dirty { residency.commit() }
    }

    func draw(in view: MTKView) {
        guard let drawable = view.currentDrawable, let msaa, let depth else { return }
        let fif = UInt64(Self.framesInFlight)
        if frameNumber >= fif { _ = event.wait(untilSignaledValue: frameNumber - fif + 1, timeoutMS: 100) }
        frameNumber += 1
        let slot = Int(frameNumber % fif)
        let now = CACurrentMediaTime(); let dt = Float(min(now - last, 0.1)); last = now

        scene.update(dt: dt)
        syncResidency()
        reportFPS(now)

        // per-frame data
        let fb = frameBuffers[slot], pb = paletteBuffers[slot]
        let size = view.drawableSize
        var fu = scene.frameUniforms(width: Float(size.width), height: Float(size.height))
        memcpy(fb.contents(), &fu, MemoryLayout<FrameUniforms>.stride)
        scene.animator.palette.withUnsafeBytes { memcpy(pb.contents(), $0.baseAddress!, $0.count) }

        let allocator = allocators[slot]
        allocator.reset()
        commandBuffer.beginCommandBuffer(allocator: allocator)
        let rp = MTL4RenderPassDescriptor()
        rp.colorAttachments[0].texture = msaa
        rp.colorAttachments[0].resolveTexture = drawable.texture
        rp.colorAttachments[0].loadAction = .clear
        rp.colorAttachments[0].storeAction = .multisampleResolve
        let sky = GameScene.sky
        rp.colorAttachments[0].clearColor = MTLClearColor(red: sky.x, green: sky.y, blue: sky.z, alpha: 1)
        rp.depthAttachment.texture = depth
        rp.depthAttachment.loadAction = .clear; rp.depthAttachment.storeAction = .dontCare
        rp.depthAttachment.clearDepth = 1
        rp.stencilAttachment.texture = depth
        rp.stencilAttachment.loadAction = .clear; rp.stencilAttachment.storeAction = .dontCare
        guard let e = commandBuffer.makeRenderCommandEncoder(descriptor: rp) else { return }
        e.setFrontFacing(.counterClockwise)
        e.setArgumentTable(table, stages: [.vertex, .fragment])
        table.setAddress(pb.gpuAddress, index: 1)
        table.setAddress(fb.gpuAddress, index: 2)
        table.setSamplerState(sampler.gpuResourceID, index: 0)

        e.setCullMode(.none)
        e.setDepthStencilState(paintState)
        e.setRenderPipelineState(skyPSO)
        e.drawPrimitives(primitiveType: .triangle, vertexStart: 0, vertexCount: 3)

        var offset = Self.frameBlock
        func writeUniforms(_ du: DrawUniforms) {
            var u = du
            memcpy(fb.contents() + offset, &u, MemoryLayout<DrawUniforms>.stride)
            table.setAddress(fb.gpuAddress + UInt64(offset), index: 3)
            offset += Self.drawStride
        }

        // Map. Flat ground is painted back to front, so only buildings and trees need depth.
        var bound: MapDraw.Pass?
        for d in scene.mapDraws() {
            if bound != d.pass {
                bound = d.pass
                switch d.pass {
                case .ground: e.setCullMode(.none); e.setRenderPipelineState(groundPSO); e.setDepthStencilState(paintState)
                case .shadow: e.setCullMode(.none); e.setRenderPipelineState(shadowPSO); e.setDepthStencilState(shadowState)
                case .prop:   e.setCullMode(.back); e.setRenderPipelineState(propPSO); e.setDepthStencilState(solidState)
                }
            }
            writeUniforms(d.uniforms)
            table.setAddress(d.vertices.gpuAddress, index: 0)
            e.drawIndexedPrimitives(primitiveType: .triangle, indexCount: d.indexCount, indexType: .uint32,
                                    indexBuffer: d.indices.gpuAddress + UInt64(d.indexOffset),
                                    indexBufferLength: d.indexCount * 4)
        }

        // character: each DrawItem is one swappable part
        e.setCullMode(.back)
        e.setDepthStencilState(solidState)
        e.setRenderPipelineState(characterPSO)
        table.setAddress(vertices.gpuAddress, index: 0)
        for d in scene.asset.draws where scene.isVisible(d.part) {
            writeUniforms(DrawUniforms(uvOffset: d.isFace ? scene.faceOffset : .zero))
            table.setTexture(textures[scene.textureIndex(d)].gpuResourceID, index: 0)
            e.drawIndexedPrimitives(primitiveType: .triangle, indexCount: d.indexCount, indexType: .uint32,
                                    indexBuffer: indices.gpuAddress + UInt64(d.indexOffset),
                                    indexBufferLength: d.indexCount * 4)
        }
        e.endEncoding()
        commandBuffer.endCommandBuffer()

        queue.waitForDrawable(drawable)
        queue.commit([commandBuffer])
        queue.signalDrawable(drawable)
        drawable.present()
        queue.signalEvent(event, value: frameNumber)
    }

    private func reportFPS(_ now: CFTimeInterval) {
        fpsFrames += 1
        guard now - fpsStart >= 1 else { return }
        let fps = Int(Double(fpsFrames) / (now - fpsStart) + 0.5)
        fpsFrames = 0; fpsStart = now
        scene.state.fps = fps
    }
}
#endif
