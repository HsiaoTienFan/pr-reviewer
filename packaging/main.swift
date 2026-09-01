// PR Reviewer — native macOS shell around the local FastAPI server.
//
// Starts the bundled server as a child process, waits for it to listen, then
// shows it in a WKWebView. Quitting the app stops the server.
//
// The PATH dance matters: GUI-launched apps inherit a minimal PATH that omits
// ~/.local/bin, and the server resolves the `claude` CLI via shutil.which().
// Without augmentedPATH() the LLM backend reports "claude CLI not found".

import Cocoa
import WebKit
import Darwin

let APP_NAME = "PR Reviewer"
let PORT_RANGE = 8712...8760
let STARTUP_TIMEOUT: TimeInterval = 420  // first run may download Python + deps

// MARK: - Environment helpers

func augmentedPATH() -> String {
    let home = NSHomeDirectory()
    var parts = [
        "\(home)/.local/bin",
        "\(home)/.cargo/bin",
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ]
    if let current = ProcessInfo.processInfo.environment["PATH"] {
        parts += current.split(separator: ":").map(String.init)
    }
    var seen = Set<String>()
    return parts.filter { seen.insert($0).inserted }.joined(separator: ":")
}

func findExecutable(_ name: String) -> String? {
    for dir in augmentedPATH().split(separator: ":") {
        let candidate = "\(dir)/\(name)"
        if FileManager.default.isExecutableFile(atPath: candidate) { return candidate }
    }
    return nil
}

/// True when something is already listening on 127.0.0.1:port.
func portIsOpen(_ port: Int) -> Bool {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    guard fd >= 0 else { return false }
    defer { close(fd) }
    var addr = sockaddr_in()
    addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = UInt16(port).bigEndian
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    return withUnsafePointer(to: &addr) { raw in
        raw.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
            Darwin.connect(fd, sa, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0
        }
    }
}

func appSupportDir() -> URL {
    let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
    let dir = base.appendingPathComponent(APP_NAME, isDirectory: true)
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    return dir
}

func logFileURL() -> URL {
    let dir = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Logs", isDirectory: true)
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    return dir.appendingPathComponent("\(APP_NAME).log")
}

func tailLog(lines: Int = 25) -> String {
    guard let text = try? String(contentsOf: logFileURL(), encoding: .utf8) else { return "" }
    return text.split(separator: "\n", omittingEmptySubsequences: false).suffix(lines).joined(separator: "\n")
}

func escapeHTML(_ s: String) -> String {
    s.replacingOccurrences(of: "&", with: "&amp;")
     .replacingOccurrences(of: "<", with: "&lt;")
     .replacingOccurrences(of: ">", with: "&gt;")
}

// MARK: - Status pages

