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
