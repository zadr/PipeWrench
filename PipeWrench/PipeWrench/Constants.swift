//
//  Constants.swift
//  PipeWrench
//
//  Created by Zachary Drayer on 10/1/22.
//

import Foundation

@objc @objcMembers public final class PipeWrenchConstants: NSObject {

	// MARK: - Enable / Disable

	///
	/// Environment variable to set if Pipe Wrench should be used to track leaks.
	///
	/// If not set, Pipe Wrench will not run, even when linked into a test target.
	///
	/// This value may be a `Boolean` (YES/NO, true/false), a `Number` (0/1) or a `String` of the preceeding values.
	/// Evaluated with truthiness as defined by NSNumber https://developer.apple.com/documentation/foundation/nsnumber/1410865-boolvalue
	public static let Enabled = "EnablePipeWrench"
}
