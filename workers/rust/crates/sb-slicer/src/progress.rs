//! The CLI's `--pipe` protocol: JSON lines, written in 512-byte chunks (spec 2026-10-16
//! §2), so one line can arrive in two reads and two lines in one.

use serde::Deserialize;

use crate::models::SliceProgress;

/// A line longer than this is no line of the protocol; it is dropped, not buffered.
const MAX_LINE: usize = 64 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Line {
    Progress(SliceProgress),
    Warning(String),
    Other,
}

#[derive(Default)]
pub struct LineBuffer {
    partial: Vec<u8>,
    /// Set while the rest of an over-long line is being skipped.
    skipping: bool,
}

impl LineBuffer {
    /// Splits on `\n`, keeps the partial tail for the next push and parses each whole line.
    pub fn push(&mut self, bytes: &[u8]) -> Vec<Line> {
        let mut lines = Vec::new();
        let mut rest = bytes;
        while let Some(end) = rest.iter().position(|&b| b == b'\n') {
            let (head, tail) = rest.split_at(end);
            rest = &tail[1..];
            if std::mem::take(&mut self.skipping) {
                self.partial.clear();
                continue;
            }
            self.partial.extend_from_slice(head);
            lines.push(parse(&self.partial));
            self.partial.clear();
        }
        if !self.skipping {
            self.partial.extend_from_slice(rest);
            if self.partial.len() > MAX_LINE {
                self.partial.clear();
                self.skipping = true;
            }
        }
        lines
    }
}

#[derive(Deserialize)]
struct Warning {
    warning: String,
}

fn parse(line: &[u8]) -> Line {
    if let Ok(w) = serde_json::from_slice::<Warning>(line) {
        return Line::Warning(w.warning);
    }
    match serde_json::from_slice::<SliceProgress>(line) {
        Ok(p) => Line::Progress(p),
        Err(_) => Line::Other,
    }
}
