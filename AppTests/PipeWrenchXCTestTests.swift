//
//  PipeWrenchXCTestTests.swift
//  PipeWrench
//
//  Created by Zachary Drayer on 5/12/26.
//

import PipeWrench
import XCTest

/// Verify PipeWrench memgraph capture works under XCTest.
///
/// ZZZ_MemgraphCapture (in PipeWrench) fires automatically as the last
/// XCTestCase and captures the bundle-level memgraph. These tests just
/// exercise the bundle so there is something to capture.
///
/// Run these from the App scheme's test action with `EnablePipeWrench=YES`
/// set in the test action's environment.

final class PipeWrenchXCTestTests: XCTestCase {

    func testLeakSomething() {
        class Leaky { var ref: Leaky? }
        let a = Leaky()
        let b = Leaky()
        a.ref = b
        b.ref = a
        _fixLifetime(a)
        _fixLifetime(b)
    }

    func testNoLeak() {
        XCTAssertEqual(1 + 1, 2)
    }
}
