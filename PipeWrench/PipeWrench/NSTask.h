// Copied from Xcode macOS 12 headers and Xcode 13.2.1

/*	NSTask.h
	Copyright (c) 1996-2019, Apple Inc. All rights reserved.
*/

#import <Foundation/NSObject.h>
#import <Foundation/NSNotification.h>

@class NSArray<ObjectType>, NSDictionary<KeyType, ObjectType>, NSString;

NS_ASSUME_NONNULL_BEGIN

typedef NS_ENUM(NSInteger, NSTaskTerminationReason) {
	NSTaskTerminationReasonExit = 1,
	NSTaskTerminationReasonUncaughtSignal = 2
};

@interface NSTask : NSObject

// Create an NSTask which can be run at a later time
// An NSTask can only be run once. Subsequent attempts to
// run an NSTask will raise.
// Upon task death a notification will be sent
//   { Name = NSTaskDidTerminateNotification; object = task; }
//

- (instancetype)init NS_DESIGNATED_INITIALIZER;

// these methods can only be set before a launch
@property (nullable, copy) NSURL *executableURL;
@property (nullable, copy) NSArray<NSString *> *arguments;
@property (nullable, copy) NSDictionary<NSString *, NSString *> *environment; // if not set, use current
@property (nullable, copy) NSURL *currentDirectoryURL;

// standard I/O channels; could be either an NSFileHandle or an NSPipe
@property (nullable, retain) id standardInput;
@property (nullable, retain) id standardOutput;
@property (nullable, retain) id standardError;

// actions
- (BOOL)launchAndReturnError:(out NSError **_Nullable)error;

- (void)interrupt; // Not always possible. Sends SIGINT.
- (void)terminate; // Not always possible. Sends SIGTERM.

- (BOOL)suspend;
- (BOOL)resume;

// status
@property (readonly) int processIdentifier;
@property (readonly, getter=isRunning) BOOL running;

@property (readonly) int terminationStatus;
@property (readonly) NSTaskTerminationReason terminationReason;

/*
A block to be invoked when the process underlying the NSTask terminates.  Setting the block to nil is valid, and stops the previous block from being invoked, as long as it hasn't started in any way.  The NSTask is passed as the argument to the block so the block does not have to capture, and thus retain, it.  The block is copied when set.  Only one termination handler block can be set at any time.  The execution context in which the block is invoked is undefined.  If the NSTask has already finished, the block is executed immediately/soon (not necessarily on the current thread).  If a terminationHandler is set on an NSTask, the NSTaskDidTerminateNotification notification is not posted for that task.  Also note that -waitUntilExit won't wait until the terminationHandler has been fully executed.  You cannot use this property in a concrete subclass of NSTask which hasn't been updated to include an implementation of the storage and use of it.
*/
@property (nullable, copy) void (^terminationHandler)(NSTask *);

@property NSQualityOfService qualityOfService;

@end

@interface NSTask (NSTaskConveniences)

+ (nullable NSTask *)launchedTaskWithExecutableURL:(NSURL *)url arguments:(NSArray<NSString *> *)arguments error:(out NSError ** _Nullable)error terminationHandler:(void (^_Nullable)(NSTask *))terminationHandler;

- (void)waitUntilExit;
	// poll the runLoop in defaultMode until task completes

@end

@interface NSTask (NSDeprecated)

@property (nullable, copy) NSString *launchPath;
@property (copy) NSString *currentDirectoryPath;

- (void)launch;

+ (NSTask *)launchedTaskWithLaunchPath:(NSString *)path arguments:(NSArray<NSString *> *)arguments;
// convenience; create and launch

@end

FOUNDATION_EXPORT NSNotificationName const NSTaskDidTerminateNotification;

NS_ASSUME_NONNULL_END
