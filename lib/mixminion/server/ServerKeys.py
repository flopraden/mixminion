# Copyright 2002-2011 Nick Mathewson.  See LICENSE for licensing information.

"""mixminion.ServerKeys

   Classes for servers to generate and store keys and server descriptors.
   """
#FFFF We need support for encrypting private keys.

__all__ = [ "ServerKeyring", "generateServerDescriptorAndKeys",
            "generateCertChain" ]

import os
import errno
import socket
import re
import sys
import time
import threading
import urllib
import urllib2
if sys.version_info >= (2,7,9):
    import ssl

import mixminion._minionlib
import mixminion.Crypto
import mixminion.NetUtils
import mixminion.Packet
import mixminion.server.HashLog
import mixminion.server.MMTPServer
import mixminion.server.ServerMain

from mixminion.ServerInfo import ServerInfo, PACKET_KEY_BYTES, MMTP_KEY_BYTES,\
     signServerInfo
from mixminion.Common import AtomicFile, LOG, MixError, MixFatalError, \
     ceilDiv, createPrivateDir, checkPrivateFile, englishSequence, \
     formatBase64, formatDate, formatTime, previousMidnight, readFile, \
     replaceFile, secureDelete, tryUnlink, UIError, writeFile
from mixminion.Config import ConfigError

#----------------------------------------------------------------------

# Seconds before a key becomes live that we want to generate
# and publish it.
#
#FFFF Make this configurable?  (Set to 2 days, 13 hours)
PUBLICATION_LATENCY = (2*24+13)*60*60

# Number of seconds worth of keys we want to generate in advance.
#
#FFFF Make this configurable?  (Set to 2 weeks).
PREPUBLICATION_INTERVAL = 14*24*60*60

# URL to which we should post published servers.
#
#FFFF Make this configurable
#DIRECTORY_UPLOAD_URL = "http://mixminion.net/minion-cgi/publish"
DIRECTORY_UPLOAD_URL = "https://anemone.mooo.com:8001/publish"


# We have our X509 certificate set to expire a bit after public key does,
# so that slightly-skewed clients don't incorrectly give up while trying to
# connect to us.  (And so that we don't mess up the world while being
# slightly skewed.)
CERTIFICATE_EXPIRY_SLOPPINESS = 2*60*60

# DOCDOC
CERTIFICATE_LIFETIME = 24*60*60

