//! Bounded, checksummed, fsync-before-advance segmented WAL.

use std::{
    fs::{File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

use fs2::FileExt as _;
use prost::Message;

use crate::{EdgeError, EdgeResult};

const MAGIC: &[u8; 8] = b"MSWAL001";
const CHECKPOINT_MAGIC: &[u8; 8] = b"MSWCP001";
const VERSION: u16 = 1;
const HEADER_BYTES: usize = 36;
const CHECKPOINT_BYTES: usize = 24;

/// Logical durable stream; each target owns separate directories for all five.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum WalKind {
    /// Qualified source snapshots and durable supplemental hints.
    Source = 1,
    /// Final, valid inference inputs.
    InferenceInput = 2,
    /// Validated central-inference results pending canonical ACK.
    InferenceResult = 3,
    /// Fenced effect state transitions and exact readback.
    EffectJournal = 4,
    /// Inference route prepare/readback/canonical-active recovery.
    RouteJournal = 5,
}

impl WalKind {
    fn from_u8(value: u8) -> EdgeResult<Self> {
        match value {
            1 => Ok(Self::Source),
            2 => Ok(Self::InferenceInput),
            3 => Ok(Self::InferenceResult),
            4 => Ok(Self::EffectJournal),
            5 => Ok(Self::RouteJournal),
            _ => Err(EdgeError::WalCorrupt(format!("unknown WAL kind {value}"))),
        }
    }
}

/// Exact capacity of a logical WAL.
#[derive(Clone, Copy, Debug)]
pub struct WalLimits {
    /// Maximum bytes across retained segments.
    pub max_bytes: u64,
    /// Maximum retained records.
    pub max_records: u64,
    /// Segment rotation threshold.
    pub segment_bytes: u64,
    /// Maximum record payload.
    pub max_record_bytes: usize,
    /// Maximum age of the oldest uncheckpointed record.
    pub max_age_seconds: u64,
}

/// Durable record returned by recovery/replay.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WalRecord {
    /// Monotonic sequence within this logical WAL.
    pub sequence: u64,
    /// Durable wall-clock admission time used only for the cross-restart age fence.
    pub recorded_at_unix_ms: u64,
    /// Opaque protobuf bytes owned by the contract layer.
    pub payload: Vec<u8>,
}

/// Current bounded durable-store pressure.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct WalStats {
    /// Retained segment bytes.
    pub bytes: u64,
    /// Retained record count.
    pub records: u64,
    /// Highest appended sequence, zero for empty.
    pub last_sequence: u64,
    /// Highest externally committed sequence.
    pub checkpoint_sequence: u64,
}

#[derive(Clone, Debug)]
struct Segment {
    path: PathBuf,
    start: u64,
    end: u64,
    bytes: u64,
    records: u64,
    first_recorded_at_unix_ms: u64,
    last_recorded_at_unix_ms: u64,
}

/// Single-writer segmented WAL. The held lock prevents two actors from sharing
/// a writable journal or queue directory.
#[derive(Debug)]
pub struct DurableWal {
    dir: PathBuf,
    kind: WalKind,
    limits: WalLimits,
    lock: File,
    current: File,
    segments: Vec<Segment>,
    stats: WalStats,
    oldest_pending_recorded_at_unix_ms: Option<u64>,
    last_recorded_at_unix_ms: u64,
}

