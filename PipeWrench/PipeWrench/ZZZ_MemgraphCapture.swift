//
//  ZZZ_MemgraphCapture.swift
//  PipeWrench
//
//  Created by Zachary Drayer on 5/12/26.
//

import XCTest

/// Empty marker test that sorts last alphabetically.
/// The test observer detects this class and captures the
/// bundle-level memgraph while a test case context is still active.
public final class ZZZ_MemgraphCapture: XCTestCase {
    public func test_captureMemgraph() {}
}
