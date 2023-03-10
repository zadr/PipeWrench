//
//  AutoStartLoader.m
//  PipeWrench
//
//  Created by Steve Sun on 2023-03-09.
//

#import "AutoStartLoader.h"
#import "PipeWrench-Swift.h"

static PWPipeWrench *__PWPipeWrench_staticInstance__ = nil;

@implementation AutoStartLoader

+ (void)load {
    BOOL enablePipeWrench = [NSProcessInfo.processInfo.environment[PipeWrenchConstants.Enabled] boolValue];
    if (enablePipeWrench) {
        // https://developer.apple.com/library/archive/documentation/Performance/Conceptual/ManagingMemory/Articles/MallocDebug.html
        setenv("MallocStackLogging", "1", 1);
        setenv("MallocStackLoggingNoCompact", "1", 1);
        setenv("MallocScribble", "1", 1);

        PWLogIngest *logger = [[PWLogIngest alloc] init];
        __PWPipeWrench_staticInstance__ = [[PWPipeWrench alloc] initWithLogger:logger];

        NSError *error = nil;
        BOOL started = [__PWPipeWrench_staticInstance__ startAndReturnError:&error];
        if (!started) {
            NSCAssert(NO, @"Unable to start Pipe Wrench leak detection: %@", error);
        }
    }
}

@end