impl DurableWal {
    /// Open and recover a WAL, truncating only an incomplete final crash tail.
    pub fn open(dir: &Path, kind: WalKind, limits: WalLimits) -> EdgeResult<Self> {
        validate_limits(limits)?;
        ensure_directory(dir)?;
        let lock_path = dir.join("writer.lock");
        let lock = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(&lock_path)
            .map_err(|source| EdgeError::io("open WAL writer lock", source))?;
        lock.try_lock_exclusive().map_err(|error| {
            EdgeError::precondition(
                "WAL_WRITER_CONFLICT",
                format!("WAL already has a writer: {error}"),
            )
        })?;

        let checkpoint = read_checkpoint(dir, kind)?;
        let mut paths = segment_paths(dir)?;
        paths.sort_unstable();
        let mut segments = Vec::with_capacity(paths.len().max(1));
        let mut expected_sequence = 1_u64;
        let mut total_bytes = 0_u64;
        let mut total_records = 0_u64;
        let mut previous_recorded_at_unix_ms = 0_u64;
        for (index, path) in paths.iter().enumerate() {
            let expected_start = parse_segment_start(path)?;
            if expected_start != expected_sequence {
                return Err(EdgeError::WalCorrupt(format!(
                    "segment starts at {expected_start}, expected {expected_sequence}"
                )));
            }
            let final_segment = index + 1 == paths.len();
            let segment = scan_segment(path, kind, expected_sequence, final_segment, limits)?;
            if segment.records > 0
                && segment.first_recorded_at_unix_ms < previous_recorded_at_unix_ms
            {
                return Err(EdgeError::precondition(
                    "WAL_CLOCK_UNPROVABLE",
                    "WAL record time regressed across segment boundaries",
                ));
            }
            if segment.records > 0 {
                previous_recorded_at_unix_ms = segment.last_recorded_at_unix_ms;
            }
            expected_sequence = segment.end.saturating_add(1);
            total_bytes = total_bytes.saturating_add(segment.bytes);
            total_records = total_records.saturating_add(segment.records);
            segments.push(segment);
        }
        let last_sequence = expected_sequence.saturating_sub(1);
        if checkpoint > last_sequence {
            return Err(EdgeError::WalCorrupt(format!(
                "checkpoint {checkpoint} exceeds last sequence {last_sequence}"
            )));
        }
        if total_bytes > limits.max_bytes || total_records > limits.max_records {
            return Err(EdgeError::exhausted(
                "WAL_FULL",
                "recovered WAL exceeds configured limits",
            ));
        }
        if segments.is_empty() {
            let path = segment_path(dir, 1);
            let current = create_segment(&path)?;
            fsync_directory(dir)?;
            segments.push(Segment {
                path,
                start: 1,
                end: 0,
                bytes: 0,
                records: 0,
                first_recorded_at_unix_ms: 0,
                last_recorded_at_unix_ms: 0,
            });
            return Ok(Self {
                dir: dir.to_owned(),
                kind,
                limits,
                lock,
                current,
                segments,
                stats: WalStats {
                    bytes: 0,
                    records: 0,
                    last_sequence: 0,
                    checkpoint_sequence: checkpoint,
                },
                oldest_pending_recorded_at_unix_ms: None,
                last_recorded_at_unix_ms: 0,
            });
        }
        let current_path = segments
            .last()
            .map(|segment| segment.path.clone())
            .ok_or_else(|| EdgeError::WalCorrupt("missing recovered current segment".into()))?;
        let current = OpenOptions::new()
            .append(true)
            .read(true)
            .open(current_path)
            .map_err(|source| EdgeError::io("open current WAL segment", source))?;
        let oldest_pending_recorded_at_unix_ms =
            oldest_pending_timestamp(&segments, checkpoint, kind, limits)?;
        let mut wal = Self {
            dir: dir.to_owned(),
            kind,
            limits,
            lock,
            current,
            segments,
            stats: WalStats {
                bytes: total_bytes,
                records: total_records,
                last_sequence,
                checkpoint_sequence: checkpoint,
            },
            oldest_pending_recorded_at_unix_ms,
            last_recorded_at_unix_ms: previous_recorded_at_unix_ms,
        };
        wal.compact()?;
        Ok(wal)
    }

    /// Append opaque bytes and fsync before returning their sequence.
    pub fn append(&mut self, payload: &[u8]) -> EdgeResult<u64> {
        self.check_retention()?;
        if payload.len() > self.limits.max_record_bytes {
            return Err(EdgeError::exhausted(
                "WAL_RECORD_TOO_LARGE",
                format!(
                    "record is {} bytes; limit is {}",
                    payload.len(),
                    self.limits.max_record_bytes
                ),
            ));
        }
        let record_bytes = HEADER_BYTES as u64 + payload.len() as u64;
        if self.stats.bytes.saturating_add(record_bytes) > self.limits.max_bytes
            || self.stats.records.saturating_add(1) > self.limits.max_records
        {
            return Err(EdgeError::exhausted(
                "WAL_FULL",
                "unacknowledged durable records reached the configured limit",
            ));
        }
        let current_bytes = self.segments.last().map_or(0, |segment| segment.bytes);
        if current_bytes > 0
            && current_bytes.saturating_add(record_bytes) > self.limits.segment_bytes
        {
            self.rotate()?;
        }
        let sequence = self.stats.last_sequence.saturating_add(1);
        let recorded_at_unix_ms = unix_ms()?;
        if recorded_at_unix_ms < self.last_recorded_at_unix_ms {
            return Err(EdgeError::precondition(
                "WAL_CLOCK_UNPROVABLE",
                "system time regressed behind the last durable WAL record",
            ));
        }
        let header = encode_header(self.kind, sequence, recorded_at_unix_ms, payload)?;
        self.current
            .write_all(&header)
            .and_then(|()| self.current.write_all(payload))
            .and_then(|()| self.current.sync_data())
            .map_err(|source| EdgeError::io("append and fsync WAL record", source))?;
        let current = self
            .segments
            .last_mut()
            .ok_or_else(|| EdgeError::WalCorrupt("current segment disappeared".into()))?;
        current.end = sequence;
        current.bytes = current.bytes.saturating_add(record_bytes);
        current.records = current.records.saturating_add(1);
        if current.first_recorded_at_unix_ms == 0 {
            current.first_recorded_at_unix_ms = recorded_at_unix_ms;
        }
        current.last_recorded_at_unix_ms = recorded_at_unix_ms;
        self.stats.bytes = self.stats.bytes.saturating_add(record_bytes);
        self.stats.records = self.stats.records.saturating_add(1);
        self.stats.last_sequence = sequence;
        self.oldest_pending_recorded_at_unix_ms
            .get_or_insert(recorded_at_unix_ms);
        self.last_recorded_at_unix_ms = recorded_at_unix_ms;
        Ok(sequence)
    }

    /// Encode and append a protobuf message.
    pub fn append_message<M: Message>(&mut self, message: &M) -> EdgeResult<u64> {
        self.append(&message.encode_to_vec())
    }

