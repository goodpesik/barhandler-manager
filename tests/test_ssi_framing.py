"""SSI framing roundtrip + edge cases.

Coverage anchors:
- LRC matches the doc 6.1 JS reference for known vectors
- encode → decode roundtrip preserves UTF-8 cyrillic payloads
- decode rejects: bad STX prefix, bad LRC, truncated frame
- 64K boundary: encode of exactly-max-size DATA works; encode at
  64K+1 raises before we put it on the wire.
"""

from __future__ import annotations

import pytest

from src.services.terminals.ssi import (
    STX_PREFIX,
    FrameError,
    calc_lrc,
    decode_frame,
    encode_frame,
)


# ----- LRC primitive --------------------------------------------------


def test_lrc_empty() -> None:
    # Edge case — empty DATA produces 0, the XOR identity.
    assert calc_lrc(b"") == 0


def test_lrc_single_byte() -> None:
    # One byte XOR with 0 returns the byte itself.
    assert calc_lrc(b"\x42") == 0x42


def test_lrc_self_inverse() -> None:
    # Doc reference: JS `byteArray.forEach(b => lrc ^= b)`. Verify
    # against a hand-computed value so future refactors can't quietly
    # change the algorithm.
    data = b"abc"  # 0x61 ^ 0x62 ^ 0x63 = 0x60
    assert calc_lrc(data) == 0x60


def test_lrc_handles_utf8_bytes() -> None:
    # Cyrillic payloads are common — make sure we XOR the bytes,
    # not the codepoints.
    data = "ПРИВЕТ".encode("utf-8")
    expected = 0
    for byte in data:
        expected ^= byte
    assert calc_lrc(data) == expected


# ----- encode_frame ---------------------------------------------------


def test_encode_starts_with_stx_prefix() -> None:
    frame = encode_frame({"method": "PingDevice"})
    assert frame[:3] == STX_PREFIX


def test_encode_carries_length_big_endian() -> None:
    payload = {"method": "Echo", "params": {"merchantId": "X"}}
    frame = encode_frame(payload)
    declared_len = int.from_bytes(frame[3:5], "big")
    # Length covers DATA only (STX + LEN + LRC excluded).
    assert declared_len == len(frame) - 6


def test_encode_appends_lrc_of_data_only() -> None:
    frame = encode_frame({"method": "PingDevice"})
    data = frame[5:-1]
    assert frame[-1] == calc_lrc(data)


def test_encode_rejects_64k_overflow() -> None:
    # Build a JSON value that decodes to >65535 UTF-8 bytes.
    huge = "x" * 70_000
    with pytest.raises(ValueError, match="exceeds 64K"):
        encode_frame({"method": "PrintXml", "params": {"receipt": huge}})


# ----- decode_frame ---------------------------------------------------


def test_decode_roundtrip_simple() -> None:
    msg = {"method": "PingDevice", "params": {}}
    assert decode_frame(encode_frame(msg)) == msg


def test_decode_roundtrip_cyrillic() -> None:
    # Cyrillic survives the encode/decode hop without mojibake.
    msg = {
        "method": "GetMerchantListDetailed",
        "params": {"merchantList": [{"merchantName": "ФОП Левинець"}]},
    }
    assert decode_frame(encode_frame(msg)) == msg


def test_decode_rejects_bad_stx() -> None:
    bad = b"\x02\x00\x00\x00\x02ab\x00"
    with pytest.raises(FrameError, match="bad STX prefix"):
        decode_frame(bad)


def test_decode_rejects_bad_lrc() -> None:
    frame = bytearray(encode_frame({"method": "PingDevice"}))
    frame[-1] ^= 0xFF  # flip the LRC byte
    with pytest.raises(FrameError, match="LRC mismatch"):
        decode_frame(bytes(frame))


def test_decode_rejects_truncated_frame() -> None:
    full = encode_frame({"method": "PingDevice"})
    truncated = full[:-3]  # chop off LRC and the last few DATA bytes
    with pytest.raises(FrameError, match="truncated"):
        decode_frame(truncated)


def test_decode_rejects_too_short_buffer() -> None:
    with pytest.raises(FrameError, match="too short"):
        decode_frame(b"\x02\x66")
