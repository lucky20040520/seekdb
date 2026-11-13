/* Test-only LD_PRELOAD library. No error is injected and no database file is
 * edited. A requested syscall boundary writes evidence, then waits for the
 * controller to kill the database process/container. Never ship this image.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

static void stop_at(const char *phase, const char *path) {
    int saved = errno;
    int fd = syscall(SYS_openat, AT_FDCWD, "/data/killpoint.control", O_RDONLY, 0);
    if (fd < 0) { errno = saved; return; }
    char command[128] = {0};
    ssize_t size = syscall(SYS_read, fd, command, sizeof(command)-1);
    syscall(SYS_close, fd);
    if (size <= 0) { errno = saved; return; }
    command[strcspn(command, "\r\n")] = 0;
    if (strcmp(command, phase)) { errno = saved; return; }
    /* Consume the command so a subsequent recovery is not stopped again. */
    syscall(SYS_unlinkat, AT_FDCWD, "/data/killpoint.control", 0);
    fd = syscall(SYS_openat, AT_FDCWD, "/data/killpoint.reached", O_CREAT|O_TRUNC|O_WRONLY, 0600);
    char evidence[2048];
    int length = snprintf(evidence, sizeof(evidence), "%s\npid=%ld\npath=%s\n", phase, (long)getpid(), path);
    if (fd < 0 || length < 0 || length >= (int)sizeof(evidence)) _exit(190);
    if (syscall(SYS_write, fd, evidence, length) != length) _exit(191);
    syscall(SYS_fsync, fd);
    syscall(SYS_close, fd);
    for (;;) { struct timespec delay = {1,0}; syscall(SYS_nanosleep, &delay, 0); }
}

static int data_path(const char *path) { return path && strncmp(path,"/data/",6)==0; }

int fsync(int fd) {
    static int (*real_fsync)(int);
    if (!real_fsync) real_fsync = dlsym(RTLD_NEXT,"fsync");
    char link[64], path[2048];
    snprintf(link,sizeof(link),"/proc/self/fd/%d",fd);
    ssize_t length = readlink(link,path,sizeof(path)-1);
    if (length >= 0) {
        path[length]=0;
        if (data_path(path)) {
            if (!strncmp(path+6,"rows-",5)) stop_at("before-rows-sync",path);
            if (!strncmp(path+6,"index-",6)) stop_at("before-index-sync",path);
            if (!strncmp(path+6,"catalog-",8)) stop_at("before-catalog-sync",path);
        }
    }
    return real_fsync(fd);
}

int rename(const char *oldpath, const char *newpath) {
    static int (*real_rename)(const char*,const char*);
    if (!real_rename) real_rename = dlsym(RTLD_NEXT,"rename");
    int catalog = newpath && !strcmp(newpath,"/data/catalog.json");
    if (catalog) stop_at("before-catalog-rename",oldpath);
    int result = real_rename(oldpath,newpath);
    if (!result && catalog) stop_at("after-catalog-rename",newpath);
    return result;
}

static void before_delete(const char *path) {
    if (data_path(path) && (!strncmp(path+6,"rows-",5) || !strncmp(path+6,"index-",6)))
        stop_at("before-collect-unlink",path);
}

int unlink(const char *path) {
    static int (*real_unlink)(const char*);
    if (!real_unlink) real_unlink=dlsym(RTLD_NEXT,"unlink");
    before_delete(path);
    return real_unlink(path);
}

int unlinkat(int dirfd, const char *path, int flags) {
    static int (*real_unlinkat)(int,const char*,int);
    if (!real_unlinkat) real_unlinkat=dlsym(RTLD_NEXT,"unlinkat");
    before_delete(path);
    return real_unlinkat(dirfd,path,flags);
}

int remove(const char *path) {
    static int (*real_remove)(const char*);
    if (!real_remove) real_remove=dlsym(RTLD_NEXT,"remove");
    before_delete(path);
    return real_remove(path);
}