    /// Replay bounded records strictly after the durable checkpoint.
    pub fn replay(&self, max_records: usize, max_bytes: usize) -> EdgeResult<Vec<WalRecord>> {
        self.check_retention()?;
        if max_records == 0 || max_bytes == 0 {
            return Err(EdgeError::invalid(
                "replay_limit",
                "record and byte replay limits must be non-zero",
            ));
        }
        let mut result = Vec::with_capacity(max_records.min(256));
        let mut bytes = 0_usize;
        for segment in &self.segments {
            if segment.records == 0 || segment.end <= self.stats.checkpoint_sequence {
                continue;
            }
            let records = read_segment_records(segment, self.kind, self.limits)?;
            for record in records {
                if record.sequence <= self.stats.checkpoint_sequence {
                    continue;
                }
                if result.len() == max_records
                    || bytes.saturating_add(record.payload.len()) > max_bytes
                {
                    return Ok(result);
                }
                bytes = bytes.saturating_add(record.payload.len());
                result.push(record);
            }
        }
        Ok(result)
    }

    /// Advance an externally proven checkpoint and compact only whole segments.
    pub fn checkpoint(&mut self, sequence: u64) -> EdgeResult<()> {
        self.check_retention()?;
        if sequence < self.stats.checkpoint_sequence || sequence > self.stats.last_sequence {
            return Err(EdgeError::precondition(
                "CHECKPOINT_OUT_OF_RANGE",
                format!(
                    "checkpoint {sequence} outside [{},{}]",
                    self.stats.checkpoint_sequence, self.stats.last_sequence
                ),
            ));
        }
        if sequence == self.stats.checkpoint_sequence {
            return Ok(());
        }
        write_checkpoint(&self.dir, self.kind, sequence)?;
        self.stats.checkpoint_sequence = sequence;
        self.compact()?;
        self.oldest_pending_recorded_at_unix_ms = oldest_pending_timestamp(
            &self.segments,
            self.stats.checkpoint_sequence,
            self.kind,
            self.limits,
        )?;
        Ok(())
    }

    /// Return exact retained resource usage.
    #[must_use]
    pub fn stats(&self) -> WalStats {
        self.stats
    }

    /// Sequence that the next successful append will receive.
    #[must_use]
    pub fn next_sequence(&self) -> u64 {
        self.stats.last_sequence.saturating_add(1)
    }

    /// True when retained bytes are at or above the supplied ratio.
    #[must_use]
    pub fn at_watermark(&self, numerator: u64, denominator: u64) -> bool {
        denominator != 0
            && self.stats.bytes.saturating_mul(denominator)
                >= self.limits.max_bytes.saturating_mul(numerator)
    }

    /// Fail closed when the oldest uncheckpointed record exceeds its frozen
    /// retention budget or wall-clock continuity cannot be proven. Records are
    /// retained; this fence never deletes or overwrites unacknowledged data.
    pub fn check_retention(&self) -> EdgeResult<()> {
        let now = unix_ms()?;
        if now < self.last_recorded_at_unix_ms {
            return Err(EdgeError::precondition(
                "WAL_CLOCK_UNPROVABLE",
                "current system time precedes the last durable WAL record",
            ));
        }
        if let Some(oldest) = self.oldest_pending_recorded_at_unix_ms {
            let maximum_age_ms = self.limits.max_age_seconds.saturating_mul(1_000);
            if now.saturating_sub(oldest) > maximum_age_ms {
                return Err(EdgeError::precondition(
                    "WAL_RETENTION_EXPIRED",
                    "oldest uncheckpointed WAL record exceeds the configured age bound",
                ));
            }
        }
        Ok(())
    }

    fn rotate(&mut self) -> EdgeResult<()> {
        self.current
            .sync_all()
            .map_err(|source| EdgeError::io("fsync WAL before rotation", source))?;
        let start = self.stats.last_sequence.saturating_add(1);
        let path = segment_path(&self.dir, start);
        let current = create_segment(&path)?;
        fsync_directory(&self.dir)?;
        self.current = current;
        self.segments.push(Segment {
            path,
            start,
            end: start.saturating_sub(1),
            bytes: 0,
            records: 0,
            first_recorded_at_unix_ms: 0,
            last_recorded_at_unix_ms: 0,
        });
        Ok(())
    }

    fn compact(&mut self) -> EdgeResult<()> {
        while self.segments.len() > 1 {
            let removable = self.segments.first().is_some_and(|segment| {
                segment.records == 0 || segment.end <= self.stats.checkpoint_sequence
            });
            if !removable {
                break;
            }
            let segment = self.segments.remove(0);
            std::fs::remove_file(&segment.path)
                .map_err(|source| EdgeError::io("remove checkpointed WAL segment", source))?;
            self.stats.bytes = self.stats.bytes.saturating_sub(segment.bytes);
            self.stats.records = self.stats.records.saturating_sub(segment.records);
            fsync_directory(&self.dir)?;
        }
        Ok(())
    }
}

impl Drop for DurableWal {
    fn drop(&mut self) {
        let _ = self.current.sync_data();
        let _ = fs2::FileExt::unlock(&self.lock);
    }
}

