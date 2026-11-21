import glob
from abc import ABC, abstractmethod
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import reduce
from logging import getLogger
from pathlib import Path
from sys import stderr
from tempfile import TemporaryDirectory
from typing import Any, Dict, Iterator, List, Optional, Union
from urllib.parse import urlparse
from uuid import uuid4

import boto3
import pandas as pd
import pyarrow.parquet as pq
from pyarrow import lib as pyarrowlib
from halo import Halo

from parquet_tools.s3_manifest import (
    S3Manifest,
    S3ManifestLister,
    raise_for_manifest,
)

logger = getLogger(__name__)


__all__ = [
    'FileNotFoundException',
    'InvalidCommandExcpetion',
    'LocalParquetFile',
    'ParquetFile',
    'S3Manifest',
    'S3ManifestLister',
    'S3ParquetFile',
    'get_datafame_from_objs',
    'raise_for_manifest',
    'to_parquet_file',
]


class InvalidCommandExcpetion(Exception):
    '''Exception for invalid command. Argment parser raises this Exception.
    '''
    pass


class FileNotFoundException(Exception):
    pass


class ParquetFile(ABC):
    '''Abstract ParquetFile.
    One object does not correspond one parquet file but one expression about file
    such as ./target.parquet, ./*.parquet, s3://bucket/foo.parquet or s3://bucket/*
    '''

    def __post_init__(self):
        self.validation()

    def validation(self) -> None:
        '''validate properties
        '''
        pass

    @abstractmethod
    def is_wildcard(self) -> bool:
        '''Return if this object correspond one or more object.
        '''
        raise NotImplementedError()

    @abstractmethod
    def resolve_wildcard(self) -> List['ParquetFile']:
        '''Return concrete Parquert file objects.
        '''
        raise NotImplementedError()

    @contextmanager
    @abstractmethod
    def get_local_path(self) -> Iterator[str]:
        '''Return local file path.
        If call this function of S3ParquetFile, return the path of downloaded file.
        '''
        raise NotImplementedError()

    @contextmanager
    def get_dataframe(self) -> pd.DataFrame:
        with self.get_local_path() as local_path:
            try:
                yield pq.read_table(local_path).to_pandas()
            except pyarrowlib.ArrowInvalid:
                print(f"File({local_path}) cannot be read as parquet.", file=stderr)
                yield None


@dataclass
class LocalParquetFile(ParquetFile):
    '''Parquet file object on local disk
    '''
    path: str

    def is_wildcard(self) -> bool:
        return '*' in self.path

    def resolve_wildcard(self) -> List[ParquetFile]:
        return sorted(
            [LocalParquetFile(f) for f in glob.glob(self.path)],
            key=lambda x: x.path
        )

    @contextmanager
    def get_local_path(self) -> Iterator[str]:
        if self.is_wildcard():
            raise Exception('Please resolve first.')
        if not Path(self.path).exists():
            raise FileNotFoundException(f'File({self.path}) not found')
        yield self.path


