import AppKit
import Combine
import Foundation
import SwiftUI
import Vision

struct TestFailure: Error, CustomStringConvertible {
    let description: String
}

func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw TestFailure(description: message) }
}

@MainActor func waitUntil(_ condition: () -> Bool) async throws {
    for _ in 0..<500 {
        if condition() { return }
        try await Task.sleep(for: .milliseconds(5))
    }
    throw TestFailure(description: "Timed out waiting for the model")
}

@MainActor final class MockBridge {
    var requests: [([String: String], CheckedContinuation<Reply, Error>)] = []

    func call(_ payload: [String: String]) async throws -> Reply {
        try await withCheckedThrowingContinuation { requests.append((payload, $0)) }
    }

    func complete(_ index: Int, state: Snapshot = Snapshot()) {
        requests[index].1.resume(returning: Reply(
            ok: true, state: state, message: nil, launcher: nil, focus: nil
        ))
    }
}

func sampleSnapshot(used: Double = 25, selected: String = "work") -> Snapshot {
    var state = Snapshot()
    let usage = AccountUsage(
        status: "ok", daily: QuotaWindow(used_percent: used, resets_at: nil, state: "available"),
        weekly: QuotaWindow(used_percent: used, resets_at: nil, state: "available"),
        email: nil, plan: nil, fetched_at: used, message: ""
    )
    state.accounts = ["work", "personal"].map {
        Account(name: $0, chrome_profile: nil, saved_login: true, usage: usage)
    }
    state.selected = selected
    return state
}

@main struct AppModelTests {
    @MainActor static func main() {
        Task { @MainActor in
            let domain = "local.devinswitch.tests.\(UUID().uuidString)"
            let defaults = UserDefaults(suiteName: domain)!
            do {
                try await run(CommandLine.arguments[1], defaults: defaults)
                defaults.removePersistentDomain(forName: domain)
                print("Passed \(CommandLine.arguments[1])")
                exit(0)
            } catch {
                defaults.removePersistentDomain(forName: domain)
                print("Failed: \(error)")
                exit(1)
            }
        }
        RunLoop.main.run()
    }

