use sha2::{Digest, Sha256};

/// A running sha256 and byte count.
#[derive(Default)]
pub struct Hasher {
    inner: Sha256,
    size: u64,
}

impl Hasher {
    pub fn update(&mut self, chunk: &[u8]) {
        self.inner.update(chunk);
        self.size += chunk.len() as u64;
    }

    /// The lowercase hex digest and the bytes seen.
    pub fn finish(self) -> (String, u64) {
        (format!("{:x}", self.inner.finalize()), self.size)
    }
}
