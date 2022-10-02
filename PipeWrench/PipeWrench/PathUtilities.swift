//
//  PathUtilities.swift
//  PipeWrench
//
//  Created by Zachary Drayer on 10/1/22.
//

import Foundation

internal func validatePathForMemgraphStorage(_ path: String) throws {
	let fileManager = FileManager()

	var isDirectory: ObjCBool = false
	if !fileManager.fileExists(atPath: path, isDirectory: &isDirectory) {
		let createIfDirectoryDoesNotExistEnvironment = ProcessInfo.processInfo.environment[PipeWrenchConstants.CreateMemgraphRootDirectory] ?? "false"
		let createIfDirectoryDoesNotExistValue = (createIfDirectoryDoesNotExistEnvironment as NSString).boolValue
		if !createIfDirectoryDoesNotExistValue {
			throw PipeWrenchDiskError.requestedPathDoesNotExist
		}

		try fileManager.createDirectory(atPath: path, withIntermediateDirectories: true)
	}

	if !isDirectory.boolValue {
		throw PipeWrenchDiskError.requestedPathIsNotADirectory
	}

	if !fileManager.isReadableFile(atPath: path) {
		throw PipeWrenchDiskError.requestedPathIsNotReadable
	}

	if !fileManager.isWritableFile(atPath: path) {
		throw PipeWrenchDiskError.requestedPathIsNotWritable
	}
}

internal func validatePathForLoggingStorage(_ path: String) throws {
	let fileManager = FileManager()

	if fileManager.fileExists(atPath: path, isDirectory: nil) {
		throw PipeWrenchDiskError.requestedPathExists
	}

	if !fileManager.isReadableFile(atPath: path) {
		throw PipeWrenchDiskError.requestedPathIsNotReadable
	}

	if !fileManager.isReadableFile(atPath: path) {
		throw PipeWrenchDiskError.requestedPathIsNotWritable
	}
}
