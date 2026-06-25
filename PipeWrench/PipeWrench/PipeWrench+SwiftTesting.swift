//
//  PipeWrench+SwiftTesting.swift
//  PipeWrench
//
//  Created by Zachary Drayer on 5/12/26.
//

import Foundation

// MARK: - Post-Test Bundle Capture (non-XCTest runners)

extension PipeWrench {

    /// Capture a bundle-level memgraph after all tests finish.
    ///
    /// Swift Testing has no "all tests finished" observation hook, so we
    /// capture from an `atexit` handler once the process is shutting down.
    /// At that point the runner is gone, so we can't hand the memgraph to
    /// it — we deliver by copying the on-disk memgraph into whichever
    /// output directory the runner gave us:
    ///
    /// - `swift test`: the `--attachments-path` directory
    /// - xcodebuild / Bazel: the `PipeWrenchResultBundlePath` directory,
    ///   set by the test runner
    ///
    /// The memgraph is always written to disk at memgraphDirectory
    /// regardless of whether either output directory is configured.
    @objc public func captureBundleMemgraph() {
        guard !didCaptureBundleMemgraph else { return }
        didCaptureBundleMemgraph = true

        let bundleName = Bundle.main.executableURL?.lastPathComponent ?? "UnknownBundle"
        logger.log("Capturing bundle memgraph for \(bundleName)")

        let memgraphPath = captureMemgraph(named: bundleName)
        guard let memgraphPath else { return }

        // swift test
        if let path = Self.attachmentsPath {
            copyMemgraph(at: memgraphPath, named: "\(bundleName).memgraph", toDirectory: path)
        }

        // xcodebuild / Bazel (via env var from test runner)
        if let path = ProcessInfo.processInfo.environment[PipeWrenchConstants.ResultBundlePath] {
            copyMemgraph(at: memgraphPath, named: "\(bundleName).memgraph", toDirectory: path)
        }
    }

    private func copyMemgraph(at sourcePath: String, named name: String, toDirectory dir: String) {
        let dest = (dir as NSString).appendingPathComponent(name)
        do {
            try? FileManager.default.removeItem(atPath: dest)
            try FileManager.default.copyItem(atPath: sourcePath, toPath: dest)
            logger.log("Copied \(name) to \(dest)")
        } catch {
            logger.log("Failed to copy \(name) to \(dest): \(error)")
        }
    }

    /// Parse --attachments-path from process arguments (swift test).
    static let attachmentsPath: String? = {
        let args = ProcessInfo.processInfo.arguments
        for (i, arg) in args.enumerated() {
            if arg == "--attachments-path" || arg == "--experimental-attachments-path" {
                let next = i + 1
                if next < args.count { return args[next] }
            }
            for prefix in ["--attachments-path=", "--experimental-attachments-path="] {
                if arg.hasPrefix(prefix) { return String(arg.dropFirst(prefix.count)) }
            }
        }
        return nil
    }()

    /// Register an atexit handler to capture the bundle memgraph after
    /// all tests complete. Called from start().
    static func registerAtExitHandler() {
        atexit {
            PipeWrench.shared?.captureBundleMemgraph()
        }
    }
}
