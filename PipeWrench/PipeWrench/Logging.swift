//
//  Logging.swift
//  PipeWrench
//
//  Created by Zachary Drayer on 10/1/22.
//

import Foundation

// MARK: - Logging

@objc(PWLogEgress)
public protocol LogEgress {
	func log(_ message: String)
}

@objc(PWLogIngest)
public final class LogIngest: NSObject, LogEgress {
	fileprivate var egressTargets = [LogEgress]()

	public func add(egressTarget: any LogEgress) {
		egressTargets.append(egressTarget)
	}

	public func removeAll() {
		egressTargets.removeAll()
	}

	public func log(_ message: String) {
		for target in egressTargets {
			target.log(message)
		}
	}
}

public final class ConsoleLogger: LogEgress {
	public func log(_ message: String) {
		print(message)
	}
}

// MARK: - Configuration

internal extension PipeWrench {
	func addLoggers() throws {
		let consoleLoggingRequested = ProcessInfo.processInfo.environment[PipeWrenchConstants.ConsoleLoggingEnabled] ?? "true"
		let consoleLoggingEnabled = (consoleLoggingRequested as NSString).boolValue

		if consoleLoggingEnabled {
			logger.add(egressTarget: ConsoleLogger())
		}
	func removeLoggers() {
		logger.removeAll()
	}
}
