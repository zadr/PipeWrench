//
//  autostart.c
//  PipeWrench
//
//  Created by Zachary Drayer on 1/29/22.
//

#import "PipeWrench-Swift.h"

__attribute__((constructor))
static void autostart(void) {
	BOOL enablePipeWrench = [NSProcessInfo.processInfo.environment[@"EnablePipeWrench"] boolValue];
	if (enablePipeWrench) {
		// https://developer.apple.com/library/archive/documentation/Performance/Conceptual/ManagingMemory/Articles/MallocDebug.html
		setenv("MallocStackLogging", "1", 1);
		setenv("MallocStackLoggingNoCompact", "1", 1);
		setenv("MallocScribble", "1", 1);

		[PWPipeWrench.sharedWrench start];
	}
}
