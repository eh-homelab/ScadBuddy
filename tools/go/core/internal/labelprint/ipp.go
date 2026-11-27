// Package labelprint prints label PDFs to the cluster's CUPS server with IPP.
package labelprint

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"math"
)

// Tag is an IPP delimiter or value tag.
// https://www.rfc-editor.org/rfc/rfc8010.html#section-3.5
type Tag byte

const (
	TagOperation Tag = 0x01
	TagJob       Tag = 0x02
	TagEnd       Tag = 0x03
	TagPrinter   Tag = 0x04
	TagInteger   Tag = 0x21
	TagBoolean   Tag = 0x22
	TagEnum      Tag = 0x23
	TagText      Tag = 0x41
	TagName      Tag = 0x42
	TagKeyword   Tag = 0x44
	TagURI       Tag = 0x45
	TagCharset   Tag = 0x47
	TagLanguage  Tag = 0x48
	TagMimeType  Tag = 0x49

	tagBegCollection Tag = 0x34
	tagMemberName    Tag = 0x4A
	tagEndCollection Tag = 0x37
)

// ErrNotIPP means the bytes are not a whole IPP message within the limit.
var ErrNotIPP = errors.New("not an IPP message")

// Attr is one attribute with all its values. Each additional value has its
// own value-tag (RFC 8010 section 3.1.5): ValueTags holds the tag of each
// value when they differ, and is nil when every value has Tag.
type Attr struct {
	Group     Tag
	Name      string
	Tag       Tag
	Values    [][]byte
	ValueTags []Tag

	member bool // a record inside a collection (Decode sets it)
}

// valueTag is the tag of value i.
func (a Attr) valueTag(i int) Tag {
	if i < len(a.ValueTags) {
		return a.ValueTags[i]
	}
	return a.Tag
}

// collection is true for a collection record: a begCollection, a member
// name, an endCollection or a member value.
func (a Attr) collection() bool {
	return a.member || isRecordTag(a.Tag)
}

// Message is an IPP request or reply. Code is the operation or the status.
type Message struct {
	Version   [2]byte
	Code      uint16
	RequestID uint32
	Attrs     []Attr
	Data      []byte
}

// StrAttr makes an attribute of string values.
func StrAttr(group Tag, name string, tag Tag, values ...string) Attr {
	a := Attr{Group: group, Name: name, Tag: tag}
	for _, v := range values {
		a.Values = append(a.Values, []byte(v))
	}
	return a
}

// IntAttr makes an attribute of one integer, enum or boolean value.
func IntAttr(group Tag, name string, tag Tag, v int32) Attr {
	if tag == TagBoolean {
		return Attr{Group: group, Name: name, Tag: tag, Values: [][]byte{{byte(v)}}}
	}
	b := make([]byte, 4)
	binary.BigEndian.PutUint32(b, uint32(v))
	return Attr{Group: group, Name: name, Tag: tag, Values: [][]byte{b}}
}

// Encode writes the message. A group opens each time Attrs changes Group.
func (m Message) Encode(w io.Writer) error {
	var b bytes.Buffer
	b.Write(m.Version[:])
	_ = binary.Write(&b, binary.BigEndian, m.Code)
	_ = binary.Write(&b, binary.BigEndian, m.RequestID)
	var group Tag
	for _, a := range m.Attrs {
		if a.Group != group || a.Tag == 0 {
			group = a.Group
			b.WriteByte(byte(group))
		}
		if a.Tag == 0 {
			continue
		}
		values := a.Values
		if len(values) == 0 {
			values = [][]byte{nil}
		}
		for i, v := range values {
			name := a.Name
			if i > 0 {
				name = "" // RFC 8010 section 3.1.5: later values have no name
			}
			if len(name) > 0xFFFF || len(v) > 0xFFFF {
				return errors.New("ipp: attribute too long")
			}
			b.WriteByte(byte(a.valueTag(i)))
			_ = binary.Write(&b, binary.BigEndian, uint16(len(name)))
			b.WriteString(name)
			_ = binary.Write(&b, binary.BigEndian, uint16(len(v)))
			b.Write(v)
		}
	}
	b.WriteByte(byte(TagEnd))
	b.Write(m.Data)
	_, err := w.Write(b.Bytes())
	return err
}