#----------------------------------------------------------------------
class ServerKeyring:
    """A ServerKeyring remembers current and future keys, descriptors, and
       hash logs for a mixminion server.  It keeps track of key rotation
       schedules, and generates new keys as needed.
       """
    ## Fields:
    # homeDir: server home directory
    # keyDir: server key directory
    # keyOverlap: How long after a new key begins do we accept the old one?
    # keySets: sorted list of (start, end, keyset)
    # nextUpdate: time_t when a new key should be added, or a current key
    #      should be removed, or "None" for uncalculated.
    # keyRange: tuple of (firstKey, lastKey) to represent which key names
    #      have keys on disk.
    # currentKeys: None, if we haven't checked for currently live keys, or
    #      a list of currently live ServerKeyset objects.
    # dhFile: pathname to file holding diffie-helman parameters.
    # _lock: A lock to prevent concurrent key generation or rotation.

    def __init__(self, config):
        "Create a ServerKeyring from a config object"
        self._lock = threading.RLock()
        self.configure(config)

    def configure(self, config):
        "Set up a ServerKeyring from a config object"
        self.config = config
        self.homeDir = config.getBaseDir()
        self.keyDir = config.getKeyDir()
        self.hashDir = os.path.join(config.getWorkDir(), 'hashlogs')
        self.dhFile = os.path.join(config.getWorkDir(), 'tls', 'dhparam')
        self.certFile = os.path.join(config.getWorkDir(), "cert_chain")
        self.keyOverlap = config['Server']['PublicKeyOverlap'].getSeconds()
        self.nickname = config['Server']['Nickname'] #DOCDOC
        self.nextUpdate = None
        self.currentKeys = None
        self._tlsContext = None #DOCDOC
        self._tlsContextExpires = -1 #DOCDOC
        self.pingerSeed = None
        self.checkKeys()

    def checkKeys(self):
        """Internal method: read information about all this server's
           currently-prepared keys from disk.

           May raise ConfigError if any of the server descriptors on disk
           are invalid.
           """
        self.keySets = []
        badKeySets = []
        firstKey = sys.maxint
        lastKey = 0

        LOG.debug("Scanning server keystore at %s", self.keyDir)

        if not os.path.exists(self.keyDir):
            LOG.info("Creating server keystore at %s", self.keyDir)
            createPrivateDir(self.keyDir)

        # Iterate over the entires in HOME/keys
        for dirname in os.listdir(self.keyDir):
            # Skip any that aren't directories named "key_INT"
            if not os.path.isdir(os.path.join(self.keyDir,dirname)):
                continue
            if not dirname.startswith('key_'):
                LOG.warn("Unexpected directory %s under %s",
                              dirname, self.keyDir)
                continue
            keysetname = dirname[4:]
            try:
                setNum = int(keysetname)
                # keep trace of the first and last used key number
                if setNum < firstKey: firstKey = setNum
                if setNum > lastKey: lastKey = setNum
            except ValueError:
                LOG.warn("Unexpected directory %s under %s",
                              dirname, self.keyDir)
                continue

            # Find the server descriptor...
            keyset = ServerKeyset(self.keyDir, keysetname, self.hashDir)
            ok = 1
            try:
                keyset.checkKeys()
            except MixError, e:
                LOG.warn("Error checking private keys in keyset %s: %s",
                         keysetname, str(e))
                ok = 0

            try:
                if ok:
                    keyset.getServerDescriptor()
            except (ConfigError, IOError), e:
                LOG.warn("Key set %s has invalid/missing descriptor: %s",
                         keysetname, str(e))
                ok = 0

            if ok:
                t1, t2 = keyset.getLiveness()
                self.keySets.append( (t1, t2, keyset) )

                LOG.trace("Found key %s (valid from %s to %s)",
                          dirname, formatDate(t1), formatDate(t2))
            else:
                badKeySets.append(keyset)

        LOG.debug("Found %s keysets: %s were incomplete or invalid.",
                  len(self.keySets), len(badKeySets))

        if badKeySets:
            LOG.warn("Removing %s invalid keysets", len(badKeySets))
        for b in badKeySets:
            b.delete()

        # Now, sort the key intervals by starting time.
        self.keySets.sort()
        self.keyRange = (firstKey, lastKey)

        # Now we try to see whether we have more or less than 1 key in effect
        # for a given time.
        for idx in xrange(len(self.keySets)-1):
            end = self.keySets[idx][1]
            start = self.keySets[idx+1][0]
            if start < end:
                LOG.warn("Multiple keys for %s.  That's unsupported.",
                              formatDate(end))
            elif start > end:
                LOG.warn("Gap in key schedule: no key from %s to %s",
                              formatDate(end), formatDate(start))

    def checkDescriptorConsistency(self, regen=1):
        """Check whether the server descriptors in this keyring are
           consistent with the server's configuration.  If 'regen' is
           true, inconsistent descriptors are regenerated."""
        identity = None
        state = []
        for _,_,ks in self.keySets:
            ok = ks.checkConsistency(self.config, 0)
            if ok == 'good':
                continue
            state.append((ok, ks))

        if not state:
            return

        LOG.warn("Some generated keysets do not match "
                  "current configuration...")

        for ok, ks in state:
            va,vu = ks.getLiveness()
            LOG.warn("Keyset %s (%s--%s):",ks.keyname,formatTime(va,1),
                     formatTime(vu,1))
            ks.checkConsistency(self.config, 1)
            if regen and ok == 'bad':
                if not identity: identity = self.getIdentityKey()
                ks.regenerateServerDescriptor(self.config, identity)

    def getIdentityKey(self):
        """Return this server's identity key.  Generate one if it doesn't
           exist."""
        password = None # FFFF Use this, somehow.
        fn = os.path.join(self.keyDir, "identity.key")
        bits = self.config['Server']['IdentityKeyBits']
        if os.path.exists(fn):
            checkPrivateFile(fn)
            key = mixminion.Crypto.pk_PEM_load(fn, password)
            keylen = key.get_modulus_bytes()*8
            if keylen != bits:
                LOG.warn(
                    "Stored identity key has %s bits, but you asked for %s.",
                    keylen, bits)
        else:
            LOG.info("Generating identity key. (This may take a while.)")
            key = mixminion.Crypto.pk_generate(bits)
            mixminion.Crypto.pk_PEM_save(key, fn, password)
            LOG.info("Generated %s-bit identity key.", bits)

        return key

    def getPingerSeed(self):
        """DOCDOC"""
        if self.pingerSeed is not None:
            return self.pingerSeed

        fn = os.path.join(self.keyDir, "pinger.seed")
        if os.path.exists(fn):
            checkPrivateFile(fn)
            r = readFile(fn)
            if len(r) == mixminion.Crypto.DIGEST_LEN:
                self.pingerSeed = r
                return r

        self.pingerSeed = r = mixminion.Crypto.trng(mixminion.Crypto.DIGEST_LEN)
        createPrivateDir(self.keyDir)
        writeFile(fn, r, 0600)
        return r

    def getIdentityKeyDigest(self):
        """DOCDOC"""
        k = self.getIdentityKey()
        return mixminion.Crypto.sha1(mixminion.Crypto.pk_encode_public_key(k))

    def publishKeys(self, allKeys=0):
        """Publish server descriptors to the directory server.  Ordinarily,
           only unpublished descriptors are sent.  If allKeys is true,
           all descriptors are sent."""
        keySets = [ ks for _, _, ks in self.keySets ]
        if allKeys:
            LOG.info("Republishing all known keys to directory server")
        else:
            keySets = [ ks for ks in keySets if not ks.isPublished() ]
            if not keySets:
                LOG.trace("publishKeys: no unpublished keys found")
                return
            LOG.info("Publishing %s keys to directory server...",len(keySets))

        rejected = 0
        for ks in keySets:
            status = ks.publish(DIRECTORY_UPLOAD_URL)
            if status == 'error':
                LOG.error("Error publishing a key; giving up")
                return 0
            elif status == 'reject':
                rejected += 1
            else:
                assert status == 'accept'
        if rejected == 0:
            LOG.info("All keys published successfully.")
            return 1
        else:
            LOG.info("%s/%s keys were rejected." , rejected, len(keySets))
            return 0

    def removeIdentityKey(self):
        """Remove this server's identity key."""
        fn = os.path.join(self.keyDir, "identity.key")
        if not os.path.exists(fn):
            LOG.info("No identity key to remove.")
        else:
            LOG.warn("Removing identity key in 10 seconds")
            time.sleep(10)
            LOG.warn("Removing identity key")
            secureDelete([fn], blocking=1)

        if os.path.exists(self.dhFile):
            LOG.info("Removing diffie-helman parameters file")
            secureDelete([self.dhFile], blocking=1)

    def createKeysAsNeeded(self,now=None):
        """Generate new keys and descriptors as needed, so that the next
           PUBLICATION_LATENCY+PREPUBLICATION_INTERVAL seconds are covered."""
        if now is None:
            now = time.time()

        if self.getNextKeygen() > now-10: # 10 seconds of leeway
            return

        if self.keySets:
            lastExpiry = self.keySets[-1][1]
            if lastExpiry < now:
                lastExpiry = now
        else:
            lastExpiry = now

        needToCoverUntil = now+PUBLICATION_LATENCY+PREPUBLICATION_INTERVAL
        timeToCover = needToCoverUntil-lastExpiry

        lifetime = self.config['Server']['PublicKeyLifetime'].getSeconds()
        nKeys = int(ceilDiv(timeToCover, lifetime))

        LOG.info("Creating %s keys", nKeys)
        self.createKeys(num=nKeys)

    def createKeys(self, num=1, startAt=None):
        """Generate 'num' public keys for this server. If startAt is provided,
           make the first key become valid at 'startAt'.  Otherwise, make the
           first key become valid right after the last key we currently have
           expires.  If we have no keys now, make the first key start now."""
        # FFFF Use this.
        #password = None

        if startAt is None:
            if self.keySets:
                startAt = self.keySets[-1][1]+60
                if startAt < time.time():
                    startAt = time.time()+60
            else:
                startAt = time.time()+60

        startAt = previousMidnight(startAt)

        firstKey, lastKey = self.keyRange

        for _ in xrange(num):
            if firstKey == sys.maxint:
                keynum = firstKey = lastKey = 1
            elif firstKey > 1:
                firstKey -= 1
                keynum = firstKey
            else:
                lastKey += 1
                keynum = lastKey

            keyname = "%04d" % keynum

            lifetime = self.config['Server']['PublicKeyLifetime'].getSeconds()
            nextStart = startAt + lifetime

            LOG.info("Generating key %s to run from %s through %s (GMT)",
                     keyname, formatDate(startAt),
                     formatDate(nextStart-3600))
            generateServerDescriptorAndKeys(config=self.config,
                                            identityKey=self.getIdentityKey(),
                                            keyname=keyname,
                                            keydir=self.keyDir,
                                            hashdir=self.hashDir,
                                            validAt=startAt)
            startAt = nextStart

        self.checkKeys()

    def regenerateDescriptors(self):
        """Regenerate all server descriptors for all keysets in this
           keyring, but keep all old keys intact."""
        LOG.info("Regenerating server descriptors; keeping old keys.")
        identityKey = self.getIdentityKey()
        for _,_,ks in self.keySets:
            ks.regenerateServerDescriptor(self.config, identityKey)

    def getNextKeygen(self):
        """Return the time (in seconds) when we should next generate keys.
           If -1 is returned, keygen should occur immediately.
        """
        if not self.keySets:
            return -1

        # Our last current key expires at 'lastExpiry'.
        lastExpiry = self.keySets[-1][1]
        # We want to have keys in the directory valid for
        # PREPUBLICATION_INTERVAL seconds after that, and we assume that
        # a key takes up to PUBLICATION_LATENCY seconds to make it into the
        # directory.
        nextKeygen = lastExpiry - PUBLICATION_LATENCY - PREPUBLICATION_INTERVAL

        LOG.info("Last expiry at %s; next keygen at %s",
                 formatTime(lastExpiry,1), formatTime(nextKeygen, 1))
        return nextKeygen

    def removeDeadKeys(self, now=None):
        """Remove all keys that have expired."""
        self.checkKeys()
        keys = self.getDeadKeys(now)
        for message, keyset in keys:
            LOG.info(message)
            keyset.delete()
        self.checkKeys()

    def getDeadKeys(self,now=None):
        """Helper function: return a list of (informative-message, keyset
           object) for each expired keyset in the keystore.  Does not rescan
           the keystore or remove dead keys.
        """
        if now is None:
            now = time.time()
            expiryStr = " expired"
        else:
            expiryStr = ""

        cutoff = now - self.keyOverlap

        result = []
        for va, vu, keyset in self.keySets:
            if vu >= cutoff:
                continue
            name = keyset.keyname
            message ="Removing%s key %s (valid from %s through %s)"%(
                      expiryStr, name, formatDate(va), formatDate(vu))
            result.append((message, keyset))

        return result

    def _getLiveKeys(self, now=None):
        """Find all keys that are now valid.  Return list of (Valid-after,
           valid-util, keyset)."""
        if not self.keySets:
            return []
        if now is None:
            now = time.time()

        cutoff = now-self.keyOverlap
        # A key is live if
        #     * it became valid before now, and
        #     * it did not become invalid until keyOverlap seconds ago

        return [ (va,vu,k) for (va,vu,k) in self.keySets
                 if va <= now and vu >= cutoff ]

    def getServerKeysets(self, now=None):
        """Return list of ServerKeyset objects for the currently live keys.
        """
        # FFFF Support passwords on keys
        keysets = [ ]
        for va, vu, ks in self._getLiveKeys(now):
            ks.load()
            keysets.append(ks)

        return keysets

    def _getDHFile(self):
        """Return the filename for the diffie-helman parameters for the
           server.  Creates the file if it doesn't yet exist."""
        dhdir = os.path.split(self.dhFile)[0]
        createPrivateDir(dhdir)
        if not os.path.exists(self.dhFile):
            # ???? This is only using 512-bit Diffie-Hellman!  That isn't
            # ???? remotely enough.
            LOG.info("Generating Diffie-Helman parameters for TLS...")
            mixminion._minionlib.generate_dh_parameters(self.dhFile, verbose=0)
            LOG.info("...done")
        else:
            LOG.debug("Using existing Diffie-Helman parameter from %s",
                           self.dhFile)

        return self.dhFile

    def _newTLSContext(self, now=None):
        """Create and return a TLS context."""
        if now is None:
            now = time.time()
        mmtpKey = mixminion.Crypto.pk_generate(MMTP_KEY_BYTES*8)

        certStarts = now - CERTIFICATE_EXPIRY_SLOPPINESS
        expires = now + CERTIFICATE_LIFETIME
        certEnds = now + CERTIFICATE_LIFETIME + CERTIFICATE_EXPIRY_SLOPPINESS

        tmpName = self.certFile + "_tmp"
        generateCertChain(tmpName, mmtpKey, self.getIdentityKey(),
                          self.nickname, certStarts, certEnds)
        replaceFile(tmpName, self.certFile)

        self._tlsContext = (
                    mixminion._minionlib.TLSContext_new(self.certFile,
                                                        mmtpKey,
                                                        self._getDHFile()))
        self._tlsContextExpires = expires
        return self._tlsContext

    def _getTLSContext(self, force=0, now=None):
        if now is None:
            now = time.time()
        if force or self._tlsContext is None or self._tlsContextExpires < now:
            return self._newTLSContext(now=now)
        else:
            return self._tlsContext

    def updateMMTPServerTLSContext(self,mmtpServer,force=0,now=None):
        """DOCDOC"""
        context = self._getTLSContext(force=force,now=now)
        mmtpServer.setServerContext(context)
        return self._tlsContextExpires

    def updateKeys(self, packetHandler, statusFile=None,when=None):
        """Update the keys stored in a PacketHandler,
           MMTPServer object, so that they contain the currently correct
           keys.  Also removes any dead keys.

           This function is idempotent.
        """
        self.checkKeys()
        deadKeys = self.getDeadKeys(when)
        self.currentKeys = keys = self.getServerKeysets(when)
        keyNames = [k.keyname for k in keys]
        deadKeyNames = [k.keyname for msg, k in deadKeys]
        LOG.info("Updating keys: %s currently valid (%s); %s expired (%s)",
                 len(keys), " ".join(keyNames),
                 len(deadKeys), " ".join(deadKeyNames))
        if packetHandler is not None:
            packetKeys = []
            hashLogs = []

            for k in keys:
                packetKeys.append(k.getPacketKey())
                hashLogs.append(k.getHashLog())
            packetHandler.setKeys(packetKeys, hashLogs)

        if statusFile:
            writeFile(statusFile,
                    "".join(["%s\n"%k.getDescriptorFileName() for k in keys]),
                    0644)

        for msg, ks in deadKeys:
            LOG.info(msg)
            ks.delete()

        if deadKeys:
            self.checkKeys()

        self.nextUpdate = None
        self.getNextKeyRotation(keys)

    def getNextKeyRotation(self, curKeys=None):
        """Calculate the next time at which we should change the set of live
           keys."""
        if self.nextUpdate is None:
            if curKeys is None:
                if self.currentKeys is None:
                    curKeys = self.getServerKeysets()
                else:
                    curKeys = self.currentKeys
            events = []
            curNames = {}
            # For every current keyset, we'll remove it at keyOverlap
            # seconds after its stated expiry time.
            for k in curKeys:
                va, vu = k.getLiveness()
                events.append((vu+self.keyOverlap, "RM"))
                curNames[k.keyname] = 1
            # For every other keyset, we'll add it when it becomes valid.
            for va, vu, k in self.keySets:
                if curNames.has_key(k.keyname): continue
                events.append((va, "ADD"))

            # Which even happens first?
            events.sort()
            if not events:
                LOG.info("No future key rotation events.")
                self.nextUpdate = sys.maxint
                return self.nextUpdate

            self.nextUpdate, eventType = events[0]
            if eventType == "RM":
                LOG.info("Next key event: old key is removed at %s",
                         formatTime(self.nextUpdate,1))
            else:
                assert eventType == "ADD"
                LOG.info("Next key event: new key becomes valid at %s",
                         formatTime(self.nextUpdate,1))

        return self.nextUpdate

    def getCurrentDescriptor(self, now=None):
        """DOCDOC"""
        self._lock.acquire()
        if now is None:
            now = time.time()
        try:
            keysets = self.getServerKeysets()
            for k in keysets:
                va,vu = k.getLiveness()
                if va <= now <= vu:
                    return k.getServerDescriptor()

            LOG.warn("getCurrentDescriptor: no live keysets??")
            return self.getServerKeysets()[-1].getServerDescriptor()
        finally:
            self._lock.release()

    def lock(self, blocking=1):
        return self._lock.acquire(blocking)

    def unlock(self):
        self._lock.release()

