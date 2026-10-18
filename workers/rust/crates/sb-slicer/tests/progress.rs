use sb_slicer::progress::{Line, LineBuffer};

#[test]
fn a_line_split_across_two_reads_is_parsed_once_whole() {
    let mut buf = LineBuffer::default();
    assert!(
        buf.push(br#"{"plate_index":0,"plate_count":1,"plate_pe"#)
            .is_empty()
    );
    let lines = buf.push(b"rcent\":40,\"total_percent\":40,\"message\":\"Slicing\"}\n");
    assert_eq!(lines.len(), 1);
    match &lines[0] {
        Line::Progress(p) => {
            assert_eq!(p.total_percent, 40);
            assert_eq!(p.plate_percent, 40);
            assert_eq!(p.message, "Slicing");
        }
        other => panic!("expected progress, got {other:?}"),
    }
}

#[test]
fn two_lines_in_one_read_are_two() {
    let mut buf = LineBuffer::default();
    let lines = buf.push(
        b"{\"plate_index\":0,\"plate_count\":1,\"plate_percent\":10,\"total_percent\":10}\n\
          {\"plate_index\":0,\"plate_count\":1,\"plate_percent\":20,\"total_percent\":20}\n",
    );
    assert_eq!(lines.len(), 2);
    assert!(matches!(&lines[1], Line::Progress(p) if p.total_percent == 20));
}

#[test]
fn a_warning_line_is_a_warning() {
    let mut buf = LineBuffer::default();
    assert_eq!(
        buf.push(b"{\"warning\":\"x\"}\n"),
        vec![Line::Warning("x".into())]
    );
}

#[test]
fn anything_else_is_other() {
    let mut buf = LineBuffer::default();
    assert_eq!(buf.push(b"not json\n"), vec![Line::Other]);
}
