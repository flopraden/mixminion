# Copyright 2002-2011 Nick Mathewson.  See LICENSE for licensing information.

"""Configuration format for server configuration files.

   See Config.py for information about the generic configuration facility."""

__all__ = [ "ServerConfig" ]

import operator
import os

import mixminion.Config
import mixminion.server.Modules
from mixminion.Config import ConfigError
from mixminion.Common import LOG

class ServerConfig(mixminion.Config._ConfigFile):
    ##
    # Fields:
    #   moduleManager
    #
    _restrictFormat = 0

    def __init__(self, fname=None, string=None, moduleManager=None):
        # We use a copy of SERVER_SYNTAX, because the ModuleManager will
        # mess it up.
        self._syntax = SERVER_SYNTAX.copy()
        self.CODING_FNS = CODING_FNS

        if moduleManager is None:
            self.moduleManager = mixminion.server.Modules.ModuleManager()
        else:
            self.moduleManager = moduleManager
        self._addCallback("Server", self.__loadModules)

        mixminion.Config._ConfigFile.__init__(self, fname, string)

    def validate(self, lines, contents):
        def _haveEntry(self, section, ent):
            entries = self._sectionEntries
            return len([e for e in entries[section] if e[0] == ent]) != 0

        # Preemptively configure the log before validation, so we don't
        # write to the terminal if we've been asked not to.
        if not self['Server'].get("EchoMessages", 0):
            LOG.handlers = []
            # ???? This can't be the best way to do this.

        # Now, validate the host section.
        mixminion.Config._validateHostSection(self['Host'])
        # Server section
        server = self['Server']
        bits = server['IdentityKeyBits']
        if not (2048 <= bits <= 4096):
            raise ConfigError("IdentityKeyBits must be between 2048 and 4096")
        if server['EncryptIdentityKey']:
            LOG.warn("Identity key encryption not yet implemented")
        if server['EncryptPrivateKey']:
            LOG.warn("Encrypted private keys not yet implemented")
        if server['PublicKeyLifetime'].getSeconds() < 24*60*60:
            raise ConfigError("PublicKeyLifetime must be at least 1 day.")
        if server['PublicKeyOverlap'].getSeconds() < 6*60*60:
            raise ConfigError("PublicKeyOverlap must be >= 6 hours")
        if server['PublicKeyOverlap'].getSeconds() > 72*60*60:
            raise ConfigError("PublicKeyOverlap must be <= 72 hours")

        if _haveEntry(self, 'Server', 'Mode'):
            LOG.warn("Mode specification is not yet supported.")

        mixInterval = server['MixInterval'].getSeconds()
        if mixInterval < 30*60:
            LOG.warn("Dangerously low MixInterval")
        if server['MixAlgorithm'] == 'TimedMixPool':
            if _haveEntry(self, 'Server', 'MixPoolRate'):
                LOG.warn("Option MixPoolRate is not used for Timed mixing.")
            if _haveEntry(self, 'Server', 'MixPoolMinSize'):
                LOG.warn("Option MixPoolMinSize is not used for Timed mixing.")
        else:
            rate = server['MixPoolRate']
            minSize = server['MixPoolMinSize']
            if rate < 0.05:
                LOG.warn("Unusually low MixPoolRate %s", rate)
            if minSize < 0:
                raise ConfigError("MixPoolMinSize %s must be nonnegative.")

        if not self['Incoming/MMTP'].get('Enabled'):
            LOG.warn("Disabling incoming MMTP is not yet supported.")
        if [e for e in self._sectionEntries['Incoming/MMTP']
            if e[0] in ('Allow', 'Deny')]:
            LOG.warn("Allow/deny are not yet supported")

        if not self['Outgoing/MMTP'].get('Enabled'):
            LOG.warn("Disabling outgoing MMTP is not yet supported.")
        if [e for e in self._sectionEntries['Outgoing/MMTP']
            if e[0] in ('Allow', 'Deny')]:
            LOG.warn("Allow/deny are not yet supported")
        mc = self['Outgoing/MMTP'].get('MaxConnections')
        if mc is not None and mc < 1:
            raise ConfigError("MaxConnections must be at least 1.")
        bw = self['Outgoing/MMTP'].get('MaxBandwidth')
        if bw is not None and bw < 4096:
            #XXXX007 this is completely arbitrary. :P
            raise ConfigError("MaxBandwidth must be at least 4KB.")

        self.validateRetrySchedule("Outgoing/MMTP")

        self.moduleManager.validate(self, lines, contents)

    def __loadModules(self, section, sectionEntries):
        """Callback from the [Server] section of a config file.  Parses
           the module options, and adds new sections to the syntax
           accordingly."""
        self.moduleManager.setPath(section.get('ModulePath'))
        for mod in section.get('Module', []):
            LOG.info("Loading module %s", mod)
            self.moduleManager.loadExtModule(mod)

        self._syntax.update(self.moduleManager.getConfigSyntax())

    def getModuleManager(self):
        "Return the module manager initialized by this server."
        return self.moduleManager

    def getInsecurities(self):
        """Return false iff this configuration is reasonably secure.
           Otherwise, return a list of reasons why it isn't."""
        reasons = ["Software is alpha"]

        # SERVER
        server = self['Server']
        if server['LogLevel'] in ('TRACE', 'DEBUG'):
            reasons.append("Log is too verbose")
        if server['LogStats'] and server['StatsInterval'].getSeconds() \
               < 2*60*60:
            reasons.append("StatsInterval is too short")
        #if not server["EncryptIdentityKey"]:
        #    reasons.append("Identity key is not encrypted")
        # ???? Pkey lifetime, sloppiness?
        if server["MixAlgorithm"] not in _SECURE_MIX_RULES:
            reasons.append("Mix algorithm is not secure")
        else:
            if server["MixPoolMinSize"] < 5:
                reasons.append("MixPoolMinSize is too small")
            #???? MixPoolRate
        if server["MixInterval"].getSeconds() < 30*60:
            reasons.append("Mix interval under 30 minutes")

        # ???? Incoming/MMTP

        # ???? Outgoing/MMTP

        # ???? Modules

        return reasons

    def getConfigurationSummary(self):
        """Return a human-readable description of this server's configuration,
           for inclusion in the testing section of the server descriptor."""
        res = []
        for section,entries in [
            ("Server", ['LogLevel', 'LogStats', 'StatsInterval',
                        'PublicKeyOverlap', 'Mode', 'MixAlgorithm',
                        'MixInterval', 'MixPoolRate', 'MixPoolMinSize',
                        'Timeout','MaxBandwidth']),
            ("Outgoing/MMTP", ['Retry','MaxConnections']),
            ("Delivery/SMTP",
             ['Enabled', 'Retry', 'SMTPServer', 'ReturnAddress', 'FromTag',
              'SubjectLine', 'MaximumSize']),
            ("Delivery/SMTP-Via-Mixmaster",
             ['Enabled', 'Retry', 'Server', 'FromTag',
              'SubjectLine', 'MaximumSize']),
            ("Delivery/Fragmented",
             ['Enabled', 'MaximumSize','MaximumInterval']),
            ("Pinging", ['Enabled', 'ServerPingPeriod', 'DullChainPingPeriod',
                        'ChainPingPeriod', 'ServerProbePeriod']),
            ]:
            for k in entries:
                ent = self[section].get(k,None)
                if ent is None:
                    continue
                v = self.getFeature(section, k)
                res.append("%s/%s=%r"%(section,k,v))
        return "; ".join(res)

    def validateRetrySchedule(self, sectionName, entryName='Retry'):
        """Check whether the retry schedule in self[sectionName][entryName]
           is reasonable.  Warn or raise ConfigError if it isn't.  Ignore
           the entry if it isn't there.
        """
        entry = self[sectionName].get(entryName)
        if not entry:
            return
        mixInterval = self['Server']['MixInterval'].getSeconds()
        _validateRetrySchedule(mixInterval, entry, sectionName)

    def _get_fname(self, sec, ent, defaultRel):
        """Helper function.  Returns the filename in self[sec][ent],
           but defaults to ${BASEDIR}/defaultRel.  All relative paths
           are also interpreted relative to ${BASEDIR}.
        """
        raw = self[sec].get(ent)
        homedir = self.getBaseDir()
        if not raw:
            return os.path.join(homedir, defaultRel)
        if os.path.isabs(raw):
            return raw
        else:
            return os.path.join(homedir, raw)

    def getBaseDir(self):
        """Return the base directory for this configuration."""
        v = self["Server"]["BaseDir"]
        if v is None:
            v = self["Server"]["Homedir"]
        if v is None:
            LOG.warn("Defaulting base directory to /var/spool/minion; this will change.")
            v = "/var/spool/minion"
        return v

    def getLogFile(self):
        """Return the configured logfile location."""
        return self._get_fname("Server", "LogFile", "log")
    def getStatsFile(self):
        """Return the configured stats file location."""
        return self._get_fname("Server", "StatsFile", "stats")
    def getKeyDir(self):
        """Return the configured key directory"""
        return self._get_fname("Server", "KeyDir", "keys")
    def getWorkDir(self):
        """Return the configured work directory"""
        return self._get_fname("Server", "WorkDir", "work")
    def getPidFile(self):
        """Return the configured PID file location"""
        return self._get_fname("Server", "PidFile", "pid")
    def getQueueDir(self):
        """Return the configured queue directory."""
        if self["Server"]["QueueDir"] is None:
            return os.path.join(self.getWorkDir(), 'queues')
        else:
            return self._get_fname("Server", "QueueDir", "work/queues")
    def isServerConfig(self):
        """DOCDOC"""
        return 1
    def getDirectoryRoot(self):
        """DOCDOC"""
        return os.path.join(self.getWorkDir(),"dir")