#----------------------------------------------------------------------
class ServerKeyset:
    """A set of expirable keys for use by a server.

       A server has one long-lived identity key, and two short-lived
       temporary keys: one for subheader encryption and one for MMTP.  The
       subheader (or 'packet') key has an associated hashlog, and the
       MMTP key has an associated self-signed X509 certificate.

       Whether we publish or not, we always generate a server descriptor
       to store the keys' lifetimes.

       When we create a new ServerKeyset object, the associated keys are not
       read from disk until the object's load method is called."""
    ## Fields:
    # keydir: Directory to store this keyset's data.
    # hashlogFile: filename of this keyset's hashlog.
    # packetKeyFile, mmtpKeyFile: filename of this keyset's short-term keys
    # descFile: filename of this keyset's server descriptor.
    # publishedFile: filename to store this server's publication time.
    #
    # packetKey, mmtpKey: This server's actual short-term keys.
    #
    # serverinfo: None, or a parsed server descriptor.
    # validAfter, validUntil: This keyset's published lifespan, or None.
    # published: has this boolean: has this server been published?
    def __init__(self, keyroot, keyname, hashroot):
        """Load a set of keys named "keyname" on a server where all keys
           are stored under the directory "keyroot" and hashlogs are stored
           under "hashroot". """
        self.keyroot = keyroot
        self.keyname = keyname
        self.hashroot= hashroot

        self.keydir = keydir = os.path.join(keyroot, "key_"+keyname)
        self.hashlogFile = os.path.join(hashroot, "hash_"+keyname)
        self.packetKeyFile = os.path.join(keydir, "mix.key")
        self.mmtpKeyFile = os.path.join(keydir, "mmtp.key")
        self.certFile = os.path.join(keydir, "mmtp.cert")
        if os.path.exists(self.mmtpKeyFile):
            secureDelete(self.mmtpKeyFile)
        if os.path.exists(self.certFile):
            secureDelete(self.certFile)
        self.descFile = os.path.join(keydir, "ServerDesc")
        self.publishedFile = os.path.join(keydir, "published")
        self.serverinfo = None
        self.validAfter = None
        self.validUntil = None
        self.published = os.path.exists(self.publishedFile)
        if not os.path.exists(keydir):
            createPrivateDir(keydir)

    def delete(self):
        """Remove this keyset from disk."""
        files = [self.packetKeyFile,
                 self.descFile,
                 self.publishedFile,
                 self.hashlogFile ]
        files = [f for f in files if os.path.exists(f)]
        secureDelete(files, blocking=1)
        mixminion.server.HashLog.deleteHashLog(self.hashlogFile)
        os.rmdir(self.keydir)

    def checkKeys(self):
        """Check whether all the required keys exist and are private."""
        checkPrivateFile(self.packetKeyFile)

    def load(self, password=None):
        """Read the short-term keys from disk.  Must be called before
           getPacketKey or getMMTPKey."""
        self.checkKeys()
        self.packetKey = mixminion.Crypto.pk_PEM_load(self.packetKeyFile,
                                                      password)
    def save(self, password=None):
        """Save this set of keys to disk."""
        mixminion.Crypto.pk_PEM_save(self.packetKey, self.packetKeyFile,
                                     password)

    def clear(self):
        """Stop holding the keys in memory."""
        self.packetKey = None

    def getHashLogFileName(self): return self.hashlogFile
    def getDescriptorFileName(self): return self.descFile
    def getPacketKey(self): return self.packetKey
    def getPacketKeyID(self):
        "Return the sha1 hash of the asn1 encoding of the packet public key"
        return mixminion.Crypto.sha1(self.packetKey.encode_key(1))
    def getServerDescriptor(self):
        """Return a ServerInfo for this keyset, reading it from disk if
           needed."""
        if self.serverinfo is None:
            self.serverinfo = ServerInfo(fname=self.descFile)
        return self.serverinfo
    def getHashLog(self):
        return mixminion.server.HashLog.getHashLog(
            self.getHashLogFileName(), self.getPacketKeyID())
    def getLiveness(self):
        """Return a 2-tuple of validAfter/validUntil for this server."""
        if self.validAfter is None or self.validUntil is None:
            info = self.getServerDescriptor()
            self.validAfter = info['Server']['Valid-After']
            self.validUntil = info['Server']['Valid-Until']
        return self.validAfter, self.validUntil
    def isPublished(self):
        """Return true iff we have published this keyset."""
        return self.published
    def markAsPublished(self):
        """Mark this keyset as published."""
        contents = "%s\n"%formatTime(time.time(),1)
        writeFile(self.publishedFile, contents, mode=0600)
        self.published = 1
    def markAsUnpublished(self):
        """Mark this keyset as unpublished."""
        tryUnlink(self.publishedFile)
        self.published = 0
    def regenerateServerDescriptor(self, config, identityKey):
        """Regenerate the server descriptor for this keyset, keeping the
           original keys."""
        self.load()
        self.markAsUnpublished()
        validAt,validUntil = self.getLiveness()
        LOG.info("Regenerating descriptor for keyset %s (%s--%s)",
                 self.keyname, formatTime(validAt,1),
                 formatTime(validUntil,1))
        generateServerDescriptorAndKeys(config, identityKey,
                         self.keyroot, self.keyname, self.hashroot,
                         validAt=validAt, validUntil=validUntil,
                         useServerKeys=1)
        self.serverinfo = self.validAfter = self.validUntil = None

    def checkConsistency(self, config, log=1):
        """Check whether this server descriptor is consistent with a
           given configuration file.  Returns are as for
           'checkDescriptorConsistency'.
        """
        return checkDescriptorConsistency(self.getServerDescriptor(),
                                          config,
                                          log=log,
                                          isPublished=self.published)

    def publish(self, url):
        """Try to publish this descriptor to a given directory URL.  Returns
           'accept' if the publication was successful, 'reject' if the
           server refused to accept the descriptor, and 'error' if
           publication failed for some other reason."""
        fname = self.getDescriptorFileName()
        descriptor = readFile(fname)
        fields = urllib.urlencode({"desc" : descriptor})
        f = None
        try:
            try:
                #############################################
                # some python versions verify certificates
                # anemone.mooo.com uses a self-signed cert
                # this workaround is not a problem because
                # the directory information is already signed
                # (although as Zax says, it is certainly a
                # kludge ;)
                if sys.version_info >= (2,7,9):
                    ctx = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    f = urllib2.urlopen(url, fields, context=ctx)
                else:
                    f = urllib2.urlopen(url, fields)
                #############################################
                #f = urllib2.urlopen(url, fields)
                info = f.info()
                reply = f.read()
            except IOError, e:
                LOG.error("Error while publishing server descriptor: %s",e)
                return 'error'
            except:
                LOG.error_exc(sys.exc_info(),
                              "Error publishing server descriptor")
                return 'error'
        finally:
            if f is not None:
                f.close()

        if info.get('Content-Type') != 'text/plain':
            LOG.error("Bad content type %s from directory"%info.get(
                'Content-Type'))
            return 'error'
        m = DIRECTORY_RESPONSE_RE.search(reply)
        if not m:
            LOG.error("Didn't understand reply from directory: %s",
                      reply)
            return 'error'
        ok = int(m.group(1))
        msg = m.group(2)
        if not ok:
            LOG.error("Directory rejected descriptor: %r", msg)
            return 'reject'

        LOG.info("Directory accepted descriptor: %r", msg)
        self.markAsPublished()
        return 'accept'

