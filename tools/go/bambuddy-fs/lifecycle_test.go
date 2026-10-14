package main

import (
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestAllowOtherFollowsFuseConf(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root may always pass allow_other")
	}
	dir := t.TempDir()
	on := filepath.Join(dir, "on.conf")
	off := filepath.Join(dir, "off.conf")
	if err := os.WriteFile(on, []byte("# mount_max = 1000\n  user_allow_other \n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(off, []byte("#user_allow_other\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	missing := filepath.Join(dir, "missing.conf")
	for _, c := range []struct {
		cfg     config
		conf    string
		want    bool
		wantErr bool
	}{
		{config{}, on, true, false},
		{config{}, off, false, false},
		{config{}, missing, false, false},
		{config{NoAllowOther: true}, on, false, false},
		{config{AllowOther: true}, on, true, false},
		{config{AllowOther: true}, off, false, true},
	} {
		got, err := allowOther(c.cfg, c.conf)
		if got != c.want || (err != nil) != c.wantErr {
			t.Errorf("%+v %s: %v %v", c.cfg, filepath.Base(c.conf), got, err)
		}
	}
}

func TestMountPathsAreUnescaped(t *testing.T) {
	if got := unescapeMountPath(`/home/u/my\040lib\134x`); got != `/home/u/my lib\x` {
		t.Fatalf("%q", got)
	}
}

func TestMountedFindsTheRoot(t *testing.T) {
	if up, err := mounted("/"); err != nil || !up {
		t.Fatalf("/: %v %v", up, err)
	}
	if up, err := mounted(t.TempDir()); err != nil || up {
		t.Fatalf("temp dir: %v %v", up, err)
	}
}

func TestRunningIgnoresAPidThatIsNotBambuddyFs(t *testing.T) {
	state := t.TempDir()
	if _, ok := running(state); ok {
		t.Fatal("no pid file")
	}
	sleeper := exec.Command("sleep", "30")
	if err := sleeper.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = sleeper.Process.Kill(); _ = sleeper.Wait() }()
	if err := os.WriteFile(pidPath(state), []byte(strconv.Itoa(sleeper.Process.Pid)), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, ok := running(state); ok {
		t.Fatal("a sleep was taken for the mount")
	}
	// The test binary is bambuddy-fs.test.
	remove, err := writePid(state)
	if err != nil {
		t.Fatal(err)
	}
	if pid, ok := running(state); !ok || pid != os.Getpid() {
		t.Fatalf("own pid: %d %v", pid, ok)
	}
	remove()
	if _, err := os.Stat(pidPath(state)); !os.IsNotExist(err) {
		t.Fatalf("pid file left: %v", err)
	}
}

func TestStopEscalatesToSIGKILL(t *testing.T) {
	state := t.TempDir()
	// A copy of the test binary named bambuddy-fs that ignores SIGTERM.
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	bin := filepath.Join(t.TempDir(), "bambuddy-fs")
	if err := os.Symlink(exe, bin); err != nil {
		t.Fatal(err)
	}
	child := exec.Command(bin, "-test.run=^TestHelperIgnoresSIGTERM$")
	child.Env = append(os.Environ(), "BAMBUDDY_FS_TEST_HELPER=1")
	if err := child.Start(); err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	go func() { _ = child.Wait(); close(done) }()
	if err := os.WriteFile(pidPath(state), []byte(strconv.Itoa(child.Process.Pid)), 0o600); err != nil {
		t.Fatal(err)
	}
	// Until the helper has set its signal mask, a SIGTERM would end it early.
	time.Sleep(500 * time.Millisecond)
	var log strings.Builder
	if err := stop(state, 300*time.Millisecond, &log); err != nil {
		t.Fatal(err)
	}
	<-done
	if !strings.Contains(log.String(), "after terminated") {
		t.Fatalf("SIGTERM should not have stopped it: %q", log.String())
	}
	if _, err := os.Stat(pidPath(state)); !os.IsNotExist(err) {
		t.Fatalf("pid file left: %v", err)
	}
}

func TestHelperIgnoresSIGTERM(t *testing.T) {
	if os.Getenv("BAMBUDDY_FS_TEST_HELPER") == "" {
		t.Skip("run by TestStopEscalatesToSIGKILL")
	}
	signal.Ignore(syscall.SIGTERM)
	time.Sleep(time.Minute)
}

func TestStatusAndUmountWithNothingMounted(t *testing.T) {
	cfg := config{Mountpoint: t.TempDir(), StateDir: t.TempDir()}
	var out, errOut strings.Builder
	if code := status(cfg, &out); code != 1 || !strings.HasPrefix(out.String(), "not mounted") {
		t.Fatalf("status: %d %q", code, out.String())
	}
	out.Reset()
	if code := umount(cfg, &out, &errOut); code != 0 || !strings.HasPrefix(out.String(), "unmounted") {
		t.Fatalf("umount: %d %q %q", code, out.String(), errOut.String())
	}
}
