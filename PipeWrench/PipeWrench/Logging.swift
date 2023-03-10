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

public final class FileLogger: LogEgress {
    let handle: FileHandle

    init(path: String) throws {
        try validatePathForLoggingStorage(path)
        handle = FileHandle(forWritingAtPath: path)!
        try handle.seekToEnd()
    }

    deinit {
        try? handle.close()
    }

    public func log(_ message: String) {
        try? handle.write(contentsOf: (message + "\n").data(using: .utf8)!)
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

        let tentativePath = ProcessInfo.processInfo.environment[PipeWrenchConstants.DiskLoggingPath]
        if let tentativePath = tentativePath {
            let fileLogger = try FileLogger(path: tentativePath)
            logger.add(egressTarget: fileLogger)
        }
    }

    func removeLoggers() {
        logger.removeAll()
    }
}