# Matches the reply a directory server gives.
DIRECTORY_RESPONSE_RE = re.compile(r'^Status: (0|1)[ \t]*\nMessage: (.*)$',
                                   re.M)

class _WarnWrapper:
    """Helper for 'checkDescriptorConsistency' to keep its implementation
       short.  Counts the number of times it's invoked, and delegates to
       LOG.warn if silence is false."""
    def __init__(self, silence, isPublished):
        self.silence = silence
        self.errors = 0
        self.called = 0
        self.published = isPublished
    def __call__(self, *args):
        self.called = 1
        self.errors += 1
        if not self.published:
            args = list(args)
            args[0] = args[0].replace("published", "in unpublished descriptor")
        if not self.silence:
            LOG.warn(*args)

def checkDescriptorConsistency(info, config, log=1, isPublished=1):
    """Given a ServerInfo and a ServerConfig, compare them for consistency.

       Returns 'good' iff info may have come from 'config'.

       If the server is inconsistent with the configuration file and should
       be regenerated, returns 'bad'.  Otherwise, returns 'so-so'.

       If 'log' is true, warn as well.  Does not check keys.
    """
    #XXXX This needs unit tests.  For now, though, it seems to work.
    warn = _WarnWrapper(silence = not log, isPublished=isPublished)

    config_s = config['Server']
    info_s = info['Server']
    if info_s['Nickname'] != config_s['Nickname']:
        warn("Mismatched nicknames: %s in configuration; %s published.",
             config_s['Nickname'], info_s['Nickname'])

    idBits = info_s['Identity'].get_modulus_bytes()*8
    confIDBits = config_s['IdentityKeyBits']
    if idBits != confIDBits:
        warn("Mismatched identity bits: %s in configuration; %s published.",
             confIDBits, idBits)
        warn.errors -= 1 # We can't do anything about this!

    if config_s['Contact-Email'] != info_s['Contact']:
        warn("Mismatched contacts: %s in configuration; %s published.",
             config_s['Contact-Email'], info_s['Contact'])
    if config_s['Contact-Fingerprint'] != info_s['Contact-Fingerprint']:
        warn("Mismatched contact fingerprints.")

    if info_s['Software'] and info_s['Software'] != (
        "Mixminion %s" % mixminion.__version__):
        warn("Mismatched versions: running %s; %s published.",
             mixminion.__version__, info_s['Software'])

    if config_s['Comments'] != info_s['Comments']:
        warn("Mismatched comments field.")

    if (previousMidnight(info_s['Valid-Until']) !=
        previousMidnight(config_s['PublicKeyLifetime'].getSeconds() +
                         info_s['Valid-After'])):
        warn("Published lifetime does not match PublicKeyLifetime")
        warn("(Future keys will be generated with the correct lifetime")
        warn.errors -= 2 # We can't do anything about this!

    insecurities = config.getInsecurities()
    if insecurities:
        if (info_s['Secure-Configuration'] or
            info_s.get('Why-Insecure',None)!=", ".join(insecurities)):
            warn("Mismatched Secure-Configuration: %r %r %r",
                 info_s['Secure-Configuration'],
                 info_s.get("Why-Insecure",None),
                 ", ".join(insecurities))
    else:
        if not info_s['Secure-Configuration'] or info_s.get('Why-Insecure'):
            warn("Mismatched Secure-Configuration")

    info_im = info['Incoming/MMTP']
    config_im = config['Incoming/MMTP']
    if info_im['Port'] != config_im['Port']:
        warn("Mismatched ports: %s configured; %s published.",
             config_im['Port'], info_im['Port'])

