//
//  AutoStartLoader.m
//  PipeWrenchTesting
//
//  Created by Steve Sun on 2023-03-13.
//  Copyright © 2023 Square, Inc. All rights reserved.
//

#import "AutoStartLoader.h"
#import "PipeWrenchTesting-Swift.h"

static PWPipeWrench *__PWPipeWrench_staticInstance__ = nil;


@implementation AutoStartLoader

+ (void)load;
{
    BOOL enablePipeWrench = [NSProcessInfo.processInfo.environment[PipeWrenchConstants.Enabled] boolValue];

    NSLog(@"PipeWrench started... enable status: %d", enablePipeWrench);

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
