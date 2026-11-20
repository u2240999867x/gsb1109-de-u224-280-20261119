__all__ = [
    'MessageSignedBase',
]


import functools
import html
import logging
import re

from typing import Generic, List, Optional, Tuple, Type, TypeVar

from ..attachments import AttachmentBase, SignedAttachment
from ..enums import AttachmentType, DeencapType, ErrorBehavior
from ..exceptions import StandardViolationError
from .message_base import MessageBase
from ..utils import inputToBytes, inputToString, unwrapMultipart


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_T = TypeVar('_T')


class MessageSignedBase(MessageBase, Generic[_T]):
    """
    Base class for Message-like MSG files that are signed.

    The class only describes the parts that a message actually contains. It
    does not verify, validate, or otherwise determine the authenticity of a
    signature.
    """

    def __init__(self, path, signedAttachmentClass: Type[_T] = SignedAttachment, **kwargs):
        """
        Supports all of the options from :meth:`MessageBase.__init__` with some
        additional ones.

        :param signedAttachmentClass: optional, the class the object will use
            for signed attachments.
        """
        self.__sAttCls = signedAttachmentClass
        super().__init__(path, **kwargs)

    def _analyzeMultipart(self, att: AttachmentBase, force: bool = False) -> dict:
        """
        Analyzes a raw attachment as a candidate signed container.

        Returns the unwrapped multipart data plus whether the attachment
        actually declares itself to be a signed container and whether it was
        parsed in full.

        :param force: Parse the attachment even when its declared type and
            leading bytes do not look like MIME. Used as a compatibility
            fallback for signed messages with a single binary attachment.
        """
        data = att.data
        mimetype = (att.mimetype or '').rstrip('\x00').lower()
        plausible = force or mimetype.startswith(('multipart/', 'message/'))
        if not plausible and isinstance(data, bytes):
            # Many clients store the MIME block as a generic binary attachment.
            # Only attempt to parse it when it actually looks like MIME data.
            head = data[:4096].lstrip().lower()
            plausible = (
                head.startswith((b'content-type:', b'content-transfer-encoding:', b'mime-version:'))
                or b'boundary=' in head
            )

        if not plausible:
            return {
                'attachments': [],
                'signature_parts': [],
                'plain_body': None,
                'html_body': None,
                'complete': False,
                'has_signed_container': False,
                'declared_signed': False,
                'strong': False,
            }

        try:
            unwrapped = unwrapMultipart(data)
        except Exception:
            logger.exception('Failed to parse a possible signed container.')
            unwrapped = None

        if not isinstance(unwrapped, dict):
            unwrapped = {
                'attachments': [],
                'signature_parts': [],
                'plain_body': None,
                'html_body': None,
                'complete': False,
                'has_signed_container': False,
            }

        # An attachment that explicitly declares multipart/signed is a strong
        # candidate even if its contents cannot be parsed. One that merely
        # contains a signed node is a weaker candidate.
        declared = mimetype == 'multipart/signed'
        unwrapped['declared_signed'] = declared or unwrapped.get('has_signed_container', False)
        unwrapped['strong'] = declared
        return unwrapped

    def _buildSignedResults(self, att: AttachmentBase, unwrapped: dict,
                            ambiguous: bool = False) -> List[_T]:
        """
        Builds the ``SignedAttachment`` list for a single container.
        """
        complete = bool(unwrapped.get('complete', False))
        results: List[_T] = []

        def build(part: dict) -> _T:
            # The part carries the fields detected by the multipart parser.
            # The container, completeness, and ambiguity are decided here, so
            # remove the parser's values to avoid duplicate keyword arguments.
            part = {
                key: value for key, value in part.items()
                if key not in ('container', 'complete', 'ambiguous')
            }
            return self.__sAttCls(
                self, **part,
                container = att, complete = complete, ambiguous = ambiguous,
            )

        for part in unwrapped.get('attachments', []):
            results.append(build(part))

        # Signature parts are made available for lookup, but they are not
        # business attachments and so are not counted as one.
        for part in unwrapped.get('signature_parts', []):
            results.append(build(part))

        return results

    def _selectContainer(self, candidates: List[Tuple[AttachmentBase, dict]]):
        """
        Chooses the signed container from the candidates, if the original
        message contains enough information to do so.

        Returns ``(chosen, ambiguous)``. If the choice cannot be made,
        ``chosen`` is ``None`` and every candidate is returned as ambiguous.
        Traversal order is never used to break a tie.
        """
        if not candidates:
            return (None, [])
        if len(candidates) == 1:
            return (candidates[0], [])

        # If exactly one candidate declares itself as signed, it is the only
        # reasonable explanation.
        strong = [candidate for candidate in candidates if candidate[1].get('strong', False)]
        if len(strong) == 1:
            return (strong[0], [])

        # The original message does not contain enough information to decide.
        # Do not use traversal order, and do not select whichever candidate
        # happened to parse "successfully".
        return (None, candidates)

    @functools.cached_property
    def attachments(self) -> List[_T]:
        """
        A list of all attachments.

        The attachments inside the signed container and the detached signature
        part are kept in separate layers from the ordinary MSG attachments.
        The ordinary MSG attachments (including any that were not used as the
        signed container) remain in the ``"message"`` layer. The signature part
        is available for lookup but is not a business attachment.

        If more than one attachment can be explained as the signed container
        and the original message is not enough to choose between them, no
        container is selected. Instead every candidate and its original
        position is preserved as ambiguous material.

        :raises StandardViolationError: The standard for signed messages was
            blatantly violated.
        """
        # Set these to None to make error handling easier.
        self._signedBody = None
        self._signedHtmlBody = None
        self._signedContainer = None
        self._signedContainerComplete = False
        self._signedAmbiguous = []
        self._signedContainers = []

        rawAtts = self.rawAttachments
        atts = [att for att in rawAtts if att.type is AttachmentType.DATA]

        # Find every attachment that could reasonably be a signed container.
        candidates: List[Tuple[AttachmentBase, dict]] = []
        for att in atts:
            unwrapped = self._analyzeMultipart(att)
            if unwrapped['declared_signed']:
                candidates.append((att, unwrapped))

        if not candidates and len(atts) == 1:
            # Compatibility fallback: a signed message with a single binary
            # attachment that does not advertise itself. Parse it directly.
            unwrapped = self._analyzeMultipart(atts[0], True)
            if unwrapped.get('has_signed_container'):
                candidates.append((atts[0], unwrapped))

        chosen, ambiguous = self._selectContainer(candidates)

        if ambiguous:
            # More than one attachment can be explained as the signed container
            # and the original is not enough to decide. Preserve all of them in
            # their original positions instead of picking one by traversal
            # order.
            logger.warning('Multiple attachments could be the signed container. Preserving all as ambiguous material.')
            self._signedAmbiguous = [att for att, _ in ambiguous]
            ambiguousById = {id(att): unwrapped for att, unwrapped in ambiguous}

            results: List[_T] = []
            for rawAtt in rawAtts:
                if id(rawAtt) in ambiguousById:
                    rawAtt._ambiguous = True
                    rawAtt._complete = bool(ambiguousById[id(rawAtt)].get('complete', False))
                    results.append(rawAtt)
                    results.extend(self._buildSignedResults(rawAtt, ambiguousById[id(rawAtt)], True))
                else:
                    results.append(rawAtt)
            return results

        if chosen is not None:
            att, unwrapped = chosen
            self._signedContainer = att
            self._signedContainers = [att]
            self._signedContainerComplete = bool(unwrapped.get('complete', False))
            att._complete = self._signedContainerComplete

            # Now store everything where it needs to be.
            self._signedBody = unwrapped.get('plain_body')
            signedHtmlBody = unwrapped.get('html_body')
            self._signedHtmlBody = inputToBytes(signedHtmlBody, 'utf-8') if signedHtmlBody is not None else None

            results: List[_T] = []
            for rawAtt in rawAtts:
                if rawAtt is att:
                    results.extend(self._buildSignedResults(att, unwrapped))
                else:
                    results.append(rawAtt)
            return results

        if ErrorBehavior.STANDARDS_VIOLATION in self.errorBehavior:
            logger.error('Standards Violation: Signed message has no usable signed container.')
            return []
        raise StandardViolationError('Signed messages *must* have a signed container. Signed attachments cannot be parsed otherwise.')

    @functools.cached_property
    def businessAttachments(self) -> List[_T]:
        """
        The business attachments of the message.

        This includes the ordinary MSG attachments (in the ``"message"``
        layer) and the business attachments from the signed container (in the
        ``"signed"`` layer). The raw signed container and the detached
        signature part are excluded, so the signature part is not counted as
        an extra business attachment. Ambiguous material is excluded because
        it has not been established as business content.
        """
        containers = {id(att) for att in self.signedContainers}
        return [
            att for att in self.attachments
            if att.role != 'signature'
            and not att.ambiguous
            and id(att) not in containers
        ]

    @functools.cached_property
    def containerAttachments(self) -> List[_T]:
        """
        The business attachments that were carried inside the signed container.

        The detached signature part is kept separately in
        :attr:`signatureParts`, so it is not counted here as a business
        attachment.
        """
        return [att for att in self.attachments if att.layer == 'signed' and att.role != 'signature']

    @property
    def signedContainerAttachments(self) -> List[_T]:
        """
        Alias of :attr:`containerAttachments`.
        """
        return self.containerAttachments

    @property
    def signedBusinessAttachments(self) -> List[_T]:
        """
        Alias of :attr:`containerAttachments`.
        """
        return self.containerAttachments

    @property
    def regularAttachments(self) -> List[AttachmentBase]:
        """
        Alias of :attr:`ordinaryAttachments`.
        """
        return self.ordinaryAttachments

    @property
    def msgAttachments(self) -> List[AttachmentBase]:
        """
        Alias of :attr:`ordinaryAttachments`.
        """
        return self.ordinaryAttachments

    @functools.cached_property
    def ordinaryAttachments(self) -> List[AttachmentBase]:
        """
        The attachments that belong to the MSG file itself.

        This excludes the raw attachments that were used, or could have been
        used, as the signed container. These stay readable even when the
        container itself is truncated.
        """
        containers = {id(att) for att in self.signedContainers}
        containers.update(id(att) for att in self.signedAmbiguous)
        return [
            att for att in self.attachments
            if att.layer == 'message' and id(att) not in containers
        ]

    @functools.cached_property
    def rawSignedAttachments(self) -> List[AttachmentBase]:
        """
        The raw attachments that are, or could be, the signed container.
        """
        containers = {id(att) for att in self.signedContainers}
        containers.update(id(att) for att in self.signedAmbiguous)
        return [
            att for att in self.attachments
            if att.layer == 'message' and id(att) in containers
        ]

    @functools.cached_property
    def body(self) -> Optional[str]:
        """
        The message body, if it exists.
        """
        if (body := self.getStringStream('__substg1.0_1000')) is not None:
            pass
        elif self.signedBody:
            body = self.signedBody
        elif self.rtfBody:
            # If the body doesn't exist, see if we can get it from the RTF
            # body.
            body = self.deencapsulateBody(self.rtfBody, DeencapType.PLAIN)

        if body:
            body = inputToString(body, 'utf-8')
            if re.search('\n', body):
                if re.search('\r\n', body):
                    self._crlf = '\r\n'

        return body

    @functools.cached_property
    def htmlBody(self) -> Optional[bytes]:
        """
        The html body, if it exists.
        """
        if (htmlBody := self.getStream('__substg1.0_10130102')) is not None:
            pass
        elif self.signedHtmlBody:
            htmlBody = self.signedHtmlBody
        elif self.rtfBody:
            logger.info('HTML body was not found, attempting to generate from RTF.')
            htmlBody = self.deencapsulateBody(self.rtfBody, DeencapType.HTML)
        elif self.body:
            # Convert the plain text body to html.
            logger.info('HTML body was not found, attempting to generate from plain text body.')
            correctedBody = html.escape(self.body).replace('\r', '').replace('\n', '<br />')
            htmlBody = f'<html><body>{correctedBody}</body></head>'.encode('utf-8')
        else:
            logger.info('HTML body could not be found nor generated.')

        return htmlBody

    @functools.cached_property
    def rawAttachments(self) -> List[AttachmentBase]:
        """
        A property to allow access to the non-signed attachments.

        This is every attachment stored directly on the MSG file. It never
        triggers or replaces the cached :attr:`attachments` property.
        """
        return self._getAttachments()

    @property
    def signedAttachmentClass(self) -> Type[_T]:
        """
        The attachment class used for signed attachments.
        """
        return self.__sAttCls

    @functools.cached_property
    def signedAmbiguous(self) -> List[AttachmentBase]:
        """
        The raw attachments that could each be explained as the signed
        container.

        This is only populated when the original message does not contain
        enough information to determine which one is correct. In that case
        none is chosen and all of the candidates are preserved as ambiguous
        material.
        """
        self.attachments
        return self._signedAmbiguous

    @functools.cached_property
    def signedContainers(self) -> List[AttachmentBase]:
        """
        The raw attachments selected as signed containers.

        This is empty when no usable container was found or when the choice
        between multiple candidates was ambiguous.
        """
        self.attachments
        return self._signedContainers

    @functools.cached_property
    def signedContainer(self) -> Optional[AttachmentBase]:
        """
        The raw attachment that was identified as the signed container.

        ``None`` if no usable container was found or if the choice between
        multiple candidates was ambiguous.
        """
        self.attachments
        return self._signedContainer

    @functools.cached_property
    def signedContainerComplete(self) -> bool:
        """
        Whether the signed container was parsed completely.

        ``False`` when the container was truncated or otherwise incomplete. In
        that case the readable parts are still available, but the message must
        not be treated as a complete signed message.
        """
        self.attachments
        return self._signedContainerComplete

    @property
    def signedMessageComplete(self) -> bool:
        """
        Whether a complete signed message could be read.

        This is ``True`` only when exactly one signed container was found and
        it was parsed in full.
        """
        return self.signedContainer is not None and self.signedContainerComplete

    @property
    def isSignedMessageComplete(self) -> bool:
        """
        Alias of :attr:`signedMessageComplete`.
        """
        return self.signedMessageComplete

    @property
    def signedParseComplete(self) -> bool:
        """
        Alias of :attr:`signedMessageComplete`.
        """
        return self.signedMessageComplete

    @functools.cached_property
    def signatureParts(self) -> List[_T]:
        """
        The detached signature parts found in the signed container.

        These are available for lookup but are not business attachments.
        """
        return [att for att in self.attachments if att.role == 'signature']

    @functools.cached_property
    def signedBody(self) -> Optional[str]:
        """
        The body from the signed message if it exists.
        """
        self.attachments
        return self._signedBody

    @functools.cached_property
    def signedHtmlBody(self) -> Optional[bytes]:
        """
        The HTML body from the signed message if it exists.
        """
        self.attachments
        return self._signedHtmlBody
