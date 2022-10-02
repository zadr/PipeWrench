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

	// MARK: - Memgraph Management

	///
	/// Environment variable to set for memgraph storage.
	///
	/// This value must be a `String` that represents a path on disk.
	/// If non-nil, Pipe Wrench will attempt to save files to this location.
	/// If this variable is not set, Pipe Wrench will write to a temporary directory.
	///
	/// If the directory does not exist, Pipe Wrench will not attempt to create it, and will throw a `PipeWrenchDirectoryError.requestedMemgraphPathDoesNotExist` error
	/// If an item exists at the requested path but it is not a directory, Pipe Wrench will throw a `PipeWrenchDirectoryError.requestedMemgraphPathIsNotADirectory` error
	/// If the directory is not readable, Pipe Wrench will throw a `PipeWrenchDirectoryError.requestedMemgraphPathIsNotReadable` error
	/// If the directory is not writable, Pipe Wrench will throw a `PipeWrenchDirectoryError.requestedMemgraphPathIsNotWritable` error
	public static let MemgraphRootDirectory = "MemgraphRootDirectory"

	///
	/// Environment variable to set if Pipe Wrench should remove memgraph files when tests complete.
	///
	/// If not set, Pipe Wrench will preserve memgraph files when tests end.
	///
	/// This value may be a `Boolean` (YES/NO, true/false), a `Number` (0/1) or a `String` of the preceeding values.
	public static let MemgraphErasedAfterTestCompletion = "MemgraphErasedAfterTestCompletion"

	// MARK: - Logging

	///
	/// Environment variable to set if Pipe Wrench should remove memgraph files when tests complete.
	///
	/// If not set, Pipe Wrench will log to console.
	///
	/// This value may be a `Boolean` (YES/NO, true/false), a `Number` (0/1) or a `String` of the preceeding values.
	public static let ConsoleLoggingEnabled = "ConsoleLoggingEnabled"

	///
	/// Environment variable to set for memgraph storage.
	///
	/// This value must be a `String` that represents a path on disk.
	/// If non-nil, Pipe Wrench will attempt to save files to this location.
	/// If this variable is not set, Pipe Wrench will write to a temporary directory.
	///
	/// If the file already exists, Pipe Wrench will not attempt to append or overwrite, and will throw a `PipeWrenchDiskError.requestedPathExists` error
	/// If the path is not readable, Pipe Wrench will throw a `PipeWrenchDiskError.requestedPathIsNotReadable` error
	/// If the path is not writable, Pipe Wrench will throw a `PipeWrenchDiskError.requestedPathIsNotWritable` error
	public static let DiskLoggingPath = "DiskLoggingPath"
}
