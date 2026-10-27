import AppKit
import Foundation

struct TestFailure: Error, CustomStringConvertible {
    let description: String
}

func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw TestFailure(description: message) }
}

func read(_ url: URL) -> String? {
    try? String(contentsOf: url, encoding: .utf8)
}

@main struct ChromeAccessTests {
    static func main() throws {
        let base = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let root = base.appendingPathComponent("Chrome")
        let destination = base.appendingPathComponent("copy")
        let files = FileManager.default
        try files.createDirectory(
            at: root.appendingPathComponent("Profile 1/Local Storage/leveldb"),
            withIntermediateDirectories: true)
        try files.createDirectory(at: destination, withIntermediateDirectories: true)
        try "cookies".write(to: root.appendingPathComponent("Profile 1/Cookies"), atomically: true, encoding: .utf8)
        try "log".write(
            to: root.appendingPathComponent("Profile 1/Local Storage/leveldb/000003.log"),
            atomically: true, encoding: .utf8)
        try "port".write(to: root.appendingPathComponent("DevToolsActivePort"), atomically: true, encoding: .utf8)
        try "stale".write(to: destination.appendingPathComponent("Cookies"), atomically: true, encoding: .utf8)
        try files.createDirectory(
            at: destination.appendingPathComponent("Local Storage/old"), withIntermediateDirectories: true)

        for bad in ["/etc/passwd", "../Messages", "Profile 1/../../x", "Profile 1//Cookies", "./Cookies", ""] {
            try expect((try? ChromeAccess.sources([bad], root: root)) == nil, "accepted \(bad)")
        }
        try expect((try? ChromeAccess.sources([], root: root)) == nil, "accepted an empty request")
        try expect(
            (try? ChromeAccess.destination(base.appendingPathComponent("missing").path)) == nil,
            "accepted a missing destination")
        try expect((try? ChromeAccess.destination("relative")) == nil, "accepted a relative destination")

        let request = AccessRequest(
            items: ["Profile 1/Cookies", "Profile 1/Local Storage", "Profile 1/Missing", "DevToolsActivePort"],
            destination: destination.path, reason: "Copy logins", requester: "/work/project")
        var asked: [String] = []
        let denied = ChromeAccess.handle(request, root: root) {
            asked.append($0)
            return .alertFirstButtonReturn
        }
        try expect(denied.decision == "deny" && asked.count == 1, "Deny did not deny")
        try expect(read(destination.appendingPathComponent("Cookies")) == "stale", "Deny copied data")
        try expect(asked[0].contains("Why: Copy logins") && asked[0].contains("From: /work/project"), asked[0])
        try expect(asked[0].contains("• Profile 1/Cookies") && asked[0].contains("To: \(destination.path)"), asked[0])

        let timedOut = ChromeAccess.handle(request, root: root) { _ in .abort }
        try expect(timedOut.decision == "timeout", "a dismissed dialog was not a timeout")
        try expect(read(destination.appendingPathComponent("DevToolsActivePort")) == nil, "timeout copied data")

        let allowed = ChromeAccess.handle(request, root: root) { _ in .alertSecondButtonReturn }
        try expect(allowed.decision == "allow", "Allow did not allow: \(allowed.decision)")
        try expect(
            allowed.copied == ["Cookies", "Local Storage", "DevToolsActivePort"] && allowed.failed.isEmpty,
            "copied \(allowed.copied), failed \(allowed.failed)")
        try expect(read(destination.appendingPathComponent("Cookies")) == "cookies", "Cookies not replaced")
        try expect(
            read(destination.appendingPathComponent("Local Storage/leveldb/000003.log")) == "log",
            "folder not copied")
        try expect(!files.fileExists(atPath: destination.appendingPathComponent("Local Storage/old").path),
            "stale folder content kept")
        try expect(read(destination.appendingPathComponent("DevToolsActivePort")) == "port", "file not copied")

        var invalidAsked = false
        let invalid = ChromeAccess.handle(
            AccessRequest(items: ["../x"], destination: destination.path, reason: "r", requester: "q"),
            root: root
        ) { _ in
            invalidAsked = true
            return .alertSecondButtonReturn
        }
        try expect(invalid.decision == "error" && !invalidAsked, "invalid request reached the dialog")

        let many = AccessRequest(
            items: (1...11).map { "Profile 1/item\($0)" }, destination: destination.path, reason: "r",
            requester: "q")
        let text = ChromeAccess.summary(many, root: root)
        try expect(text.contains("• Profile 1/item8") && !text.contains("item9"), text)
        try expect(text.contains("• and 3 more"), text)
        print("ok")
    }
}
