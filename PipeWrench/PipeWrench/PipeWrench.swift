//
//  PipeWrench.swift
//  PipeWrench
//
//  Created by Zachary Drayer on 1/29/22.
//

import Darwin
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
    public func testCaseWillStart(_ testCase: XCTestCase) {
        // ZZZ_MemgraphCapture is a marker test that sorts last. Capture
        // the bundle-level memgraph while a test case context is active.
        if testCase is ZZZ_MemgraphCapture,
           let bundleName = Bundle(for: type(of: testCase)).executableURL?.lastPathComponent
        {
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

    // MARK: - Helper Methods

    @discardableResult
    private func run(leaks arguments: [String], onRead: (String) -> Void = { _ in }, context: String = #function) -> Int32 {
        // Strip simulator DYLD vars so `leaks` doesn't respawn into the runtime
        // binary (DYLD_ROOT_PATH) or load XCTest-linked dylibs (DYLD_INSERT_LIBRARIES).
        var env = ProcessInfo.processInfo.environment
        env.removeValue(forKey: "DYLD_ROOT_PATH")
        env.removeValue(forKey: "DYLD_INSERT_LIBRARIES")

        return spawn("/usr/bin/leaks", arguments: arguments, environment: env, onRead: onRead, context: context)
    }

    /// Run `path` with `arguments` via POSIX `posix_spawn`, capturing
    /// stdout+stderr and returning its exit status. Simulator-only: iOS has no
    /// `NSTask`/`Process`, and child processes only work on the Simulator.
    @discardableResult
    private func spawn(_ path: String, arguments: [String], environment: [String: String], onRead: (String) -> Void = { _ in }, context: String = #function) -> Int32 {
        // Build NULL-terminated argv and "KEY=VALUE" envp arrays for posix_spawn.
        let argv: [UnsafeMutablePointer<CChar>?] = ([path] + arguments).map { strdup($0) } + [nil]
        defer { argv.forEach { free($0) } }

        let envp: [UnsafeMutablePointer<CChar>?] = environment.map { strdup("\($0)=\($1)") } + [nil]
        defer { envp.forEach { free($0) } }

        // Redirect the child's stdout + stderr into a pipe we can read.
        var fds: [Int32] = [0, 0]
        guard pipe(&fds) == 0 else {
            logger.log("\(context) pipe failed: \(String(cString: strerror(errno)))")
            return -1
        }
        let (readFD, writeFD) = (fds[0], fds[1])

        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        posix_spawn_file_actions_adddup2(&actions, writeFD, STDOUT_FILENO)
        posix_spawn_file_actions_adddup2(&actions, writeFD, STDERR_FILENO)
        posix_spawn_file_actions_addclose(&actions, readFD)
        defer { posix_spawn_file_actions_destroy(&actions) }

        var pid: pid_t = 0
        let rc = posix_spawn(&pid, path, &actions, nil, argv, envp)
        close(writeFD)
        guard rc == 0 else {
            logger.log("\(context) posix_spawn failed: \(String(cString: strerror(rc)))")
            close(readFD)
            return -1
        }

        let handle = FileHandle(fileDescriptor: readFD, closeOnDealloc: true)
        if let data = try? handle.readToEnd(), let string = String(data: data, encoding: .utf8) {
            onRead(string)
        }

        var status: Int32 = 0
        waitpid(pid, &status, 0)
        // Hand-rolled WIFEXITED / WEXITSTATUS; the C macros aren't imported.
        return (status & 0x7f) == 0 ? (status >> 8) & 0xff : -1
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

    @discardableResult
    public func saveMemgraphToDisk(with name: String) -> Bool {
        memgraphNames.insert(name)
        let memgraphPath = memgraphLocation(for: name)
        run(leaks: ["\(getpid())", "--outputGraph=\(memgraphPath)"])

        guard FileManager.default.fileExists(atPath: memgraphPath) else {
            logger.log("saveMemgraphToDisk: memgraph was not written to \(memgraphPath)")
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

        spawn("/usr/bin/heap", arguments: [
            "--addresses=all",
            "\(memgraphLocation(for: name))",
        ], environment: ProcessInfo.processInfo.environment)

        logger.log("addr \(address)")
    }
}