fn validate_limits(limits: WalLimits) -> EdgeResult<()> {
    if limits.max_bytes < limits.segment_bytes
        || limits.max_records == 0
        || limits.max_record_bytes == 0
        || limits.max_age_seconds == 0
        || limits.max_age_seconds > 86_400
        || limits.segment_bytes < HEADER_BYTES as u64 + limits.max_record_bytes as u64
    {
        return Err(EdgeError::invalid("wal_limits", "inconsistent WAL limits"));
    }
    Ok(())
}

fn ensure_directory(path: &Path) -> EdgeResult<()> {
    if path.exists() {
        let metadata = std::fs::symlink_metadata(path)
            .map_err(|source| EdgeError::io("stat WAL directory", source))?;
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err(EdgeError::precondition(
                "WAL_PATH_INVALID",
                "WAL path must be a non-symlink directory",
            ));
        }
        return Ok(());
    }
    std::fs::create_dir_all(path)
        .map_err(|source| EdgeError::io("create WAL directory", source))?;
    fsync_directory(path.parent().unwrap_or(path))
}

fn segment_paths(dir: &Path) -> EdgeResult<Vec<PathBuf>> {
    let mut result = Vec::new();
    for entry in
        std::fs::read_dir(dir).map_err(|source| EdgeError::io("list WAL directory", source))?
    {
        let entry = entry.map_err(|source| EdgeError::io("read WAL directory entry", source))?;
        let path = entry.path();
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.starts_with("segment-") && name.ends_with(".wal") {
            let metadata = entry
                .metadata()
                .map_err(|source| EdgeError::io("stat WAL segment", source))?;
            if !metadata.is_file() {
                return Err(EdgeError::WalCorrupt(format!(
                    "segment is not a regular file: {}",
                    path.display()
                )));
            }
            result.push(path);
        }
    }
    Ok(result)
}

fn segment_path(dir: &Path, start: u64) -> PathBuf {
    dir.join(format!("segment-{start:020}.wal"))
}

fn parse_segment_start(path: &Path) -> EdgeResult<u64> {
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| EdgeError::WalCorrupt("non-UTF8 segment name".into()))?;
    let digits = name
        .strip_prefix("segment-")
        .and_then(|rest| rest.strip_suffix(".wal"))
        .ok_or_else(|| EdgeError::WalCorrupt(format!("invalid segment name {name}")))?;
    if digits.len() != 20 || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(EdgeError::WalCorrupt(format!(
            "invalid segment sequence {name}"
        )));
    }
    digits
        .parse::<u64>()
        .map_err(|error| EdgeError::WalCorrupt(format!("invalid segment sequence: {error}")))
}

fn create_segment(path: &Path) -> EdgeResult<File> {
    OpenOptions::new()
        .create_new(true)
        .append(true)
        .read(true)
        .open(path)
        .map_err(|source| EdgeError::io("create WAL segment", source))
}

fn encode_header(
    kind: WalKind,
    sequence: u64,
    recorded_at_unix_ms: u64,
    payload: &[u8],
) -> EdgeResult<[u8; HEADER_BYTES]> {
    let payload_len = u32::try_from(payload.len())
        .map_err(|_| EdgeError::exhausted("WAL_RECORD_TOO_LARGE", "payload exceeds u32"))?;
    let mut header = [0_u8; HEADER_BYTES];
    header[0..8].copy_from_slice(MAGIC);
    header[8..10].copy_from_slice(&VERSION.to_be_bytes());
    header[10] = kind as u8;
    header[11] = 0;
    header[12..20].copy_from_slice(&sequence.to_be_bytes());
    header[20..28].copy_from_slice(&recorded_at_unix_ms.to_be_bytes());
    header[28..32].copy_from_slice(&payload_len.to_be_bytes());
    let crc = record_crc(&header[8..32], payload);
    header[32..36].copy_from_slice(&crc.to_be_bytes());
    Ok(header)
}

fn record_crc(metadata: &[u8], payload: &[u8]) -> u32 {
    let mut bytes = Vec::with_capacity(metadata.len() + payload.len());
    bytes.extend_from_slice(metadata);
    bytes.extend_from_slice(payload);
    crc32c::crc32c(&bytes)
}

