//
//  PipeWrench.swift
//  PipeWrench
//
//  Created by Zachary Drayer on 1/29/22.
//

import Darwin
import Foundation
import XCTest

@objc(PWPipeWrench)
public final class PipeWrench: NSObject {
	private var isRunning = false

	private var memgraphDirectory: String!
	internal var memgraphNames = Set<String>()

	internal var logger: LogIngest

	@objc
	public init(logger: LogIngest) {
		self.logger = logger
	}

	@objc public func start() throws {
		if !isRunning {
			let path = ProcessInfo.processInfo.environment[PipeWrenchConstants.MemgraphRootDirectory] ?? FileManager.default.temporaryDirectory.absoluteString
			try validatePathForMemgraphStorage(path)
			memgraphDirectory = path

			try addLoggers()

			isRunning = true

			XCTestObservationCenter.shared.addTestObserver(self)
		}
	}

	@objc public func stop() {
		if isRunning {
			XCTestObservationCenter.shared.removeTestObserver(self)
			isRunning = false

			removeLoggers()
		}
	}
}

extension PipeWrench: XCTestObservation {
	public func testCase(_ testCase: XCTestCase, didRecord expectedFailure: XCTExpectedFailure) {
		let location = expectedFailure.issue.sourceCodeContext.location!
		let name = "\(location.fileURL.lastPathComponent):\(location.lineNumber)"
		saveMemgraphToDisk(with: name)

		if hasLeaks(fromMemgraph: name) {
			logger.log("test  completed: has memory leak")

			printLeaks(fromMemgraph: name)
		}
	}

	public func testBundleDidFinish(_ testBundle: Bundle) {
		let name = testBundle.bundleURL.lastPathComponent
		saveMemgraphToDisk(with: name)

		if hasLeaks(fromMemgraph: name) {
			logger.log("test bundle completed: has memory leak")

			printLeaks(fromMemgraph: name)
			findAddressesForLeaks(fromMemgraph: name)
		}
	}

	public func testCaseDidFinish(_ testCase: XCTestCase) {
		let eraseOnCompletionEnvironment = ProcessInfo.processInfo.environment[PipeWrenchConstants.MemgraphErasedAfterTestCompletion] ?? "false"
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

	private func printValueFromPipe(_ string: String) {
		logger.log(string.replacingOccurrences(of: "\\n", with: "\n"))
	}

	private func read(from pipe: Pipe, named name: String, onRead: (String) -> Void) {
		let fileHandle = pipe.fileHandleForReading
		do {
			if let data = try fileHandle.readToEnd(),
			   let string = String(data: data, encoding: .utf8) {
				onRead(string)
			}
		} catch {
			logger.log("caught exception reading \(name): \(error)")
		}
	}

	private func memgraphLocation(for name: String) -> String {
		memgraphDirectory + "/\(name).memgraph"
	}

	// MARK: - Methods That Do Things

	private func saveMemgraphToDisk(with name: String) {
		memgraphNames.insert(name)

		let task = NSTask()
		task.executableURL = URL(fileURLWithPath: "/usr/bin/leaks")
		task.arguments = [
			"\(getpid())",
			"--outputGraph=\(memgraphLocation(for: name))"
		]

		let (stderr, stdout, stdin) = (Pipe(), Pipe(), Pipe())
		task.standardError = stderr
		task.standardOutput = stdout
		task.standardInput = stdin

		task.launch()
		task.waitUntilExit()

		read(from: stdout, named: "stdout", onRead: { string in
			logger.log(string.replacingOccurrences(of: "\\n", with: "\n"))
		})
	}

	private func hasLeaks(fromMemgraph name: String) -> Bool {
		let task = NSTask()
		task.executableURL = URL(fileURLWithPath: "/usr/bin/leaks")
		task.arguments = [
			"\(memgraphLocation(for: name))"
		]

		let (stderr, stdout, stdin) = (Pipe(), Pipe(), Pipe())
		task.standardError = stderr
		task.standardOutput = stdout
		task.standardInput = stdin

		task.launch()
		task.waitUntilExit()

		return task.terminationStatus == 1
	}

	private func printLeaks(fromMemgraph name: String) {
		let task = NSTask()
		task.executableURL = URL(fileURLWithPath: "/usr/bin/leaks")
		task.arguments = [
			"\(memgraphLocation(for: name))"
		]

		let (stderr, stdout, stdin) = (Pipe(), Pipe(), Pipe())
		task.standardError = stderr
		task.standardOutput = stdout
		task.standardInput = stdin

		task.launch()
		task.waitUntilExit()

		read(from: stdout, named: "stdout", onRead: { string in
			logger.log(string.replacingOccurrences(of: "\\n", with: "\n"))
		})
	}

	private func findAddressesForLeaks(fromMemgraph name: String) {
		let task = NSTask()
		task.executableURL = URL(fileURLWithPath: "/usr/bin/leaks")
		task.arguments = [
			"\(memgraphLocation(for: name))"
		]

		let (stderr, stdout, stdin) = (Pipe(), Pipe(), Pipe())
		task.standardError = stderr
		task.standardOutput = stdout
		task.standardInput = stdin

		task.launch()
		task.waitUntilExit()

//		for addr in `leaks ~/Desktop/MemgraphTests.xctest.memgraph | grep LEAK | awk '{ print $6 }’`
//		do
//			heap --addresses=all ~/Desktop/MemgraphTests.xctest.memgraph | grep $leak
//		done
		read(from: stdout, named: "stdout", onRead: { string in
			for line in string.components(separatedBy: "\n") {
				if !line.contains("LEAK") {
					continue
				}

				let regex = try? NSRegularExpression(pattern: "0x(\\d|\\w)*", options: [])
				let range = NSRange(location: 0, length: (line as NSString).length )
				regex?.enumerateMatches(in: line, options: [], range: range) { result, flags, stop in
					if let result = result {
						let result = (line as NSString).substring(with: result.range)
						printClass(for: result, fromMemgraph: name)
					}
				}
			}
		})
	}

	// heap --addresses=all ~/Desktop/MemgraphTests.xctest.memgraph | grep $leak
	private func printClass(for address: String, fromMemgraph name: String) {
		logger.log("heap for \(address) \(memgraphLocation(for: name))")

		let task = NSTask()
		task.executableURL = URL(fileURLWithPath: "/usr/bin/heap")
		task.arguments = [
			"--addresses=all",
			"\(memgraphLocation(for: name))"
		]

		let (stderr, stdout, stdin) = (Pipe(), Pipe(), Pipe())
		task.standardError = stderr
		task.standardOutput = stdout
		task.standardInput = stdin

		task.launch()
		task.waitUntilExit()

		logger.log("addr \(address)")
		read(from: stdin, named: "stdin", onRead: { string in
			logger.log(string.replacingOccurrences(of: "\\n", with: "\n"))
		})
		read(from: stdout, named: "stdout", onRead: { string in
			logger.log(string.replacingOccurrences(of: "\\n", with: "\n"))
		})
		read(from: stderr, named: "stderr", onRead: { string in
			logger.log(string.replacingOccurrences(of: "\\n", with: "\n"))
		})
	}
}
