"""Client-side configuration provenance for submitted HS2 operations.

The record is built from three observable protocol points:

* the configuration sent in ``OpenSession``,
* the configuration returned in the ``OpenSession`` response,
* the ``confOverlay`` sent in ``ExecuteStatement``.

Client-only options (currently ``paramstyle``) are kept separate because they
are never part of either Thrift request.  An operation record therefore never
claims that the server used a value unless the client actually observed that
value in a server response.
"""


CLIENT_ONLY_CONFIGURATION_KEYS = frozenset(['paramstyle'])

SOURCE_REQUESTED_SESSION = 'requested_session'
SOURCE_SERVER_SESSION = 'server_session'
SOURCE_STATEMENT_OVERLAY = 'statement_overlay'
SOURCE_CLIENT_ONLY = 'client_only'

SOURCE_ALIASES = {
    'requested_session': SOURCE_REQUESTED_SESSION,
    'session_request': SOURCE_REQUESTED_SESSION,
    'open_session_request': SOURCE_REQUESTED_SESSION,
    'server_session': SOURCE_SERVER_SESSION,
    'session_response': SOURCE_SERVER_SESSION,
    'open_session_response': SOURCE_SERVER_SESSION,
    'statement_overlay': SOURCE_STATEMENT_OVERLAY,
    'conf_overlay': SOURCE_STATEMENT_OVERLAY,
    'execute_statement': SOURCE_STATEMENT_OVERLAY,
    'client_only': SOURCE_CLIENT_ONLY,
    'client': SOURCE_CLIENT_ONLY,
}


class _UnknownValue(object):
    """Sentinel for a value the client has no evidence for."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(_UnknownValue, cls).__new__(cls)
        return cls._instance

    def __repr__(self):
        return 'UNKNOWN'

    def __str__(self):
        return 'unknown'

    def __bool__(self):
        return False

    def __eq__(self, other):
        return isinstance(other, _UnknownValue)

    def __hash__(self):
        return hash(_UnknownValue)


UNKNOWN = _UnknownValue()


def split_configuration(configuration):
    """Return ``(server_configuration, client_options)`` without mutating input."""
    server_configuration = {}
    client_options = {}
    if configuration:
        for key, value in configuration.items():
            if key in CLIENT_ONLY_CONFIGURATION_KEYS:
                client_options[key] = value
            else:
                server_configuration[key] = value
    return server_configuration, client_options


def merge_client_options(base_options=None, overlay_options=None,
                         base_source=SOURCE_REQUESTED_SESSION,
                         overlay_source=SOURCE_STATEMENT_OVERLAY):
    """Merge two client-only option maps and remember each key's origin."""
    merged = {}
    sources = {}
    if base_options:
        for key, value in base_options.items():
            merged[key] = value
            sources[key] = base_source
    if overlay_options:
        for key, value in overlay_options.items():
            merged[key] = value
            sources[key] = overlay_source
    return merged, sources


def _copy_configuration(configuration):
    return dict(configuration) if configuration else {}


def _handle_value(handle, attribute):
    if handle is None:
        return None
    return getattr(handle, attribute, None)


def _handle_id(handle, attribute):
    value = _handle_value(handle, attribute)
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return str(value)


def _jsonable(value):
    if value is UNKNOWN:
        return 'unknown'
    return value


