import asyncio
import datetime

import aioice.exceptions
from pyee import EventEmitter

from . import dtls, sdp, sctp
from .exceptions import InternalError, InvalidAccessError, InvalidStateError
from .rtcdatachannel import RTCDataChannel
from .rtcrtptransceiver import RTCRtpReceiver, RTCRtpSender, RTCRtpTransceiver
from .rtcsessiondescription import RTCSessionDescription
from .rtcsctptransport import RTCSctpTransport


DUMMY_CANDIDATE = aioice.Candidate(
    foundation='',
    component=1,
    transport='udp',
    priority=1,
    host='0.0.0.0',
    port=0,
    type='host')
MEDIA_KINDS = ['audio', 'video']


def get_ntp_seconds():
    return int((
        datetime.datetime.utcnow() - datetime.datetime(1900, 1, 1, 0, 0, 0)
    ).total_seconds())


def ice_connection_sdp(iceConnection):
    sdp = []
    for candidate in iceConnection.local_candidates:
        sdp += ['a=candidate:%s' % candidate.to_sdp()]
    sdp += [
        'a=ice-pwd:%s' % iceConnection.local_password,
        'a=ice-ufrag:%s' % iceConnection.local_username,
    ]
    if iceConnection.ice_controlling:
        sdp += ['a=setup:actpass']
    else:
        sdp += ['a=setup:active']
    return sdp


async def run_dtls(dtlsSession):
    try:
        await dtlsSession.run()
    except aioice.exceptions.ConnectionError:
        pass