def _validateRetrySchedule(mixInterval, schedule, sectionName):
    """Backend for ServerConfig.validateRetrySchedule -- separated for testing.

       mixInterval -- our batching interval.
       schedule -- a retry schedule as returned by _parseIntervalList.
       sectionName -- the name of the retrying subsystem: used for messages.
    """
    total = reduce(operator.add, schedule, 0)

    # Warn if we try for less than a day.
    if total < 24*60*60:
        LOG.warn("Dangerously low retry timeout for %s (<1 day)", sectionName)

    # Warn if we try for more than two weeks.
    if total > 2*7*24*60*60:
        LOG.warn("Very high retry timeout for %s (>14 days)", sectionName)

    # Warn if any of our intervals are less than the mix interval...
    if min(schedule) < mixInterval-2:
        LOG.warn("Rounding retry intervals for %s to the nearest mix",
                 sectionName)

    # ... or less than 5 minutes.
    elif min(schedule) < 5*60:
        LOG.warn("Very fast retry intervals for %s (< 5 minutes)", sectionName)

    # Warn if we make fewer than 5 attempts.
    if len(schedule) < 5:
        LOG.warn("Dangerously low number of retries for %s (<5)", sectionName)

    # Warn if we make more than 50 attempts.
    if len(schedule) > 50:
        LOG.warn("Very high number of retries for %s (>50)", sectionName)

