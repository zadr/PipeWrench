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
		throw PipeWrenchDiskError.requestedPathDoesNotExist
	}

	if !isDirectory.boolValue {
		throw PipeWrenchDiskError.requestedPathIsNotADirectory
	}

	if !fileManager.isReadableFile(atPath: path) {
		throw PipeWrenchDiskError.requestedPathIsNotReadable
	}

	if !fileManager.isReadableFile(atPath: path) {
		throw PipeWrenchDiskError.requestedPathIsNotWritable
	}
}