class ConfigurationEntry(object):
    """Per-key view of all the places a configuration value came from."""

    def __init__(self, key, values, sources, origin=None,
                 server_reported=True):
        self.key = key
        self._values = dict(values)
        self.origin = origin
        self._server_reported = bool(server_reported)
        self._sources = tuple(
            source for source in (
                SOURCE_REQUESTED_SESSION,
                SOURCE_SERVER_SESSION,
                SOURCE_STATEMENT_OVERLAY,
                SOURCE_CLIENT_ONLY)
            if source in self._values)

    @property
    def sources(self):
        return self._sources

    @property
    def values(self):
        return dict(self._values)

    def get(self, source, default=None):
        source = SOURCE_ALIASES.get(source, source)
        return self._values.get(source, default)

    def has(self, source):
        source = SOURCE_ALIASES.get(source, source)
        return source in self._values

    def _source_value(self, source):
        return self._values.get(source, UNKNOWN)

    @property
    def requested_session(self):
        return self._source_value(SOURCE_REQUESTED_SESSION)

    @property
    def requested_session_value(self):
        return self.requested_session

    @property
    def server_session(self):
        return self._source_value(SOURCE_SERVER_SESSION)

    @property
    def server_session_value(self):
        return self.server_session

    @property
    def reported_session(self):
        return self.server_session

    @property
    def reported_session_value(self):
        return self.server_session

    @property
    def statement_overlay(self):
        return self._source_value(SOURCE_STATEMENT_OVERLAY)

    @property
    def statement_overlay_value(self):
        return self.statement_overlay

    @property
    def overlay(self):
        return self.statement_overlay

    @property
    def client_only(self):
        return self._source_value(SOURCE_CLIENT_ONLY)

    @property
    def client_only_value(self):
        return self.client_only

    @property
    def server_confirmed(self):
        return SOURCE_SERVER_SESSION in self._values

    @property
    def server_reported(self):
        return self.server_confirmed

    @property
    def server_configuration_reported(self):
        return self._server_reported

    @property
    def omitted(self):
        return self._server_reported and not self.server_confirmed

    @property
    def confirmed(self):
        """Alias for ``server_confirmed`` used by callers reading a record."""
        return self.server_confirmed

    @property
    def confirmed_value(self):
        """The value the server reported, or UNKNOWN when it said nothing."""
        return self._source_value(SOURCE_SERVER_SESSION)

    @property
    def server_reported_value(self):
        return self.confirmed_value

    @property
    def effective_value(self):
        """A server execution-plan value is never observable in these RPCs."""
        return UNKNOWN

    @property
    def effective_known(self):
        return False

    @property
    def unconfirmed(self):
        if SOURCE_CLIENT_ONLY in self._values:
            return False
        if not self.server_confirmed:
            return True
        if SOURCE_STATEMENT_OVERLAY in self._values:
            # The OpenSession response predates ExecuteStatement, so it cannot
            # confirm a per-statement overlay value.
            return True
        confirmed = self._values[SOURCE_SERVER_SESSION]
        return (
            SOURCE_REQUESTED_SESSION in self._values
            and self._values[SOURCE_REQUESTED_SESSION] != confirmed)

    @property
    def unconfirmed_values(self):
        if SOURCE_CLIENT_ONLY in self._values:
            return []
        confirmed = self._values.get(SOURCE_SERVER_SESSION, UNKNOWN)
        unconfirmed = []
        if (SOURCE_REQUESTED_SESSION in self._values
                and self._values[SOURCE_REQUESTED_SESSION] != confirmed):
            unconfirmed.append(
                (SOURCE_REQUESTED_SESSION,
                 self._values[SOURCE_REQUESTED_SESSION]))
        if SOURCE_STATEMENT_OVERLAY in self._values:
            unconfirmed.append(
                (SOURCE_STATEMENT_OVERLAY,
                 self._values[SOURCE_STATEMENT_OVERLAY]))
        return unconfirmed

    @property
    def overlay_unconfirmed(self):
        """True when this statement supplied an overlay value for the key.

        ``ExecuteStatement`` returns no configuration, and the ``OpenSession``
        response predates the statement, so the client never sees the server
        confirm an overlay value.
        """
        return SOURCE_STATEMENT_OVERLAY in self._values

    @property
    def session_status(self):
        if SOURCE_CLIENT_ONLY in self._values:
            return 'client_only'
        if self.server_confirmed:
            if (SOURCE_REQUESTED_SESSION in self._values
                    and self._values[SOURCE_REQUESTED_SESSION]
                    != self._values[SOURCE_SERVER_SESSION]):
                return 'server_reported_different_value'
            return 'server_reported'
        if self._server_reported:
            return 'server_omitted'
        return 'server_not_reported'

    @property
    def status(self):
        if self.overlay_unconfirmed:
            return 'statement_overlay_unconfirmed'
        return self.session_status

    def as_dict(self):
        result = {
            'sources': list(self._sources),
            'values': {
                source: _jsonable(value)
                for source, value in self._values.items()
            },
            'origin': self.origin,
            'server_confirmed': self.server_confirmed,
            'server_configuration_reported': self._server_reported,
            'omitted': self.omitted,
            'confirmed_value': _jsonable(self.confirmed_value),
            'effective_value': _jsonable(self.effective_value),
            'effective_known': self.effective_known,
            'unconfirmed': self.unconfirmed,
            'overlay_unconfirmed': self.overlay_unconfirmed,
            'unconfirmed_values': [
                (source, _jsonable(value))
                for source, value in self.unconfirmed_values
            ],
            'session_status': self.session_status,
            'status': self.status,
        }
        return result

    def to_dict(self):
        return self.as_dict()

    def __repr__(self):
        return ('ConfigurationEntry(key=%r, sources=%r, values=%r)'
                % (self.key, self._sources, self._values))

    def __eq__(self, other):
        return (isinstance(other, ConfigurationEntry)
                and self.key == other.key
                and self._values == other._values)

    def __ne__(self, other):
        return not self == other


