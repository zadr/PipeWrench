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
	@objc(sharedWrench)
	public static let shared = PipeWrench()
	private var isRunning = false

	@objc public func start() {
		if !isRunning {
			isRunning = true
			XCTestObservationCenter.shared.addTestObserver(self)
		}
	}

	@objc public func stop() {
		if isRunning {
			XCTestObservationCenter.shared.removeTestObserver(self)
			isRunning = false
		}
	}
}

extension PipeWrench: XCTestObservation {
	public func testCase(_ testCase: XCTestCase, didRecord expectedFailure: XCTExpectedFailure) {
		let location = expectedFailure.issue.sourceCodeContext.location!
		let name = "\(location.fileURL.lastPathComponent):\(location.lineNumber)"
		saveMemgraphToDisk(for: name)

		if hasLeaksFromMemgraph(for: name) {
			print("leaking :(")

			printLeaksFromMemgraph(for: name)
		}
	}

	public func testBundleDidFinish(_ testBundle: Bundle) {
		let name = testBundle.bundleURL.lastPathComponent
		saveMemgraphToDisk(for: name)

		if hasLeaksFromMemgraph(for: name) {
			print("leaking :(")
			printLeaksFromMemgraph(for: name)
		}
	}

	// MARK: - Helper Methods

	private func read(from pipe: Pipe, named name: String) {
		let fileHandle = pipe.fileHandleForReading
		do {
			if let data = try fileHandle.readToEnd(),
			   let string = String(data: data, encoding: .utf8) {
				print(string.replacingOccurrences(of: "\\n", with: "\n"))
			}
		} catch {
			print("caught exception reading \(name): \(error)")
		}
	}

	private func memgraphLocation(for name: String) -> String {
		"/Users/z/Desktop/\(name).memgraph"
	}

	// MARK: - Methods That Do Things

	private func saveMemgraphToDisk(for name: String) {
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

//		read(from: stdout, named: "stdout")
	}

	private func hasLeaksFromMemgraph(for name: String) -> Bool {
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

	private func printLeaksFromMemgraph(for name: String) {
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

		read(from: stdout, named: "stdout")
	}
}
