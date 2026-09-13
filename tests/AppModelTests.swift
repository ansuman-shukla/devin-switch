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
        case "combined-quota":
            try combinedQuota()
        case "combined-quota-exhaustion":
            try combinedQuotaExhaustion()
        case "combined-quota-render":
            try await combinedQuotaRender(model: model, defaults: defaults)
        case "adaptive-layout":
            try await adaptiveLayout(model: model, defaults: defaults)
        case "workspace-controls":
            try await workspaceControls(model: model, defaults: defaults)
        case "navigation":
            model.snapshot = sampleSnapshot()
            model.focus = "personal"
            model.showAllAccounts()
            try expect(model.focus == nil && model.account == nil, "Back must return to the overview")
            try expect(model.snapshot == sampleSnapshot(), "Browsing must not switch the default or retarget sessions")
            try expect(bridge.requests.isEmpty, "Navigation must not invoke the CLI")
        case "rename-display":
            model.snapshot = sampleSnapshot()
            model.focus = "work"
            model.beginRenaming(model.account!)
            model.perform(["action": "rename_display", "account": "work", "display_name": "Work Account"])
            try await waitUntil { bridge.requests.count == 1 }
            try expect(bridge.requests[0].0["account"] == "work", "Rename must address the stable alias")
            var renamed = sampleSnapshot()
            renamed.accounts[0].display_name = "Work Account"
            bridge.complete(0, state: renamed)
            try await waitUntil { !model.working && bridge.requests.count == 2 }
            try expect(model.renaming == nil && model.renameError.isEmpty, "A successful rename must dismiss the editor")
            try expect(model.account?.label == "Work Account" && model.focus == "work", "Rename must update labels without losing account navigation")
            try expect(model.snapshot.selected == "work", "Rename must preserve the default")
            try expect(bridge.requests[1].0["action"] == "state", "Rename must not make network usage requests")
            bridge.complete(1, state: renamed)
            model.refreshUsage(force: true)
            try await waitUntil { bridge.requests.count == 3 }
            bridge.complete(2, state: sampleSnapshot(used: 50))
            try await waitUntil { !model.refreshingUsage && bridge.requests.count == 4 }
            try expect(model.account?.label == "Work Account", "A late usage result must not overwrite a display name")
            try expect(model.displayName(for: "work") == "Work Account", "Session badges must resolve the display name")
            try expect(model.displayName(for: "deleted") == "deleted", "Historical accounts must fall back to their aliases")
            bridge.complete(3, state: renamed)
        case "rename-errors":
            model.snapshot = sampleSnapshot()
            model.beginRenaming(model.snapshot.accounts[0])
            model.perform(["action": "rename_display", "account": "work", "display_name": "Work Account"])
            try await waitUntil { bridge.requests.count == 1 }
            bridge.requests[0].1.resume(returning: Reply(ok: false, state: nil, message: "Name rejected", launcher: nil, focus: nil))
            try await waitUntil { !model.working && bridge.requests.count == 2 }
            try expect(model.renameError == "Name rejected" && model.renaming?.name == "work", "Validation errors must stay visible in the editor")
            bridge.complete(1, state: sampleSnapshot())
            model.perform(["action": "rename_display", "account": "work", "display_name": "Work Account"])
            try await waitUntil { bridge.requests.count == 3 }
            bridge.requests[2].1.resume(throwing: BridgeError.message("Bridge unavailable"))
            try await waitUntil { !model.working && bridge.requests.count == 4 }
            try expect(model.renameError == "Bridge unavailable", "Transport errors must be visible inside the rename sheet")
            try expect(model.snapshot == sampleSnapshot(), "Failed renames must not change labels or logins")
            bridge.complete(3, state: sampleSnapshot())
        case "display-name-decoding":
            let json = """
            {"name":"work","chrome_profile":null,"saved_login":true,
             "usage":{"status":"sign_in","daily":{"state":"unknown"},"weekly":{"state":"unknown"},"message":""}}
            """
            var object = try JSONSerialization.jsonObject(with: Data(json.utf8)) as! [String: Any]
            let old = try JSONDecoder().decode(Account.self, from: Data(json.utf8))
            try expect(old.label == "work", "Existing metadata without a label must still decode")
            object["display_name"] = "Personal Lab"
            let renamed = try JSONDecoder().decode(Account.self, from: JSONSerialization.data(withJSONObject: object))
            try expect(renamed.label == "Personal Lab" && renamed.id == "work", "Display names must decode without changing row identity")
        default:
            throw TestFailure(description: "Unknown scenario: \(scenario)")
        }
    }

    static func quotaAccount(
        name: String = "work", daily: Double? = 25, weekly: Double? = 50,
        status: String = "ok", savedLogin: Bool = true, fetched: Double? = 1000,
        reset: Double? = 2000, dailyState: String = "available", plan: String = "Pro"
    ) -> Account {
        Account(name: name, chrome_profile: nil, saved_login: savedLogin, usage: AccountUsage(
            status: status, daily: QuotaWindow(used_percent: daily, resets_at: reset, state: dailyState),
            weekly: QuotaWindow(used_percent: weekly, resets_at: reset, state: "available"),
            email: nil, plan: plan, fetched_at: fetched, message: ""
        ))
    }

    static func combinedQuota() throws {
        let accounts = [
            quotaAccount(name: "first", daily: 25, weekly: 60),
            quotaAccount(name: "second", daily: 75, weekly: 20, plan: "Max"),
            quotaAccount(name: "stale", status: "stale"),
            quotaAccount(name: "weekly-only", daily: nil, weekly: 100, dailyState: "not_applicable")
        ]
        let daily = CombinedQuota(accounts: accounts, window: \.daily, now: 1000)
        let weekly = CombinedQuota(accounts: accounts, window: \.weekly, now: 1000)
        try expect(daily.remainingPercent == 50, "Combined daily quota must average account percentages, not add them above 100%")
        try expect(weekly.remainingPercent == 40, "Daily and weekly quota must retain separate averages, including exhausted accounts as zero")
        try expect(daily.includedCount == 2 && daily.unavailableCount == 1 && daily.notApplicableCount == 1, "Partial coverage must distinguish unavailable and inapplicable quotas")
        try expect(weekly.includedCount == 3 && weekly.notApplicableCount == 0, "Each quota window must have its own denominator")
        for account in [
            quotaAccount(status: "stale"), quotaAccount(status: "unavailable"), quotaAccount(status: "sign_in"),
            quotaAccount(savedLogin: false), quotaAccount(fetched: nil), quotaAccount(fetched: 879),
            quotaAccount(fetched: 1001), quotaAccount(fetched: .nan), quotaAccount(reset: nil),
            quotaAccount(reset: 1000), quotaAccount(reset: .infinity), quotaAccount(dailyState: "unavailable"),
            quotaAccount(daily: nil), quotaAccount(daily: -1), quotaAccount(daily: 101), quotaAccount(daily: .nan)
        ] {
            let result = CombinedQuota(accounts: [account], window: \.daily, now: 1000)
            try expect(result.remainingPercent == nil && result.unavailableCount == 1, "Unknown, stale, signed-out or invalid readings must not inflate global quota")
        }
        let boundary = CombinedQuota(accounts: [quotaAccount(fetched: 880)], window: \.daily, now: 1000)
        try expect(boundary.remainingPercent == 75, "The freshness boundary must match the CLI's 120-second cache window")
        let exhausted = CombinedQuota(accounts: [quotaAccount(daily: 100)], window: \.daily, now: 1000)
        let full = CombinedQuota(accounts: [quotaAccount(daily: 0)], window: \.daily, now: 1000)
        try expect(exhausted.remainingPercent == 0 && full.remainingPercent == 100, "Zero and full quota must be real values, not missing readings")
        let empty = CombinedQuota(accounts: [], window: \.daily, now: 1000)
        try expect(empty.remainingPercent == nil && empty.totalCount == 0, "An empty account list must not report free quota")
        let inapplicable = CombinedQuota(accounts: [accounts[3]], window: \.daily, now: 1000)
        try expect(inapplicable.remainingPercent == nil && inapplicable.notApplicableCount == 1 && inapplicable.unavailableCount == 0, "Inapplicable quotas must not be treated as exhausted or in need of refresh")
    }

    static func combinedQuotaExhaustion() throws {
        let available = quotaAccount(name: "available", daily: 20, weekly: 40)
        let blocked = [
            quotaAccount(name: "weekly-limit", daily: 25, weekly: 100),
            quotaAccount(name: "daily-limit", daily: 100, weekly: 50),
            quotaAccount(name: "both-limits", daily: 100, weekly: 100)
        ]
        for (window, expected) in [(\AccountUsage.daily, 40.0), (\AccountUsage.weekly, 30.0)] {
            for account in blocked {
                let solo = CombinedQuota(accounts: [account], window: window, now: 1000)
                try expect(solo.remainingPercent == 0 && solo.includedCount == 1, "An account at \(account.name) must contribute zero to both global windows")
                let mixed = CombinedQuota(accounts: [available, account], window: window, now: 1000)
                try expect(mixed.remainingPercent == expected, "Blocked accounts must contribute zero without shrinking the average's denominator")
                try expect(mixed.includedCount == 2 && mixed.unavailableCount == 0, "Exhaustion must remain a valid reading, not missing data")
            }
            let exhausted = CombinedQuota(accounts: blocked, window: window, now: 1000)
            try expect(exhausted.remainingPercent == 0 && exhausted.includedCount == 3, "When every account is blocked, both global meters must show zero")
        }
        let valid = quotaAccount().usage.daily
        let otherWindows = [
            QuotaWindow(used_percent: nil, resets_at: nil, state: "not_applicable"),
            QuotaWindow(used_percent: 100, resets_at: 2000, state: "not_applicable"),
            QuotaWindow(used_percent: 99.9, resets_at: 2000, state: "available"),
            QuotaWindow(used_percent: 100, resets_at: 2000, state: "unknown"),
            QuotaWindow(used_percent: nil, resets_at: 2000, state: "available"),
            QuotaWindow(used_percent: -1, resets_at: 2000, state: "available"),
            QuotaWindow(used_percent: 101, resets_at: 2000, state: "available"),
            QuotaWindow(used_percent: .nan, resets_at: 2000, state: "available"),
            QuotaWindow(used_percent: .infinity, resets_at: 2000, state: "available"),
            QuotaWindow(used_percent: 100, resets_at: nil, state: "available"),
            QuotaWindow(used_percent: 100, resets_at: 1000, state: "available"),
            QuotaWindow(used_percent: 100, resets_at: .nan, state: "available"),
            QuotaWindow(used_percent: 100, resets_at: .infinity, state: "available")
        ]
        for other in otherWindows {
            for (daily, weekly, window) in [(valid, other, \AccountUsage.daily), (other, valid, \AccountUsage.weekly)] {
                let account = Account(name: "partial", chrome_profile: nil, saved_login: true, usage: AccountUsage(
                    status: "ok", daily: daily, weekly: weekly, email: nil, plan: nil, fetched_at: 1000, message: ""
                ))
                let result = CombinedQuota(accounts: [account], window: window, now: 1000)
                if other.state == "not_applicable" || other.used_percent == 99.9 {
                    try expect(result.remainingPercent == 75 && result.includedCount == 1, "Inapplicable or non-exhausted windows must not block the other allowance")
                } else {
                    try expect(result.remainingPercent == nil && result.unavailableCount == 1, "Both applicable windows must be valid and unexpired before counting an account's quota")
                }
            }
        }
    }

    @MainActor static func combinedQuotaRender(model: AppModel, defaults: UserDefaults) async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let now = Date().timeIntervalSince1970
        let current = [
            quotaAccount(name: "work", daily: 25, weekly: 60, fetched: now, reset: now + 3600),
            quotaAccount(name: "personal", daily: 75, weekly: 20, fetched: now, reset: now + 7200)
        ]
        let weeklyLimited = quotaAccount(name: "weekly-limit", daily: 25, weekly: 100, fetched: now, reset: now + 3600)
        let dailyLimited = quotaAccount(name: "daily-limit", daily: 100, weekly: 50, fetched: now, reset: now + 3600)
        let stale = quotaAccount(name: "stale", status: "stale", fetched: now, reset: now + 3600)
        for (scenario, accounts, percentages) in [
            ("complete", current, ["50%", "60%"]),
            ("partial", current + [stale], ["50%", "60%"]),
            ("weekly-limit", [current[0], weeklyLimited], ["37.5%", "20%"]),
            ("daily-limit", [current[0], dailyLimited], ["37.5%", "20%"]),
            ("exhausted", [weeklyLimited, dailyLimited], ["0%"])
        ] {
            model.snapshot.accounts = accounts
            for width in [339.0, 895] {
                let renderer = ImageRenderer(content: GlobalQuotaPanel(width: width).environmentObject(model)
                    .frame(width: width).padding(16).background(Palette.background)
                    .foregroundStyle(Palette.text).preferredColorScheme(.dark))
                renderer.scale = 3
                guard let image = renderer.cgImage else { throw TestFailure(description: "Could not render global quota") }
                let labels = try recognizedText(in: image)
                for label in ["Global quota", "Daily left", "Weekly left", "averages", "accounts included", "either limit", "both windows"] + percentages {
                    try expect(labels.contains(label), "The \(scenario) global quota panel must keep \(label) visible at \(width) points; found: \(labels)")
                }
                if scenario == "partial" {
                    try expect(labels.contains("Partial readings") && labels.contains("excluded"), "Partial totals must visibly disclose excluded accounts")
                } else {
                    try expect(!labels.contains("Partial readings") && !labels.contains("Needs refresh"), "Exhausted accounts must not be mislabeled as missing or stale readings")
                }
                if CommandLine.arguments.count > 2 {
                    let path = URL(fileURLWithPath: CommandLine.arguments[2]).appendingPathComponent("global-quota-\(Int(width))-\(scenario).png")
                    try NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:])!.write(to: path)
                }
            }
        }
        model.snapshot.accounts = current
        model.snapshot.selected = "work"
        let image = try await workspaceImage(model: model, defaults: defaults, width: 1180, height: 820)
        let labels = try recognizedText(in: image)
        try expect(labels.contains("Global quota") && labels.contains("50%") && labels.contains("60%"), "The All accounts dashboard must display live combined quota")
        if CommandLine.arguments.count > 2 {
            let path = URL(fileURLWithPath: CommandLine.arguments[2]).appendingPathComponent("global-quota-overview.png")
            try NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:])!.write(to: path)
        }
    }

    @MainActor static func workspaceControls(model: AppModel, defaults: UserDefaults) async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        defaults.set(true, forKey: "sidebarVisible")
        model.snapshot = sampleSnapshot()
        let view = NSHostingView(rootView: ContentView().environmentObject(model).defaultAppStorage(defaults))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 620), styleMask: [.titled, .resizable], backing: .buffered, defer: false)
        window.contentView = view
        window.makeKeyAndOrderFront(nil)
        defer { window.orderOut(nil) }
        try await Task.sleep(for: .milliseconds(60))
        view.layoutSubtreeIfNeeded()
        func click(x: CGFloat, y: CGFloat, count: Int = 1) async throws {
            let location = view.convert(NSPoint(x: x, y: view.isFlipped ? y : view.bounds.height - y), to: nil)
            for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
                let event = NSEvent.mouseEvent(with: type, location: location, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber, context: nil, eventNumber: 0, clickCount: count, pressure: 1)!
                window.sendEvent(event)
            }
            try await Task.sleep(for: .milliseconds(240))
        }
        try await click(x: 192, y: 34)
        try expect(!defaults.bool(forKey: "sidebarVisible"), "Clicking the collapse control inside the sidebar must collapse it")
        try await click(x: 28, y: 174)
        try expect(model.focus == "work", "Account shortcuts must work with the sidebar collapsed")
        try await click(x: 135, y: 31)
        try expect(model.focus == nil, "The detail back button must return to all accounts with the sidebar collapsed")
        try expect(model.snapshot.selected == "work", "Back navigation must not change the default")
        try await click(x: 28, y: 34)
        try expect(defaults.bool(forKey: "sidebarVisible"), "Clicking the brand icon in the rail must reopen the sidebar")
        for (type, x) in [(NSEvent.EventType.leftMouseDown, 222.0), (.leftMouseDragged, 242.0), (.leftMouseDragged, 282.0), (.leftMouseUp, 282.0)] {
            let location = view.convert(NSPoint(x: x, y: 220), to: nil)
            let event = NSEvent.mouseEvent(with: type, location: location, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: 1)!
            window.sendEvent(event)
            try await Task.sleep(for: .milliseconds(30))
        }
        try expect(defaults.double(forKey: "sidebarWidth") >= 260, "Dragging the sidebar edge must resize it and save the width; got \(defaults.double(forKey: "sidebarWidth"))")
        try await click(x: defaults.double(forKey: "sidebarWidth") - 2, y: 220, count: 2)
        try expect(defaults.double(forKey: "sidebarWidth") == 224, "Double-clicking the resize handle must reset the sidebar width")
        window.setContentSize(NSSize(width: 640, height: 520))
        try await Task.sleep(for: .milliseconds(30))
        view.layoutSubtreeIfNeeded()
        try expect(view.bounds.width == 640 && view.bounds.height == 520, "The content must follow native window resizing")
        withExtendedLifetime(window) {}
    }

    @MainActor static func workspaceImage(model: AppModel, defaults: UserDefaults, width: CGFloat, height: CGFloat) async throws -> CGImage {
        let view = NSHostingView(rootView: ContentView().environmentObject(model).defaultAppStorage(defaults))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: height), styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = view
        view.frame = NSRect(x: 0, y: 0, width: width, height: height)
        try await Task.sleep(for: .milliseconds(30))
        view.layoutSubtreeIfNeeded()
        guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else {
            throw TestFailure(description: "Could not capture workspace bitmap")
        }
        view.cacheDisplay(in: view.bounds, to: bitmap)
        guard let image = bitmap.cgImage else { throw TestFailure(description: "Could not render workspace") }
        return image
    }

    @MainActor static func adaptiveLayout(model: AppModel, defaults: UserDefaults) async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        model.snapshot = sampleSnapshot()
        model.snapshot.accounts[0].display_name = "Personal Lab"
        let view = NSHostingView(rootView: ContentView().environmentObject(model).defaultAppStorage(defaults))
        try expect(view.fittingSize.width <= 640, "The window must shrink to 640 points without a fixed desktop-width layout")
        try expect(view.fittingSize.height <= 520, "The window must shrink to 520 points with scrolling content")
        for width in [640.0, 800, 1180, 1600] {
            for expanded in [false, true] {
                for preferred in [190.0, 224, 320] {
                    let layout = WorkspaceLayout(width: width, sidebarVisible: expanded, preferredSidebarWidth: preferred)
                    try expect(layout.mainWidth >= 379, "Resizing the sidebar must preserve room for the content")
                    try expect(layout.sidebarWidth <= 320, "Sidebar width must remain bounded")
                    try expect(expanded || layout.sidebarWidth == 56, "Collapsed mode must retain an icon rail")
                    try expect(layout.contentWidth > 0, "Content must have a positive proposed width")
                }
                defaults.set(expanded, forKey: "sidebarVisible")
                defaults.set(224, forKey: "sidebarWidth")
                for detail in [false, true] {
                    model.focus = detail ? "work" : nil
                    let image = try await workspaceImage(model: model, defaults: defaults, width: width, height: 620)
                    if CommandLine.arguments.count > 2 {
                        let path = URL(fileURLWithPath: CommandLine.arguments[2]).appendingPathComponent("workspace-\(Int(width))-\(expanded ? "expanded" : "rail")-\(detail ? "detail" : "overview").png")
                        try NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:])!.write(to: path)
                    }
                    let labels = try recognizedText(in: image)
                    try expect(labels.contains("accounts"), "The overview navigation must remain visible at every size, including collapsed detail views; found: \(labels)")
                    if detail { try expect(labels.contains("Personal Lab"), "The detail must show the display name") }
                }
            }
        }
        let compact = WorkspaceLayout(width: 640, sidebarVisible: true, preferredSidebarWidth: 320)
        try expect(compact.compactAccounts, "Small windows must use account cards instead of squeezing table columns")
        let wide = WorkspaceLayout(width: 1180, sidebarVisible: true, preferredSidebarWidth: 224)
        try expect(!wide.compactAccounts, "Wide windows must retain the account table")
        try expect(WorkspaceLayout.columns(width: 1500, minimum: 145, count: 3, spacing: 14).count == 3, "Summary cards must fill wide windows without empty columns")
        try expect(WorkspaceLayout.columns(width: 339, minimum: 230, count: 2, spacing: 16).count == 1, "Quota cards must stack in narrow details")
        model.focus = nil
        defaults.set(true, forKey: "sidebarVisible")
        defaults.set(320, forKey: "sidebarWidth")
        let image = try await workspaceImage(model: model, defaults: defaults, width: 640, height: 1300)
        let labels = try recognizedText(in: image)
        for label in ["Personal Lab", "DAILY LEFT", "WEEKLY LEFT", "Switch", "Resume"] {
            try expect(labels.contains(label), "Compact cards must keep \(label) visible; found: \(labels)")
        }
    }

    @MainActor static func recognizedText(in image: CGImage) throws -> String {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["en-US"]
        request.usesLanguageCorrection = false
        try VNImageRequestHandler(cgImage: image).perform([request])
        return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: " ")
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

        let account = model.snapshot.accounts[1]
        let hidden = try render(account, revealed: false)
        let hiddenText = try recognizedText(in: hidden)
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
            let labels = try recognizedText(in: image)
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