class RTCPeerConnection(EventEmitter):
    def __init__(self, loop=None):
        super().__init__(loop=loop)
        self.__datachannels = []
        self.__dtlsContext = dtls.DtlsSrtpContext()
        self.__sctp = None
        self.__transceivers = []

        self.__iceConnectionState = 'new'
        self.__iceGatheringState = 'new'
        self.__isClosed = False
        self.__signalingState = 'stable'

        self.__currentLocalDescription = None
        self.__currentRemoteDescription = None

    @property
    def iceConnectionState(self):
        return self.__iceConnectionState

    @property
    def iceGatheringState(self):
        return self.__iceGatheringState

    @property
    def localDescription(self):
        return self.__currentLocalDescription

    @property
    def remoteDescription(self):
        return self.__currentRemoteDescription

    @property
    def signalingState(self):
        return self.__signalingState

    def addTrack(self, track):
        """
        Add a new media track to the set of media tracks while will be
        transmitted to the other peer.
        """
        # check state is valid
        self.__assertNotClosed()
        if track.kind not in ['audio', 'video']:
            raise InternalError('Invalid track kind "%s"' % track.kind)

        # don't add track twice
        for sender in self.getSenders():
            if sender.track == track:
                raise InvalidAccessError('Track already has a sender')

        for transceiver in self.__transceivers:
            if transceiver._kind == track.kind and transceiver.sender.track is None:
                transceiver.sender._track = track
                return transceiver.sender

        # we only support a single media track for now
        if len(self.__transceivers):
            raise InternalError('Only a single media track is supported for now')

        transceiver = RTCRtpTransceiver(
            receiver=RTCRtpReceiver(),
            sender=RTCRtpSender(track))
        transceiver._kind = track.kind
        self.__createTransport(transceiver, controlling=True)

        self.__transceivers.append(transceiver)
        return transceiver.sender

    async def close(self):
        """
        Terminate the ICE agent, ending ICE processing and streams.
        """
        if self.__isClosed:
            return
        self.__isClosed = True
        self.__setSignalingState('closed')
        for transceiver in self.__transceivers:
            await transceiver._iceConnection.close()
        if self.__sctp:
            await self.__sctpEndpoint.close()
            await self.__sctp._iceConnection.close()
        self.__setIceConnectionState('closed')

    async def createAnswer(self):
        """
        Create an SDP answer to an offer received from a remote peer during
        the offer/answer negotiation of a WebRTC connection.
        """
        # check state is valid
        self.__assertNotClosed()
        if self.signalingState not in ['have-remote-offer', 'have-local-pranswer']:
            raise InvalidStateError('Cannot create answer in signaling state "%s"' %
                                    self.signalingState)

        return RTCSessionDescription(
            sdp=self.__createSdp(),
            type='answer')

    def createDataChannel(self, label):
        if not self.__sctp:
            self.__sctp = RTCSctpTransport()
            self.__createTransport(self.__sctp, controlling=True)
            self.__sctpEndpoint = sctp.Endpoint(
                is_server=not self.__sctp._iceConnection.ice_controlling,
                transport=self.__sctp._dtlsSession.data)
            self.__sctpStream = 1

        channel = RTCDataChannel(id=self.__sctpStream, label=label,
                                 endpoint=self.__sctpEndpoint, loop=self._loop)
        self.__datachannels.append(channel)
        self.__sctpStream += 2

        return channel

    async def createOffer(self):
        """
        Create an SDP offer for the purpose of starting a new WebRTC
        connection to a remote peer.
        """
        # check state is valid
        self.__assertNotClosed()

        if not self.__sctp and not self.__transceivers:
            raise InternalError('Cannot create an offer with no media and not data channels')

        return RTCSessionDescription(
            sdp=self.__createSdp(),
            type='offer')

    def getReceivers(self):
        return list(map(lambda x: x.receiver, self.__transceivers))

    def getSenders(self):
        return list(map(lambda x: x.sender, self.__transceivers))

    async def setLocalDescription(self, sessionDescription):
        if sessionDescription.type == 'offer':
            self.__setSignalingState('have-local-offer')
        elif sessionDescription.type == 'answer':
            self.__setSignalingState('stable')

        # gather
        await self.__gather()

        # connect
        asyncio.ensure_future(self.__connect())

        self.__currentLocalDescription = RTCSessionDescription(
            sdp=self.__createSdp(),
            type=sessionDescription.type)

    async def setRemoteDescription(self, sessionDescription):
        # check description is compatible with signaling state
        if sessionDescription.type == 'offer':
            if self.signalingState not in ['stable', 'have-remote-offer']:
                raise InvalidStateError('Cannot handle offer in signaling state "%s"' %
                                        self.signalingState)
        elif sessionDescription.type == 'answer':
            if self.signalingState not in ['have-local-offer', 'have-remote-pranswer']:
                raise InvalidStateError('Cannot handle answer in signaling state "%s"' %
                                        self.signalingState)

        # parse description
        parsedRemoteDescription = sdp.SessionDescription.parse(sessionDescription.sdp)

        # apply description
        for media in parsedRemoteDescription.media:
            if media.kind in ['audio', 'video']:
                # find transceiver
                transceiver = None
                for t in self.__transceivers:
                    if t._kind == media.kind:
                        transceiver = t
                if transceiver is None:
                    transceiver = RTCRtpTransceiver(
                        sender=RTCRtpSender(),
                        receiver=RTCRtpReceiver())
                    self.__createTransport(transceiver, controlling=False)
                    transceiver._kind = media.kind
                    self.__transceivers.append(transceiver)

                # configure transport
                transceiver._iceConnection.remote_candidates = media.ice_candidates
                transceiver._iceConnection.remote_username = media.ice_ufrag
                transceiver._iceConnection.remote_password = media.ice_pwd
                transceiver._dtlsSession.remote_fingerprint = media.dtls_fingerprint
            elif media.kind == 'application':
                if not self.__sctp:
                    self.__sctp = RTCSctpTransport()
                    self.__createTransport(self.__sctp, controlling=False)
                    self.__sctpEndpoint = sctp.Endpoint(
                        is_server=not self.__sctp._iceConnection.ice_controlling,
                        transport=self.__sctp._dtlsSession.data)
                    self.__sctpStream = 0

                # configure transport
                self.__sctp._iceConnection.remote_candidates = media.ice_candidates
                self.__sctp._iceConnection.remote_username = media.ice_ufrag
                self.__sctp._iceConnection.remote_password = media.ice_pwd
                self.__sctp._dtlsSession.remote_fingerprint = media.dtls_fingerprint

        # connect
        asyncio.ensure_future(self.__connect())

        # update signaling state
        if sessionDescription.type == 'offer':
            self.__setSignalingState('have-remote-offer')
        elif sessionDescription.type == 'answer':
            self.__setSignalingState('stable')

        self.__currentRemoteDescription = sessionDescription

    async def __connect(self):
        for iceConnection, dtlsSession in self.__transports():
            if (not iceConnection.local_candidates or not iceConnection.remote_candidates):
                return

        if self.iceConnectionState == 'new':
            self.__setIceConnectionState('checking')
            for iceConnection, dtlsSession in self.__transports():
                await iceConnection.connect()
                await dtlsSession.connect()
                asyncio.ensure_future(run_dtls(dtlsSession))
            if self.__sctp:
                asyncio.ensure_future(self.__sctpEndpoint.run())
            self.__setIceConnectionState('completed')

    async def __gather(self):
        if self.__iceGatheringState == 'new':
            self.__setIceGatheringState('gathering')
            for iceConnection, dtlsSession in self.__transports():
                await iceConnection.gather_candidates()
            self.__setIceGatheringState('complete')

    def __assertNotClosed(self):
        if self.__isClosed:
            raise InvalidStateError('RTCPeerConnection is closed')

    def __createSdp(self):
        ntp_seconds = get_ntp_seconds()
        sdp = [
            'v=0',
            'o=- %d %d IN IP4 0.0.0.0' % (ntp_seconds, ntp_seconds),
            's=-',
            't=0 0',
            'a=fingerprint:sha-256 %s' % self.__dtlsContext.local_fingerprint,
        ]

        for transceiver in self.__transceivers:
            iceConnection = transceiver._iceConnection
            default_candidate = iceConnection.get_default_candidate(1)
            if default_candidate is None:
                default_candidate = DUMMY_CANDIDATE
            sdp += [
                # FIXME: negotiate codec
                'm=audio %d UDP/TLS/RTP/SAVPF 0' % default_candidate.port,
                'c=IN IP4 %s' % default_candidate.host,
                'a=rtcp:9 IN IP4 0.0.0.0',
            ]
            sdp += ice_connection_sdp(iceConnection)
            sdp += ['a=%s' % transceiver.direction]
            sdp += ['a=rtcp-mux']

            # FIXME: negotiate codec
            sdp += ['a=rtpmap:0 PCMU/8000']

        if self.__sctp:
            iceConnection = self.__sctp._iceConnection
            default_candidate = iceConnection.get_default_candidate(1)
            if default_candidate is None:
                default_candidate = DUMMY_CANDIDATE
            sdp += [
                'm=application %d DTLS/SCTP 5000' % default_candidate.port,
                'c=IN IP4 %s' % default_candidate.host,
            ]
            sdp += ice_connection_sdp(iceConnection)
            sdp += ['a=sctpmap:5000 webrtc-datachannel 256']

        return '\r\n'.join(sdp) + '\r\n'

    def __createTransport(self, transceiver, controlling):
        transceiver._iceConnection = aioice.Connection(ice_controlling=controlling)
        transceiver._dtlsSession = dtls.DtlsSrtpSession(
            self.__dtlsContext,
            is_server=controlling,
            transport=transceiver._iceConnection)

    def __setIceConnectionState(self, state):
        self.__iceConnectionState = state
        self.emit('iceconnectionstatechange')

    def __setIceGatheringState(self, state):
        self.__iceGatheringState = state
        self.emit('icegatheringstatechange')

    def __setSignalingState(self, state):
        self.__signalingState = state
        self.emit('signalingstatechange')

    def __transports(self):
        for transceiver in self.__transceivers:
            yield transceiver._iceConnection, transceiver._dtlsSession
        if self.__sctp:
            yield self.__sctp._iceConnection, self.__sctp._dtlsSession
