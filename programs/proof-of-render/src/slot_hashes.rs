//! Reading the newest entry of the SlotHashes sysvar from raw account data, without deserializing the whole
//! sysvar (which holds up to 512 entries). Layout: `u64 LE entry count`, then entries of
//! `(u64 LE slot, [u8; 32] hash)`, newest first. Spec: docs/PROTOCOL.md section 5.
use crate::errors::PorError;
use anchor_lang::prelude::*;

const LEN_PREFIX: usize = 8;
const ENTRY_SIZE: usize = 8 + 32;

/// Returns `(slot, hash)` of the first (most recent) entry, or `SlotHashesUnavailable` if the data is empty,
/// truncated, or declares more entries than it contains.
pub fn newest_slot_hash(data: &[u8]) -> Result<(u64, [u8; 32])> {
    let unavailable = || error!(PorError::SlotHashesUnavailable);
    let count = u64::from_le_bytes(
        data.get(..LEN_PREFIX)
            .and_then(|b| <[u8; 8]>::try_from(b).ok())
            .ok_or_else(unavailable)?,
    );
    if count == 0 {
        return Err(unavailable());
    }
    let declared_end = usize::try_from(count)
        .ok()
        .and_then(|c| c.checked_mul(ENTRY_SIZE))
        .and_then(|n| n.checked_add(LEN_PREFIX))
        .ok_or_else(unavailable)?;
    if declared_end > data.len() {
        return Err(unavailable());
    }
    let entry = data.get(LEN_PREFIX..LEN_PREFIX + ENTRY_SIZE).ok_or_else(unavailable)?;
    let slot = u64::from_le_bytes(<[u8; 8]>::try_from(&entry[..8]).map_err(|_| unavailable())?);
    let hash = <[u8; 32]>::try_from(&entry[8..]).map_err(|_| unavailable())?;
    Ok((slot, hash))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn data(entries: &[(u64, [u8; 32])]) -> Vec<u8> {
        let mut out = (entries.len() as u64).to_le_bytes().to_vec();
        for (slot, hash) in entries {
            out.extend_from_slice(&slot.to_le_bytes());
            out.extend_from_slice(hash);
        }
        out
    }

    #[test]
    fn reads_only_the_first_entry() {
        let d = data(&[(150, [7; 32]), (149, [8; 32]), (3, [9; 32])]);
        assert_eq!(newest_slot_hash(&d).unwrap(), (150, [7; 32]));
    }

    #[test]
    fn a_real_sized_account_with_trailing_zero_padding_is_fine() {
        let mut d = data(&[(u64::MAX, [1; 32])]);
        d.resize(20_488, 0);
        assert_eq!(newest_slot_hash(&d).unwrap(), (u64::MAX, [1; 32]));
    }

    #[test]
    fn rejects_empty_truncated_and_lying_data() {
        for bad in [vec![], vec![1, 0, 0], data(&[]), {
            let mut d = data(&[(5, [0; 32])]);
            d.truncate(d.len() - 1); // declares 1 entry, holds 39 bytes of it
            d
        }, {
            let mut d = data(&[(5, [0; 32])]);
            d[..8].copy_from_slice(&u64::MAX.to_le_bytes()); // absurd count: must not overflow or panic
            d
        }] {
            assert!(newest_slot_hash(&bad).is_err());
        }
    }
}
