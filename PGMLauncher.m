#import <Cocoa/Cocoa.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <string.h>

static void fail(NSString *message) {
    [NSApplication sharedApplication];
    [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
    [NSApp activateIgnoringOtherApps:YES];
    NSAlert *alert = [NSAlert new];
    alert.messageText = @"三木的PGM回套软件无法启动";
    alert.informativeText = message;
    [alert addButtonWithTitle:@"好"];
    [alert runModal];
}
int main(int argc, char **argv) {
    @autoreleasepool {
        NSString *configPath = [[NSBundle mainBundle] pathForResource:@"Launcher" ofType:@"json"];
        NSData *data = configPath ? [NSData dataWithContentsOfFile:configPath] : nil;
        NSDictionary *config = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
        NSString *python = config[@"python"], *entry = config[@"entry"], *root = [entry stringByDeletingLastPathComponent];
        if (!python || !entry || ![[NSFileManager defaultManager] isExecutableFileAtPath:python] || ![[NSFileManager defaultManager] fileExistsAtPath:entry]) {
            fail(@"找不到已安装的程序或Python运行环境。请重新运行配套的安装三木的PGM回套软件.command安装入口。");
            return 1;
        }
        if (chdir(root.fileSystemRepresentation) != 0) {
            fail([NSString stringWithFormat:@"无法访问程序目录：%@", root]); return 1;
        }
        NSString *log = [root stringByAppendingPathComponent:@"runtime.log"];
        int fd = open(log.fileSystemRepresentation, O_CREAT|O_WRONLY|O_APPEND, 0644);
        if (fd >= 0) { dup2(fd, STDOUT_FILENO); dup2(fd, STDERR_FILENO); if (fd > 2) close(fd); }
        setenv("PYTHONUTF8", "1", 1);
        setenv("PYTHONUNBUFFERED", "1", 1);
        setenv("QT_QPA_PLATFORM", "cocoa", 1);
        unsetenv("PYTHONHOME");
        unsetenv("__PYVENV_LAUNCHER__");
        const char *args[] = {python.fileSystemRepresentation, "-u", entry.fileSystemRepresentation, NULL};
        execv(args[0], (char *const *)args);
        fail([NSString stringWithFormat:@"启动失败：%s\n日志：%@",strerror(errno),log]);
        return 1;
    }
}