##     info_ip = info_im.get('IP',None)
##     if config_im['IP'] == '0.0.0.0':
##         guessed = _guessLocalIP()
##         if guessed != info_ip:
##             warn("Mismatched IPs: Guessed IP (%s); %s published.",
##                  guessed, info_ip)
##     elif config_im['IP'] != info_ip:
##         warn("Mismatched IPs: %s configured; %s published.",
##              config_im['IP'], info_ip)

    info_host = info_im.get('Hostname',None)
    config_host = config_im['Hostname']
    if config_host is None:
        guessed = socket.getfqdn()
        if guessed != info_host:
            warn("Mismatched hostnames: %s guessed; %s published",
                 guessed, info_host)
    elif config_host != info_host:
        warn("Mismatched hostnames: %s configured, %s published",
             config_host, info_host)

    if config_im['Enabled'] and not info_im.get('Version'):
        warn("Incoming MMTP enabled but not published.")
    elif not config_im['Enabled'] and info_im.get('Version'):
        warn("Incoming MMTP published but not enabled.")

    for section in ('Outgoing/MMTP', 'Delivery/MBOX', 'Delivery/SMTP'):
        info_out = info[section].get('Version')
        config_out = (config[section].get('Enabled') and
                      config[section].get('Advertise',1))
        if not config_out and section == 'Delivery/SMTP':
            config_out = (config['Delivery/SMTP-Via-Mixmaster'].get("Enabled")
                 and config['Delivery/SMTP-Via-Mixmaster'].get("Advertise", 1))
        if info_out and not config_out:
            warn("%s published, but not enabled.", section)
        if config_out and not info_out:
            warn("%s enabled, but not published.", section)

    info_testing = info.get("Testing",{})
    if info_testing.get("Platform", "") != getPlatformSummary():
        warn("Mismatched platform: running %r, but %r published",
             getPlatformSummary(), info_testing.get("Platform",""))
    if not warn.errors and info_testing.get("Configuration", "") != config.getConfigurationSummary():
        warn("Configuration has changed since last publication")

    if warn.errors:
        return "bad"
    elif warn.called:
        return "so-so"
    else:
        return "good"

