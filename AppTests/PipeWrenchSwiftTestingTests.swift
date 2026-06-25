//
//  PipeWrenchSwiftTestingTests.swift
//  PipeWrench
//
//  Created by Zachary Drayer on 5/12/26.
//

import Testing
@testable import PipeWrench

/// Verify PipeWrench memgraph capture works under Swift Testing.
///
/// The atexit handler in PipeWrench captures a bundle-level memgraph after
/// all tests finish and writes it to the runner's attachments directory.
///
/// Run these from the App scheme's test action with `EnablePipeWrench=YES`
/// set in the test action's environment.

@Suite struct PipeWrenchSwiftTestingTests {

    @Test func testPipeWrenchStarted() {
        #expect(PipeWrench.shared?.isRunning == true)
    }

    @Test func testLeakDetection() {
        class Leaky { var ref: Leaky? }
        let a = Leaky()
        let b = Leaky()
        a.ref = b
        b.ref = a
        _fixLifetime(a)
        _fixLifetime(b)
    }
}