    @MainActor static func run(_ scenario: String, defaults: UserDefaults) async throws {
        let bridge = MockBridge()
        let model = AppModel(defaults: defaults, bridge: bridge.call)
        switch scenario {
        case "quiet-polling":
            model.refresh()
            try expect(!model.working && !model.blocked, "Background polling must not disable actions")
            try await waitUntil { bridge.requests.count == 1 }
            model.refresh()
            await Task.yield()
            try expect(bridge.requests.count == 1, "State polls must not overlap")
            bridge.complete(0)
        case "unchanged-state":
            var updates = 0
            let observation = model.$snapshot.dropFirst().sink { _ in updates += 1 }
            model.refresh()
            try await waitUntil { bridge.requests.count == 1 }
            bridge.complete(0)
            try await Task.sleep(for: .milliseconds(40))
            try expect(updates == 0, "An unchanged snapshot must not redraw the UI")
            withExtendedLifetime(observation) {}
        case "external-default":
            model.snapshot = sampleSnapshot()
            model.focus = "work"
            model.autoRefreshUsage = false
            model.refresh()
            try await waitUntil { bridge.requests.count == 1 }
            bridge.complete(0, state: sampleSnapshot(selected: "personal"))
            try await waitUntil { model.snapshot.selected == "personal" }
            try expect(model.snapshot.selected == "personal", "A CLI switch must update the app's default on state refresh")
            try expect(model.focus == "work", "Updating the default must not interrupt account browsing")
            try expect(!model.working && !model.blocked, "An external switch must not leave account controls busy")
        case "usage-race":
            model.snapshot = sampleSnapshot()
            model.focus = "work"
            model.resuming = model.account
            var updates = 0
            let observation = model.$snapshot.dropFirst().sink { _ in updates += 1 }
            model.refresh()
            try await waitUntil { bridge.requests.count == 1 }
            model.refreshUsage(force: true)
            try await waitUntil { bridge.requests.count == 2 }
            model.refreshUsage(force: true)
            await Task.yield()
            try expect(bridge.requests.count == 2, "Usage requests must not overlap")
            bridge.complete(1, state: sampleSnapshot(used: 50, selected: "personal"))
            try await waitUntil { !model.refreshingUsage }
            try expect(updates == 1, "Usage must be published atomically, not once per account")
            try expect(model.snapshot == sampleSnapshot(used: 50), "Usage must not change the default profile")
            try expect(model.focus == "work" && model.resuming?.name == "work", "Refresh must preserve navigation and sheets")
            bridge.complete(0, state: sampleSnapshot())
            try await waitUntil { bridge.requests.count == 3 }
            try expect(model.snapshot == sampleSnapshot(used: 50), "A late state poll must not roll back fresh usage")
            bridge.complete(2, state: sampleSnapshot(used: 50))
            try await Task.sleep(for: .milliseconds(40))
            try expect(updates == 1, "Unchanged usage must not redraw the UI")
            withExtendedLifetime(observation) {}
        case "action-race":
            model.snapshot = sampleSnapshot()
            model.refresh()
            try await waitUntil { bridge.requests.count == 1 }
            model.perform(["action": "select", "account": "personal"])
            try await waitUntil { bridge.requests.count == 2 }
            try expect(model.working, "User actions must still block duplicate actions")
            bridge.complete(1, state: sampleSnapshot(selected: "personal"))
            try await waitUntil { !model.working && bridge.requests.count == 3 }
            bridge.complete(0, state: sampleSnapshot())
            try await waitUntil { bridge.requests.count == 4 }
            try expect(model.snapshot.selected == "personal", "A late poll must not roll back a user action")
            bridge.complete(2, state: sampleSnapshot(selected: "personal"))
            try await waitUntil { !model.refreshingUsage }
            bridge.complete(3, state: sampleSnapshot(selected: "personal"))
            try await waitUntil { bridge.requests.count == 5 }
            bridge.complete(4, state: sampleSnapshot(selected: "personal"))
        case "errors":
            model.snapshot = sampleSnapshot()
            model.refreshUsage(force: true)
            try await waitUntil { bridge.requests.count == 1 }
            bridge.requests[0].1.resume(throwing: BridgeError.message("Offline"))
            try await waitUntil { !model.refreshingUsage && bridge.requests.count == 2 }
            try expect(model.failed && model.message == "Offline", "Refresh failures must be visible")
            try expect(model.snapshot == sampleSnapshot(), "Errors must retain the last quota reading")
            bridge.requests[1].1.resume(returning: Reply(
                ok: false, state: nil, message: "State unavailable", launcher: nil, focus: nil
            ))
            try await waitUntil { model.message == "State unavailable" }
            model.refreshUsage(force: true)
            model.refresh()
            try await waitUntil { bridge.requests.count == 4 }
            let usageIndex = bridge.requests[2].0["action"] == "usage" ? 2 : 3
            let stateIndex = usageIndex == 2 ? 3 : 2
            bridge.complete(usageIndex, state: sampleSnapshot(used: 60))
            try await waitUntil { !model.refreshingUsage }
            bridge.complete(stateIndex, state: sampleSnapshot())
            try await waitUntil { bridge.requests.count == 5 }
            bridge.complete(4, state: sampleSnapshot(used: 60))
            try expect(model.snapshot == sampleSnapshot(used: 60), "Refresh must recover after an error")
        case "preferences":
            defaults.set(false, forKey: "autoRefreshUsage")
            let paused = AppModel(defaults: defaults, bridge: bridge.call)
            try expect(!paused.autoRefreshUsage, "The saved auto refresh preference must be restored")
            paused.startRefreshing(stateInterval: 60, usageInterval: 60)
            try await waitUntil { bridge.requests.count == 1 }
            try expect(bridge.requests[0].0["action"] == "state", "Disabled auto refresh must not request usage on launch")
            bridge.complete(0)
            var changes = 0
            let observation = paused.$autoRefreshUsage.dropFirst().sink { _ in changes += 1 }
            paused.autoRefreshUsage = true
            try await waitUntil { bridge.requests.count == 2 }
            try expect(bridge.requests[1].0 == ["action": "usage", "force": "true"], "Enabling auto refresh must fetch immediately without a view callback")
            try expect(defaults.bool(forKey: "autoRefreshUsage") && changes == 1, "Preference changes must persist and update the UI")
            bridge.complete(1)
            try await waitUntil { !paused.refreshingUsage && bridge.requests.count == 3 }
            bridge.complete(2)
            withExtendedLifetime(observation) {}
        case "timers":
            try await timers(defaults: defaults)
        case "wake":
            try await wake(defaults: defaults)
        case "row-labels":
            try rowLabels(model: model)
        default:
            throw TestFailure(description: "Unknown scenario: \(scenario)")
        }
    }

    @MainActor static func rowLabels(model: AppModel) throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        model.snapshot = sampleSnapshot()