fn scan_segment(
    path: &Path,
    kind: WalKind,
    expected_start: u64,
    final_segment: bool,
    limits: WalLimits,
) -> EdgeResult<Segment> {
    let mut file = OpenOptions::new()
        .read(true)
        .write(final_segment)
        .open(path)
        .map_err(|source| EdgeError::io("open WAL segment for recovery", source))?;
    let file_len = file
        .metadata()
        .map_err(|source| EdgeError::io("stat WAL segment for recovery", source))?
        .len();
    let mut offset = 0_u64;
    let mut expected = expected_start;
    let mut records = 0_u64;
    let mut first_recorded_at_unix_ms = 0_u64;
    let mut last_recorded_at_unix_ms = 0_u64;
    while offset < file_len {
        let remaining = file_len - offset;
        if remaining < HEADER_BYTES as u64 {
            return truncate_tail_or_corrupt(
                &mut file,
                path,
                offset,
                final_segment,
                "partial header",
            )
            .map(|()| Segment {
                path: path.to_owned(),
                start: expected_start,
                end: expected.saturating_sub(1),
                bytes: offset,
                records,
                first_recorded_at_unix_ms,
                last_recorded_at_unix_ms,
            });
        }
        let mut header = [0_u8; HEADER_BYTES];
        file.read_exact(&mut header)
            .map_err(|source| EdgeError::io("read WAL header", source))?;
        validate_header(&header, kind, expected, limits)?;
        let recorded_at_unix_ms = header_recorded_at(&header)?;
        if recorded_at_unix_ms < last_recorded_at_unix_ms {
            return Err(EdgeError::precondition(
                "WAL_CLOCK_UNPROVABLE",
                format!("WAL record time regressed at {}:{offset}", path.display()),
            ));
        }
        if first_recorded_at_unix_ms == 0 {
            first_recorded_at_unix_ms = recorded_at_unix_ms;
        }
        last_recorded_at_unix_ms = recorded_at_unix_ms;
        let length = u32::from_be_bytes(
            header[28..32]
                .try_into()
                .map_err(|_| EdgeError::WalCorrupt("invalid WAL payload-length bytes".into()))?,
        ) as usize;
        let total = HEADER_BYTES as u64 + length as u64;
        if remaining < total {
            return truncate_tail_or_corrupt(
                &mut file,
                path,
                offset,
                final_segment,
                "partial payload",
            )
            .map(|()| Segment {
                path: path.to_owned(),
                start: expected_start,
                end: expected.saturating_sub(1),
                bytes: offset,
                records,
                first_recorded_at_unix_ms,
                last_recorded_at_unix_ms,
            });
        }
        let mut payload = vec![0_u8; length];
        file.read_exact(&mut payload)
            .map_err(|source| EdgeError::io("read WAL payload", source))?;
        let expected_crc = u32::from_be_bytes(
            header[32..36]
                .try_into()
                .map_err(|_| EdgeError::WalCorrupt("invalid WAL checksum bytes".into()))?,
        );
        if record_crc(&header[8..32], &payload) != expected_crc {
            return Err(EdgeError::WalCorrupt(format!(
                "checksum mismatch at {}:{offset}",
                path.display()
            )));
        }
        offset = offset.saturating_add(total);
        expected = expected.saturating_add(1);
        records = records.saturating_add(1);
    }
    Ok(Segment {
        path: path.to_owned(),
        start: expected_start,
        end: expected.saturating_sub(1),
        bytes: offset,
        records,
        first_recorded_at_unix_ms,
        last_recorded_at_unix_ms,
    })
}

fn validate_header(
    header: &[u8; HEADER_BYTES],
    kind: WalKind,
    expected_sequence: u64,
    limits: WalLimits,
) -> EdgeResult<()> {
    if &header[0..8] != MAGIC {
        return Err(EdgeError::WalCorrupt("WAL magic mismatch".into()));
    }
    let version = u16::from_be_bytes(
        header[8..10]
            .try_into()
            .map_err(|_| EdgeError::WalCorrupt("invalid WAL version bytes".into()))?,
    );
    if version != VERSION {
        return Err(EdgeError::WalCorrupt(format!(
            "unsupported WAL version {version}"
        )));
    }
    if WalKind::from_u8(header[10])? != kind || header[11] != 0 {
        return Err(EdgeError::WalCorrupt(
            "WAL kind or reserved flags mismatch".into(),
        ));
    }
    let sequence = u64::from_be_bytes(
        header[12..20]
            .try_into()
            .map_err(|_| EdgeError::WalCorrupt("invalid WAL sequence bytes".into()))?,
    );
    if sequence != expected_sequence {
        return Err(EdgeError::WalCorrupt(format!(
            "WAL sequence {sequence}, expected {expected_sequence}"
        )));
    }
    header_recorded_at(header)?;
    let length = u32::from_be_bytes(
        header[28..32]
            .try_into()
            .map_err(|_| EdgeError::WalCorrupt("invalid WAL length bytes".into()))?,
    ) as usize;
    if length > limits.max_record_bytes {
        return Err(EdgeError::WalCorrupt(format!(
            "record length {length} exceeds {}",
            limits.max_record_bytes
        )));
    }
    Ok(())
}

fn header_recorded_at(header: &[u8; HEADER_BYTES]) -> EdgeResult<u64> {
    let recorded_at_unix_ms = u64::from_be_bytes(
        header[20..28]
            .try_into()
            .map_err(|_| EdgeError::WalCorrupt("invalid WAL record-time bytes".into()))?,
    );
    if recorded_at_unix_ms == 0 {
        return Err(EdgeError::WalCorrupt(
            "WAL record time must be positive".into(),
        ));
    }
    Ok(recorded_at_unix_ms)
}

fn truncate_tail_or_corrupt(
    file: &mut File,
    path: &Path,
    offset: u64,
    final_segment: bool,
    detail: &str,
) -> EdgeResult<()> {
    if !final_segment {
        return Err(EdgeError::WalCorrupt(format!(
            "{detail} before final segment at {}:{offset}",
            path.display()
        )));
    }
    file.set_len(offset)
        .and_then(|()| file.sync_all())
        .map_err(|source| EdgeError::io("truncate incomplete final WAL tail", source))
}

