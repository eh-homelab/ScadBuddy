package main

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// detachedEnv marks the process --background starts, which mounts in the foreground.
const detachedEnv = "BAMBUDDY_FS_DETACHED"

// fuseConf is where FUSE says whether a user other than root may pass allow_other.
var fuseConf = "/etc/fuse.conf"

func pidPath(stateDir string) string { return filepath.Join(stateDir, "mount.pid") }
func logPath(stateDir string) string { return filepath.Join(stateDir, "mount.log") }

// userAllowOther reports whether conf has user_allow_other on a line of its own.
func userAllowOther(conf string) bool {
	data, err := os.ReadFile(conf)
	if err != nil {
		return false
	}
	for line := range strings.SplitSeq(string(data), "\n") {
		if strings.TrimSpace(line) == "user_allow_other" {
			return true
		}
	}
	return false
}

// allowOther decides FUSE allow_other: on whenever conf permits it, which Windows
// Explorer (\\wsl$) needs, unless --no-allow-other; --allow-other insists on it.
// root may always pass it.
func allowOther(cfg config, conf string) (bool, error) {
	switch {
	case cfg.NoAllowOther:
		return false, nil
	case os.Geteuid() == 0 || userAllowOther(conf):
		return true, nil
	case cfg.AllowOther:
		return false, fmt.Errorf("--allow-other needs user_allow_other uncommented in %s", conf)
	}
	return false, nil
}

// mounted reports whether dir is a mountpoint, from /proc/self/mountinfo, so a mount
// whose process is gone (every stat of it fails with ENOTCONN) still counts.
func mounted(dir string) (bool, error) {
	abs, err := filepath.Abs(dir)
	if err != nil {
		return false, err
	}
	if real, err := filepath.EvalSymlinks(abs); err == nil {
		abs = real
	}
	f, err := os.Open("/proc/self/mountinfo")
	if err != nil {
		return false, err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64<<10), 1<<20)
	for sc.Scan() {
		if fields := strings.Fields(sc.Text()); len(fields) > 4 && unescapeMountPath(fields[4]) == abs {
			return true, nil
		}
	}
	return false, sc.Err()
}

