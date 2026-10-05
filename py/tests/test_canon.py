import math

import pytest

from porf.canon import canonical_bytes, hash_json, sha256


def test_key_order_irrelevant():
    a = {"b": 1, "a": [1, 2.5, "x"], "c": {"z": None, "y": True}}
    b = {"c": {"y": True, "z": None}, "a": [1, 2.5, "x"], "b": 1}
    assert canonical_bytes(a) == canonical_bytes(b)


def test_exact_bytes_and_hash():
    obj = {"b": 1, "a": [1, 2.5, "é"], "c": {"z": None, "y": True}}
    assert canonical_bytes(obj) == b'{"a":[1,2.5,"\\u00e9"],"b":1,"c":{"y":true,"z":null}}'
    assert hash_json(obj).hex() == "eb9504ed6d071f0e465f9ab5f5e7fb193db5b7eae14953711412e8a31f7c65d5"
    assert hash_json({"a": 1}).hex() == "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"


def test_non_ascii_is_escaped():
    out = canonical_bytes({"k": "é中\U0001f600"})
    assert out.isascii()
    assert out == b'{"k":"\\u00e9\\u4e2d\\ud83d\\ude00"}'


def test_no_whitespace():
    assert b" " not in canonical_bytes({"a": [1, 2], "b": {"c": 3}})


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_nan_and_infinity_rejected(bad):
    with pytest.raises(ValueError):
        canonical_bytes({"x": bad})
    with pytest.raises(ValueError):
        canonical_bytes([1, {"deep": [bad]}])


def test_sha256_known_answer():
    assert sha256(b"").hex() == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