fn read_segment_records(
    segment: &Segment,
    kind: WalKind,
    limits: WalLimits,
) -> EdgeResult<Vec<WalRecord>> {
    let mut file = File::open(&segment.path)
        .map_err(|source| EdgeError::io("open WAL segment for replay", source))?;
    let mut result = Vec::with_capacity(segment.records as usize);
    let mut expected = segment.start;
    for _ in 0..segment.records {
        let mut header = [0_u8; HEADER_BYTES];
        file.read_exact(&mut header)
            .map_err(|source| EdgeError::io("read replay WAL header", source))?;
        validate_header(&header, kind, expected, limits)?;
        let recorded_at_unix_ms = header_recorded_at(&header)?;
        let length = u32::from_be_bytes(
            header[28..32]
                .try_into()
                .map_err(|_| EdgeError::WalCorrupt("invalid replay WAL length bytes".into()))?,
        ) as usize;
        let mut payload = vec![0_u8; length];
        file.read_exact(&mut payload)
            .map_err(|source| EdgeError::io("read replay WAL payload", source))?;
        let expected_crc = u32::from_be_bytes(
            header[32..36]
                .try_into()
                .map_err(|_| EdgeError::WalCorrupt("invalid replay WAL checksum bytes".into()))?,
        );
        if record_crc(&header[8..32], &payload) != expected_crc {
            return Err(EdgeError::WalCorrupt(format!(
                "checksum mismatch during replay at sequence {expected}"
            )));
        }
        result.push(WalRecord {
            sequence: expected,
            recorded_at_unix_ms,
            payload,
        });
        expected = expected.saturating_add(1);
    }
    Ok(result)
}

fn oldest_pending_timestamp(
    segments: &[Segment],
    checkpoint: u64,
    kind: WalKind,
    limits: WalLimits,
) -> EdgeResult<Option<u64>> {
    for segment in segments {
        if segment.records == 0 || segment.end <= checkpoint {
            continue;
        }
        if checkpoint < segment.start {
            return Ok(Some(segment.first_recorded_at_unix_ms));
        }
        return first_timestamp_after(segment, checkpoint, kind, limits);
    }
    Ok(None)
}

fn first_timestamp_after(
    segment: &Segment,
    checkpoint: u64,
    kind: WalKind,
    limits: WalLimits,
) -> EdgeResult<Option<u64>> {
    let mut file = File::open(&segment.path)
        .map_err(|source| EdgeError::io("open WAL segment for retention cursor", source))?;
    let mut expected = segment.start;
    for _ in 0..segment.records {
        let mut header = [0_u8; HEADER_BYTES];
        file.read_exact(&mut header)
            .map_err(|source| EdgeError::io("read WAL retention header", source))?;
        validate_header(&header, kind, expected, limits)?;
        let recorded_at_unix_ms = header_recorded_at(&header)?;
        let length = u32::from_be_bytes(
            header[28..32]
                .try_into()
                .map_err(|_| EdgeError::WalCorrupt("invalid retention WAL length bytes".into()))?,
        );
        if expected > checkpoint {
            return Ok(Some(recorded_at_unix_ms));
        }
        file.seek(SeekFrom::Current(i64::from(length)))
            .map_err(|source| EdgeError::io("seek WAL retention payload", source))?;
        expected = expected.saturating_add(1);
    }
    Ok(None)
}

fn checkpoint_path(dir: &Path) -> PathBuf {
    dir.join("checkpoint.bin")
}

fn read_checkpoint(dir: &Path, kind: WalKind) -> EdgeResult<u64> {
    let path = checkpoint_path(dir);
    if !path.exists() {
        return Ok(0);
    }
    let mut payload = [0_u8; CHECKPOINT_BYTES];
    let mut file =
        File::open(path).map_err(|source| EdgeError::io("open WAL checkpoint", source))?;
    file.read_exact(&mut payload)
        .map_err(|source| EdgeError::io("read WAL checkpoint", source))?;
    if file
        .seek(SeekFrom::End(0))
        .map_err(|source| EdgeError::io("measure WAL checkpoint", source))?
        != CHECKPOINT_BYTES as u64
    {
        return Err(EdgeError::WalCorrupt(
            "checkpoint has an invalid length".into(),
        ));
    }
    if &payload[0..8] != CHECKPOINT_MAGIC
        || u16::from_be_bytes(
            payload[8..10]
                .try_into()
                .map_err(|_| EdgeError::WalCorrupt("invalid checkpoint version bytes".into()))?,
        ) != VERSION
        || WalKind::from_u8(payload[10])? != kind
        || payload[11] != 0
    {
        return Err(EdgeError::WalCorrupt("checkpoint identity mismatch".into()));
    }
    let expected_crc = u32::from_be_bytes(
        payload[20..24]
            .try_into()
            .map_err(|_| EdgeError::WalCorrupt("invalid checkpoint checksum bytes".into()))?,
    );
    if crc32c::crc32c(&payload[8..20]) != expected_crc {
        return Err(EdgeError::WalCorrupt("checkpoint checksum mismatch".into()));
    }
    Ok(u64::from_be_bytes(payload[12..20].try_into().map_err(
        |_| EdgeError::WalCorrupt("invalid checkpoint sequence bytes".into()),
    )?))
}

