package fsys

import (
	"context"
	"log/slog"
	"syscall"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/api"
	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
)

// Folder and name operations: create, mkdir, unlink, rmdir and rename, and the ones
// the library has no counterpart for (spec §9).
//
// The kernel holds the parent directory's lock while it waits for create, mkdir,
// unlink, rmdir and rename, and an entry notification takes that lock too, so those
// handlers never notify: their answers update the kernel's entries, and they drop
// only this tree's listings. An upload notifies, from flush or release, which run
// under no directory lock.

var (
	_ fs.NodeCreater   = (*dir)(nil)
	_ fs.NodeMkdirer   = (*dir)(nil)
	_ fs.NodeUnlinker  = (*dir)(nil)
	_ fs.NodeRmdirer   = (*dir)(nil)
	_ fs.NodeRenamer   = (*dir)(nil)
	_ fs.NodeSymlinker = (*dir)(nil)
	_ fs.NodeLinker    = (*dir)(nil)
	_ fs.NodeMknoder   = (*dir)(nil)
	_ fs.NodeSetattrer = (*dir)(nil)
)

func (d *dir) Create(ctx context.Context, name string, _, _ uint32, out *fuse.EntryOut) (*fs.Inode, fs.FileHandle, uint32, syscall.Errno) {
	if d.tree.opts.ReadOnly {
		return nil, nil, 0, syscall.EROFS
	}
	if errno := d.free(ctx, d, name); errno != 0 {
		return nil, nil, 0, errno
	}
	n := &file{tree: d.tree, file: api.File{Name: name, FolderID: d.id, Created: time.Now().UTC()}}
	w := n.attach(true, true)
	n.attr(&out.Attr)
	// Ino 0: go-fuse numbers it until the upload gives the file an ID.
	return d.NewInode(ctx, n, fs.StableAttr{Mode: syscall.S_IFREG}), w, 0, 0
}

func (d *dir) Mkdir(ctx context.Context, name string, _ uint32, out *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	if d.tree.opts.ReadOnly {
		return nil, syscall.EROFS
	}
	if errno := d.free(ctx, d, name); errno != 0 {
		return nil, errno
	}
	f, err := d.tree.lib.Mkdir(ctx, d.id, name)
	d.tree.forget(d.id)
	if err != nil {
		return nil, failed("mkdir", err, "parent", d.id, "name", name)
	}
	child := &dir{tree: d.tree, id: f.ID}
	child.attr(&out.Attr)
	return d.NewInode(ctx, child, fs.StableAttr{Mode: syscall.S_IFDIR, Ino: dirIno(f.ID)}), 0
}

func (d *dir) Unlink(ctx context.Context, name string) syscall.Errno {
	if d.tree.opts.ReadOnly {
		return syscall.EROFS
	}
	l, errno := d.tree.list(ctx, d.id)
	if errno != 0 {
		return errno
	}
	f, ok := l.files[name]
	if !ok {
		if _, isDir := l.folders[name]; isDir {
			return syscall.EISDIR
		}
		if child, _ := d.unsent(name); child != nil {
			// Never uploaded: its writer finds it unlinked and sends nothing.
			return 0
		}
		return syscall.ENOENT
	}
	err := d.tree.lib.DeleteFiles(ctx, f.ID)
	d.tree.forget(d.id)
	if err != nil {
		return failed("unlink", err, "file", f.ID)
	}
	d.tree.cache.drop(f.ID)
	return 0
}

// Rmdir deletes the folder with everything in it (spec §5); the route refuses with
// 409, EBUSY, before deleting anything if a file in it is queued to print.
func (d *dir) Rmdir(ctx context.Context, name string) syscall.Errno {
	if d.tree.opts.ReadOnly {
		return syscall.EROFS
	}
	l, errno := d.tree.list(ctx, d.id)
	if errno != 0 {
		return errno
	}
	f, ok := l.folders[name]
	if !ok {
		if _, isFile := l.files[name]; isFile {
			return syscall.ENOTDIR
		}
		return syscall.ENOENT
	}
	err := d.tree.lib.DeleteFolder(ctx, f.ID)
	// Even a refused delete may have moved or trashed some of the subtree.
	d.tree.forgetAll()
	if err != nil {
		return failed("rmdir", err, "folder", f.ID)
	}
	return 0
}

