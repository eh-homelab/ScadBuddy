package filamentpresets

// MirrorState is a preset's mirror in Bambuddy, from its columns (plan ruling R9):
// bambuddy_owned for a preset Bambuddy made; failed when the last send failed and
// Bambuddy lacks this document; pending when it lacks it and no send failed; else sent.
// A mirror_error beside sent is the note of a lost create, shown as a note.
func MirrorState(source, sha256 string, mirroredSHA256, mirrorError *string) string {
	switch {
	case source == "bambuddy":
		return "bambuddy_owned"
	case mirroredSHA256 != nil && *mirroredSHA256 == sha256:
		return "sent"
	case mirrorError != nil && *mirrorError != "":
		return "failed"
	default:
		return "pending"
	}
}