fn write_checkpoint(dir: &Path, kind: WalKind, sequence: u64) -> EdgeResult<()> {
    let mut payload = [0_u8; CHECKPOINT_BYTES];
    payload[0..8].copy_from_slice(CHECKPOINT_MAGIC);
    payload[8..10].copy_from_slice(&VERSION.to_be_bytes());
    payload[10] = kind as u8;
    payload[11] = 0;
    payload[12..20].copy_from_slice(&sequence.to_be_bytes());
    let crc = crc32c::crc32c(&payload[8..20]);
    payload[20..24].copy_from_slice(&crc.to_be_bytes());
    let temporary = dir.join(format!("checkpoint-{}.tmp", uuid::Uuid::new_v4()));
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&temporary)
        .map_err(|source| EdgeError::io("create temporary WAL checkpoint", source))?;
    file.write_all(&payload)
        .and_then(|()| file.sync_all())
        .map_err(|source| EdgeError::io("write and fsync WAL checkpoint", source))?;
    std::fs::rename(&temporary, checkpoint_path(dir))
        .map_err(|source| EdgeError::io("atomically replace WAL checkpoint", source))?;
    fsync_directory(dir)
}

fn fsync_directory(path: &Path) -> EdgeResult<()> {
    let directory =
        File::open(path).map_err(|source| EdgeError::io("open directory for fsync", source))?;
    directory
        .sync_all()
        .map_err(|source| EdgeError::io("fsync directory", source))
}

