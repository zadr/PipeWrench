//
//  PipeWrench+XCTest.swift
//  PipeWrench
//
//  Created by Zachary Drayer on 5/12/26.
//

import XCTest

// MARK: - XCTest Observation

extension PipeWrench: XCTestObservation {
    public func testCaseWillStart(_ testCase: XCTestCase) {
        // ZZZ_MemgraphCapture is a marker test that sorts last. Capture
        // the bundle-level memgraph while a test case context is active.
        if testCase is ZZZ_MemgraphCapture,
           let bundleName = Bundle(for: type(of: testCase)).executableURL?.lastPathComponent
        {
            didCaptureBundleMemgraph = true
            saveMemgraphToDisk(with: bundleName)
        }
    }

    public func testCaseDidFinish(_ testCase: XCTestCase) {
        guard !(testCase is ZZZ_MemgraphCapture) else { return }

        let name = testCase.name
        saveMemgraphToDisk(with: name)

        if hasLeaks(fromMemgraph: name) {
            logger.log("test bundle completed: has memory leak")

            printLeaks(fromMemgraph: name)
        } else {
            logger.log("It's good, no leaks detected! 👍")
        }

        let eraseOnCompletionEnvironment = ProcessInfo.processInfo.environment[PipeWrenchConstants.EraseMemgraphAfterTestCompletion] ?? "false"
        let eraseOnCompletionValue = (eraseOnCompletionEnvironment as NSString).boolValue
        guard eraseOnCompletionValue else {
            return
        }

        for name in memgraphNames {
            let fileManager = FileManager()
            try? fileManager.removeItem(atPath: memgraphLocation(for: name))
        }
    }

    @discardableResult
    @preconcurrency @MainActor
    public func saveMemgraphToDisk(with name: String) -> Bool {
        guard let memgraphPath = captureMemgraph(named: name) else {
            return false
        }

        let attachment = XCTAttachment(contentsOfFile: URL(fileURLWithPath: memgraphPath))
        attachment.name = "Memory Graph of \(name)"
        attachment.lifetime = .keepAlways

        XCTContext.runActivity(named: "Generating Memgraph") { activity in
            activity.add(attachment)
        }
        return true
    }
}
