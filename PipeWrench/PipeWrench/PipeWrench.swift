//
//  PipeWrench.swift
//  PipeWrench
//
//  Created by Zachary Drayer on 1/29/22.
//

import Foundation
import XCTest

@objc(PWPipeWrench)
public final class PipeWrench: NSObject {
    var isRunning = false

    var memgraphDirectory: String!
    var memgraphNames = Set<String>()
    var didCaptureBundleMemgraph = false

    lazy var asXCTestObservation: Bool = {
        let asTestObservation = ProcessInfo.processInfo.environment[PipeWrenchConstants.AsXCTestObservation] as? NSString
        return asTestObservation?.boolValue ?? true
    }()

    /// The running instance, set by start() when called from AutoStartLoader +load.
    private(set) static var shared: PipeWrench?

    internal var logger: LogIngest

    @objc
    public init(logger: LogIngest) {
        self.logger = logger
    }

    /// Start PipeWrench
    @objc public func start() throws {
        if !isRunning {
            try addLoggers()

            let path = ProcessInfo.processInfo.environment[PipeWrenchConstants.MemgraphRootDirectory] ?? "/tmp/PipeWrench/\(UUID().uuidString)/".replacingOccurrences(of: "-", with: "")
            try validatePathForMemgraphStorage(path)
            logger.log("using memgraph root: \(path)")
            memgraphDirectory = path

            isRunning = true
            Self.shared = self

            if asXCTestObservation {
                XCTestObservationCenter.shared.addTestObserver(self)
            }

            Self.registerAtExitHandler()
        }
    }

    @objc public func stop() {
        if isRunning {
            if asXCTestObservation {
                XCTestObservationCenter.shared.removeTestObserver(self)
            }

            isRunning = false

            removeLoggers()
        }
    }
}
