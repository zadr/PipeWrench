//
//  AppUITests.swift
//  AppUITests
//
//  Created by Zachary Drayer on 1/29/22.
//

import XCTest

class AppUITests: XCTestCase {
    func testExample() throws {
        let app = XCUIApplication()
        app.launch()
    }

    func testLaunchPerformance() throws {
		measure(metrics: [XCTApplicationLaunchMetric()]) {
			XCUIApplication().launch()
		}
    }
}
