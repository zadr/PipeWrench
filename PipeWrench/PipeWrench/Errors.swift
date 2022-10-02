//
//  Errors.swift
//  PipeWrench
//
//  Created by Zachary Drayer on 10/1/22.
//

public enum PipeWrenchDiskError: Error {
	case requestedPathExists
	case requestedPathDoesNotExist
	case requestedPathIsNotADirectory
	case requestedPathIsNotReadable
	case requestedPathIsNotWritable
}