#======================================================================

_MIX_RULE_NAMES = {
    'timed' : "TimedMixPool",
    'cottrell'     : "CottrellMixPool",
    'mixmaster'    : "CottrellMixPool",
    'dynamicpool'  : "CottrellMixPool",
    'binomial'            : "BinomialCottrellMixPool",
    'binomialcottrell'    : "BinomialCottrellMixPool",
    'binomialdynamicpool' : "BinomialCottrellMixPool",
}

_SECURE_MIX_RULES = [ "CottrellMixPool", "BinomialCottrellMixPool" ]

def _parseMixRule(s):
    """Validation function.  Given a string representation of a mixing
       algorithm, return the name of the Mix queue class to be used."""
    name = s.strip().lower()
    v = _MIX_RULE_NAMES.get(name)
    if not v:
        raise ConfigError("Unrecognized mix algorithm %s"%s)
    return v

def _parseFraction(frac):
    """Validation function.  Converts a percentage or a number into a
       number between 0 and 1."""
    s = frac.strip().lower()
    try:
        if s.endswith("%"):
            ratio = float(s[:-1].strip())/100.0
        else:
            ratio = float(s)
    except ValueError:
        raise ConfigError("%s is not a fraction" %frac)
    if not 0 <= ratio <= 1:
        raise ConfigError("%s is not in range (between 0%% and 100%%)"%frac)
    return ratio

# alias to make the syntax more terse.

