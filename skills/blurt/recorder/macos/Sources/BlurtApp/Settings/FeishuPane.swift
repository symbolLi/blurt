// Settings → Feishu: bind this Mac to the team's bug bitable. The app's own credentials (App Key / Secret of a custom
// app) — Bind checks them against the table and writes config export.feishu, which is what makes the main list's
// Export to Feishu appear (Scripts.feishuExportReady). No lark-cli, nothing to install: feishu_export.py talks to
// Feishu directly with these. The secret goes to the CLI on stdin and is kept only in this Mac's config file.
import BlurtCore
import SwiftUI

@MainActor
final class FeishuModel: ObservableObject {
    @Published var appKey = ""
    @Published var secret = ""
    @Published var table = ""
    @Published var owner = ""
    @Published var state = State.idle

    enum State: Equatable {
        case idle, binding
        case ok(table: String, proposer: Bool)
        case failed(String)
    }

    /// A secret is already saved: the field stays empty to keep it (bind treats empty as "the saved one").
    private var secretSaved = false

    init() {
        let f = Config.shared["export.feishu"] as? [String: Any] ?? [:]
        appKey = f["app_id"] as? String ?? ""
        owner = f["owner_name"] as? String ?? ""
        secretSaved = !(f["app_secret"] as? String ?? "").isEmpty
    }

    var secretPlaceholder: String {
        secretSaved ? T("settings.feishu.secret.kept") : T("settings.feishu.secret.placeholder")
    }

    var canBind: Bool {
        !appKey.trimmingCharacters(in: .whitespaces).isEmpty && (secretSaved || !secret.isEmpty)
    }

    var statusLine: String {
        switch state {
        case .idle:
            return Scripts.feishuExportReady ? T("settings.feishu.bound") : T("settings.feishu.bind.hint")
        case .binding:
            return T("settings.feishu.checking")
        case .ok(let table, let proposer):
            var s = String(format: T("settings.feishu.ok"), table)
            if !proposer { s += " " + T("settings.feishu.ok.noproposer") }   // only bound when a name was given
            return s
        case .failed(let error):
            return error
        }
    }

    /// Whatever was said doesn't hold for what's in the fields now.
    func edited() {
        if state != .idle, state != .binding { state = .idle }
    }

    func bind() {
        guard canBind, state != .binding else { return NSSound.beep() }
        state = .binding
        emit(["event": "feishu_bind"])
        Scripts.feishuBind(appKey: appKey.trimmingCharacters(in: .whitespaces),
                           secret: secret,
                           table: table.trimmingCharacters(in: .whitespacesAndNewlines),
                           owner: owner.trimmingCharacters(in: .whitespaces)) { [weak self] r in
            guard let self else { return }
            if (r["ok"] as? Bool) == true {
                self.state = .ok(table: (r["table"] as? String) ?? "", proposer: (r["proposer"] as? Bool) ?? true)
                self.secretSaved = true
                self.secret = ""
            } else {
                self.state = .failed((r["error"] as? String) ?? T("main.error.generic"))
            }
        }
    }
}

struct FeishuPane: View {
    @StateObject private var m = FeishuModel()

    var body: some View {
        SettingsPage(.feishu) {
            SettingsGroup("feishu.bind", footer: T("settings.feishu.how")) {
                SettingRow("feishu.app") {
                    TextField("cli_a…", text: $m.appKey)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 240)
                        .onChange(of: m.appKey) { _ in m.edited() }
                }
                SettingRow("feishu.secret") {
                    SecureField(m.secretPlaceholder, text: $m.secret)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 240)
                        .onChange(of: m.secret) { _ in m.edited() }
                }
                SettingRow("feishu.table") {
                    TextField("https://xxx.feishu.cn/base/…?table=tbl…", text: $m.table)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 300)
                        .onChange(of: m.table) { _ in m.edited() }
                }
                SettingRow("feishu.owner") {
                    TextField(m.owner.isEmpty ? T("settings.feishu.owner.placeholder") : m.owner, text: $m.owner)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 240)
                        .onChange(of: m.owner) { _ in m.edited() }
                }
                SettingRow("feishu.bind", detail: m.statusLine) {
                    Button(m.state == .binding ? T("settings.feishu.checking") : T("settings.feishu.bind.button")) { m.bind() }
                        .disabled(m.state == .binding || !m.canBind)
                }
            }
        }
    }
}
