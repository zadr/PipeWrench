//
//  AppUITestsLaunchTests.swift
//  AppUITests
//
//  Created by Zachary Drayer on 1/29/22.
//

import XCTest

class AppUITestsLaunchTests: XCTestCase {
    func testLaunch() throws {
        let app = XCUIApplication()
        app.launch()

        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = "Launch Screen"
        attachment.lifetime = .keepAlways

        add(attachment)
    }
}
