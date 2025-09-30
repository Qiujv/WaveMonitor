import msgpack
import msgpack_numpy


def decode(data: bytes):
    return msgpack.unpackb(data, object_hook=msgpack_numpy.decode, raw=False)


def encode(obj) -> bytes:
    return msgpack.packb(obj, default=msgpack_numpy.encode, use_bin_type=True)
