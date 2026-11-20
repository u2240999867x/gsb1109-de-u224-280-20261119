"""Check the public imports of an Python client.

Courtesy of ac1982@no-response.github.com

Run with:
    python -m mypy --no-implicit-reexport --follow-imports=silent tests/typing/public_exports.py
    python tests/typing/public_exports.py
"""

from influxdb_client import (
    Bucket,
    BucketsService,
    Configuration,
    InfluxDBClient,
    OMIT_TAG,
    Point,
    TagMergeOptions,
    TagMerger,
    TagOrigin,
    WriteOptions,
    WritePrecision,
)


def check_exports() -> None:
    point = Point("temperature").tag("room", "office").field("value", 21)
    assert point.to_line_protocol() == "temperature,room=office value=21i"
    assert WriteOptions(batch_size=10).batch_size == 10
    assert WritePrecision.NS == "ns"
    assert Bucket is not None
    assert BucketsService is not None
    assert Configuration is not None
    assert InfluxDBClient is not None

    options = TagMergeOptions(default_tags={"region": "us-east-1"}, omit_tag_keys=["debug"])
    merger = TagMerger.from_options(options)
    batch = merger.merge_batch([point, Point("temperature").tag("region", OMIT_TAG).field("v", 1)])
    assert batch.records[0].tag_origins()["region"] == TagOrigin.DEFAULT
    assert batch.records[1].omitted_tags == ["region"]


if __name__ == "__main__":
    check_exports()