fn unix_ms() -> EdgeResult<u64> {
    let duration = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| EdgeError::precondition("CLOCK_INVALID", "system clock before Unix epoch"))?;
    u64::try_from(duration.as_millis())
        .map_err(|_| EdgeError::precondition("CLOCK_INVALID", "Unix milliseconds exceed u64"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn limits() -> WalLimits {
        WalLimits {
            max_bytes: 4096,
            max_records: 16,
            segment_bytes: 128,
            max_record_bytes: 64,
            max_age_seconds: 86_400,
        }
    }

    fn temporary() -> Result<tempfile::TempDir, std::io::Error> {
        tempfile::tempdir()
    }

    fn required_error<T>(
        result: EdgeResult<T>,
        message: &'static str,
    ) -> Result<EdgeError, Box<dyn std::error::Error>> {
        result.err().ok_or_else(|| message.into())
    }

    fn rewrite_record_times(
        wal: &DurableWal,
        recorded_at_unix_ms: &[u64],
    ) -> Result<(), Box<dyn std::error::Error>> {
        let mut time_index = 0_usize;
        for segment in &wal.segments {
            let records = read_segment_records(segment, wal.kind, wal.limits)?;
            let mut file = OpenOptions::new()
                .write(true)
                .truncate(true)
                .open(&segment.path)?;
            for record in records {
                let recorded_at = *recorded_at_unix_ms
                    .get(time_index)
                    .ok_or("missing replacement WAL record time")?;
                file.write_all(&encode_header(
                    wal.kind,
                    record.sequence,
                    recorded_at,
                    &record.payload,
                )?)?;
                file.write_all(&record.payload)?;
                time_index = time_index.saturating_add(1);
            }
            file.sync_all()?;
        }
        if time_index != recorded_at_unix_ms.len() {
            return Err("unused replacement WAL record time".into());
        }
        Ok(())
    }

    #[test]
    fn append_recover_replay_checkpoint() -> Result<(), Box<dyn std::error::Error>> {
        let temp = temporary()?;
        {
            let mut wal = DurableWal::open(temp.path(), WalKind::Source, limits())?;
            assert_eq!(1, wal.append(b"one")?);
            assert_eq!(2, wal.append(b"two")?);
            wal.checkpoint(1)?;
        }
        let wal = DurableWal::open(temp.path(), WalKind::Source, limits())?;
        assert_eq!(1, wal.stats().checkpoint_sequence);
        let replay = wal.replay(4, 1024)?;
        assert_eq!(1, replay.len());
        assert_eq!(2, replay[0].sequence);
        assert!(replay[0].recorded_at_unix_ms > 0);
        assert_eq!(b"two", replay[0].payload.as_slice());
        Ok(())
    }

    #[test]
    fn incomplete_final_tail_is_truncated() -> Result<(), Box<dyn std::error::Error>> {
        let temp = temporary()?;
        let segment;
        {
            let mut wal = DurableWal::open(temp.path(), WalKind::Source, limits())?;
            wal.append(b"one")?;
            segment = wal
                .segments
                .first()
                .ok_or_else(|| std::io::Error::other("missing WAL segment"))?
                .path
                .clone();
        }
        let mut file = OpenOptions::new().append(true).open(segment)?;
        file.write_all(b"MSWA")?;
        file.sync_all()?;
        drop(file);
        let wal = DurableWal::open(temp.path(), WalKind::Source, limits())?;
        assert_eq!(1, wal.stats().last_sequence);
        Ok(())
    }

    #[test]
    fn complete_checksum_corruption_fails_closed() -> Result<(), Box<dyn std::error::Error>> {
        let temp = temporary()?;
        let segment;
        {
            let mut wal = DurableWal::open(temp.path(), WalKind::Source, limits())?;
            wal.append(b"one")?;
            segment = wal
                .segments
                .first()
                .ok_or_else(|| std::io::Error::other("missing WAL segment"))?
                .path
                .clone();
        }
        let mut file = OpenOptions::new().write(true).open(segment)?;
        file.seek(SeekFrom::Start(HEADER_BYTES as u64))?;
        file.write_all(b"x")?;
        file.sync_all()?;
        assert!(DurableWal::open(temp.path(), WalKind::Source, limits()).is_err());
        Ok(())
    }

    #[test]
    fn second_writer_is_rejected() -> Result<(), Box<dyn std::error::Error>> {
        let temp = temporary()?;
        let _first = DurableWal::open(temp.path(), WalKind::Source, limits())?;
        assert!(DurableWal::open(temp.path(), WalKind::Source, limits()).is_err());
        Ok(())
    }

    #[test]
    fn expired_uncheckpointed_record_is_retained_and_fails_closed()
    -> Result<(), Box<dyn std::error::Error>> {
        let temp = temporary()?;
        let mut short = limits();
        short.max_age_seconds = 1;
        let payload = b"stale-uncheckpointed";
        {
            let mut wal = DurableWal::open(temp.path(), WalKind::InferenceInput, short)?;
            wal.append(payload)?;
        }
        let segment = segment_path(temp.path(), 1);
        let retained_bytes = std::fs::metadata(&segment)?.len();
        let stale_time = unix_ms()?.saturating_sub(2_000);
        let stale_header = encode_header(WalKind::InferenceInput, 1, stale_time, payload)?;
        let mut file = OpenOptions::new().write(true).open(&segment)?;
        file.seek(SeekFrom::Start(0))?;
        file.write_all(&stale_header)?;
        file.sync_all()?;
        drop(file);

        let wal = DurableWal::open(temp.path(), WalKind::InferenceInput, short)?;
        let error = required_error(wal.check_retention(), "expired WAL was accepted")?;
        assert_eq!("WAL_RETENTION_EXPIRED", error.reason_code());
        assert_eq!(
            "WAL_RETENTION_EXPIRED",
            required_error(wal.replay(4, 1024), "expired WAL replay advanced")?.reason_code()
        );
        assert_eq!(retained_bytes, std::fs::metadata(&segment)?.len());
        assert_eq!(1, wal.stats().records);
        Ok(())
    }

    #[test]
    fn oldest_uncheckpointed_record_is_recovered_inside_an_older_segment()
    -> Result<(), Box<dyn std::error::Error>> {
        let temp = temporary()?;
        let mut short = limits();
        short.segment_bytes = 100;
        short.max_age_seconds = 1;
        let stale = unix_ms()?.saturating_sub(2_000);
        {
            let mut wal = DurableWal::open(temp.path(), WalKind::InferenceResult, short)?;
            for payload in [b"record-001", b"record-002", b"record-003"] {
                wal.append(payload)?;
            }
            assert_eq!(2, wal.segments.len());
            wal.checkpoint(1)?;
            assert_eq!(2, wal.segments.len());
            rewrite_record_times(&wal, &[stale, stale, stale])?;
        }

        let wal = DurableWal::open(temp.path(), WalKind::InferenceResult, short)?;
        assert_eq!(Some(stale), wal.oldest_pending_recorded_at_unix_ms);
        let error = required_error(
            wal.check_retention(),
            "stale record after a mid-segment checkpoint was accepted",
        )?;
        assert_eq!("WAL_RETENTION_EXPIRED", error.reason_code());
        assert_eq!(
            "WAL_RETENTION_EXPIRED",
            required_error(wal.replay(8, 1024), "stale WAL replay advanced")?.reason_code()
        );
        Ok(())
    }

    #[test]
    fn record_clock_regression_across_segments_fails_recovery()
    -> Result<(), Box<dyn std::error::Error>> {
        let temp = temporary()?;
        let mut segmented = limits();
        segmented.segment_bytes = 100;
        {
            let mut wal = DurableWal::open(temp.path(), WalKind::RouteJournal, segmented)?;
            for payload in [b"record-001", b"record-002", b"record-003"] {
                wal.append(payload)?;
            }
            assert_eq!(2, wal.segments.len());
            let now = unix_ms()?;
            rewrite_record_times(&wal, &[now, now, now.saturating_sub(1)])?;
        }

        let error = required_error(
            DurableWal::open(temp.path(), WalKind::RouteJournal, segmented),
            "cross-segment clock regression was accepted",
        )?;
        assert_eq!("WAL_CLOCK_UNPROVABLE", error.reason_code());
        Ok(())
    }

    #[test]
    fn future_record_clock_fails_retention_without_deleting_data()
    -> Result<(), Box<dyn std::error::Error>> {
        let temp = temporary()?;
        let segment;
        {
            let mut wal = DurableWal::open(temp.path(), WalKind::EffectJournal, limits())?;
            wal.append(b"future-record")?;
            segment = wal.segments[0].path.clone();
            rewrite_record_times(&wal, &[unix_ms()?.saturating_add(60_000)])?;
        }
        let retained_bytes = std::fs::metadata(&segment)?.len();
        let wal = DurableWal::open(temp.path(), WalKind::EffectJournal, limits())?;
        let error = required_error(wal.check_retention(), "future WAL record was accepted")?;
        assert_eq!("WAL_CLOCK_UNPROVABLE", error.reason_code());
        assert_eq!(retained_bytes, std::fs::metadata(segment)?.len());
        assert_eq!(1, wal.stats().records);
        Ok(())
    }
}
