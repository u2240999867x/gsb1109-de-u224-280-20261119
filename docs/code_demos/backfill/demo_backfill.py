"""Resume an append-only event backfill from destination-confirmed coverage."""
import etlhelper as etl
from etlhelper.backfill import EventRange

SELECT_SQL = """
    SELECT event_id, reading
    FROM readings
    WHERE event_id >= {start} AND event_id <= {end}
    ORDER BY event_id
    """

INSERT_SQL = """
    INSERT INTO archive (event_id, reading)
    VALUES (%(event_id)s, %(reading)s)
    """

BLOCKS = [EventRange(41, 41), EventRange(42, 44), EventRange(45, 45)]


def backfill_readings(source_conn, dest_conn):
    result = etl.backfill(
        'readings-41-45',
        source_conn,
        dest_conn,
        start_event_id=41,
        end_event_id=45,
        select_query=SELECT_SQL,
        insert_query=INSERT_SQL,
        blocks=BLOCKS,
    )
    print(f'confirmed {result.coverage}')
    print(f'rejected {result.rejected_event_ids}')
    return result