// Decode reads a message of at most limit bytes. A collection's member
// records stay plain records of the group; they are never nested.
func Decode(r io.Reader, limit int64) (Message, error) {
	n := limit
	if n < math.MaxInt64 {
		n++ // one byte more, to see a stream past limit
	}
	raw, err := io.ReadAll(io.LimitReader(r, n))
	if err != nil {
		return Message{}, err
	}
	if int64(len(raw)) > limit || len(raw) < 8 {
		return Message{}, ErrNotIPP
	}
	m := Message{
		Version:   [2]byte{raw[0], raw[1]},
		Code:      binary.BigEndian.Uint16(raw[2:4]),
		RequestID: binary.BigEndian.Uint32(raw[4:8]),
	}
	p := raw[8:]
	var group Tag
	depth := 0
	for {
		if len(p) == 0 {
			return Message{}, ErrNotIPP
		}
		tag := Tag(p[0])
		p = p[1:]
		if tag <= 0x0F {
			if depth != 0 {
				return Message{}, ErrNotIPP // an unclosed collection
			}
			if tag == TagEnd {
				m.Data = append([]byte(nil), p...)
				return m, nil
			}
			group = tag
			m.Attrs = append(m.Attrs, groupStart(group))
			continue
		}
		name, rest, ok := lenPrefixed(p)
		if !ok {
			return Message{}, ErrNotIPP
		}
		value, rest, ok := lenPrefixed(rest)
		if !ok {
			return Message{}, ErrNotIPP
		}
		p = rest
		inCollection := depth > 0
		switch tag {
		case tagBegCollection:
			depth++
		case tagEndCollection:
			if depth == 0 {
				return Message{}, ErrNotIPP // an end with no begin
			}
			depth--
		}
		if !inCollection && !isRecordTag(tag) && len(name) == 0 {
			if len(m.Attrs) == 0 {
				return Message{}, ErrNotIPP
			}
			last := &m.Attrs[len(m.Attrs)-1]
			if last.Group != group || last.Tag == 0 || last.collection() {
				return Message{}, ErrNotIPP // an additional value with no attribute
			}
			if tag != last.Tag && last.ValueTags == nil {
				last.ValueTags = make([]Tag, len(last.Values))
				for i := range last.ValueTags {
					last.ValueTags[i] = last.Tag
				}
			}
			if last.ValueTags != nil {
				last.ValueTags = append(last.ValueTags, tag)
			}
			last.Values = append(last.Values, append([]byte(nil), value...))
			continue
		}
		m.Attrs = append(m.Attrs, Attr{Group: group, Name: string(name), Tag: tag, Values: [][]byte{append([]byte(nil), value...)}, member: inCollection})
	}
}

// groupStart is the marker Decode puts at each group delimiter, so that two
// groups of one tag stay two groups. Encode writes a delimiter for it.
func groupStart(group Tag) Attr { return Attr{Group: group} }

func lenPrefixed(p []byte) (val, rest []byte, ok bool) {
	if len(p) < 2 {
		return nil, nil, false
	}
	n := int(binary.BigEndian.Uint16(p))
	p = p[2:]
	if len(p) < n {
		return nil, nil, false
	}
	return p[:n], p[n:], true
}

func isRecordTag(t Tag) bool {
	return t == tagBegCollection || t == tagMemberName || t == tagEndCollection
}

// Strings returns the character-string values of the named attribute in the
// group: tags 0x41 to 0x49, and the text of 0x35 textWithLanguage and 0x36
// nameWithLanguage (RFC 8010 section 3.9). A collection, an integer, an
// octetString syntax (dateTime, resolution, rangeOfInteger) and an
// out-of-band value give none.
func (m Message) Strings(group Tag, name string) []string {
	var out []string
	for _, a := range m.Attrs {
		if a.Group != group || a.Name != name || a.collection() {
			continue
		}
		for i, v := range a.Values {
			switch t := a.valueTag(i); {
			case t >= 0x41 && t <= 0x49:
				out = append(out, string(v))
			case t == 0x35 || t == 0x36:
				if _, rest, ok := lenPrefixed(v); ok {
					if text, rest, ok := lenPrefixed(rest); ok && len(rest) == 0 {
						out = append(out, string(text))
					}
				}
			}
		}
	}
	return out
}

// Int returns the first integer, enum or boolean value of the named attribute.
func (m Message) Int(group Tag, name string) (int32, bool) {
	for _, a := range m.Attrs {
		if a.Group != group || a.Name != name || len(a.Values) == 0 || a.collection() {
			continue
		}
		if t := a.Tag; t != TagInteger && t != TagBoolean && t != TagEnum {
			continue
		}
		switch v := a.Values[0]; len(v) {
		case 4:
			return int32(binary.BigEndian.Uint32(v)), true
		case 1:
			return int32(v[0]), true
		}
	}
	return 0, false
}

// Groups returns one slice per group of the tag, in stream order. A group
// starts at a marker (see groupStart) or at a change of Group, so two printers
// are two slices. The markers and the collection records are left out of
// the slices.
func (m Message) Groups(group Tag) [][]Attr {
	var out [][]Attr
	open := false
	for _, a := range m.Attrs {
		if a.Group != group {
			open = false
			continue
		}
		if a.Tag == 0 {
			open = false
			continue
		}
		if a.collection() {
			continue
		}
		if !open {
			out = append(out, nil)
			open = true
		}
		out[len(out)-1] = append(out[len(out)-1], a)
	}
	return out
}
