package discover

import (
	"bufio"
	"os"
	"os/user"
	"runtime"
	"strconv"
	"strings"
)

// Machine is this machine: the body of PUT …/fleet-rotator/machines/{machine_id}. ID is in
// the route, never the raw machine id.
type Machine struct {
	ID             string `json:"-"`
	Hostname       string `json:"hostname"`
	OS             string `json:"os"`
	OSUser         string `json:"os_user"`
	ServiceVersion string `json:"service_version"`
}

// MachineOptions say where the machine's facts are; a test gives fakes.
type MachineOptions struct {
	MachineIDFile string // /etc/machine-id
	OSReleaseFile string // /etc/os-release
	User          string // "" is the current user
	Hostname      string // "" is os.Hostname
	Version       string
}

// CurrentUser is the OS user's name, else $USER.
func CurrentUser() string {
	if u, err := user.Current(); err == nil && u.Username != "" {
		return u.Username
	}
	return os.Getenv("USER")
}

// ReadMachine collects this machine's facts.
func ReadMachine(o MachineOptions) (Machine, error) {
	name := o.User
	if name == "" {
		name = CurrentUser()
	}
	id, err := MachineID(o.MachineIDFile, name)
	if err != nil {
		return Machine{}, err
	}
	host := o.Hostname
	if host == "" {
		host, _ = os.Hostname()
	}
	if host = cleanHostname(host); host == "" {
		host = "unknown"
	}
	version := cleanVersion(o.Version)
	if version == "" {
		version = "dev"
	}
	osName := runtime.GOOS + "/" + runtime.GOARCH
	if pretty := prettyName(o.OSReleaseFile); pretty != "" {
		osName += " " + pretty
	}
	if r := []rune(osName); len(r) > 100 {
		osName = string(r[:100])
	}
	osUser := cleanOSUser(name)
	if osUser == "" {
		// core requires one: a name it would refuse must not lose the machine.
		osUser = "unknown"
	}
	return Machine{ID: id, Hostname: host, OS: cleanOS(osName), OSUser: osUser, ServiceVersion: version}, nil
}

// prettyName is os-release's PRETTY_NAME, or "".
func prettyName(path string) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	for s.Scan() {
		value, ok := strings.CutPrefix(s.Text(), "PRETTY_NAME=")
		if !ok {
			continue
		}
		if u, err := strconv.Unquote(value); err == nil {
			value = u
		}
		return plain(strings.Trim(value, `'"`), 80)
	}
	return ""
}
