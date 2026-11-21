# Backwards compatibility  # noqa: D104
from bids_validator import BIDSValidator

from .index import BIDSLayoutIndexer
from .layout import BIDSLayout, Query
from .models import (
    BIDSDataFile,
    BIDSFile,
    BIDSImageFile,
    BIDSJSONFile,
    Config,
    Entity,
    MetadataProvenance,
    Tag,
)
from .provenance import (
    FieldProvenance,
    MetadataProvenanceReport,
    MetadataSource,
    explain_metadata_provenance,
)
from .utils import add_config_paths, parse_file_entities

__all__ = [
    'BIDSLayout',
    'BIDSLayoutIndexer',
    'BIDSValidator',
    'add_config_paths',
    'parse_file_entities',
    'BIDSFile',
    'BIDSImageFile',
    'BIDSDataFile',
    'BIDSJSONFile',
    'Config',
    'Entity',
    'Tag',
    'Query',
    'MetadataProvenance',
    'MetadataProvenanceReport',
    'FieldProvenance',
    'MetadataSource',
    'explain_metadata_provenance',
]