// Rename renames and/or moves a file or folder with one update. A name that is taken
// is EEXIST: the library cannot replace an entry atomically.
func (d *dir) Rename(ctx context.Context, name string, newParent fs.InodeEmbedder, newName string, flags uint32) syscall.Errno {
	if d.tree.opts.ReadOnly {
		return syscall.EROFS
	}
	to, ok := newParent.(*dir)
	if !ok || flags&fs.RENAME_EXCHANGE != 0 {
		return syscall.ENOTSUP
	}
	l, errno := d.tree.list(ctx, d.id)
	if errno != 0 {
		return errno
	}
	if errno := d.free(ctx, to, newName); errno != 0 {
		return errno
	}
	defer d.tree.forget(to.id)
	defer d.tree.forget(d.id)

	if f, ok := l.folders[name]; ok {
		newName, parent := changes(f.Name, newName, d.id, to.id)
		if _, err := d.tree.lib.UpdateFolder(ctx, f.ID, newName, parent); err != nil {
			return failed("rename folder", err, "folder", f.ID)
		}
		return 0
	}
	if f, ok := l.files[name]; ok {
		newName, folder := changes(f.Name, newName, d.id, to.id)
		got, err := d.tree.lib.UpdateFile(ctx, f.ID, newName, folder)
		if err != nil {
			return failed("rename", err, "file", f.ID)
		}
		if child := d.GetChild(name); child != nil {
			if n, ok := child.Operations().(*file); ok {
				// The answer has no size or times: keep the file's.
				if cur := n.current(); cur.ID == f.ID {
					cur.Name, cur.FolderID = got.Name, got.FolderID
					n.set(cur)
				}
			}
		}
		return 0
	}
	if _, n := d.unsent(name); n != nil {
		// Not uploaded yet: it will be uploaded under its new name.
		f := n.current()
		f.Name, f.FolderID = newName, to.id
		n.set(f)
		return 0
	}
	return syscall.ENOENT
}

// changes is what a rename sends: within a folder always the name, and on a move the
// folder (0 being the top level) and the name only if it changes.
func changes(name, newName string, from, to int) (*string, *int) {
	if from == to {
		return &newName, nil
	}
	if newName == name {
		return nil, &to
	}
	return &newName, &to
}

// free is EEXIST if name is shown in in, or is a file being created there.
func (d *dir) free(ctx context.Context, in *dir, name string) syscall.Errno {
	l, errno := d.tree.list(ctx, in.id)
	if errno != 0 {
		return errno
	}
	_, isDir := l.folders[name]
	_, isFile := l.files[name]
	if child, _ := in.unsent(name); isDir || isFile || child != nil {
		return syscall.EEXIST
	}
	return 0
}

// failed logs a library error with the operation and maps it to its errno.
func failed(op string, err error, args ...any) syscall.Errno {
	slog.Warn(op+" failed", append(args, "err", err)...)
	return api.Errno(err)
}

// refused is a write the library has no counterpart for: EROFS on a read-only mount,
// else ENOTSUP (spec §9).
func (t *tree) refused() syscall.Errno {
	if t.opts.ReadOnly {
		return syscall.EROFS
	}
	return syscall.ENOTSUP
}

func (d *dir) Symlink(context.Context, string, string, *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	return nil, d.tree.refused()
}

func (d *dir) Link(context.Context, fs.InodeEmbedder, string, *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	return nil, d.tree.refused()
}

func (d *dir) Mknod(context.Context, string, uint32, uint32, *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	return nil, d.tree.refused()
}

// timesOnly is a setattr that sets only times. Times are Bambuddy's created_at and
// cannot change, but refusing them would fail touch, cp -p and rsync -t, so such a
// setattr succeeds and changes nothing.
const timesOnly = fuse.FATTR_ATIME | fuse.FATTR_MTIME | fuse.FATTR_ATIME_NOW | fuse.FATTR_MTIME_NOW |
	fuse.FATTR_CTIME | fuse.FATTR_FH | fuse.FATTR_LOCKOWNER

func (d *dir) Setattr(_ context.Context, _ fs.FileHandle, in *fuse.SetAttrIn, out *fuse.AttrOut) syscall.Errno {
	if d.tree.opts.ReadOnly || in.Valid&^timesOnly != 0 {
		return d.tree.refused()
	}
	d.attr(&out.Attr)
	return 0
}

// Extended attributes: there are none, and none can be set. Reads answer as for a
// file without any (ENODATA, an empty list), so ls and cp stay quiet.
var (
	_ fs.NodeGetxattrer    = (*dir)(nil)
	_ fs.NodeListxattrer   = (*dir)(nil)
	_ fs.NodeSetxattrer    = (*dir)(nil)
	_ fs.NodeRemovexattrer = (*dir)(nil)
	_ fs.NodeGetxattrer    = (*file)(nil)
	_ fs.NodeListxattrer   = (*file)(nil)
	_ fs.NodeSetxattrer    = (*file)(nil)
	_ fs.NodeRemovexattrer = (*file)(nil)
)

func (d *dir) Getxattr(context.Context, string, []byte) (uint32, syscall.Errno) {
	return 0, syscall.ENODATA
}

func (d *dir) Listxattr(context.Context, []byte) (uint32, syscall.Errno) { return 0, 0 }

func (d *dir) Setxattr(context.Context, string, []byte, uint32) syscall.Errno {
	return d.tree.refused()
}

func (d *dir) Removexattr(context.Context, string) syscall.Errno { return d.tree.refused() }

func (n *file) Getxattr(context.Context, string, []byte) (uint32, syscall.Errno) {
	return 0, syscall.ENODATA
}

func (n *file) Listxattr(context.Context, []byte) (uint32, syscall.Errno) { return 0, 0 }

func (n *file) Setxattr(context.Context, string, []byte, uint32) syscall.Errno {
	return n.tree.refused()
}

func (n *file) Removexattr(context.Context, string) syscall.Errno { return n.tree.refused() }