@dataclass
class S3ParquetFile(ParquetFile):
    '''Parquet file object on S3
    '''
    aws_session: boto3.Session
    bucket: str
    key: str
    endpoint_url: Optional[str] = None

    def validation(self):
        ''' key can have *. But it must be last of the string.
        '''
        if self.is_wildcard() and not self.key.index('*') in (-1, len(self.key) - 1):
            raise InvalidCommandExcpetion('You can use * only end of the path')

    def is_wildcard(self) -> bool:
        return '*' in self.key

    @property
    def prefix(self) -> str:
        """The S3 prefix selected by a trailing-wildcard expression."""
        if not self.is_wildcard():
            return self.key
        return self.key[:-1]  # remove *

    def _manifest_lister(
        self,
        *,
        page_size: Optional[int] = None,
        client: Any = None,
    ) -> S3ManifestLister:
        return S3ManifestLister(
            aws_session=self.aws_session,
            bucket=self.bucket,
            prefix=self.prefix,
            endpoint_url=self.endpoint_url,
            page_size=page_size,
            client=client,
        )

    def resolve_wildcard_manifest(
        self,
        checkpoint: Optional[Union[S3Manifest, str, Dict[str, Any]]] = None,
        *,
        page_size: Optional[int] = None,
        fetch_all: bool = True,
        client: Any = None,
    ) -> S3Manifest:
        """Return a resumable manifest for this wildcard expression.

        With no checkpoint this starts at the first page.  With a checkpoint
        (an ``S3Manifest``, its JSON string or its dict form) it continues
        from the stored page.  ``fetch_all=False`` fetches only one next page
        and is useful for a scheduler that wants to persist state explicitly
        after every page.
        """
        lister = self._manifest_lister(page_size=page_size, client=client)
        if checkpoint is None:
            state = lister.initial_state()
        else:
            state = _coerce_manifest(checkpoint)
        if fetch_all:
            return lister.resume(state)
        return lister.advance(state)

    def continue_wildcard_manifest(
        self,
        checkpoint: Union[S3Manifest, str, Dict[str, Any]],
        *,
        page_size: Optional[int] = None,
        client: Any = None,
    ) -> S3Manifest:
        """Resume a saved checkpoint for this expression."""
        return self.resolve_wildcard_manifest(
            checkpoint=checkpoint,
            page_size=page_size,
            fetch_all=True,
            client=client,
        )

    def resolve_wildcard_complete(
        self,
        checkpoint: Optional[Union[S3Manifest, str, Dict[str, Any]]] = None,
        *,
        page_size: Optional[int] = None,
        client: Any = None,
    ) -> S3Manifest:
        """Return a complete, conflict-free manifest or raise a specific error."""
        manifest = self.resolve_wildcard_manifest(
            checkpoint=checkpoint,
            page_size=page_size,
            fetch_all=True,
            client=client,
        )
        raise_for_manifest(manifest)
        return manifest

    def resolve_wildcard(self) -> List[ParquetFile]:
        """Resolve a wildcard to a sorted list in one call.

        This is the historical entry point.  It still returns the same list
        for a normal prefix, but it no longer silently accepts a partial
        listing, a conflicting observation, a wrong endpoint or a token
        cycle.  Callers that need to persist and resume should use
        ``resolve_wildcard_manifest`` instead.
        """
        manifest = self.resolve_wildcard_manifest()
        raise_for_manifest(manifest)

        return [
            S3ParquetFile(
                aws_session=self.aws_session,
                bucket=self.bucket,
                key=observation.key,
                endpoint_url=self.endpoint_url,
            )
            for observation in manifest.objects
        ]

    @contextmanager
    def get_local_path(self) -> Iterator[str]:
        if self.is_wildcard():
            raise Exception('Please resolve first.')
        with TemporaryDirectory() as tmp_path:
            localfile = f'{tmp_path}/{uuid4()}.parquet'
            logger.info(f'Download stat parquet file on s3://{self.bucket}/{self.key} -> {localfile}')
            try:
                with Halo(text='Downloading from s3', spinner='dots', stream=stderr) as spinner:
                    self.aws_session.resource('s3', endpoint_url=self.endpoint_url)\
                        .meta.client.download_file(self.bucket, self.key, localfile)
                    spinner.info(f's3://{self.bucket}/{self.key} => {localfile}')
            except Exception:
                raise FileNotFoundException(f's3://{self.bucket}/{self.key} not found or cannot access')
            else:
                yield localfile


def _coerce_manifest(checkpoint: Union[S3Manifest, str, Dict[str, Any]]) -> S3Manifest:
    return S3Manifest.from_checkpoint(checkpoint)


def get_aws_session(profile_name: Optional[str]) -> boto3.Session:
    return boto3.Session(profile_name=profile_name)


def _is_s3_file(filename: str) -> bool:
    return filename[:5] == 's3://'


def to_parquet_file(file_exp: str, awsprofile: Optional[str], endpoint_url: Optional[str]) -> ParquetFile:
    '''Transform file_exp to ParquetFile object.
    '''
    if _is_s3_file(file_exp):
        parsed_url = urlparse(file_exp)
        return S3ParquetFile(
            aws_session=get_aws_session(awsprofile),
            bucket=parsed_url.netloc,
            key=parsed_url.path[1:],
            endpoint_url=endpoint_url
        )
    else:
        return LocalParquetFile(
            path=file_exp
        )


@contextmanager
def get_datafame_from_objs(objs: List[ParquetFile], head: Union[int, float] = None):
    '''Get pandas dataframe of ParquetFile object list.
    '''

    if head is None or head <= 0:
        head = float('inf')

    cumsum_row: int = 0
    dfs: List[pd.DataFrame] = []
    with ExitStack() as stack:
        for obj in objs:
            for pf in _resolve_wildcard(obj):
                df: Optional[pd.DataFrame] = stack.enter_context(pf.get_dataframe())
                if df is None:
                    continue
                cumsum_row += len(df)
                dfs.append(df)

                if cumsum_row >= head:
                    break
            if cumsum_row >= head:
                break
        if dfs:
            yield reduce(lambda x, y: pd.concat([x, y]), dfs)
        else:
            yield None


def _resolve_wildcard(obj: ParquetFile) -> List[ParquetFile]:
    if not obj.is_wildcard():
        return [obj]
    else:
        return obj.resolve_wildcard()