        func render(_ account: Account, revealed: Bool) throws -> CGImage {
            let renderer = ImageRenderer(content:
                AccountRowActions(account: account, revealed: revealed).environmentObject(model)
                    .padding(20).background(Palette.background).foregroundStyle(Palette.text)
                    .preferredColorScheme(.dark)
            )
            renderer.scale = 3
            guard let image = renderer.cgImage else {
                throw TestFailure(description: "Could not render the row actions")
            }
            return image
        }

        func text(in image: CGImage) throws -> String {
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.recognitionLanguages = ["en-US"]
            request.usesLanguageCorrection = false
            try VNImageRequestHandler(cgImage: image).perform([request])
            return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: " ")
        }

        let account = model.snapshot.accounts[1]
        let hidden = try render(account, revealed: false)
        let hiddenText = try text(in: hidden)
        try expect(!hiddenText.contains("Switch") && !hiddenText.contains("Resume"), "Quick-action captions must follow row hover visibility")
        for state in ["enabled", "selected", "signed-out", "busy"] {
            model.snapshot.selected = state == "selected" ? account.name : "work"
            model.working = state == "busy"
            let rowAccount = Account(
                name: account.name, chrome_profile: nil, saved_login: state != "signed-out", usage: account.usage
            )
            let image = try render(rowAccount, revealed: true)
            if CommandLine.arguments.count > 2 {
                let path = URL(fileURLWithPath: CommandLine.arguments[2]).appendingPathComponent("row-actions-\(state).png")
                try NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:])!.write(to: path)
                print("Rendered \(path.path)")
            }
            let labels = try text(in: image)
            try expect(labels.contains("Switch") && labels.contains("Resume"), "Hovered \(state) actions must visibly render Switch and Resume; found: \(labels)")
            try expect(image.width == hidden.width && image.height == hidden.height, "Showing action captions must not shift the row layout")
        }
    }

    @MainActor static func timers(defaults: UserDefaults) async throws {
        var requests: [[String: String]] = []
        var model: AppModel? = AppModel(defaults: defaults) { payload in
            requests.append(payload)
            return Reply(ok: true, state: Snapshot(), message: nil, launcher: nil, focus: nil)
        }
        weak var weakModel = model
        func usageCount() -> Int { requests.filter { $0["action"] == "usage" }.count }
        try expect(AppModel.usageRefreshInterval == 60, "Automatic usage refresh must run every minute")
        for index in 0..<40 {
            model?.message = "View update \(index)"
            model?.startRefreshing(stateInterval: 0.015, usageInterval: 0.08)
            try await Task.sleep(for: .milliseconds(5))
        }
        try expect(usageCount() >= 3, "Frequent state updates must not starve the usage timer")
        model?.autoRefreshUsage = false
        let pausedCount = usageCount()
        let stateCount = requests.count
        try await Task.sleep(for: .milliseconds(180))
        try expect(usageCount() == pausedCount, "Turning auto refresh off must stop periodic usage requests")
        try expect(requests.count > stateCount, "Session polling must continue when automatic usage is off")
        model?.refreshUsage(force: true)
        try await waitUntil { usageCount() == pausedCount + 1 }
        model?.autoRefreshUsage = true
        try await waitUntil { usageCount() >= pausedCount + 2 }
        try expect(requests.filter { $0["action"] == "usage" }.allSatisfy { $0["force"] == "true" }, "Minute ticks must bypass the quota cache")
        model = nil
        try await waitUntil { weakModel == nil }
        let finalCount = requests.count
        try await Task.sleep(for: .milliseconds(100))
        try expect(requests.count == finalCount, "Refresh subscriptions must end with the model")
    }

    @MainActor static func wake(defaults: UserDefaults) async throws {
        var usageCount = 0
        let model = AppModel(defaults: defaults) { payload in
            if payload["action"] == "usage" { usageCount += 1 }
            return Reply(ok: true, state: Snapshot(), message: nil, launcher: nil, focus: nil)
        }
        model.refreshAfterActivation()
        try await waitUntil { usageCount == 1 && !model.refreshingUsage }
        model.refreshAfterActivation()
        try await Task.sleep(for: .milliseconds(40))
        try expect(usageCount == 1, "Reactivating too soon must not flood the quota service")
        model.refreshAfterActivation(now: Date().addingTimeInterval(61))
        try await waitUntil { usageCount == 2 && !model.refreshingUsage }
        model.autoRefreshUsage = false
        model.refreshAfterActivation(now: Date().addingTimeInterval(120))
        try await Task.sleep(for: .milliseconds(40))
        try expect(usageCount == 2, "Wake refresh must respect the auto refresh setting")
    }
}