class ConfigurationRecord(object):
    """Immutable snapshot of configuration visible for one submitted operation.

    The record deliberately copies every mapping it receives.  Callers may
    freely reuse or mutate the dictionaries they passed to ``cursor()`` or
    ``execute()`` after submission without changing this record.
    """

    def __init__(self, session=None, operation=None,
                 requested_session=None, server_session=None,
                 statement_configuration=None, client_options=None,
                 client_option_sources=None,
                 session_handle=None, operation_handle=None,
                 statement=None, submitted=True,
                 server_session_reported=True):
        self.session = session
        self.operation = operation
        self.statement = statement
        self.submitted = bool(submitted)
        self.successful = bool(submitted)
        self.server_session_reported = bool(server_session_reported)

        self._requested_session = _copy_configuration(requested_session)
        self._server_session = _copy_configuration(server_session)
        self._statement_configuration = _copy_configuration(
            statement_configuration)
        self._client_options = _copy_configuration(client_options)
        self._client_option_sources = dict(client_option_sources or {})

        if session_handle is None and session is not None:
            session_handle = getattr(session, 'handle', None)
        if operation_handle is None and operation is not None:
            operation_handle = getattr(operation, 'handle', None)
        self.session_handle = session_handle
        self.operation_handle = operation_handle

        keys = []
        for configuration in (
                self._requested_session,
                self._server_session,
                self._statement_configuration,
                self._client_options):
            for key in configuration:
                if key not in keys:
                    keys.append(key)

        self._entries = {}
        for key in keys:
            values = {}
            if key in self._requested_session:
                values[SOURCE_REQUESTED_SESSION] = \
                    self._requested_session[key]
            if key in self._server_session:
                values[SOURCE_SERVER_SESSION] = self._server_session[key]
            if key in self._statement_configuration:
                values[SOURCE_STATEMENT_OVERLAY] = \
                    self._statement_configuration[key]
            if key in self._client_options:
                values[SOURCE_CLIENT_ONLY] = self._client_options[key]
            origin = None
            if key in self._client_options:
                origin = self._client_option_sources.get(key)
            if key in self._statement_configuration:
                origin = SOURCE_STATEMENT_OVERLAY
            elif key in self._server_session:
                origin = SOURCE_SERVER_SESSION
            elif key in self._requested_session:
                origin = SOURCE_REQUESTED_SESSION
            self._entries[key] = ConfigurationEntry(
                key, values, values.keys(), origin,
                server_reported=self.server_session_reported)

        self.changed_session_configuration = {
            key: {
                'requested': self._requested_session[key],
                'server_reported': self._server_session[key],
            }
            for key in self._requested_session
            if key in self._server_session
            and self._requested_session[key] != self._server_session[key]
        }

        self.unconfirmed_session_configuration = {
            key: self._requested_session[key]
            for key in self._requested_session
            if key not in self._server_session
        }

        self.unconfirmed_statement_configuration = dict(
            self._statement_configuration)

        self.unconfirmed_configuration = dict(
            self.unconfirmed_session_configuration)
        self.unconfirmed_configuration.update(
            self.unconfirmed_statement_configuration)

        self.omitted_session_configuration = (
            {
                key: self._requested_session[key]
                for key in self._requested_session
                if key not in self._server_session
            }
            if self.server_session_reported else {})

        self.unreported_session_configuration = (
            dict(self._requested_session)
            if not self.server_session_reported else {})

        self.effective_configuration = {
            key: UNKNOWN
            for key in self._entries
            if key not in self._client_options
        }

    @property
    def submitted_successfully(self):
        return self.successful

    @property
    def failed(self):
        return not self.successful

    @property
    def requested_session_configuration(self):
        return dict(self._requested_session)

    @property
    def session_defaults(self):
        return self.requested_session_configuration

    @property
    def requested_defaults(self):
        return self.requested_session_configuration

    @property
    def session_request_configuration(self):
        return self.requested_session_configuration

    @property
    def open_session_configuration(self):
        return self.requested_session_configuration

    @property
    def server_session_configuration(self):
        return dict(self._server_session)

    @property
    def reported_session_configuration(self):
        return self.server_session_configuration

    @property
    def session_response_configuration(self):
        return self.server_session_configuration

    @property
    def open_session_response_configuration(self):
        return self.server_session_configuration

    @property
    def confirmed_session_configuration(self):
        return self.server_session_configuration

    @property
    def statement_configuration(self):
        return dict(self._statement_configuration)

    @property
    def statement_overlay(self):
        return self.statement_configuration

    @property
    def conf_overlay(self):
        return self.statement_configuration

    @property
    def execute_statement_configuration(self):
        return self.statement_configuration

    @property
    def client_configuration(self):
        return dict(self._client_options)

    @property
    def client_options(self):
        return self.client_configuration

    @property
    def client_only_configuration(self):
        return self.client_configuration

    @property
    def paramstyle(self):
        return self._client_options.get('paramstyle')

    @property
    def required_session_configuration(self):
        return dict(self._requested_session)

    @property
    def requested_configuration(self):
        return dict(self._requested_session)

    @property
    def server_reported_configuration(self):
        return dict(self._server_session)

    @property
    def statement_overlay_configuration(self):
        return dict(self._statement_configuration)

    @property
    def paramstyle_source(self):
        return self._client_option_sources.get('paramstyle')

    @property
    def client_option_sources(self):
        return dict(self._client_option_sources)

    @property
    def entries(self):
        return dict(self._entries)

    @property
    def provenance(self):
        return {
            key: entry.sources for key, entry in self._entries.items()
        }

    @property
    def operation_id(self):
        return _handle_id(self.operation_handle, 'operationId')

    @property
    def session_id(self):
        return _handle_id(self.session_handle, 'sessionId')

    @property
    def session_guid(self):
        return _handle_id(_handle_value(self.session_handle, 'sessionId'),
                          'guid')

    @property
    def operation_guid(self):
        return _handle_id(_handle_value(self.operation_handle, 'operationId'),
                          'guid')

    def source(self, key, source, default=None):
        entry = self._entries.get(key)
        if entry is None:
            return default
        return entry.get(source, default)

    def sources_for(self, key):
        entry = self._entries.get(key)
        return entry.sources if entry is not None else ()

    def entry(self, key):
        return self._entries.get(key)

    def get(self, key, default=None):
        return self._entries.get(key, default)

    def keys(self):
        return self._entries.keys()

    def items(self):
        return self._entries.items()

    def values(self):
        return self._entries.values()

    def __getitem__(self, key):
        if key in SOURCE_ALIASES:
            source = SOURCE_ALIASES[key]
            return {
                self_key: entry.get(source)
                for self_key, entry in self._entries.items()
                if entry.has(source)
            }
        return self._entries[key]

    def __contains__(self, key):
        return key in self._entries

    def __iter__(self):
        return iter(self._entries)

    def __len__(self):
        return len(self._entries)

    def as_dict(self):
        return {
            'submitted': self.submitted,
            'successful': self.successful,
            'statement': self.statement,
            'session_id': _jsonable(self.session_id),
            'operation_id': _jsonable(self.operation_id),
            'requested_session_configuration':
                dict(self._requested_session),
            'server_session_configuration': dict(self._server_session),
            'statement_configuration':
                dict(self._statement_configuration),
            'client_configuration': dict(self._client_options),
            'client_option_sources': dict(self._client_option_sources),
            'paramstyle': self.paramstyle,
            'paramstyle_source': self.paramstyle_source,
            'changed_session_configuration':
                dict(self.changed_session_configuration),
            'unconfirmed_session_configuration':
                dict(self.unconfirmed_session_configuration),
            'omitted_session_configuration':
                dict(self.omitted_session_configuration),
            'unreported_session_configuration':
                dict(self.unreported_session_configuration),
            'unconfirmed_statement_configuration':
                dict(self.unconfirmed_statement_configuration),
            'unconfirmed_configuration':
                dict(self.unconfirmed_configuration),
            'effective_configuration': {
                key: _jsonable(value)
                for key, value in self.effective_configuration.items()
            },
            'provenance': {
                key: list(sources)
                for key, sources in self.provenance.items()
            },
            'entries': {
                key: entry.as_dict()
                for key, entry in self._entries.items()
            },
        }

    def to_dict(self):
        return self.as_dict()

    def report(self):
        return self.as_dict()

    def __eq__(self, other):
        if not isinstance(other, ConfigurationRecord):
            return NotImplemented
        if self is other:
            return True
        return (
            self.session_guid == other.session_guid
            and self.operation_guid == other.operation_guid
            and self.statement == other.statement
            and self._requested_session == other._requested_session
            and self._server_session == other._server_session
            and self._statement_configuration
            == other._statement_configuration
            and self._client_options == other._client_options)

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash((self.session_guid, self.operation_guid,
                     self.statement))

    def __repr__(self):
        return (
            'ConfigurationRecord(session_id=%r, operation_id=%r, '
            'requested_session=%r, server_session=%r, statement_overlay=%r, '
            'client_options=%r)'
            % (self.session_id, self.operation_id,
               self._requested_session, self._server_session,
               self._statement_configuration, self._client_options))


# Descriptive aliases for callers that use a different qualifier.
ClientConfigurationRecord = ConfigurationRecord
OperationConfigurationRecord = ConfigurationRecord
QueryConfigurationRecord = ConfigurationRecord
SessionConfigurationRecord = ConfigurationRecord
HiveServer2ConfigurationRecord = ConfigurationRecord