#----------------------------------------------------------------------
# Functionality to generate keys and server descriptors

def generateServerDescriptorAndKeys(config, identityKey, keydir, keyname,
                                    hashdir, validAt=None, now=None,
                                    useServerKeys=0, validUntil=None):
    """Generate and sign a new server descriptor, and generate all the keys to
       go with it.

          config -- Our ServerConfig object.
          identityKey -- This server's private identity key
          keydir -- The root directory for storing key sets.
          keyname -- The name of this new key set within keydir
          hashdir -- The root directory for storing hash logs.
          validAt -- The starting time (in seconds) for this key's lifetime.
          useServerKeys -- If true, try to read an existing keyset from
               (keydir,keyname,hashdir) rather than generating a fresh one.
          validUntil -- Time at which the generated descriptor should
               expire.
    """
    if useServerKeys:
        serverKeys = ServerKeyset(keydir, keyname, hashdir)
        serverKeys.load()
        packetKey = serverKeys.packetKey
    else:
        # First, we generate both of our short-term keys...
        packetKey = mixminion.Crypto.pk_generate(PACKET_KEY_BYTES*8)

        # ...and save them to disk, setting up our directory structure while
        # we're at it.
        serverKeys = ServerKeyset(keydir, keyname, hashdir)
        serverKeys.packetKey = packetKey
        serverKeys.save()

    # FFFF unused
    # allowIncoming = config['Incoming/MMTP'].get('Enabled', 0)

    # Now, we pull all the information we need from our configuration.
    nickname = config['Server']['Nickname']
    contact = config['Server']['Contact-Email']
    fingerprint = config['Server']['Contact-Fingerprint']
    comments = config['Server']['Comments']
    if not now:
        now = time.time()
    if not validAt:
        validAt = now

    insecurities = config.getInsecurities()
    if insecurities:
        secure = "no"
    else:
        secure = "yes"

    # Calculate descriptor and X509 certificate lifetimes.
    # (Round validAt to previous midnight.)
    validAt = mixminion.Common.previousMidnight(validAt+30)
    if not validUntil:
        keyLifetime = config['Server']['PublicKeyLifetime'].getSeconds()
        validUntil = previousMidnight(validAt + keyLifetime + 30)

    mmtpProtocolsIn = mixminion.server.MMTPServer.MMTPServerConnection \
                      .PROTOCOL_VERSIONS[:]
    mmtpProtocolsOut = mixminion.server.MMTPServer.MMTPClientConnection \
                       .PROTOCOL_VERSIONS[:]
    mmtpProtocolsIn.sort()
    mmtpProtocolsOut.sort()
    mmtpProtocolsIn = ",".join(mmtpProtocolsIn)
    mmtpProtocolsOut = ",".join(mmtpProtocolsOut)


    #XXXX009 remove: hasn't been checked since 007 or used since 005.
    identityKeyID = formatBase64(
                      mixminion.Crypto.sha1(
                          mixminion.Crypto.pk_encode_public_key(identityKey)))

    fields = {
        # XXXX009 remove: hasn't been checked since 007.
        "IP": config['Incoming/MMTP'].get('IP', "0.0.0.0"),
        "Hostname": config['Incoming/MMTP'].get('Hostname', None),
        "Port": config['Incoming/MMTP'].get('Port', 0),
        "Nickname": nickname,
        "Identity":
           formatBase64(mixminion.Crypto.pk_encode_public_key(identityKey)),
        "Published": formatTime(now),
        "ValidAfter": formatDate(validAt),
        "ValidUntil": formatDate(validUntil),
        "PacketKey":
           formatBase64(mixminion.Crypto.pk_encode_public_key(packetKey)),
        "KeyID": identityKeyID,
        "MMTPProtocolsIn" : mmtpProtocolsIn,
        "MMTPProtocolsOut" : mmtpProtocolsOut,
        "PacketVersion" : mixminion.Packet.PACKET_VERSION,
        "mm_version" : mixminion.__version__,
        "Secure" : secure,
        "Contact" : contact,
        }

    # If we don't know our IP address, try to guess
    if fields['IP'] == '0.0.0.0': #XXXX008 remove; not needed since 005.
        try:
            fields['IP'] = _guessLocalIP()
            LOG.warn("No IP configured; guessing %s",fields['IP'])
        except IPGuessError, e:
            LOG.error("Can't guess IP: %s", str(e))
            raise UIError("Can't guess IP: %s" % str(e))
    # If we don't know our Hostname, try to guess
    if fields['Hostname'] is None:
        fields['Hostname'] = socket.getfqdn()
        LOG.warn("No Hostname configured; guessing %s",fields['Hostname'])
    try:
        _checkHostnameIsLocal(fields['Hostname'])
        dnsResults = mixminion.NetUtils.getIPs(fields['Hostname'])
    except socket.error, e:
        LOG.warn("Can't resolve configured hostname %r: %s",
                 fields['Hostname'],str(e))
    else:
        found = [ ip for _,ip,_ in dnsResults ]
        if fields['IP'] not in found:
            LOG.warn("Configured hostname %r resolves to %s, but we're publishing the IP %s",
                     fields['Hostname'], englishSequence(found), fields['IP'])

    # Fill in a stock server descriptor.  Note the empty Digest: and
    # Signature: lines.
    info = """\
        [Server]
        Descriptor-Version: 0.2
        Nickname: %(Nickname)s
        Identity: %(Identity)s
        Digest:
        Signature:
        Published: %(Published)s
        Valid-After: %(ValidAfter)s
        Valid-Until: %(ValidUntil)s
        Packet-Key: %(PacketKey)s
        Packet-Versions: %(PacketVersion)s
        Software: Mixminion %(mm_version)s
        Secure-Configuration: %(Secure)s
        Contact: %(Contact)s
        """ % fields
    if insecurities:
        info += "Why-Insecure: %s\n"%(", ".join(insecurities))
    if fingerprint:
        info += "Contact-Fingerprint: %s\n"%fingerprint
    if comments:
        info += "Comments: %s\n"%comments

    # Only advertise incoming MMTP if we support it.
    if config["Incoming/MMTP"].get("Enabled", 0):
        info += """\
            [Incoming/MMTP]
            Version: 0.1
            IP: %(IP)s
            Hostname: %(Hostname)s
            Port: %(Port)s
            Key-Digest: %(KeyID)s
            Protocols: %(MMTPProtocolsIn)s
            """ % fields
        for k,v in config.getSectionItems("Incoming/MMTP"):
            if k not in ("Allow", "Deny"):
                continue
            info += "%s: %s" % (k, _rule(k=='Allow',v))

    # Only advertise outgoing MMTP if we support it.
    if config["Outgoing/MMTP"].get("Enabled", 0):
        info += """\
            [Outgoing/MMTP]
            Version: 0.1
            Protocols: %(MMTPProtocolsOut)s
            """ % fields
        for k,v in config.getSectionItems("Outgoing/MMTP"):
            if k not in ("Allow", "Deny"):
                continue
            info += "%s: %s" % (k, _rule(k=='Allow',v))

    if not config.moduleManager.isConfigured():
        config.moduleManager.configure(config)

    # Ask our modules for their configuration information.
    info += "".join(config.moduleManager.getServerInfoBlocks())

    info += """\
          [Testing]
          Platform: %s
          Configuration: %s
          """ %(getPlatformSummary(),
                config.getConfigurationSummary())

    # Remove extra (leading or trailing) whitespace from the lines.
    lines = [ line.strip() for line in info.split("\n") ]
    # Remove empty lines
    lines = filter(None, lines)
    # Force a newline at the end of the file, rejoin, and sign.
    lines.append("")
    info = "\n".join(lines)
    info = signServerInfo(info, identityKey)

    # Write the desciptor
    writeFile(serverKeys.getDescriptorFileName(), info, mode=0644)

    # This is for debugging: we try to parse and validate the descriptor
    #   we just made.
    # FFFF Remove this once we're more confident.
    inf = ServerInfo(string=info)
    ok = checkDescriptorConsistency(inf, config, log=0, isPublished=0)
    if ok not in ('good', 'so-so'):
        print "========"
        print info
        print "======"
        checkDescriptorConsistency(inf, config, log=1, isPublished=0)
    assert ok in ('good', 'so-so')

    return info

