from random import seed

from pytboss import codec


def test_encode_decode():
    seed(1234)
    # Client sends a PB.SetDevicePassword RPC with this encoding
    newpw = codec.encode(b"foobar")
    # The firmware then decodes and saves in the grillPassword global
    saved = codec.decode(newpw)
    # When authenticating, the password is time-encoded based on grill uptime
    param = codec.encode(b"foobar", key=codec.timed_key(11.0))
    # The firmware then decodes with its uptime
    check = codec.decode(param, key=codec.timed_key(12.0))
    # And then we compare that the saved version matches
    assert saved == check
