//
//  PipeWrench.swift
//  PipeWrench
//
//  Created by Zachary Drayer on 1/29/22.
//

import XCTest

@objc(PWPipeWrench)
public final class PipeWrench: NSObject {
    private var isRunning = false

    private var memgraphDirectory: String!
    private var memgraphNames = Set<String>()

    private lazy var asXCTestObservation: Bool = {
        let asTestObservation = ProcessInfo.processInfo.environment[PipeWrenchConstants.AsXCTestObservation] as? NSString
        return asTestObservation?.boolValue ?? true
    }()

    internal var logger: LogIngest

    @objc
    public init(logger: LogIngest) {
        self.logger = logger
    }

    /// Start PipeWrench
    @objc public func start() throws {
        if !isRunning {
            try addLoggers()

            let path = ProcessInfo.processInfo.environment[PipeWrenchConstants.MemgraphRootDirectory] ?? "/tmp/PipeWrench/\(UUID().uuidString)/".replacingOccurrences(of: "-", with: "")
            try validatePathForMemgraphStorage(path)
            logger.log("using memgraph root: \(path)")
            memgraphDirectory = path

            isRunning = true

            if asXCTestObservation {
                XCTestObservationCenter.shared.addTestObserver(self)
            }
        }
    }

    @objc public func stop() {
        if isRunning {
            if asXCTestObservation {
                XCTestObservationCenter.shared.removeTestObserver(self)
            }

            isRunning = false

            removeLoggers()
        }
    }
}

extension PipeWrench: XCTestObservation {
    public func testCaseDidFinish(_ testCase: XCTestCase) {
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

    // MARK: - Helper Methods

    @discardableResult
    private func run(leaks arguments: [String], onRead: (String) -> Void = { _ in }, context: String = #function) -> Int32 {
        let task = NSTask()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/leaks")
        task.arguments = arguments

        do {
            try task.launchAndReturnError()
        } catch {
            logger.log("\(context) task.launch returns error \(error.localizedDescription)")
        }

        task.waitUntilExit()

        return task.terminationStatus
    }

    private func printValueFromPipe(_ string: String) {
        logger.log(string.replacingOccurrences(of: "\\n", with: "\n"))
    }

    private func read(from pipe: Pipe, named name: String, onRead: (String) -> Void) {
        let fileHandle = pipe.fileHandleForReading
        do {
            if let data = try fileHandle.readToEnd(),
               let string = String(data: data, encoding: .utf8)
            {
                onRead(string)
            }
        } catch {
            logger.log("caught exception reading \(name): \(error)")
        }
    }

    private func memgraphLocation(for name: String) -> String {
        let canonicalName = name
            .replacingOccurrences(of: " ", with: "")
            .replacingOccurrences(of: ":", with: "")
        return memgraphDirectory + "/\(canonicalName).memgraph"
    }

    // MARK: - Methods That Do Things

    public func saveMemgraphToDisk(with name: String) {
        memgraphNames.insert(name)
        let memgraphPath = memgraphLocation(for: name)
        run(leaks: ["\(getpid())", "--outputGraph=\(memgraphPath)"])

        let attachment = XCTAttachment(contentsOfFile: URL(fileURLWithPath: memgraphPath))
        attachment.name = "Memory Graph of \(name)"
        attachment.lifetime = .keepAlways

        XCTContext.runActivity(named: "Generating Memgraph") { activity in
            activity.add(attachment)
        }
    }

    public func hasLeaks(fromMemgraph name: String) -> Bool {
        let arguments = [
            "\(memgraphLocation(for: name))",
        ]
        return run(leaks: arguments) == 1
    }

    public func printLeaks(fromMemgraph name: String) {
        let arguments = [
            "\(memgraphLocation(for: name))",
        ]
        run(leaks: arguments, onRead: printValueFromPipe(_:))
    }

    public func findAddressesForLeaks(fromMemgraph name: String) {
        let arguments = [
            "\(memgraphLocation(for: name))",
        ]

        // for addr in `leaks ~/Desktop/MemgraphTests.xctest.memgraph | grep LEAK | awk '{ print $6 }’`
        // do
        //     heap --addresses=all ~/Desktop/MemgraphTests.xctest.memgraph | grep $leak
        // done
        run(leaks: arguments) { string in
            for line in string.components(separatedBy: "\n") {
                if !line.contains("LEAK") {
                    continue
                }

                let regex = try? NSRegularExpression(pattern: "0x(\\d|\\w)*", options: [])
                let range = NSRange(location: 0, length: (line as NSString).length)
                regex?.enumerateMatches(in: line, options: [], range: range) { result, flags, stop in
                    if let result {
                        let result = (line as NSString).substring(with: result.range)
                        printClass(for: result, fromMemgraph: name)
                    }
                }
            }
        }
    }

    // heap --addresses=all ~/Desktop/MemgraphTests.xctest.memgraph | grep $leak
    private func printClass(for address: String, fromMemgraph name: String) {
        logger.log("heap for \(address) \(memgraphLocation(for: name))")

        let task = NSTask()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/heap")
        task.arguments = [
            "--addresses=all",
            "\(memgraphLocation(for: name))",
        ]

        task.launch()
        task.waitUntilExit()

        logger.log("addr \(address)")
    }
}