SERVER_SYNTAX =  {
        'Host' : mixminion.Config.ClientConfig._syntax['Host'],
        'Server' : { '__SECTION__' : ('REQUIRE', None, None),
                     'BaseDir' : ("ALLOW", "filename", None),
                     'Homedir' : ('ALLOW', "filename", None),
                     'LogFile' : ('ALLOW', "filename", None),
                     'StatsFile' : ('ALLOW', "filename", None),
                     'KeyDir' : ('ALLOW', "filename", None),
                     'WorkDir' : ('ALLOW', "filename", None),
                     'QueueDir' : ('ALLOW', "filename", None),
                     'PidFile' : ('ALLOW', "filename", None),

                     'LogLevel' : ('ALLOW', "severity", "WARN"),
                     'EchoMessages' : ('ALLOW', "boolean", "no"),
                     'Daemon' : ('ALLOW', "boolean", "no"),
                     'LogStats' : ('ALLOW', "boolean", 'yes'),
                     'StatsInterval' : ('ALLOW', "interval",
                                        "1 day"),
                     'EncryptIdentityKey' :('ALLOW', "boolean", "no"),
                     'IdentityKeyBits': ('ALLOW', "int", "2048"),
                     'PublicKeyLifetime' : ('ALLOW', "interval",
                                            "30 days"),
                     'PublicKeyOverlap': ('ALLOW', "interval",
                                          "24 hours"),
                     'EncryptPrivateKey' : ('ALLOW', "boolean", "no"),
                     'Mode' : ('REQUIRE', "serverMode", "local"),
                     'Nickname': ('REQUIRE', "nickname", None),
                     'Contact-Email': ('REQUIRE', "email", None),
                     'Contact-Fingerprint': ('ALLOW', None, None),
                     'Comments': ('ALLOW', None, None),
                     'ModulePath': ('ALLOW', None, None),
                     'Module': ('ALLOW*', None, None),
                     'MixAlgorithm' : ('ALLOW', "mixRule", "Timed"),
                     'MixInterval' : ('ALLOW', "interval", "30 min"),
                     'MixPoolRate' : ('ALLOW', "fraction", "60%"),
                     'MixPoolMinSize' : ('ALLOW', "int", "5"),
		     'Timeout' : ('ALLOW', "interval", "5 min"),
                     'MaxBandwidth' : ('ALLOW', "size", None),
                     'MaxBandwidthSpike' : ('ALLOW', "size", None),
                     },
        #DOCDOC
        'Pinging' : { 'Enabled' : ('ALLOW', 'boolean', 'yes'),
                      'RecomputeInterval' : ('ALLOW', 'interval', '30 min'),
                      'ServerPingPeriod' : ('ALLOW', 'interval', '2 hours'),
                      'DullChainPingPeriod' : ('ALLOW', 'interval', '4 days'),
                      'ChainPingPeriod' : ('ALLOW', 'interval', '1 day'),
                      'ServerProbePeriod' : ('ALLOW', 'interval', '1 hour'),
                      # XXXX008 enforce > 15 days.
                      'RetainData' : ('ALLOW', 'interval', '30 days'),
                      # XXXX008 enforce > 15 days
                      'RetainResults' : ('ALLOW', 'interval', '1 year'),
                      },
        'DirectoryServers' : { # '__SECTION__' : ('REQUIRE', None, None),
                               'ServerURL' : ('ALLOW*', None, None),
                               'PublishURL' : ('ALLOW*', None, None),
                               'Publish' : ('ALLOW', "boolean", "no"),
                               'MaxSkew' : ('ALLOW', "interval",
                                            "10 minutes",) },
        # FFFF Generic multi-port listen/publish options.
        'Incoming/MMTP' : { 'Enabled' : ('REQUIRE', "boolean", "no"),
                            #XXXX009 Stop looking at IP; not checked since 008.
                            'IP' : ('ALLOW', "IP", "0.0.0.0"),
                          'Hostname' : ('ALLOW', "host", None),
                          'Port' : ('ALLOW', "int", "48099"),
                          'ListenIP' : ('ALLOW', "IP", None),
                          'ListenPort' : ('ALLOW', "int", None),
                          'ListenIP6' : ('ALLOW', "IP6", None),
  		          'Allow' : ('ALLOW*', "addressSet_allow", None),
                          'Deny' : ('ALLOW*', "addressSet_deny", None)
			 },
        'Outgoing/MMTP' : { 'Enabled' : ('REQUIRE', "boolean", "no"),
                            'Retry' : ('ALLOW', "intervalList",
                              "every 1 hour for 1 day, 7 hours for 5 days"),
                           'MaxConnections' : ('ALLOW', 'int', '16'),
                           'Allow' : ('ALLOW*', "addressSet_allow", None),
                           'Deny' : ('ALLOW*', "addressSet_deny", None) },
        # FFFF Missing: Queue-Size / Queue config options
        # FFFF         listen timeout??
        }

CODING_FNS = mixminion.Config._ConfigFile.CODING_FNS.copy()
CODING_FNS.update({'mixRule':(_parseMixRule,str),
                   'fraction':(_parseFraction,
                               lambda r: "%.2f%%"%(100.*r))})