def _rule(allow, (ip, mask, portmin, portmax)):
    """Return an external representation of an IP allow/deny rule."""
    if mask == '0.0.0.0':
        ip="*"
        mask=""
    elif mask == "255.255.255.255":
        mask = ""
    else:
        mask = "/%s" % mask

    if portmin==portmax==48099 and allow:
        ports = ""
    elif portmin == 0 and portmax == 65535 and not allow:
        ports = ""
    elif portmin == portmax:
        ports = " %s" % portmin
    else:
        ports = " %s-%s" % (portmin, portmax)

    return "%s%s%s\n" % (ip,mask,ports)

#----------------------------------------------------------------------
# Helpers to guess a reasonable local IP when none is provided.

class IPGuessError(MixError):
    """Exception: raised when we can't guess a single best IP."""
    pass

# Cached guessed IP address
_GUESSED_IP = None

def _guessLocalIP():
    "Try to find a reasonable IP for this host."
    global _GUESSED_IP
    if _GUESSED_IP is not None:
        return _GUESSED_IP

    # First, let's see what our name resolving subsystem says our
    # name is.
    ip_set = {}
    try:
        ip_set[ socket.gethostbyname(socket.gethostname()) ] = 1
    except socket.error:
        try:
            ip_set[ socket.gethostbyname(socket.getfqdn()) ] = 1
        except socket.error:
            pass

    # And in case that doesn't work, let's see what other addresses we might
    # think we have by using 'getsockname'.
    for target_addr in ('18.0.0.1', '10.0.0.1', '192.168.0.1',
                        '172.16.0.1')+tuple(ip_set.keys()):
        # open a datagram socket so that we don't actually send any packets
        # by connecting.
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((target_addr, 9)) #discard port
            ip_set[ s.getsockname()[0] ] = 1
        except socket.error:
            pass

    for ip in ip_set.keys():
        if ip.startswith("127.") or ip.startswith("0."):
            del ip_set[ip]

    # FFFF reject 192.168, 10., 176.16.x

    if len(ip_set) == 0:
        raise IPGuessError("No address found")

    if len(ip_set) > 1:
        raise IPGuessError("Multiple addresses found: %s" % (
                    ", ".join(ip_set.keys())))

    IP = ip_set.keys()[0]
    if IP.startswith("192.168.") or IP.startswith("10.") or \
       IP.startswith("176.16."):
        raise IPGuessError("Only address found is in a private IP block")

    return IP

