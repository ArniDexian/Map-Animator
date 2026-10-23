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

/// The processor this half of the universal executable was compiled for: the Python that runs the server must match it.
#if arch(arm64)
private let nativeArch = "arm64"
#else
private let nativeArch = "x86_64"
#endif

/// Russian if the system language is Russian, English otherwise.
private let isRussian = (Locale.preferredLanguages.first ?? "en").hasPrefix("ru")
private func L(_ ru: String, _ en: String) -> String { isRussian ? ru : en }

// MARK: - Python backend (ui.py)

final class Backend: ObservableObject {
    enum Phase: Equatable { case starting, ready, failed(String), needsInstall(String), needsAccess(String) }

    static let shared = Backend()
    @Published var phase: Phase = .starting { didSet { appLog("state -> \(phase)") } }

    private var process: Process?
    private var startedAt = Date()
    private var busyCached = false          // updated in the background, read instantly
    private var busyTimer: Timer?

    // Where things live:
    //   appDir   code, page, config.json, docs, installer — Contents/Resources/app inside this .app
    //            (an .app without bundled resources, built for development, uses the checkout next to it)
    //   dataDir  internal data: the Python environment, settings, uploaded maps
    //            ~/Library/Application Support/Map Animator
    //   homeDir  what the user sees: videos, debug images, logs — ~/Documents/Map Animator
    // MAP_ANIMATOR_DATA / MAP_ANIMATOR_HOME override the last two (used for tests).
    private let env = ProcessInfo.processInfo.environment
    let packaged: Bool
    let appDir: String
    let dataDir: String
    let homeDir: String

    init() {
        if let r = Bundle.main.resourcePath, FileManager.default.fileExists(atPath: r + "/app/ui.py") {
            appDir = r + "/app"; packaged = true
        } else {
            appDir = (Bundle.main.bundlePath as NSString).deletingLastPathComponent; packaged = false
        }
        dataDir = env["MAP_ANIMATOR_DATA"] ?? (NSHomeDirectory() + "/Library/Application Support/Map Animator")
        homeDir = env["MAP_ANIMATOR_HOME"] ?? (NSHomeDirectory() + "/Documents/Map Animator")
    }

    /// The app's own environment (packaged: in Application Support; development checkout: ./.venv).
    private var ownPython: String { packaged ? dataDir + "/venv/bin/python" : appDir + "/.venv/bin/python" }
    /// The Python that actually runs the server (see resolvePython()).
    private var pythonPath: String = ""
    private var logPath: String { (packaged ? homeDir : appDir) + "/logs/ui_server.log" }

    /// A short diagnostic trail of what the app decided (logs/app.log) — handy when something does not start.
    func appLog(_ text: String) {
        let dir = ((packaged ? homeDir : appDir) + "/logs") as String
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let line = "\(ISO8601DateFormatter().string(from: Date())) \(text)\n"
        let path = dir + "/app.log"
        if let h = FileHandle(forWritingAtPath: path) { h.seekToEndOfFile(); h.write(Data(line.utf8)); try? h.close() }
        else { try? line.write(toFile: path, atomically: true, encoding: .utf8) }
    }

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

    // MARK: first-run setup: Python / packages missing -> offer to run install.command in Terminal

    private let requiredModules = ["cv2", "numpy", "scipy", "skimage", "networkx", "tqdm", "PIL", "imageio_ffmpeg", "markdown", "fontTools", "uharfbuzz"]
    private var installTimer: Timer?
    private var offeredInstall = false

    /// Finds a Python that already has every required package, so nothing needs to be installed:
    ///  1. MAP_ANIMATOR_PYTHON (if set)           2. the app's own environment
    ///  3. the environment this app was built next to (local builds only; not in disk images)
    ///  4. any Python on this Mac (Homebrew, python.org, Xcode / Command Line Tools) that has the packages
    private func resolvePython() -> String? {
        var candidates: [String] = []
        if let forced = env["MAP_ANIMATOR_PYTHON"] { candidates.append(forced) }
        candidates.append(ownPython)
        if let dev = Bundle.main.object(forInfoDictionaryKey: "MapAnimatorDevEnvironment") as? String { candidates.append(dev) }
        for v in ["3.13", "3.12", "3.11", "3.10", "3.9"] {
            candidates += ["/opt/homebrew/bin/python\(v)", "/usr/local/bin/python\(v)",
                           "/Library/Frameworks/Python.framework/Versions/\(v)/bin/python3"]
        }
        // NOT /usr/bin/python3: without developer tools that is a stub which pops up an install dialog when run
        candidates += ["/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                       "/Library/Developer/CommandLineTools/usr/bin/python3",
                       "/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9"]
        for c in candidates where FileManager.default.isExecutableFile(atPath: c) {
            let m = missingModules(python: c)
            appLog("python candidate \(c): " + (m == nil ? "cannot run" : (m!.isEmpty ? "has every package" : "missing " + m!.joined(separator: ","))))
            if let m = m, m.isEmpty { return c }
        }
        appLog("no Python with all packages found")
        return nil
    }

