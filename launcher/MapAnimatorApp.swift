// Map Animator — native macOS shell (SwiftUI) around the local web UI.
//
// The window is a WKWebView showing http://127.0.0.1:8765/ — the page served by ui.py.
// ui.py is started by this app (and stopped when the app quits), so there is nothing to run
// by hand. Generation itself is still `animate_map.py`, called by ui.py.
//
// Why a real binary: macOS grants access to protected folders (Downloads) per executable;
// a shell-script app would be judged as /bin/bash and could never be allowed.
import SwiftUI
import WebKit

private let serverURL = URL(string: "http://127.0.0.1:8765/")!

/// Russian if the system language is Russian, English otherwise.
private let isRussian = (Locale.preferredLanguages.first ?? "en").hasPrefix("ru")
private func L(_ ru: String, _ en: String) -> String { isRussian ? ru : en }

// MARK: - Python backend (ui.py)

final class Backend: ObservableObject {
    enum Phase: Equatable { case starting, ready, failed(String) }

    static let shared = Backend()
    @Published var phase: Phase = .starting

    private var process: Process?
    private var startedAt = Date()
    private var busyCached = false          // updated in the background, read instantly
    private var busyTimer: Timer?

    /// Project folder = the folder that contains this .app (fallback: where it was built).
    let projectDir: String = {
        let beside = (Bundle.main.bundlePath as NSString).deletingLastPathComponent
        if FileManager.default.fileExists(atPath: beside + "/ui.py") { return beside }
        return "/Users/arturchembrovsky/Downloads/tz"
    }()

    func start() {
        startedAt = Date()
        busyTimer?.invalidate()
        busyTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.refreshBusy() }
        DispatchQueue.global().async { [weak self] in
            guard let self = self else { return }
            if self.isUp() {                          // a server is already running: reuse it
                DispatchQueue.main.async { self.phase = .ready }
            } else {
                DispatchQueue.main.async { self.launchServer() }
            }
        }
    }

    private func launchServer() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: projectDir + "/.venv/bin/python")
        p.arguments = [projectDir + "/ui.py", "--no-window"]
        p.currentDirectoryURL = URL(fileURLWithPath: projectDir)
        let logPath = projectDir + "/logs/ui_server.log"
        try? FileManager.default.createDirectory(atPath: projectDir + "/logs", withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: logPath, contents: nil)   // fresh log per start
        if let h = FileHandle(forWritingAtPath: logPath) {
            p.standardOutput = h
            p.standardError = h
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self = self else { return }
                if self.phase == .ready { NSApp.terminate(nil) }       // server stopped by itself
                else {
                    self.phase = .failed(L("Сервер остановился с кодом \(proc.terminationStatus). Подробности в logs/ui_server.log",
                                           "The server stopped with code \(proc.terminationStatus). Details: logs/ui_server.log"))
                }
            }
        }
        do { try p.run(); process = p } catch {
            phase = .failed(L("Не удалось запустить \(projectDir)/.venv/bin/python:\n\(error.localizedDescription)", "Could not start \(projectDir)/.venv/bin/python:\n\(error.localizedDescription)"))
            return
        }
        waitUntilReady()
    }

    private func waitUntilReady() {
        DispatchQueue.global().async { [weak self] in
            for _ in 0..<100 {                                       // up to ~20 s
                if self?.isUp() == true {
                    DispatchQueue.main.async { self?.phase = .ready }
                    return
                }
                Thread.sleep(forTimeInterval: 0.2)
            }
        }
    }

    func stop() {
        busyTimer?.invalidate()
        process?.terminationHandler = nil
        process?.terminate()        // SIGTERM: ui.py cancels a running job and exits on its own; no waiting here
    }

    /// True while a video is being generated (cached, so it never blocks the UI).
    func isBusy() -> Bool { busyCached }

    private func refreshBusy() {
        var req = URLRequest(url: serverURL.appendingPathComponent("api/busy"))
        req.timeoutInterval = 1
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            var busy = false
            if let d = data, let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                busy = (o["busy"] as? Bool) ?? false
            }
            DispatchQueue.main.async { self?.busyCached = busy }
        }.resume()
    }

    func isUp() -> Bool { fetchBusy() != nil }

    private func fetchBusy() -> Bool? {
        var req = URLRequest(url: serverURL.appendingPathComponent("api/busy"))
        req.timeoutInterval = 1
        let sem = DispatchSemaphore(value: 0)
        var result: Bool?
        URLSession.shared.dataTask(with: req) { data, _, _ in
            if let d = data, let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                result = o["busy"] as? Bool
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 1.5)
        return result
    }
}

// MARK: - Web view

struct WebView: NSViewRepresentable {
    let url: URL

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> WKWebView {
        let web = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        web.uiDelegate = context.coordinator
        web.load(URLRequest(url: url))
        return web
    }

    func updateNSView(_ nsView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKUIDelegate {
        // "Click to choose a file" inside the page needs an explicit open panel on macOS.
        func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
            let panel = NSOpenPanel()
            panel.allowsMultipleSelection = parameters.allowsMultipleSelection
            panel.canChooseDirectories = false
            panel.allowedContentTypes = [.image]
            panel.begin { completionHandler($0 == .OK ? panel.urls : nil) }
        }
    }
}

// MARK: - UI

struct ContentView: View {
    @ObservedObject var backend = Backend.shared

    var body: some View {
        Group {
            switch backend.phase {
            case .starting:
                VStack(spacing: 12) {
                    ProgressView()
                    Text(L("Запуск Map Animator…", "Starting Map Animator…")).foregroundStyle(.secondary)
                }
            case .ready:
                WebView(url: serverURL)
            case .failed(let message):
                VStack(spacing: 10) {
                    Image(systemName: "exclamationmark.triangle").font(.largeTitle).foregroundStyle(.orange)
                    Text(L("Не удалось запустить сервер", "Could not start the server")).font(.headline)
                    Text(message).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    Button(L("Повторить", "Retry")) { backend.phase = .starting; backend.start() }
                }
                .padding(30)
            }
        }
        .frame(minWidth: 820, minHeight: 620)
        .onAppear { if backend.phase == .starting { backend.start() } }
    }
}

// MARK: - App lifecycle

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var idleTimer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Dock icon straight from the bundle (does not depend on the system icon cache)
        if let path = Bundle.main.path(forResource: "AppIcon", ofType: "icns"),
           let icon = NSImage(contentsOfFile: path) {
            NSApp.applicationIconImage = icon
        }
    }

    // Closing the window quits the app — unless a video is being generated: then the app
    // stays alive in the Dock and quits by itself as soon as the generation is finished.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        if Backend.shared.isBusy() {
            idleTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { _ in
                if !Backend.shared.isBusy() { NSApp.terminate(nil) }
            }
            return false
        }
        return true
    }

    // Cmd+Q during a generation: ask first.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard Backend.shared.isBusy() else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = L("Идёт генерация видео", "A video is being generated")
        alert.informativeText = L("Если выйти сейчас, генерация будет прервана.", "If you quit now, the generation will be cancelled.")
        alert.addButton(withTitle: L("Не выходить", "Don't quit"))
        alert.addButton(withTitle: L("Прервать и выйти", "Cancel and quit"))
        return alert.runModal() == .alertSecondButtonReturn ? .terminateNow : .terminateCancel
    }

    func applicationWillTerminate(_ notification: Notification) { Backend.shared.stop() }
}

@main
struct MapAnimatorApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        Window("Map Animator", id: "main") {
            ContentView()
        }
        .defaultSize(width: 1100, height: 900)
    }
}