_KNOWN_LOCAL_HOSTNAMES = {}

def _checkHostnameIsLocal(name):
    if _KNOWN_LOCAL_HOSTNAMES.has_key(name):
        return
    r = mixminion.NetUtils.getIPs(name)
    for family, addr, _ in r:
        if family == mixminion.NetUtils.AF_INET:
            if addr.startswith("127.") or addr.startswith("0."):
                LOG.warn("Hostname %r resolves to reserved address %s",
                         name, addr)
        else:
            if addr in ("::", "::1"):
                LOG.warn("Hostname %r resolves to reserved address %s",
                         name,addr)
    _KNOWN_LOCAL_HOSTNAMES[name] = 1

def generateCertChain(filename, mmtpKey, identityKey, nickname,
                      certStarts, certEnds):
    """Create a two-certificate chain for use in MMTP.

       filename -- location to store certificate chain.
       mmtpKey -- a short-term RSA key to use for connection
           encryption (1024 bits).
       identityKey -- our long-term signing key (2048-4096 bits).
       nickname -- nickname to use in our certificates.
       certStarts, certEnds -- certificate lifetimes.
    """
    fname = filename+"_tmp"
    mixminion.Crypto.generate_cert(fname,
                                   mmtpKey, identityKey,
                                   "%s<MMTP>" %nickname,
                                   nickname,
                                   certStarts, certEnds)
    certText = readFile(fname)
    os.unlink(fname)
    mixminion.Crypto.generate_cert(fname,
                                   identityKey, identityKey,
                                   nickname, nickname,
                                   certStarts, certEnds)

    identityCertText = readFile(fname)
    os.unlink(fname)
    writeFile(filename, certText+identityCertText, 0600)

def getPlatformSummary():
    """Return a string describing the current software and platform."""
    if hasattr(os, "uname"):
        uname = " ".join(os.uname())
    else:
        uname = sys.platform

    return "Mixminion %s; Python %r on %r" % (
        mixminion.__version__, sys.version, uname)

