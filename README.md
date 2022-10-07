#### What Is This?
PipeWrench is an iOS library that can be linked into a [testing target](https://developer.apple.com/library/archive/documentation/DeveloperTools/Conceptual/testing_with_xcode/chapters/01-introduction.html#//apple_ref/doc/uid/TP40014132) that automatically runs `leaks` when a test fails, and when a test suite finishes running.

#### How Does It Work?
PipeWrench copies `NSTask.h` from the macOS SDK, and then shells out to the `leaks` command line tool to do all the heavy lifting.

Since iOS doesn't have `NSTask`, this PipeWrench is limited to running on the iOS Simulator.

#### Why Wouldn't I Use The Memory Graph In Xcode?
Can't press buttons manually when running automated tests on CI systems. Even if you could, it would be very tedious to have to press the same button all the time.

#### Integration?
This is a prototype. If you want to use PipeWrench, you'll have to add it as a submodule and deal with the xcodeproj manually.

#### Running the Prototype?
1. Open `App.xcodeproj`.
2. Hit ⌘U to run tests. When running with Xcode 13.2.1, you'll see something like this at the end of the test run:


```
Test Suite 'Selected tests' started at 2022-02-19 00:30:11.360
Test Suite 'AppTests.xctest' started at 2022-02-19 00:30:11.360
Test Suite 'AppTests' started at 2022-02-19 00:30:11.360
Test Case '-[AppTests.AppTests testExample]' started.
Test Case '-[AppTests.AppTests testExample]' passed (0.001 seconds).
Test Case '-[AppTests.AppTests testPerformanceExample]' started.
/Users/z/Desktop/App/AppTests/AppTests.swift:15: Test Case '-[AppTests.AppTests testPerformanceExample]' measured [Time, seconds] average: 0.000, relative standard deviation: 154.614%, values: [0.000258, 0.000029, 0.000023, 0.000021, 0.000022, 0.000021, 0.000020, 0.000020, 0.000021, 0.000022], performanceMetricID:com.apple.XCTPerformanceMetric_WallClockTime, baselineName: "", baselineAverage: , polarity: prefers smaller, maxPercentRegression: 10.000%, maxPercentRelativeStandardDeviation: 10.000%, maxRegression: 0.100, maxStandardDeviation: 0.100
Test Case '-[AppTests.AppTests testPerformanceExample]' passed (0.264 seconds).
Test Suite 'AppTests' passed at 2022-02-19 00:30:11.626.
	 Executed 2 tests, with 0 failures (0 unexpected) in 0.265 (0.266) seconds
Test Suite 'AppTests.xctest' passed at 2022-02-19 00:30:11.626.
	 Executed 2 tests, with 0 failures (0 unexpected) in 0.265 (0.266) seconds
Test Suite 'Selected tests' passed at 2022-02-19 00:30:11.627.
	 Executed 2 tests, with 0 failures (0 unexpected) in 0.265 (0.267) seconds
leaking :(
Process:         App [78941]
Path:            /Users/z/Library/Developer/CoreSimulator/Devices/87C655D7-8376-41BF-81D5-9CE1B66680EC/data/Containers/Bundle/Application/60DB5134-FD92-4EE6-87DB-ECD7746A47BD/App.app/App
Load Address:    0x102fd4000
Identifier:      App
Version:         ???
Code Type:       ARM64
Parent Process:  debugserver [78942]

Date/Time:       2022-02-19 00:30:12.040 -0800
Launch Time:     2022-02-19 00:30:10.689 -0800
OS Version:      iPhone OS 15.2 (19C51)
Report Version:  7
Analysis Tool:   /Applications/Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform/Library/Developer/CoreSimulator/Profiles/Runtimes/iOS.simruntime/Contents/Resources/RuntimeRoot/usr/bin/leaks
Analysis Tool Version:  iOS Simulator 15.2 (19C51)

Physical footprint:         12.5M
Physical footprint (peak):  12.5M
----

leaks Report Version: 4.0
Process 78941: 31480 nodes malloced for 4419 KB
Process 78941: 1 leak for 32 total leaked bytes.

    1 (32 bytes) ROOT LEAK: 0x6000017ebc40 [32]
````

which tells us there is one leak found, along with the memory address.