func statusHTML(title: String, detail: String, spinner: Bool, log: String = "") -> String {
    let spin = spinner ? "<div class='spin'></div>" : "<div class='bang'>!</div>"
    let logBlock = log.isEmpty ? "" : "<pre>\(escapeHTML(log))</pre>"
    return """
    <!doctype html><html><head><meta charset='utf-8'><style>
    :root { color-scheme: light dark;
      --bg:#f6f8fa; --fg:#1f2328; --muted:#59636e; --card:#fff; --border:#d1d9e0; --accent:#0969da; }
    @media (prefers-color-scheme: dark) {
      :root { --bg:#0d1117; --fg:#f0f6fc; --muted:#9198a1; --card:#151b23; --border:#3d444d; --accent:#58a6ff; } }
    * { box-sizing:border-box }
    body { margin:0; height:100vh; display:flex; align-items:center; justify-content:center;
      background:var(--bg); color:var(--fg);
      font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif; }
    .card { text-align:center; padding:34px 40px; max-width:640px; }
    h1 { font-size:17px; margin:18px 0 6px; font-weight:600 }
    p { color:var(--muted); margin:0; font-size:13px }
    .spin { width:26px; height:26px; margin:0 auto; border-radius:50%;
      border:2.5px solid var(--border); border-top-color:var(--accent);
      animation:r .8s linear infinite }
    @keyframes r { to { transform:rotate(360deg) } }
    .bang { width:26px; height:26px; margin:0 auto; border-radius:50%; background:#cf222e;
      color:#fff; font-weight:700; line-height:26px }
    pre { text-align:left; margin:18px 0 0; padding:12px 14px; background:var(--card);
      border:1px solid var(--border); border-radius:8px; font-size:11px; color:var(--muted);
      max-height:260px; overflow:auto; white-space:pre-wrap; word-break:break-all }
    </style></head><body><div class='card'>
    \(spin)<h1>\(escapeHTML(title))</h1><p>\(escapeHTML(detail))</p>\(logBlock)
    </div></body></html>
    """
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var server: Process?
    var port = PORT_RANGE.lowerBound
    private var didLoadApp = false

    func applicationDidFinishLaunching(_: Notification) {
        buildMenu()
        buildWindow()
        show(statusHTML(title: "Starting \(APP_NAME)…",
                        detail: "Preparing the local server. The first launch can take a "
                              + "few minutes while Python and dependencies are installed.",
                        spinner: true))
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in self?.startServer() }
    }

    func applicationWillTerminate(_: Notification) { stopServer() }

    func applicationShouldTerminateAfterLastWindowClosed(_: NSApplication) -> Bool { true }

    // MARK: UI

    private func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = APP_NAME
        window.minSize = NSSize(width: 900, height: 600)
        window.center()
        window.setFrameAutosaveName("PRReviewerMainWindow")

        let config = WKWebViewConfiguration()
        webView = WKWebView(frame: window.contentLayoutRect, configuration: config)
        webView.autoresizingMask = [.width, .height]
        window.contentView = webView

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func show(_ html: String) {
        DispatchQueue.main.async { self.webView.loadHTMLString(html, baseURL: nil) }
    }

    private func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About \(APP_NAME)",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Open Server Log", action: #selector(openLog), keyEquivalent: "l")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide \(APP_NAME)",
                        action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit \(APP_NAME)",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r")
        viewItem.submenu = view
        main.addItem(viewItem)

        NSApp.mainMenu = main
    }

    @objc private func reload() {
        if didLoadApp { webView.reload() }
    }

    @objc private func openLog() {
        NSWorkspace.shared.open(logFileURL())
    }

    // MARK: Server lifecycle

    private func startServer() {
        guard let resources = Bundle.main.resourceURL?.appendingPathComponent("app", isDirectory: true),
              FileManager.default.fileExists(atPath: resources.path) else {
            show(statusHTML(title: "Bundle is incomplete",
                            detail: "Contents/Resources/app is missing. Rebuild with packaging/build_app.sh.",
                            spinner: false))
            return
        }
        guard let uv = findExecutable("uv") else {
            show(statusHTML(title: "uv is not installed",
                            detail: "PR Reviewer runs its server with uv. Install it from "
                                  + "https://docs.astral.sh/uv/ and reopen this app.",
                            spinner: false))
            return
        }

        port = PORT_RANGE.first { !portIsOpen($0) } ?? PORT_RANGE.lowerBound

        let logURL = logFileURL()
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let handle = try? FileHandle(forWritingTo: logURL)
        handle?.seekToEndOfFile()
        let stamp = ISO8601DateFormatter().string(from: Date())
        handle?.write("\n=== \(APP_NAME) starting \(stamp) on port \(port) ===\n".data(using: .utf8)!)

        let process = Process()
        process.executableURL = URL(fileURLWithPath: uv)
        process.arguments = ["run", "--project", resources.path, "--no-dev",
                             "uvicorn", "pr_reviewer.app:app",
                             "--host", "127.0.0.1", "--port", String(port)]
        process.currentDirectoryURL = resources

        var env = ProcessInfo.processInfo.environment
        env["PATH"] = augmentedPATH()
        env["HOME"] = NSHomeDirectory()
        // Keep the venv outside the bundle so /Applications stays read-only.
        env["UV_PROJECT_ENVIRONMENT"] = appSupportDir().appendingPathComponent("venv").path
        process.environment = env

        if let handle {
            process.standardOutput = handle
            process.standardError = handle
        }

        do {
            try process.run()
        } catch {
            show(statusHTML(title: "Could not start the server",
                            detail: error.localizedDescription, spinner: false, log: tailLog()))
            return
        }
        server = process

        let deadline = Date().addingTimeInterval(STARTUP_TIMEOUT)
        while Date() < deadline {
            if portIsOpen(port) {
                didLoadApp = true
                let url = URL(string: "http://127.0.0.1:\(port)/")!
                DispatchQueue.main.async { self.webView.load(URLRequest(url: url)) }
                return
            }
            if !process.isRunning {
                show(statusHTML(title: "The server stopped unexpectedly",
                                detail: "Exit code \(process.terminationStatus). Recent log output:",
                                spinner: false, log: tailLog()))
                return
            }
            Thread.sleep(forTimeInterval: 0.4)
        }
        show(statusHTML(title: "The server did not start in time",
                        detail: "Gave up after \(Int(STARTUP_TIMEOUT)) seconds. Recent log output:",
                        spinner: false, log: tailLog()))
    }

    private func stopServer() {
        server?.terminate()
        // uv spawns uvicorn as a child, so sweep anything still holding the port.
        let sweep = Process()
        sweep.executableURL = URL(fileURLWithPath: "/bin/sh")
        sweep.arguments = ["-c", "pids=$(/usr/sbin/lsof -ti tcp:\(port) 2>/dev/null); "
                               + "[ -n \"$pids\" ] && kill $pids 2>/dev/null; exit 0"]
        try? sweep.run()
        sweep.waitUntilExit()
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
