import AppKit
import Foundation

/// A request from `ds` to copy items out of Chrome's data folder after the user allows it.
///
/// macOS 27 lets only Chrome and apps with Full Disk Access read that folder, and never prompts.
/// This app is the only thing given Full Disk Access: it shows the request in a dialog and
/// copies nothing unless the user clicks Allow.
struct AccessRequest: Decodable {
    let items: [String]
    let destination: String
    let reason: String
    let requester: String
}

struct AccessResult: Encodable {
    let decision: String
    var copied: [String] = []
    var failed: [String] = []
    var message: String = ""
}

struct AccessError: Error, CustomStringConvertible {
    let description: String
}

enum ChromeAccess {
    static let chromeData = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Google/Chrome")
    static let shownItems = 8

    /// Item paths relative to Chrome's folder; nothing absolute and nothing that leaves it.
    static func sources(_ items: [String], root: URL) throws -> [URL] {
        if items.isEmpty { throw AccessError(description: "No Chrome items were requested.") }
        return try items.map { item in
            let parts = item.split(separator: "/", omittingEmptySubsequences: false)
            if item.hasPrefix("/") || parts.contains(where: { $0.isEmpty || $0 == "." || $0 == ".." }) {
                throw AccessError(description: "Invalid Chrome item: \(item)")
            }
            return root.appendingPathComponent(item)
        }
    }

    static func destination(_ path: String) throws -> URL {
        var isDirectory: ObjCBool = false
        guard path.hasPrefix("/"), FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory),
              isDirectory.boolValue
        else { throw AccessError(description: "The destination folder does not exist: \(path)") }
        return URL(fileURLWithPath: path, isDirectory: true)
    }

    /// Copies each source into `destination` under its own name, replacing what was there.
    /// Missing items are skipped; folders keep whatever could be read while Chrome writes them.
    static func copy(_ sources: [URL], into destination: URL) -> (copied: [String], failed: [String]) {
        let files = FileManager.default
        var copied: [String] = []
        var failed: [String] = []
        for source in sources {
            let name = source.lastPathComponent
            var isDirectory: ObjCBool = false
            guard files.fileExists(atPath: source.path, isDirectory: &isDirectory) else { continue }
            let target = destination.appendingPathComponent(name)
            try? files.removeItem(at: target)
            if isDirectory.boolValue {
                if copyTree(source, to: target) { copied.append(name) } else { failed.append(name) }
            } else if (try? files.copyItem(at: source, to: target)) != nil {
                copied.append(name)
            } else {
                failed.append(name)
            }
        }
        return (copied, failed)
    }

    private static func copyTree(_ source: URL, to target: URL) -> Bool {
        let files = FileManager.default
        guard (try? files.createDirectory(at: target, withIntermediateDirectories: false)) != nil,
              let entries = try? files.contentsOfDirectory(at: source, includingPropertiesForKeys: nil)
        else { return false }
        for entry in entries {
            let child = target.appendingPathComponent(entry.lastPathComponent)
            let values = try? entry.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
            if values?.isDirectory == true, values?.isSymbolicLink != true {
                _ = copyTree(entry, to: child)
            } else {
                try? files.copyItem(at: entry, to: child)
            }
        }
        return true
    }

    static func summary(_ request: AccessRequest, root: URL) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        func short(_ path: String) -> String {
            path.hasPrefix(home + "/") ? "~" + path.dropFirst(home.count) : path
        }
        var lines = request.items.prefix(shownItems).map { "• \($0)" }
        if request.items.count > shownItems {
            lines.append("• and \(request.items.count - shownItems) more")
        }
        return """
            Why: \(request.reason)
            From: \(short(request.requester))

            Copy from \(short(root.path)):
            \(lines.joined(separator: "\n"))
            To: \(short(request.destination))
            """
    }

    /// Validates the request, asks the user, then copies only after Allow.
    static func handle(_ request: AccessRequest, root: URL, ask: (String) -> NSApplication.ModalResponse)
        -> AccessResult
    {
        let sources: [URL]
        let destination: URL
        do {
            sources = try self.sources(request.items, root: root)
            destination = try self.destination(request.destination)
        } catch {
            return AccessResult(decision: "error", message: "\(error)")
        }
        switch ask(summary(request, root: root)) {
        case .alertSecondButtonReturn:
            let outcome = copy(sources, into: destination)
            return AccessResult(decision: "allow", copied: outcome.copied, failed: outcome.failed)
        case .abort:
            return AccessResult(decision: "timeout")
        default:
            return AccessResult(decision: "deny")
        }
    }

    @MainActor static func ask(_ summary: String, timeout: TimeInterval) -> NSApplication.ModalResponse {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        app.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "A Devin agent wants to read your Chrome data"
        alert.informativeText = summary
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Deny")
        alert.addButton(withTitle: "Allow")
        alert.window.level = .floating
        let timer = Timer(timeInterval: timeout, repeats: false) { _ in
            MainActor.assumeIsolated { NSApp.abortModal() }
        }
        RunLoop.main.add(timer, forMode: .modalPanel)
        defer { timer.invalidate() }
        return alert.runModal()
    }

    static func write(_ result: AccessResult, to path: String) {
        let data = (try? JSONEncoder().encode(result)) ?? Data("{\"decision\":\"error\"}".utf8)
        FileManager.default.createFile(
            atPath: path, contents: data, attributes: [.posixPermissions: 0o600])
    }
}

#if !CHROME_ACCESS_TESTS
@main struct ChromeAccessApp {
    @MainActor static func main() {
        let arguments = CommandLine.arguments
        func value(_ flag: String) -> String? {
            arguments.firstIndex(of: flag).flatMap { $0 + 1 < arguments.count ? arguments[$0 + 1] : nil }
        }
        guard let requestPath = value("--request"), let resultPath = value("--result") else {
            FileHandle.standardError.write(Data("Usage: --request PATH --result PATH\n".utf8))
            exit(2)
        }
        let timeout = value("--timeout").flatMap(TimeInterval.init) ?? 120
        let result: AccessResult
        do {
            let data = try Data(contentsOf: URL(fileURLWithPath: requestPath))
            let request = try JSONDecoder().decode(AccessRequest.self, from: data)
            result = ChromeAccess.handle(request, root: ChromeAccess.chromeData) {
                ChromeAccess.ask($0, timeout: timeout)
            }
        } catch {
            result = AccessResult(decision: "error", message: "Unreadable request: \(error)")
        }
        ChromeAccess.write(result, to: resultPath)
    }
}
#endif
