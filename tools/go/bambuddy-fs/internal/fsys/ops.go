package fsys

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/api"
	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/telemetry"
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

func (d *dir) Create(ctx context.Context, name string, _, _ uint32, out *fuse.EntryOut) (_ *fs.Inode, _ fs.FileHandle, _ uint32, errno syscall.Errno) {
	if d.tree.opts.ReadOnly {
		return nil, nil, 0, syscall.EROFS
	}
	ctx, op := telemetry.StartOp(ctx, "create", telemetry.FolderID(d.id))
	defer func() { op.End(errno) }()
	if errno := d.free(ctx, d, name); errno != 0 {
		return nil, nil, 0, errno
	}
	n := &file{tree: d.tree, file: api.File{Name: name, FolderID: d.id, Created: time.Now().UTC()}}
	w, _ := n.attach(true, true)
	n.attr(&out.Attr)
	// Ino 0: go-fuse numbers it until the upload gives the file an ID.
	return d.NewInode(ctx, n, fs.StableAttr{Mode: syscall.S_IFREG}), w, 0, 0
}

func (d *dir) Mkdir(ctx context.Context, name string, _ uint32, out *fuse.EntryOut) (_ *fs.Inode, errno syscall.Errno) {
	if d.tree.opts.ReadOnly {
		return nil, syscall.EROFS
	}
	ctx, op := telemetry.StartOp(ctx, "mkdir", telemetry.FolderID(d.id))
	defer func() { op.End(errno) }()
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

func (d *dir) Unlink(ctx context.Context, name string) (errno syscall.Errno) {
	if d.tree.opts.ReadOnly {
		return syscall.EROFS
	}
	ctx, op := telemetry.StartOp(ctx, "unlink", telemetry.FolderID(d.id))
	defer func() { op.End(errno) }()
	l, errno := d.tree.view(ctx, d.id)
	if errno != 0 {
		return errno
	}
	f, ok := l.files[name]
	if ok {
		op.Set(telemetry.FileID(f.ID))
	}
	if !ok {
		if _, isDir := l.folders[name]; isDir {
			return syscall.EISDIR
		}
		child, n := d.unsent(name)
		e := l.queued[name]
		if e == nil && n != nil {
			e = n.entry()
		}
		if e != nil {
			return d.tree.unqueue(ctx, e)
		}
		if child != nil {
			// Never uploaded: its writer finds it unlinked and sends nothing. What a
			// rename landed it on goes with it.
			n.mu.Lock()
			var shadowed []base
			if n.pending != nil {
				shadowed = slices.Clone(n.pending.shadowed)
			}
			n.mu.Unlock()
			return d.tree.deleteBases(ctx, shadowed, 0)
		}
		return syscall.ENOENT
	}
	err := d.tree.lib.DeleteFiles(ctx, f.ID)
	d.tree.forget(d.id)
	if api.Errno(err) == syscall.ENOENT {
		// Already gone from the library: no longer shown here either.
		d.tree.unremember(f.ID)
		d.tree.cache.drop(f.ID)
	}
	if err != nil {
		return failed("unlink", err, "file", f.ID)
	}
	d.tree.cache.drop(f.ID)
	d.tree.unremember(f.ID)
	return 0
}

// unqueue removes a queued write, and the stored files it replaces that are still in
// place, which the write had already overwritten.
func (t *tree) unqueue(ctx context.Context, e *entry) syscall.Errno {
	rec, ok := t.unsent.record(e)
	if !ok {
		return 0
	}
	errno := t.deleteBases(ctx, rec.Bases, 0)
	t.forgetAll()
	if errno != 0 {
		return errno
	}
	t.unsent.drop(e)
	return 0
}

// Rmdir deletes the folder with everything in it (spec §5); the route refuses with
// 409, EBUSY, before deleting anything if a file in it is queued to print.
func (d *dir) Rmdir(ctx context.Context, name string) (errno syscall.Errno) {
	if d.tree.opts.ReadOnly {
		return syscall.EROFS
	}
	ctx, op := telemetry.StartOp(ctx, "rmdir")
	defer func() { op.End(errno) }()
	l, errno := d.tree.list(ctx, d.id)
	if errno != 0 {
		return errno
	}
	f, ok := l.folders[name]
	if ok {
		op.Set(telemetry.FolderID(f.ID))
	}
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

// Rename renames and/or moves a file or folder with one update. Over an existing file
// it replaces it, as rename(2) does, though the library cannot do so atomically: a
// stored file renamed over another takes the name and then the other is deleted (if
// that fails, both remain and the answer is EIO); a write not sent yet takes the name
// at once, shadowing what it lands on, and replaces it when it is sent (see
// record.Bases); a queued save it lands on is superseded once its own bytes are
// durable. RENAME_NOREPLACE over anything, a folder over a folder, and anything over
// a file still being written are EEXIST; a file over a folder is EISDIR, a folder
// over a file ENOTDIR.
func (d *dir) Rename(ctx context.Context, name string, newParent fs.InodeEmbedder, newName string, flags uint32) (errno syscall.Errno) {
	if d.tree.opts.ReadOnly {
		return syscall.EROFS
	}
	to, ok := newParent.(*dir)
	if !ok || flags&fs.RENAME_EXCHANGE != 0 {
		return syscall.ENOTSUP
	}
	ctx, op := telemetry.StartOp(ctx, "rename", telemetry.FolderID(d.id), telemetry.ToFolderID(to.id))
	defer func() { op.End(errno) }()
	src, errno := d.tree.view(ctx, d.id)
	if errno != 0 {
		return errno
	}
	dst, errno := d.tree.view(ctx, to.id)
	if errno != 0 {
		return errno
	}
	_, dstDir := dst.folders[newName]
	dstFile, dstIsFile := dst.files[newName]
	dstQ := dst.queued[newName]
	dstBusy := false
	if _, n := to.unsent(newName); n != nil && !dstIsFile {
		if e := n.entry(); e != nil && dstQ == nil {
			dstQ = e
		} else if e == nil && dstQ == nil {
			dstBusy = true
		}
	}
	if (dstDir || dstIsFile || dstQ != nil || dstBusy) && (flags&renameNoReplace != 0 || dstBusy) {
		return syscall.EEXIST
	}
	defer d.tree.forget(to.id)
	defer d.tree.forget(d.id)

	if f, ok := src.folders[name]; ok {
		switch {
		case dstDir:
			return syscall.EEXIST
		case dstIsFile || dstQ != nil:
			return syscall.ENOTDIR
		}
		newName, parent := changes(f.Name, newName, d.id, to.id)
		if _, err := d.tree.lib.UpdateFolder(ctx, f.ID, newName, parent); err != nil {
			return failed("rename folder", err, "folder", f.ID)
		}
		return 0
	}
	srcFile, srcIsFile := src.files[name]
	_, srcN := d.unsent(name)
	srcE := src.queued[name]
	if srcE == nil && srcN != nil {
		srcE = srcN.entry()
	}
	switch {
	case !srcIsFile && srcN == nil && srcE == nil:
		return syscall.ENOENT
	case dstDir:
		return syscall.EISDIR
	case srcIsFile:
		return d.renameStored(ctx, name, srcFile, to, newName, dstFile, dstIsFile, dstQ)
	}
	return d.renameUnsent(ctx, srcN, srcE, to, newName, dstFile, dstIsFile, dstQ)
}

// renameNoReplace is renameat2's RENAME_NOREPLACE.
const renameNoReplace = 0x1

// renameStored renames a stored file f, then deletes what it replaced: a stored file
// t, or a queued save q and the stored files that save replaced.
func (d *dir) renameStored(ctx context.Context, name string, f api.File, to *dir, newName string, t api.File, isFile bool, q *entry) syscall.Errno {
	var got api.File
	if retypes(f.Name, newName) {
		var errno syscall.Errno
		if got, errno = d.retype(ctx, f, to.id, newName); errno != 0 {
			return errno
		}
		// A retry after a lost answer is answered with the copy the first try made,
		// which is now listed as the file this rename replaces: it is the result.
		isFile = isFile && t.ID != got.ID
	} else {
		newN, folder := changes(f.Name, newName, d.id, to.id)
		var err error
		if got, err = d.tree.lib.UpdateFile(ctx, f.ID, newN, folder); err != nil {
			return failed("rename", err, "file", f.ID)
		}
		d.tree.moved(f.ID, got.Name, got.FolderID)
		d.tree.rebase(f.ID, got)
	}
	if child := d.GetChild(name); child != nil {
		if n, ok := child.Operations().(*file); ok {
			// The answer has no size or times: keep the file's.
			if cur := n.current(); cur.ID == f.ID {
				cur.ID, cur.Name, cur.FolderID = got.ID, got.Name, got.FolderID
				n.set(cur)
			}
		}
	}
	switch {
	case isFile:
		if err := d.tree.lib.DeleteFiles(ctx, t.ID); err != nil {
			slog.Error("rename: the renamed file has the name, but the file it replaces remains beside it", "file", f.ID, "replaced", t.ID, "err", err)
			return syscall.EIO
		}
		d.tree.cache.drop(t.ID)
		d.tree.unremember(t.ID)
	case q != nil:
		rec, ok := d.tree.unsent.record(q)
		d.tree.unsent.drop(q)
		if ok {
			if errno := d.tree.deleteBases(ctx, rec.Bases, f.ID); errno != 0 {
				return syscall.EIO
			}
		}
	}
	return 0
}

// renameUnsent renames a write not sent yet: queued (e), still being written (n), or
// both (a queued save being written again). What it lands on becomes its base: a
// stored file t, or a queued save q's bases, q being superseded at once by a queued
// save and, by one still being written, once that is durable.
func (d *dir) renameUnsent(ctx context.Context, n *file, e *entry, to *dir, newName string, t api.File, isFile bool, q *entry) syscall.Errno {
	uq := d.tree.unsent
	var shadow []base
	if isFile {
		shadow = []base{baseOf(t, t.Created)}
	}
	add := slices.Clone(shadow)
	if q == e {
		q = nil
	}
	if q != nil && e == nil && !uq.hold(q) {
		// Sent meanwhile: its file now has the name.
		q = nil
		d.tree.forget(to.id)
		v, errno := d.tree.view(ctx, to.id)
		if errno != 0 {
			return errno
		}
		if t, ok := v.files[newName]; ok {
			shadow = []base{baseOf(t, t.Created)}
			add = slices.Clone(shadow)
		}
	}
	if q != nil {
		rec, _ := uq.record(q)
		add = union(add, rec.Bases...)
	}
	if e != nil {
		err := uq.update(e, func(r *record) {
			r.Name, r.FolderID = newName, to.id
			r.Bases = union(r.Bases, add...)
		})
		if err == nil && q != nil {
			err = uq.supersede(e, q)
		} else if err == nil {
			err = uq.supersede(e)
		}
		if err != nil {
			return errQueue(err, "path", e.dir)
		}
	}
	if n == nil {
		return 0
	}
	n.mu.Lock()
	p := n.pending
	if p != nil {
		p.bases = union(p.bases, add...)
		if e == nil {
			p.shadowed = append(p.shadowed, shadow...)
			if q != nil {
				p.over = append(p.over, q)
			}
		}
	}
	n.mu.Unlock()
	if e == nil {
		if p == nil {
			if q != nil {
				uq.unhold(q)
			}
			return syscall.ENOENT
		}
		uq.shadow(shadow, 1)
	}
	f := n.current()
	f.Name, f.FolderID = newName, to.id
	n.set(f)
	return 0
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

// free is EEXIST if name is shown in in, or is a file being created or queued there.
func (d *dir) free(ctx context.Context, in *dir, name string) syscall.Errno {
	l, errno := d.tree.view(ctx, in.id)
	if errno != 0 {
		return errno
	}
	_, isDir := l.folders[name]
	_, isFile := l.files[name]
	if child, _ := in.unsent(name); isDir || isFile || child != nil || l.queued[name] != nil {
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

// fileType is what Bambuddy types a file by, its name's extension ("gcode.3mf" for a
// sliced 3MF), lower-cased.
func fileType(name string) string {
	lower := strings.ToLower(name)
	if strings.HasSuffix(lower, ".gcode.3mf") {
		return "gcode.3mf"
	}
	return strings.TrimPrefix(filepath.Ext(lower), ".")
}

// typed are the file types Bambuddy treats as models (types the file and makes its
// thumbnail from the name it is uploaded under).
var typed = map[string]bool{"3mf": true, "gcode.3mf": true, "gcode": true, "stl": true}

// retypes is whether renaming from to to must send the file again: it becomes a type
// Bambuddy handles differently. Any other rename (model.3mf to model.3mf.bak) keeps the
// file's ID, so its print history and queue entries still point at it.
func retypes(from, to string) bool {
	return fileType(from) != fileType(to) && typed[fileType(to)]
}

// retype renames f to name in folder by uploading its contents under the new name and
// deleting f. Bambuddy types a file (and makes its thumbnail) from the name it is
// uploaded under, and a rename keeps both, so an app that saves to "x.3mf.tmp" and
// renames it to "x.3mf" would leave a file Bambuddy cannot print.
//
// The upload's Idempotency-Key comes from f and the new name, so a rename retried
// after a lost answer is answered with the same file instead of a second copy. That
// answer can name a copy a rolled-back try deleted, so the copy is looked for before f
// is deleted, and sent again under a fresh key if it is gone. If f cannot be deleted
// the new copy is, and the rename fails with f as it was.
func (d *dir) retype(ctx context.Context, f api.File, folder int, name string) (api.File, syscall.Errno) {
	got, errno := d.upload(ctx, f, folder, name, retypeKey(f, folder, name))
	if errno != 0 {
		return api.File{}, errno
	}
	if l, err := d.tree.lib.List(ctx, folder, true); err != nil {
		return api.File{}, failed("rename: list", err, "folder", folder)
	} else if !slices.ContainsFunc(l.Files, func(x api.File) bool { return x.ID == got.ID }) {
		if got, errno = d.upload(ctx, f, folder, name, api.NewKey()); errno != 0 {
			return api.File{}, errno
		}
	}
	if err := d.tree.lib.DeleteFiles(ctx, f.ID); err != nil {
		slog.Error("rename: the old file could not be deleted, so the copy under the new name is", "file", f.ID, "new", got.ID, "err", err)
		if err := d.tree.lib.DeleteFiles(ctx, got.ID); err != nil {
			slog.Error("rename: the copy under the new name remains beside the old file", "file", f.ID, "new", got.ID, "err", err)
			d.tree.forget(folder)
		}
		return api.File{}, syscall.EIO
	}
	got.Size, got.Created = f.Size, f.Created
	if got.FolderID == 0 {
		got.FolderID = folder
	}
	// Both IDs are claimed, so no download of either writes its cache file meanwhile.
	if err := d.tree.cache.claim(ctx, f.ID); err != nil {
		d.tree.cache.drop(f.ID)
	} else {
		if err := d.tree.cache.claim(ctx, got.ID); err == nil {
			if err := os.Rename(d.tree.cache.path(f.ID), d.tree.cache.path(got.ID)); err != nil {
				d.tree.cache.drop(f.ID)
			}
			d.tree.cache.release(got.ID)
		} else {
			d.tree.cache.drop(f.ID)
		}
		d.tree.cache.release(f.ID)
	}
	d.tree.unremember(f.ID)
	d.tree.remember(got)
	d.tree.rebase(f.ID, got)
	return got, 0
}

// upload sends f's contents (cached, or downloaded first) as name in folder.
func (d *dir) upload(ctx context.Context, f api.File, folder int, name, key string) (api.File, syscall.Errno) {
	fh, err := d.tree.cache.open(ctx, d.tree.lib, f, "")
	if err != nil {
		return api.File{}, failed("rename: read", err, "file", f.ID)
	}
	got, err := d.tree.lib.Upload(ctx, folder, name, key, fh)
	_ = fh.Close()
	if err != nil {
		return api.File{}, failed("rename: upload", err, "file", f.ID, "name", name)
	}
	return got, 0
}

// retypeKey is the Idempotency-Key of f's retype to name in folder.
func retypeKey(f api.File, folder int, name string) string {
	sum := sha256.Sum256([]byte(fmt.Sprintf("retype\x00%d\x00%d\x00%d\x00%d\x00%s", f.ID, f.Created.UnixNano(), f.Size, folder, name)))
	return hex.EncodeToString(sum[:16])
}
