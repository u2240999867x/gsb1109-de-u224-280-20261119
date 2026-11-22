### pywb.utils

This package contains a utils used by pywb wayback tool suite.

#### Modules

* [binsearch.py](binsearch.py) -- Binary search implementation over text files

* [loaders.py](loaders.py) -- Loading abstraction for loading via http or local file system.

* [timemap_reader.py](timemap_reader.py) -- Incremental, resumable parser for Memento TimeMaps
  in ``application/link-format``, with byte-offset provenance and content-validator generations.

* [bufferedreaders.py](bufferedreaders.py) -- Buffering wrappers for file-like object, also provide gzip decompression and
de-chunking facilities.

* [statusandheaders.py](statusandheaders.py) -- Represent http status line + headers and parsing them out from a stream

* [timeutils.py](timeutils.py) -- Utility functions for converting between standard datetime formats 14-digit timestamp

