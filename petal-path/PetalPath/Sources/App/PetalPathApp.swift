import SwiftUI
import MetalKit

@main
struct PetalPathApp: App {
    var body: some SwiftUI.Scene { WindowGroup { GameView() } }
}

struct MetalView: UIViewRepresentable {
    @ObservedObject var state: GameState
    final class Coordinator { var renderer: MTKViewDelegate?; var scene: GameScene? }
    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> MTKView {
        let v = MTKView(frame: .zero, device: MTLCreateSystemDefaultDevice())
        v.colorPixelFormat = .bgra8Unorm_srgb
        v.preferredFramesPerSecond = 60
        v.isOpaque = true
        do {
            guard let device = v.device else { throw GameError.noMetal }
            let scene = try GameScene(state: state, device: device)
            context.coordinator.scene = scene
            #if targetEnvironment(simulator)
            let r = try SimulatorRenderer(view: v, scene: scene)
            let name = "Simulator preview · classic Metal"
            #else
            let r = try Metal4Renderer(view: v, scene: scene)
            let name = "Metal 4"
            #endif
            guard let r else { throw GameError.noMetal }
            v.delegate = r
            context.coordinator.renderer = r
            r.mtkView(v, drawableSizeWillChange: v.drawableSize)
            DispatchQueue.main.async { state.backend = name }
        } catch {
            DispatchQueue.main.async { state.error = error.localizedDescription }
        }
        return v
    }
    func updateUIView(_ v: MTKView, context: Context) {}
}

enum GameError: LocalizedError {
    case noMetal
    var errorDescription: String? { "This device does not support Metal 4." }
}