// unescapeMountPath undoes mountinfo's octal escapes, as in "a\040b" for "a b".
func unescapeMountPath(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '\\' && i+3 < len(s) {
			if n, err := strconv.ParseUint(s[i+1:i+4], 8, 8); err == nil {
				b.WriteByte(byte(n))
				i += 3
				continue
			}
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

// running returns the pid in stateDir's pid file while that process is alive and is a
// bambuddy-fs, so a pid the kernel reused for something else is never signalled.
func running(stateDir string) (int, bool) {
	data, err := os.ReadFile(pidPath(stateDir))
	if err != nil {
		return 0, false
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil || pid <= 0 {
		return 0, false
	}
	cmdline, err := os.ReadFile(fmt.Sprintf("/proc/%d/cmdline", pid))
	if err != nil {
		return 0, false
	}
	argv0, _, _ := bytes.Cut(cmdline, []byte{0})
	if !strings.HasPrefix(filepath.Base(string(argv0)), "bambuddy-fs") {
		return 0, false
	}
	return pid, true
}

// writePid records this process in stateDir; the returned func removes the file while
// it still names this process.
func writePid(stateDir string) (func(), error) {
	pid := strconv.Itoa(os.Getpid())
	if err := os.WriteFile(pidPath(stateDir), []byte(pid+"\n"), 0o600); err != nil {
		return nil, err
	}
	return func() {
		if data, err := os.ReadFile(pidPath(stateDir)); err == nil && strings.TrimSpace(string(data)) == pid {
			_ = os.Remove(pidPath(stateDir))
		}
	}, nil
}

// background starts this command again detached, in its own session with its output
// appended to the state directory's mount.log after a banner, and returns once the
// mount is up, or prints what the log gained and fails.
func background(cfg config, args []string, out, errOut io.Writer) int {
	if up, _ := mounted(cfg.Mountpoint); up {
		fmt.Fprintf(errOut, "bambuddy-fs: already mounted at %s (bambuddy-fs umount first)\n", cfg.Mountpoint)
		return 1
	}
	if pid, ok := running(cfg.StateDir); ok {
		fmt.Fprintf(errOut, "bambuddy-fs: pid %d from an earlier mount is still running (bambuddy-fs umount stops it)\n", pid)
		return 1
	}
	if err := os.MkdirAll(cfg.StateDir, 0o700); err != nil {
		fmt.Fprintf(errOut, "bambuddy-fs: %v\n", err)
		return 1
	}
	exe, err := os.Executable()
	if err != nil {
		fmt.Fprintf(errOut, "bambuddy-fs: %v\n", err)
		return 1
	}
	logFile, err := os.OpenFile(logPath(cfg.StateDir), os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		fmt.Fprintf(errOut, "bambuddy-fs: %v\n", err)
		return 1
	}
	defer logFile.Close()
	start, _ := logFile.Seek(0, io.SeekEnd)
	fmt.Fprintf(logFile, "=== %s mount %s at %s\n", time.Now().Format(time.RFC3339), cfg.API, cfg.Mountpoint)

	cmd := exec.Command(exe, args...)
	cmd.Env = append(os.Environ(), detachedEnv+"=1")
	cmd.Stdout, cmd.Stderr = logFile, logFile
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		fmt.Fprintf(errOut, "bambuddy-fs: %v\n", err)
		return 1
	}
	exited := make(chan struct{})
	go func() {
		_ = cmd.Wait()
		close(exited)
	}()
	deadline := time.After(30 * time.Second)
	for {
		if up, _ := mounted(cfg.Mountpoint); up {
			fmt.Fprintf(out, "mounted %s at %s (pid %d, log %s)\n", cfg.API, cfg.Mountpoint, cmd.Process.Pid, logPath(cfg.StateDir))
			return 0
		}
		select {
		case <-exited:
			printSince(errOut, logPath(cfg.StateDir), start)
			fmt.Fprintln(errOut, "bambuddy-fs: the mount did not come up")
			return 1
		case <-deadline:
			fmt.Fprintf(errOut, "bambuddy-fs: not mounted after 30s; pid %d is still running (log %s)\n", cmd.Process.Pid, logPath(cfg.StateDir))
			return 1
		case <-time.After(100 * time.Millisecond):
		}
	}
}

func printSince(w io.Writer, path string, offset int64) {
	f, err := os.Open(path)
	if err != nil {
		return
	}
	defer f.Close()
	if _, err := f.Seek(offset, io.SeekStart); err == nil {
		_, _ = io.Copy(w, f)
	}
}

// umount unmounts and stops the mount's process. fusermount3 -u alone can leave it
// serving: on WSL, Windows reaches the mount through \\wsl$ from another mount
// namespace, whose copy keeps the FUSE connection open. So it waits for the process,
// then sends SIGTERM (it stops its retrier and exits; unsent writes are on disk), then
// SIGKILL as the last resort.
func umount(cfg config, out, errOut io.Writer) int {
	if up, err := mounted(cfg.Mountpoint); err != nil {
		fmt.Fprintf(errOut, "bambuddy-fs: %v\n", err)
		return 1
	} else if up {
		if err := fusermount(cfg.Mountpoint); err != nil {
			fmt.Fprintf(errOut, "bambuddy-fs: unmount %s: %v (is it busy? close what uses it)\n", cfg.Mountpoint, err)
			return 1
		}
	}
	if err := stop(cfg.StateDir, 10*time.Second, errOut); err != nil {
		fmt.Fprintf(errOut, "bambuddy-fs: %v\n", err)
		return 1
	}
	fmt.Fprintf(out, "unmounted %s\n", cfg.Mountpoint)
	return 0
}

func fusermount(dir string) error {
	tool := "fusermount3"
	if _, err := exec.LookPath(tool); err != nil {
		tool = "fusermount"
	}
	output, err := exec.Command(tool, "-u", dir).CombinedOutput()
	if err != nil && len(output) > 0 {
		return fmt.Errorf("%s: %s", tool, strings.TrimSpace(string(output)))
	}
	return err
}

// stop waits up to wait for the state directory's mount process to exit, at each of
// no signal, SIGTERM and SIGKILL.
func stop(stateDir string, wait time.Duration, errOut io.Writer) error {
	pid, ok := running(stateDir)
	if !ok {
		_ = os.Remove(pidPath(stateDir))
		return nil
	}
	for _, sig := range []syscall.Signal{0, syscall.SIGTERM, syscall.SIGKILL} {
		if sig != 0 {
			_ = syscall.Kill(pid, sig)
		}
		for end := time.Now().Add(wait); time.Now().Before(end); time.Sleep(100 * time.Millisecond) {
			if _, alive := running(stateDir); !alive {
				_ = os.Remove(pidPath(stateDir))
				return nil
			}
		}
		if sig == 0 {
			fmt.Fprintf(errOut, "bambuddy-fs: pid %d still running\n", pid)
		} else {
			fmt.Fprintf(errOut, "bambuddy-fs: pid %d still running after %v\n", pid, sig)
		}
	}
	return errors.New("pid " + strconv.Itoa(pid) + " would not exit")
}

// status reports the mount and its process; it fails unless both are there.
func status(cfg config, out io.Writer) int {
	up, err := mounted(cfg.Mountpoint)
	if err != nil {
		fmt.Fprintf(out, "bambuddy-fs: %v\n", err)
		return 1
	}
	pid, alive := running(cfg.StateDir)
	switch {
	case up && alive:
		fmt.Fprintf(out, "mounted at %s (pid %d)\n", cfg.Mountpoint, pid)
		return 0
	case up:
		fmt.Fprintf(out, "mounted at %s, but its process is gone (bambuddy-fs umount clears it)\n", cfg.Mountpoint)
	case alive:
		fmt.Fprintf(out, "not mounted (%s), but pid %d is still running (bambuddy-fs umount stops it)\n", cfg.Mountpoint, pid)
	default:
		fmt.Fprintf(out, "not mounted (%s)\n", cfg.Mountpoint)
	}
	return 1
}