    /// Names of the packages that `python` cannot find; nil if it cannot run at all.
    private func missingModules(python: String? = nil) -> [String]? {
        let py = python ?? (pythonPath.isEmpty ? ownPython : pythonPath)
        guard FileManager.default.isExecutableFile(atPath: py) else { return nil }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: py)
        let list = requiredModules.map { "\"\($0)\"" }.joined(separator: ", ")
        // prints "<processor>|<missing packages>", e.g. "arm64|cv2 numpy"
        // find_spec only looks for the files; the two big binary packages are also really imported, because files of the
        // wrong processor type (or a broken install) are present but unusable.
        let code = """
        import importlib.util as u, platform
        miss = [m for m in [\(list)] if u.find_spec(m) is None]
        if not miss:
            for m in ("numpy", "cv2"):
                try: __import__(m)
                except Exception: miss.append(m + "(unusable)")
        print(platform.machine() + "|" + " ".join(miss))
        """
        p.arguments = ["-c", code]
        let out = Pipe(); p.standardOutput = out; p.standardError = Pipe()
        do { try p.run() } catch { return nil }
        p.waitUntilExit()
        guard p.terminationStatus == 0 else { return nil }
        let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        let parts = text.split(separator: "|", maxSplits: 1, omittingEmptySubsequences: false).map(String.init)
        guard parts.count == 2 else { return nil }
        if parts[0] != nativeArch {                     // e.g. an Intel Python on an Apple-Silicon Mac: slow, different packages
            appLog("python \(py) is \(parts[0]) but this Mac needs \(nativeArch): skipped")
            return nil
        }
        return parts[1].split(whereSeparator: { $0 == " " || $0 == "\n" || $0 == "\r" }).map(String.init)   // "" (everything present) -> []
    }

    /// Show the "install now?" question once and keep re-checking until the installer has finished.
    private func needInstall(_ reason: String) {
        phase = .needsInstall(reason)
        if !offeredInstall {
            offeredInstall = true
            let a = NSAlert()
            a.messageText = L("Нужно установить компоненты", "Setup is required")
            a.informativeText = reason + "\n\n" + L("Установить сейчас? Откроется Terminal с пошаговой установкой.",
                                                    "Install now? Terminal will open with a step-by-step installer.")
            a.addButton(withTitle: L("Установить", "Install"))
            a.addButton(withTitle: L("Позже", "Later"))
            NSApp.activate(ignoringOtherApps: true)
            if a.runModal() == .alertFirstButtonReturn { runInstaller() }
        }
        installTimer?.invalidate()
        installTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            DispatchQueue.global().async {
                guard let self = self, self.resolvePython() != nil else { return }
                DispatchQueue.main.async {
                    self.installTimer?.invalidate(); self.offeredInstall = false
                    self.phase = .starting; self.start()
                }
            }
        }
    }

    // MARK: macOS privacy: the project folder (e.g. ~/Downloads) may need the user's permission

    /// Reading a file here makes macOS ask "Map Animator would like to access files in ..." in the app's name.
    private func canReadProject() -> Bool {
        let fm = FileManager.default
        if !packaged {                                                               // development checkout
            return (try? String(contentsOfFile: appDir + "/.venv/pyvenv.cfg", encoding: .utf8)) != nil
                || !fm.fileExists(atPath: appDir + "/.venv")
        }
        // Creating the folders also makes macOS ask for access to Documents (in the app's name, once).
        accessFolder = nil
        do {
            for d in [dataDir, homeDir, homeDir + "/debug", homeDir + "/logs"] {
                try fm.createDirectory(atPath: d, withIntermediateDirectories: true)
            }
        } catch { accessFolder = homeDir; return false }
        // A local build uses the environment next to the sources (often inside ~/Downloads, which macOS protects).
        // Reading a file there is what makes macOS ask for permission; if it is refused we say so instead of
        // claiming that nothing is installed.
        if !fm.isExecutableFile(atPath: ownPython), let dev = devEnvironmentDir, fm.fileExists(atPath: dev + "/pyvenv.cfg"),
           (try? Data(contentsOf: URL(fileURLWithPath: dev + "/pyvenv.cfg"))) == nil {
            accessFolder = dev
            return false
        }
        return true
    }

    private var accessTimer: Timer?
    private var accessFolder: String?          // the folder macOS is blocking (shown to the user)

    /// The environment recorded at build time on this Mac (local builds only), e.g. ~/Downloads/project/.venv
    private var devEnvironmentDir: String? {
        guard let py = Bundle.main.object(forInfoDictionaryKey: "MapAnimatorDevEnvironment") as? String else { return nil }
        return ((py as NSString).deletingLastPathComponent as NSString).deletingLastPathComponent
    }

    private func needAccess() {
        let folder = accessFolder ?? (packaged ? homeDir : appDir)
        phase = .needsAccess(L("macOS не даёт приложению работать с папкой:\n\(folder)",
                               "macOS does not let the app use the folder:\n\(folder)"))
        accessTimer?.invalidate()
        accessTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            DispatchQueue.global().async {
                guard let self = self, self.canReadProject() else { return }
                DispatchQueue.main.async { self.accessTimer?.invalidate(); self.phase = .starting; self.start() }
            }
        }
    }

    func openPrivacySettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles") { NSWorkspace.shared.open(url) }
    }

    func runInstaller() {
        let script = appDir + "/install.command"
        // a file that came with a downloaded app is quarantined: strip that, otherwise Terminal refuses to run it
        let x = Process(); x.executableURL = URL(fileURLWithPath: "/usr/bin/xattr")
        x.arguments = ["-d", "com.apple.quarantine", script]; x.standardError = Pipe(); try? x.run(); x.waitUntilExit()
        try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script)
        NSWorkspace.shared.open(URL(fileURLWithPath: script))                       // opens in Terminal
    }

    func recheck() { phase = .starting; offeredInstall = true; start() }

    private func launchServer() {
        guard canReadProject() else { needAccess(); return }
        guard let py = resolvePython() else {
            needInstall(L("Не найден Python, в котором уже есть нужные пакеты (OpenCV, ffmpeg и др.). Приложение создаст для себя окружение: ваш Python будет использован как есть, он не устанавливается и не меняется, скачиваются только пакеты.",
                          "No Python with the required packages (OpenCV, ffmpeg, …) was found. The app will create an environment for itself: your Python is used as it is, it is not installed or changed, only the packages are downloaded."))
            return
        }
        pythonPath = py
        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath)
        p.arguments = [appDir + "/ui.py", "--no-window"]
        p.currentDirectoryURL = URL(fileURLWithPath: packaged ? dataDir : appDir)
        if packaged {                                    // tell ui.py where to keep things (see the table at the top)
            var e = env; e["MAP_ANIMATOR_DATA"] = dataDir; e["MAP_ANIMATOR_HOME"] = homeDir; p.environment = e
        }
        try? FileManager.default.createDirectory(atPath: (logPath as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
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
                    DispatchQueue.global().async {                      // why did it stop? permission / missing packages / other
                        let tail = (try? String(contentsOfFile: self.logPath, encoding: .utf8)) ?? ""
                        if tail.contains("Operation not permitted") || tail.contains("PermissionError") {
                            DispatchQueue.main.async { self.needAccess() }
                            return
                        }
                        let missing = self.missingModules()
                        DispatchQueue.main.async {
                            if missing == nil || !(missing ?? []).isEmpty {
                                let what = (missing ?? []).isEmpty ? L("Python не запускается.", "Python does not start.")
                                    : L("Не хватает пакетов: ", "Missing packages: ") + (missing ?? []).joined(separator: ", ")
                                self.needInstall(what)
                            } else {
                                self.phase = .failed(L("Сервер остановился с кодом \(proc.terminationStatus). Подробности в logs/ui_server.log",
                                                       "The server stopped with code \(proc.terminationStatus). Details: logs/ui_server.log"))
                            }
                        }
                    }
                }
            }
        }
        do { try p.run(); process = p } catch {
            phase = .failed(L("Не удалось запустить \(pythonPath):\n\(error.localizedDescription)", "Could not start \(pythonPath):\n\(error.localizedDescription)"))
            return
        }
        waitUntilReady()
    }

    private func waitUntilReady() {
        // The first start after an installation can take a minute or more: macOS inspects every freshly installed
        // library before Python may use it. So wait for up to 4 minutes, as long as the server process is alive.
        DispatchQueue.global().async { [weak self] in
            let begun = Date()
            while Date().timeIntervalSince(begun) < 240 {
                guard let self = self else { return }
                if self.isUp() {
                    self.appLog("server is up after \(Int(Date().timeIntervalSince(begun))) s")
                    DispatchQueue.main.async { self.phase = .ready }
                    return
                }
                if let p = self.process, !p.isRunning { return }     // it stopped: terminationHandler explains why
                Thread.sleep(forTimeInterval: 0.3)
            }
            self?.appLog("the server did not answer within 4 minutes")
            DispatchQueue.main.async { if self?.phase == .starting { self?.phase = .failed(L("Сервер не ответил за 4 минуты. Откройте «Диагностика» после запуска или файл logs/app.log.", "The server did not answer within 4 minutes. See logs/app.log.")) } }
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

extension Notification.Name { static let openDocs = Notification.Name("MapAnimatorOpenDocs") }

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
        // The page's "Documentation" button calls window.open("/docs"): show it in the app's own docs window.
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if navigationAction.request.url?.path == "/docs" {
                NotificationCenter.default.post(name: .openDocs, object: nil)
            }
            return nil
        }

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
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Group {
            switch backend.phase {
            case .starting:
                VStack(spacing: 12) {
                    ProgressView()
                    Text(L("Запуск Map Animator…", "Starting Map Animator…")).foregroundStyle(.secondary)
                    Text(L("Первый запуск после установки может занять около минуты: macOS проверяет новые файлы.",
                           "The first start after an installation can take about a minute: macOS is checking the new files."))
                        .font(.footnote).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 420)
                }
            case .ready:
                WebView(url: serverURL)
            case .needsAccess(let reason):
                VStack(spacing: 12) {
                    Image(systemName: "lock.shield").font(.system(size: 40)).foregroundStyle(.secondary)
                    Text(L("Нужно разрешение macOS", "macOS permission is needed")).font(.headline)
                    Text(reason).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    Text(L("Если появился запрос на доступ к папке — нажмите «Разрешить». Иначе откройте настройки и включите Map Animator в разделе «Полный доступ к диску» (или «Файлы и папки»). Приложение продолжит само.",
                           "If a prompt asks for access to the folder, press Allow. Otherwise open the settings and turn Map Animator on under Full Disk Access (or Files & Folders). The app continues by itself."))
                        .font(.footnote).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 520)
                    HStack {
                        Button(L("Открыть настройки", "Open Settings")) { backend.openPrivacySettings() }.keyboardShortcut(.defaultAction)
                        Button(L("Проверить снова", "Check again")) { backend.recheck() }
                    }
                }
                .padding(30)
            case .needsInstall(let reason):
                VStack(spacing: 12) {
                    Image(systemName: "shippingbox").font(.system(size: 40)).foregroundStyle(.secondary)
                    Text(L("Нужно установить компоненты", "Setup is required")).font(.headline)
                    Text(reason).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    Text(L("Установщик откроется в Terminal; после него приложение запустится само.",
                           "The installer opens in Terminal; the app starts by itself afterwards."))
                        .font(.footnote).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    HStack {
                        Button(L("Установить", "Install")) { backend.runInstaller() }.keyboardShortcut(.defaultAction)
                        Button(L("Проверить снова", "Check again")) { backend.recheck() }
                    }
                }
                .padding(30)
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
        .onReceive(NotificationCenter.default.publisher(for: .openDocs)) { _ in openWindow(id: "docs") }
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

/// Documentation window: the README rendered by the local server (single source of truth).
struct DocsView: View {
    @ObservedObject var backend = Backend.shared
    @State private var reload = UUID()          // changes when the page asks for the docs again (language may differ)

    var body: some View {
        Group {
            if backend.phase == .ready { WebView(url: serverURL.appendingPathComponent("docs")).id(reload) }
            else { ProgressView() }
        }
        .frame(minWidth: 640, minHeight: 480)
        .onReceive(NotificationCenter.default.publisher(for: .openDocs)) { _ in reload = UUID() }
    }
}

/// "Documentation" in the app menu (Map Animator → Documentation) and in the Help menu.
struct DocsCommands: Commands {
    @Environment(\.openWindow) private var openWindow

    var body: some Commands {
        CommandGroup(after: .appInfo) {
            Button(L("Документация", "Documentation")) { openWindow(id: "docs"); NotificationCenter.default.post(name: .openDocs, object: nil) }
                .keyboardShortcut("d", modifiers: [.command, .shift])
        }
        CommandGroup(replacing: .help) {
            Button(L("Документация Map Animator", "Map Animator Documentation")) { openWindow(id: "docs"); NotificationCenter.default.post(name: .openDocs, object: nil) }
                .keyboardShortcut("?", modifiers: .command)
        }
    }
}

@main
struct MapAnimatorApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        Window("Map Animator", id: "main") {
            ContentView()
        }
        .defaultSize(width: 1100, height: 900)

        Window(L("Документация", "Documentation"), id: "docs") {
            DocsView()
        }
        .defaultSize(width: 980, height: 800)
        .commands { DocsCommands() }
    }
}
