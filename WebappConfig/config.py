#!/usr/bin/python -O
#
# /usr/sbin/webapp-config
#       Python script for managing the deployment of web-based
#       applications
#
#       Originally written for the Gentoo Linux distribution
#
# Copyright (c) 1999-2007 Authors
#       Released under v2 of the GNU GPL
#
# Author(s)     Stuart Herbert
#               Renat Lumpau   <rl03@gentoo.org>
#               Gunnar Wrobel  <wrobel@gentoo.org>
#
# ========================================================================

# ========================================================================
# Dependencies
# ------------------------------------------------------------------------

import sys, os, os.path, re, socket, time

if sys.hexversion >= 0x3000000:
    # Python 3
    import configparser
    from configparser import ConfigParser as configparser_ConfigParser
    from configparser import ExtendedInterpolation
else:
    # Python 2
    import ConfigParser as configparser
    from ConfigParser import SafeConfigParser as configparser_ConfigParser

import WebappConfig.server
import WebappConfig.permissions as Perm
import WebappConfig.wrapper as wrapper

from argparse             import ArgumentParser
from WebappConfig.debug   import OUT
from WebappConfig.eprefix import EPREFIX
from WebappConfig.version import WCVERSION

from WebappConfig.permissions import PermissionMap


# ========================================================================
# BashParser class
# ------------------------------------------------------------------------

class BashConfigParser(configparser_ConfigParser):

    _interpvar_match = re.compile(r"(%\(([^)]+)\)s|\$\{([^}]+)\})").match

    def __init__(self, defaults=None):
        self.error_action = 1
        if sys.hexversion >= 0x3000000:
            configparser_ConfigParser.__init__(self, defaults, interpolation=ExtendedInterpolation())
        else:
            configparser_ConfigParser.__init__(self, defaults)

    def on_error(self, action = 0):
        self.error_action = action

    def get(self, section, option, *args, **kwargs):
        try:
            return configparser_ConfigParser.get(self, section, option, *args, **kwargs)
        except Exception as e:
            error = '\nThere is a problem with your configuration file or' \
                ' an environment variable.\n' \
                'webapp-config tried to read the variable "' + str(option) \
                + '"\nand received the following error:\n\n' + str(e) +    \
                '\nPlease note that webapp-config is not written in bash ' \
                'anymore\nand that you cannot use the bash scripting feat' \
                'ures.'
            if self.error_action == 0:
                OUT.die(error)
            elif self.error_action == 1:
                OUT.warn(error)
            return ''

    def _interpolate_some(self, option, accum, rest, section, map, depth):
        if depth > configparser.MAX_INTERPOLATION_DEPTH:
            raise configparser.InterpolationDepthError(option, section, rest)
        while rest:
            p = rest.find("%")
            if p < 0:
                p = rest.find("$")
            if p < 0:
                accum.append(rest)
                return
            if p > 0:
                accum.append(rest[:p])
                rest = rest[p:]
            # p is no longer used
            c = rest[1:2]

            OUT.debug('Parsing', 7)

            if c == "%" or c == "$":
                accum.append(c)
                rest = rest[2:]
            elif c == "(" or c == "{":
                m = self._interpvar_match(rest)
                if m is None:
                    raise configparser.InterpolationSyntaxError(option, section,
                        "bad interpolation variable reference %r" % rest)
                var = m.group(2)
                if not var:
                    var = m.group(3)
                var = self.optionxform(var)
                rest = rest[m.end():]
                try:
                    v = map[var]
                except KeyError:
                    raise configparser.InterpolationMissingOptionError(
                        option, section, rest, var)
                if "%" in v or "$" in v:
                    self._interpolate_some(option, accum, v,
                                           section, map, depth + 1)
                else:
                    accum.append(v)
            else:
                raise configparser.InterpolationSyntaxError(
                    option, section,
                    "'" + c + "' must be followed by '" + c + "', '{', or '(', found: %r" % (rest,))

    OPTCRE = re.compile(
        r'(?P<option>[^:=\s][^:=]*)'          # very permissive!
        r'\s*(?P<vi>[:=])\s*'                 # any number of space/tab,
                                              # followed by separator
                                              # (either : or =), followed
                                              # by any # space/tab
        r'(?P<value>.*)$'                     # everything up to eol
        )

    def _read(self, fp, fpname):
        """Parse a sectioned setup file.

        The sections in setup file contains a title line at the top,
        indicated by a name in square brackets (`[]'), plus key/value
        options lines, indicated by `name: value' format lines.
        Continuations are represented by an embedded newline then
        leading whitespace.  Blank lines, lines beginning with a '#',
        and just about everything else are ignored.
        """
        cursect = None                            # None, or a dictionary
        optname = None
        lineno = 0
        e = None                                  # None, or an exception

        # Read everything into the "USER" section
        if 'USER' in self._sections:
            cursect = self._sections['USER']
        else:
            cursect = {'__name__': 'USER'}
            self._sections['USER'] = cursect

        while True:
            line = fp.readline()
            if not line:
                break
            lineno = lineno + 1
            # comment or blank line?
            if line.strip() == '' or line[0] in '#;':
                continue
            if line.split(None, 1)[0].lower() == 'rem' and line[0] in "rR":
                # no leading whitespace
                continue
            # continuation line?
            if line[0].isspace() and cursect is not None and optname:
                value = line.strip()
                if value:
                    cursect[optname] = "%s\n%s" % (cursect[optname], value)
            # a section header or option header?
            else:
                # an option line?
                mo = self.OPTCRE.match(line)
                if mo:
                    optname, vi, optval = mo.group('option', 'vi', 'value')
                    if vi in ('=', ':') and ';' in optval:
                        # ';' is a comment delimiter only if it follows
                        # a spacing character
                        pos = optval.find(';')
                        if pos != -1 and optval[pos-1].isspace():
                            optval = optval[:pos]
                    optval = optval.strip()
                    # allow empty values
                    if optval == '""':
                        optval = ''
                    if optval[0] == '"' and optval[-1] == '"' :
                        optval = optval[1:-1]
                    optname = self.optionxform(optname.rstrip())
                    cursect[optname] = optval
                else:
                    # a non-fatal parsing error occurred.  set up the
                    # exception but keep going. the exception will be
                    # raised at the end of the file and will contain a
                    # list of all bogus lines
                    if not e:
                        e = configparser.ParsingError(fpname)
                    e.append(lineno, repr(line))
        # if any parsing errors occurred, raise an exception
        if e:
            raise e

# ========================================================================
# Config class
# ------------------------------------------------------------------------

class Config:

    # --------------------------------------------------------------------
    # Init functions

    def __init__(self):

        ## These are the webapp-config default configuration values.

        hostname = 'localhost'
        try:
            hostname = socket.gethostbyaddr(socket.gethostname())[0]
        except:
            pass

        self.__d = {
            'config_protect'               : '',
            # Necessary to load the config file
            'my_etcconfig'                 : EPREFIX + '/etc/vhosts/webapp-config',
            'my_dotconfig'                 : '.webapp',
            'my_version'                   : WCVERSION,
            'my_conf_version'              : '7',
            'my_bugsurl'                   : wrapper.bugs_link,
            'g_myname'                     : sys.argv[0],
            'g_orig_installdir'            : EPREFIX + '/',
            'g_installdir'                 : EPREFIX + '/',
            'g_link_options'               : '',
            'g_link_type'                  : 'hard',
            'g_configprefix'               : '._cfg',
            'g_perms_dotconfig'            : '0600',
            # USER section (only 'get' these variables from
            # the USER section)
            'vhost_hostname'               : hostname,
            'vhost_server'                 : 'apache',
            'vhost_default_uid'            : '0',
            'vhost_default_gid'            : '0',
            'vhost_config_gid'             : str(os.getgid()),
            'vhost_config_uid'             : str(os.getuid()),
            'vhost_config_virtual_files'   : 'virtual',
            'vhost_config_default_dirs'    : 'default-owned',
            'vhost_config_dir'             : '${vhost_root}/conf',
            'vhost_htdocs_insecure'        : 'htdocs',
            'vhost_htdocs_secure'          : 'htdocs-secure',
            'vhost_perms_serverowned_dir'  : '0775',
            'vhost_perms_serverowned_file' : '0664',
            'vhost_perms_configowned_dir'  : '0755',
            'vhost_perms_configowned_file' : '0644',
            'vhost_perms_defaultowned_dir' : '0755',
            'vhost_perms_virtualowned_file': 'o-w',
            'vhost_perms_installdir'       : '0755',
            # FIXME: I added the following two as default values so that the
            # commands --show-postinst and --show-postupgrade do not
            # need a fully initialized server instance. That would make
            # things more complex. On the other hand these parameters
            # will not have the correct server values when running
            # --show-post*. Think about it again.
            #
            # -- wrobel
            'vhost_server_uid'  : 'root',
            'vhost_server_gid'  : 'root',
            'my_persistroot'    : EPREFIX + '/var/db/webapps',
            'wa_installsbase'   : 'installs',
            'vhost_root'        : EPREFIX + '/var/www/${vhost_hostname}',
            'g_htdocsdir'       : '${vhost_root}/${my_htdocsbase}',
            'my_appdir'         : '${my_approot}/${my_appsuffix}',
            'my_htdocsdir'      : '${my_appdir}/htdocs',
            'my_persistdir'     : '${my_persistroot}/${my_appsuffix}',
            'my_hostrootdir'    : '${my_appdir}/${my_hostrootbase}',
            'my_cgibindir'      : '${my_hostrootdir}/${my_cgibinbase}',
            'my_iconsdir'       : '${my_hostrootdir}/${my_iconsbase}',
            'my_errorsdir'      : '${my_hostrootdir}/${my_errorsbase}',
            'g_cgibindir'      : '${vhost_root}/${my_cgibinbase}',
            'my_approot'        : EPREFIX + '/usr/share/webapps',
            'package_manager'   : 'portage',
            'allow_absolute'    : 'no',
            'my_hostrootbase'   : 'hostroot',
            'my_cgibinbase'     : 'cgi-bin',
            'my_iconsbase'      : 'icons',
            'my_errorsbase'     : 'error',
            'my_sqlscriptsdir'  : '${my_appdir}/sqlscripts',
            'my_hookscriptsdir' : '${my_appdir}/hooks',
            'my_serverconfigdir': '${my_appdir}/conf',
            'wa_configlist'     : '${my_appdir}/config-files',
            'wa_solist'         : '${my_appdir}/server-owned-files',
            'wa_virtuallist'    : '${my_appdir}/virtuals',
            'wa_installs'       : '${my_persistdir}/${wa_installsbase}',
            'wa_postinstallinfo':
            '${my_appdir}/post-install-instructions.txt',
            }

        # Setup basic defaults
        self.config = BashConfigParser(self.__d)
        self.config.add_section('USER')

        # Setup the command line parser
        self.setup_parser()

        self.work = ''

        self.flag_dir = False

    def set_configprotect(self):
        self.config.set('USER', 'config_protect',
           wrapper.config_protect(self.maybe_get('cat'),
                                  self.config.get('USER', 'pn'),
                                  self.config.get('USER', 'pvr'),
                                  self.config.get('USER', 'package_manager') ))

    def set_vars(self):

        if not self.config.has_option('USER', 'my_appsuffix'):
            self.determine_appsuffix()

        self.set_configprotect()

    def determine_appsuffix(self):

        cat = self.maybe_get('cat')
        pn  = self.config.get('USER', 'pn')
        pvr = self.config.get('USER', 'pvr')
        my_approot = self.config.get('USER', 'my_approot')

        old_layout = os.path.join(my_approot, pn, pvr)

        if cat:
            new_layout = os.path.join(my_approot, cat, pn, pvr)

            OUT.debug('Checking for new layout', 7)

            # include cat in persist_suffix. Note that webapp.eclass will always
            # supply category info, so the db will always end up in the right place.
            self.config.set('USER', 'persist_suffix', '/'.join([cat,pn,pvr]))

            if os.path.isdir(new_layout):
                self.config.set('USER', 'my_appsuffix', '${cat}/${pn}/${pvr}')
            elif os.path.isdir(old_layout):
                self.config.set('USER', 'my_appsuffix', '${pn}/${pvr}')
                self.config.set('USER', 'cat', '')
            else:
                OUT.die('Unable to determine location of master copy')
        else:

            OUT.debug('Checking for old layout', 7)

            if os.path.isdir(old_layout):
                self.config.set('USER', 'my_appsuffix', '${pn}/${pvr}')
                self.config.set('USER', 'cat', '')
                # no category info at all is available, so drop it from persist_suffix
                self.config.set('USER', 'persist_suffix', '/'.join([pn,pvr]))
            else:
                hits = []
                for e in os.listdir(my_approot):
                    if os.path.isdir(os.path.join(my_approot, e, pn, pvr)):
                            hits.append('/'.join([e,pn,pvr]))

                if len(hits) == 0:
                    OUT.die('Unable to determine location of master copy')
                elif len(hits) > 1:
                    msg = 'Multiple packages found:\n'
                    for a in hits:
                        msg += my_approot + '/' + a + '\n'
                    msg += 'Please specify a category'
                    OUT.die(msg)
                else:
                    self.config.set('USER', 'my_appsuffix', hits[0])
                    cat = hits[0].split('/')[0]
                    self.config.set('USER', 'cat', cat)
                    self.config.set('USER', 'persist_suffix', '/'.join([cat,pn,pvr]))

    def setup_parser(self):

        self.parser  = ArgumentParser(
            usage    = '%(prog)s [-ICU] [-dghus] <APPLICATION VERSION>',
            add_help = False)

        self.parser.add_argument('-v',
                                 '--version',
                                 action = 'version',
                                 version = self.config.get('USER',
                                 'my_version'))
        #-----------------------------------------------------------------
        # Usage

        app_ver = self.parser.add_argument_group('APPLICATION VERSION',
                                                 'The name and version number '
                                                 'of the web application to '
                                                 'install. e.g. phpmyadmin 2.'
                                                 '5.4. The APPLICATION must '
                                                 'have already been installed '
                                                 'into the %(approot)s direct'
                                                 'ory tree using emerge'
                                                 % {'approot':
                                                    self.config.get('USER',
                                                    'my_approot')})

        #-----------------------------------------------------------------
        # Main Options

        main_opts = self.parser.add_argument_group('<Main Options>')

        main_opts.add_argument('-I',
                               '--install',
                               nargs = 2,
                               help   = 'Install a web application')

        main_opts.add_argument('-C',
                               '--clean',
                               nargs = 2,
                               help   = 'Remove a web application')

        main_opts.add_argument('-U',
                               '--upgrade',
                               nargs = 2,
                               help   = 'Upgrade a web application')

        #-----------------------------------------------------------------
        # Path Options

        inst_locs = self.parser.add_argument_group('<Installation Location>')

        inst_locs.add_argument('-d',
                               '--dir',
                               nargs = 1,
                               help = 'Install <application> into DIR under the'
                               ' htdocs dir. Not specifying using this flag'
                               ' will result in defaulting to the package '
                               ' name.')

        inst_locs.add_argument('-h',
                               '--host',
                               nargs = 1,
                               help = 'The hostname to configure this applicati'
                               'on to serve.  Also affects where some files go.'
                               ' If you get this setting wrong, you may need to'
                               ' re-install the application to correct the pro'
                               'blem! Default is HOST = '
                               + self.config.get('USER', 'vhost_hostname') +
                               '. To change the default, change the value of "v'
                               'host_hostname" in '
                               + self.config.get('USER', 'my_etcconfig') +
                               ' <NOTE>: if the default value is currently "loc'
                               'alhost", that probably means that this computer'
                               '\'s /etc/hosts file is not correctly configured'
                              )

        inst_locs.add_argument('-S',
                               '--secure', action='store_true',
                               help = 'Install, upgrade, or clean files in htdo'
                               'cs-secure rather than in the htdocs directory.')

        inst_locs.add_argument('-s',
                               '--server',
                               nargs = 1,
                               help = 'Specify which web SERVER to install the '
                               'application to run under. Use webapp-config --l'
                               'ist-servers to see supported web servers. The d'
                               'efault is -s '
                               + self.config.get('USER', 'vhost_server') +
                               '. To change the default, change the value of VH'
                               'OST_SERVER in '
                               + self.config.get('USER', 'my_etcconfig'))


        #-----------------------------------------------------------------
        # Installation Options

        inst_opts = self.parser.add_argument_group('<Installation Options>')

        inst_opts.add_argument('-c',
                               '--copy',
                               action='store_true',
                               help = 'Directly copy the webapp files from'
                               ' the /usr/share/webapps/ directory when installing'
                               ' the webapp.')

        inst_opts.add_argument('-sf',
                               '--soft',
                               action='store_true',
                               help = 'Use symbolic links instead of hard links'
                               ' when creating virtual files. <NOTE>: some pack'
                               'ages will not work if you use this option')

        inst_opts.add_argument('-g',
                               '--group',
                               nargs = 1,
                               help = 'Install config files so that they can be'
                               ' edited by GROUP. GROUP can be a group name. Nu'
                               'merical group ids are NOT supported.Default is '
                               'GROUP = '
                               + self.config.get('USER', 'vhost_config_gid') +
                               'To change the default, change the value of VHOS'
                               'T_CONFIG_GID in '
                               + self.config.get('USER', 'my_etcconfig'))

        inst_opts.add_argument('-u',
                               '--user',
                               nargs = 1,
                               help = 'Install config files so that they can be'
                               ' edited by USER. USER can be a username.Numeric'
                               'al user ids are NOT supported. Default is USER '
                               '= '
                               + self.config.get('USER', 'vhost_config_uid') +
                               ' To change the default, change the value of VHO'
                               'ST_CONFIG_UID in '
                               + self.config.get('USER', 'my_etcconfig'))

        inst_opts.add_argument('-vf',
                               '--virtual-files',
                               choices = ['server-owned',
                                          'config-owned',
                                          'virtual'],
                               help = 'Decide what happens when we\'re installi'
                               'ng a file that could be shared (ie, one we woul'
                               'dn\'t normally create a local copy of). VIRTUAL'
                               '_FILES must be one of: "server-owned" [files ar'
                               'e owned by the user and group thatthe web-serve'
                               'r runs under], "config-owned" [files are owned '
                               'by the user and group specified by the -u and -'
                               'g switches to this script],"virtual" [files are'
                               ' shared; a local copy is not created]. Default '
                               'is '
                               + self.config.get('USER',
                                                 'vhost_config_virtual_files') +
                               '. To change these defaults, change the value of'
                               ' VHOST_CONFIG_VIRTUAL_FILES in '
                               + self.config.get('USER', 'my_etcconfig') +
                               ' <NOTE>: Some -s <server> options may not suppo'
                               'rt all values of VIRTUAL_FILES and will report '
                               'an error')

        inst_opts.add_argument('-dd',
                               '--default-dirs',
                               choices = ['server-owned',
                                          'config-owned',
                                          'default-owned'],
                               help = 'Decide what happens when we\'re installi'
                              'ng a directory that could be shared (ie, one we'
                              ' wouldn\'t normally create a local copy of). DE'
                              'FAULT_DIRS must be one of: "server-owned" [dirs'
                              ' are owned by the user and group thatthe web-se'
                              'rver runs under], "config-owned" [dirs are owne'
                              'd by the user and group specified by the -u and'
                              ' -g switches to this script],"default-owned" [d'
                              'irs are owned by the user specified in VHOST_DE'
                              'FAULT_UID:VHOST_DEFAULT_GID]. Default is '
                              + self.config.get('USER',
                                                'vhost_config_default_dirs') +
                              '. To change these defaults, change the value of'
                              ' VHOST_CONFIG_DEFAULT_DIRS in '
                              + self.config.get('USER', 'my_etcconfig') +
                              ' <NOTE>: Some -s <server> options may not suppo'
                              'rt all values of DEFAULT_DIRS and will report a'
                              'n error')


        #-----------------------------------------------------------------
        # Information Options

        info_opts = self.parser.add_argument_group('<Information Options>')

        info_opts.add_argument('-P',
                               '--pretend',
                               action='store_true',
                               help = 'Output information about what webapp-con'
                               'fig would do, then quit without actually doing '
                               'it')

        info_opts.add_argument('-V',
                               '--verbose',
                               action='store_true',
                               help = 'Output even more information than normal'
                               )

        info_opts.add_argument('-li',
                               '--list-installs',
                               nargs = '*',
                               help = 'List all current virtual installs for <a'
                               'pplication>. Use * for the package name and/or '
                               'version number to list more than one package / '
                               'version of a package.  Remember to include the '
                               '* in single quotes to stop your shell from expa'
                               'nding it first!!')

        info_opts.add_argument('-ls',
                               '--list-servers',
                               action='store_true',
                               help = 'List all web servers currently supported'
                               ' by webapp-config')

        info_opts.add_argument('-lui',
                               '--list-unused-installs',
                               nargs = '*',
                               help = 'List all master images which currently a'
                               're not used. Optionally, provide a package and/'
                               'or version number as arguments to restrict the '
                               'listing.')

        info_opts.add_argument('-pd',
                               '--prune-database',
                               choices = ['pretend',
                                          'clean'],
                               help = 'This will list all outdated entries in '
                               'the webapp-config "database".')

        info_opts.add_argument('-si',
                               '--show-installed',
                               action='store_true',
                               help = 'Show what application is installed in DI'
                               'R')

        info_opts.add_argument('-spi',
                               '--show-postinst',
                               nargs = 2,
                               help = 'Show the post-installation instructions '
                               'for <application>. Very handy if you\'ve lost t'
                               'he instructions when they were shown to you ;-)'
                               )

        info_opts.add_argument('-spu',
                               '--show-postupgrade',
                               nargs = 2,
                               help = 'Show the post-upgrade instructions for '
                               '<application>. Very handy if you\'ve lost the '
                               'instructions when they were shown to you ;-)')

        info_opts.add_argument('--query',
                               nargs=2)

        #-----------------------------------------------------------------
        # Other Options

        alio_opts = self.parser.add_argument_group('<Other Options>')

        alio_opts.add_argument('-D',
                               '--define',
                               action = 'append',
                               help = 'Allows to name a <KEY>=<VALUE> pair that'
                               'will be imported into the configuration variabl'
                               'es of webapp-config. This allows you to provide'
                               ' customized variables which can be used in the '
                               'configuration file. This can also be used to te'
                               'mporarily overwrite variables from the configur'
                               'ation file.')

        alio_opts.add_argument('--envall',
                               action='store_true',
                               help = 'Imports all environment variables and ov'
                               'erwrites configurations read from the configura'
                               'tion file. Setting this switch is not recommend'
                               'ed since you might have environment variables s'
                               'et to values that cannot be parsed.')

        alio_opts.add_argument('-E',
                               '--envvar',
                               action='append',
                               help = 'Allows to name single environment variab'
                               'le that will be imported by webapp-config. This'
                               ' allows you to provide customized variables whi'
                               'ch can be used in the configuration file. This '
                               'can also be used to temporarily overwrite varia'
                               'bles from the configuration file.')

        alio_opts.add_argument('-?',
                               '--help',
                               action='help',
                               help = 'Show this help')


        #-----------------------------------------------------------------
        # Debug Options

        OUT.cli_opts(self.parser)

        #-----------------------------------------------------------------
        # Bug Options

        bug_opts = self.parser.add_argument_group('<Reporting Bugs>',
                                                  'To report bugs about webapp'
                                                  '-config, please go to '
                                                  + self.config.get('USER',
                                                                  'my_bugsurl')
                                                  + '. Include the output of w'
                                                  'ebapp-config --bug-report <'
                                                  'your parameters here> to he'
                                                  'lp us to help you')

        bug_opts.add_argument('--bug-report',
                              action='store_true')


    # --------------------------------------------------------------------
    # Variable functions

    def maybe_get(self, option, section = 'USER'):

        OUT.debug('Retrieving option ' + option, 7)

        if not self.config.has_option(section, option):

            OUT.debug('Missing option ' + option, 7)

            return ''
        return self.config.get(section, option)

    def get_perm(self, permission):
        result = None
        try:
            result = PermissionMap(self.maybe_get(permission))
        except Exception:
            OUT.die('You specified an invalid permission value for the'
                    ' variable "' + permission + "'")
        return result

    def get_user(self, user):
        result = None
        try:
            result = Perm.get_user(self.maybe_get(user))
        except KeyError:
            OUT.die('You specified an invalid user value for the'
                    ' variable "' + user + "'")
        return result

    def maybe_get_user(self, user):
        result = None
        input_user = self.maybe_get(user)
        if not input_user:
            return result
        try:
            result = Perm.get_user(input_user)
        except KeyError:
            OUT.die('You specified an invalid user value for the'
                    ' variable "' + user + "'")
        return result

    def get_group(self, group):
        result = None
        try:
            result = Perm.get_group(self.maybe_get(group))
        except KeyError:
            OUT.die('You specified an invalid group value for the'
                    ' variable "' + group + "'")
        return result

    def maybe_get_group(self, group):
        result = None
        input_group = self.maybe_get(group)
        if not input_group:
            return result
        try:
            result = Perm.get_group(input_group)
        except KeyError:
            OUT.die('You specified an invalid group value for the'
                    ' variable "' + group + "'")
        return result

    def installdir(self):
        return self.maybe_get('g_installdir')

    def packagename(self, sep='-'):
        return self.maybe_get('cat') + '/' + self.maybe_get('pn') + sep + self.maybe_get('pvr')

    def maybe_getboolean(self, option, section = 'USER'):

        OUT.debug('Retrieving boolean option ' + option, 7)

        if not self.config.has_option(section, option):

            OUT.debug('Missing boolean option ' + option, 7)

            return False

        return self.config.getboolean(section, option)

    def removing(self):
        return self.maybe_getboolean('g_remove')

    def installing(self):
        return self.maybe_getboolean('g_install')

    def upgrading(self):
        return self.maybe_getboolean('g_upgrade')

    def verbose(self):
        return self.maybe_getboolean('g_verbose')

    def pretend(self):
        return self.maybe_getboolean('g_pretend')

    # --------------------------------------------------------------------
    # fn_parseparams()
    #
    # Parse the command-line parameters, ready to verify them
    #
    # Inputs:
    #  $* - the command-line the script was called with
    #
    # Outputs
    #  None

    def parseparams (self):

        OUT.debug('Parsing all configuration parameters', 6)

        # we import /etc/vhosts/webapp-config so that we can snag the
        # defaults to embed in this output

        if not os.access(self.__d['my_etcconfig'], os.R_OK):
            OUT.die('The configuration file ' + self.__d['my_etcconfig'] +
                    ' is not accessible!')

        try:
            self.config.read(self.__d['my_etcconfig'])
        except Exception as e:
            OUT.die('The config file '
                    + self.config.get('USER', 'my_etcconfig') +
                    ' cannot be read by the configuration parser.'
                    '.\nMaybe you need to etc-update?\nError was: ' + str(e))

        # check the version id in the config file
        #
        # if they don't match, it's probably because the user hasn't had
        # the chance to etc-update yet

        if (self.config.get('USER', 'wa_conf_version') !=
            self.config.get('USER', 'my_conf_version')):

            OUT.die('The config file '
                    + self.config.get('USER', 'my_etcconfig') +
                    ' appears to be for an older version of webapp-config'
                    '.\nMaybe you need to etc-update?\n'
                    + self.config.get('USER', 'my_etcconfig') +
                    ' needs updating!')


        OUT.debug('Successfully parsed configuration file options', 7)

        # Parse the command line
        options = vars(self.parser.parse_args())

        OUT.debug('Successfully parsed command line options', 7)

        # handle debugging
        OUT.cli_handle(options)

        # Second config level are environment variables

        # Handle -E
        envmap = []

        if ('envall' in options and 
            options['envall']):
            envmap = 'all'

        elif ('envvar' in options and 
              options['envvar']):
            envmap = [x.lower() for x in options['envvar']]

        OUT.debug('Trying to import environment variables', 7)

        if envmap:
            for (key, value) in list(os.environ.items()):

                if envmap == 'all' or key.lower() in envmap:

                    OUT.debug('Adding environment variable', 8)

                    try:
                        self.config.set('USER',
                                        key.lower(),
                                        value)
                    except ValueError:
                        pass

        if ('define' in options and
              options['define']):
            for i in options['define']:
                if '=' in i:
                    self.config.set('USER', 
                                    i.split('=')[0].lower(),
                                    i.split('=')[1])

        # Indicate that --dir was found
        if 'dir' in options:
            self.flag_dir = True

        # Map command line options into the configuration
        option_to_config = {'host'         : 'vhost_hostname',
                            'dir'          : 'g_installdir',
                            'server'       : 'vhost_server',
                            'secure'       : 'g_secure',
                            'user'         : 'vhost_config_uid',
                            'group'        : 'vhost_config_gid',
                            'soft'         : 'g_soft',
                            'copy'         : 'g_copy',
                            'virtual_files': 'vhost_config_virtual_files',
                            'default_dirs' : 'vhost_config_default_dirs',
                            'pretend'      : 'g_pretend',
                            'verbose'      : 'g_verbose',
                            'bug_report'   : 'g_bugreport'}

        for key in option_to_config:
            if key in options and options[key]:
                # If it's a list, we're expecting only one value in the list.
                if isinstance(options[key], list):
                    self.config.set('USER', option_to_config[key],
                                    str(options[key][0]))
                else:
                    self.config.set('USER', option_to_config[key],
                                    str(options[key]))

        # handle verbosity
        if ('pretend' in options
            and options['pretend']):

            self.config.set('USER', 'g_verbose', 'True')

        if self.verbose():

            OUT.debug('Setting verbose', 7)

            OUT.set_info_level(4)

        else:

            OUT.debug('Setting quiet', 7)

            OUT.set_info_level(1)

        # Provide simple subdomain support
        self.split_hostname()

        # support --secure
        if not self.config.has_option('USER', 'my_htdocsbase'):

            OUT.debug('Setting "my_htdocsbase"', 7)

            if (self.config.has_option('USER', 'g_secure') and
                self.config.getboolean('USER', 'g_secure')):
                self.config.set('USER', 'my_htdocsbase',
                                '${vhost_htdocs_secure}')
            else:
                self.config.set('USER', 'my_htdocsbase',
                                '${vhost_htdocs_insecure}')

        # set the action to be performed
        work = ['install', 'clean', 'upgrade', 'list_installs',
                'list_servers', 'list_unused_installs',
                'prune_database', 'show_installed', 'show_postinst',
                'show_postupgrade', 'check_config', 'query']

        if len(sys.argv) ==  1:
            self.parser.print_help()
            sys.exit()

        for i in work:
            if options.get(i) != None and options.get(i) != False:
                self.work = i
                break

        if options.get('prune_database'):
            self.prune_action = options.get('prune_database')

        OUT.debug('Checking command line arguments', 1)

        if self.work in ['install', 'clean', 'query', 'list_installs',
                         'show_postinst', 'show_postupgrade', 'upgrade']:
            # get cat / pn
            args = options[self.work]

            if len(args):
                m    = args[0].split('/')

                if self.work == 'list_installs' and len(args) > 2:
                    msg = os.path.basename(sys.argv[0]) + ': error: argument '\
                          '-li/--list-installs: expected up to 2 arguments'

                    self.parser.print_usage()
                    print(msg)
                    sys.exit()

                if len(m) == 1:
                    if '*' not in m:
                        self.config.set('USER', 'pn',  m[0])
                elif len(m) == 2:
                    self.config.set('USER', 'cat', m[0])
                    if '*' not in m:
                        self.config.set('USER', 'pn',  m[1])
                else:
                    OUT.die('Invalid package name')

                if len(args) > 1:
                    pvr = args[1]
                    has_int = False # A package version should have at least one
                                    # numerical value, but we want to allow for
                                    # the flexibility of having any alphanumeric
                                    # value while checking to make sure it's sane.

                    for char in pvr:
                        if char.isdigit():
                            has_int = True

                    if not has_int:
                        OUT.die('Invalid package version: "%(pvr)s"'
                                % {'pvr': args[1]})

                    self.config.set('USER', 'pvr', pvr)

                if (not options['dir'] and
                    self.work not in ('list_installs', 'query')):
                    pn  = self.config.get('USER', 'pn')
                    msg = 'Install dir flag not supplied, defaulting to '\
                          '"%(pn)s".' % {'pn': pn}

                    OUT.warn(msg)
                    self.config.set('USER', 'g_installdir', pn)
                    self.flag_dir = True

        # store original installdir
        self.config.set('USER', 'g_orig_installdir',
                        self.config.get('USER', 'g_installdir'))


    # --------------------------------------------------------------------
    # Helper functions

    def check_package_set(self):
        if not self.config.has_option('USER', 'pn'):
            self.parser.print_help()
            OUT.die('You need to specify at least the application you'
                    ' would like to handle!')
        else:
            return self.config.get('USER', 'pn')

    def check_version_set(self):
        if not self.config.has_option('USER', 'pvr'):
            OUT.die('You did not specify which version to handle.\n Use "'
                    + self.config.get('USER','g_myname') +
                    ' --help" for usage')
        else:
            return self.config.get('USER', 'pvr')

    def split_hostname(self):

        hostname = self.config.get('USER', 'vhost_hostname')

        subdomains = hostname.split('.')

        j = len(subdomains)
        for i in subdomains:
            if not self.maybe_get('USER', 'vhost_subdomain_' + str(j)):
                self.config.set('USER', 'vhost_subdomain_' + str(j), i)

            OUT.debug('Storing subdomain name', 8)

            j -= 1

    def setinstalldir(self):

        # set our install directory
        #
        # the sed is to make sure we don't have any '//' or '///' and so
        # on in the final directory string
        #
        # this makes sure we don't write rubbish into the installs list

        g_installdir = self.config.get('USER', 'g_installdir')

        if (os.path.isabs(g_installdir) 
            and self.config.get('USER', 'allow_absolute') == 'yes'):
            installpath = g_installdir
        else:
            installpath = self.config.get('USER', 'g_htdocsdir') + '/' + g_installdir

        installpath = re.compile('/+').sub('/', self.__r + installpath)

        while installpath[-1] == '/':
            installpath = installpath[:-1]

        OUT.info('Install directory is: ' + installpath)

        self.config.set('USER', 'g_installdir', installpath)

    def checkconfig (self):

        OUT.debug('Running checkconfig', 6)

        # handle the softlink support

        if ((self.config.has_option('USER', 'vhost_link_type') and
             self.config.get('USER', 'vhost_link_type') == 'soft') or
            (self.config.has_option('USER', 'g_soft') and
             self.config.getboolean('USER', 'g_soft'))):

            OUT.debug('Selecting soft links' , 7)

            self.config.set('USER', 'g_link_type', 'soft')

        elif ((self.config.has_option('USER', 'vhost_link_type') and
               self.config.get('USER', 'vhost_link_type') == 'soft') or
              (self.config.has_option('USER', 'g_copy') and
               self.config.getboolean('USER', 'g_copy'))):

            OUT.debug('Selecting copying of links', 7)

            self.config.set('USER', 'g_link_type', 'copy')

        else:

            OUT.debug('Selecting hard links' , 7)

            self.config.set('USER', 'g_link_type', 'hard')

        # here, we output some useful information that might make a
        # difference when dealing with a bug report

        OUT.info('\nParameters from section "USER":\n')
        usr_list = []
        for i in self.config.options('USER'):

            OUT.debug('Reporting parameter' , 7)

            OUT.info('  Parameter ' + OUT.maybe_color('turquoise', i)
                     + ': "' + self.config.get('USER', i) + '"')
            usr_list.append(i)

        OUT.info('\nParameters from section "USER":\n')
        for i in self.config.options('USER'):
            if not i in usr_list:
                OUT.info('  Parameter ' + OUT.maybe_color('turquoise', i)
                         + ': "' + self.config.get('USER', i) + '"')

        # if we're running with --bug-report, time to quit

        if self.config.has_option('USER', 'g_bugreport') and                          \
               self.config.getboolean('USER', 'g_bugreport'):
            sys.exit(0)

        # if we get to here, then all is well

        OUT.info('All config file checks successfully passed')

    # --------------------------------------------------------------------
    # Main functions

    def run(self):

        OUT.debug('Handling ' + self.work, 6)

        if self.work == 'help':
            self.parser.print_help()
            sys.exit(0)

        if self.work == 'list_servers':
            from WebappConfig.server import listservers
            # List the supported servers
            listservers()

        if self.work == 'query':

            # The user needs to specify package and version
            # for this action
            self.check_package_set()
            self.check_version_set()

            # set my_appsuffix right away
            cat = self.maybe_get('cat')
            pn  = self.config.get('USER', 'pn')
            pvr = self.config.get('USER', 'pvr')
            self.config.set('USER', 'my_appsuffix',   '/'.join([cat,pn,pvr]))
            self.config.set('USER', 'persist_suffix', '/'.join([cat,pn,pvr]))

            self.set_vars()

            # List all variables in bash format for the eclass
            self.config.on_error(2)
            for i in self.config.options('USER'):
                if not i in ['pn', 'pvr']:
                    try:
                        print(i.upper() + '="' + self.config.get('USER', i) + '"')
                    except configparser.InterpolationSyntaxError:
                        print('# Failed to evaluate: ' + i.upper())

            sys.exit(0)

        if self.work == 'list_unused_installs':
            # Get the handler for the virtual install db
            self.__r = wrapper.get_root(self)
            db = self.create_webapp_db( self.maybe_get('cat'),
                                        self.maybe_get('pn'),
                                        self.maybe_get('pvr'))

            # Compare this against the installed web applications
            self.create_webapp_source().listunused(db)

        if self.work == 'list_installs':
            # Get the handler for the virtual install db and list the
            # virtual installations
            self.__r = wrapper.get_root(self)
            self.create_webapp_db(  self.maybe_get('cat'),
                                    self.maybe_get('pn'),
                                    self.maybe_get('pvr')).listinstalls()
        if self.work == 'prune_database':
            # Get the handler for the virtual install db. If the action is equal
            # to clean, then it'll simply prune the "db" of outdated entries.
            # If it's not set to clean, then it'll list the outdated entries
            # in the db to be cleaned out.
            self.__r = wrapper.get_root(self)
            self.create_webapp_db(  self.maybe_get('cat'),
                                    self.maybe_get('pn'),
                                    self.maybe_get('pvr')).prune_database(self.prune_action)

        if self.work == 'show_installed':

            # This reads a .webapp file in the specified installdir.
            self.__r = wrapper.get_root(self)
            self.setinstalldir()
            self.create_dotconfig().show_installed()

        if self.work == 'show_postinst':

            # The user needs to specify package and version
            # for this action
            self.__r = wrapper.get_root(self)
            wrapper.want_category(self)
            self.check_package_set()
            self.check_version_set()
            self.set_vars()

            # The package must be installed
            self.create_webapp_source().reportpackageavail()

            # Show the post install file
            self.create_ebuild().show_postinst()

        if self.work == 'show_postupgrade':

            # The user needs to specify package and version
            # for this action
            self.__r = wrapper.get_root(self)
            wrapper.want_category(self)
            self.check_package_set()
            self.check_version_set()
            self.set_vars()

            # The package must be installed
            self.create_webapp_source().reportpackageavail()

            # Show the post upgrade file
            self.create_ebuild().show_postupgrade()

        if self.work == 'install':

            # This function's job is to perform *all* checks necessary to ensure
            # that we can go ahead and install the application.
            #
            # If any of the tests fail, this function should abort by calling
            # libsh_die()
            #
            # If this function returns, the caller can assume it is safe to
            # proceed with the installation attempt
            #
            # This function is called from fn_verifyparams, *after* the
            # command-line parameters have been parsed, and assigned to the
            # $G_... variables.

            # Required: package name and version
            # Category may be required

            self.__r = wrapper.get_root(self)
            wrapper.want_category(self)
            self.check_package_set()
            self.check_version_set()
            self.set_vars()

            # Check that all configurations are valid
            self.checkconfig()

            # Check package availability and read config/server-owned files
            ws = self.create_webapp_source()
            ws.reportpackageavail()
            ws.read(
                virtual_files = self.config.get('USER', 'vhost_config_virtual_files'),
                default_dirs  = self.config.get('USER', 'vhost_config_default_dirs')
                )

            # Set the installation directory
            self.setinstalldir()

            # Check if there is a conflicting package
            OUT.info('Is there already a package installed in '
                     + self.config.get('USER', 'g_installdir') + '?')

            old = self.create_dotconfig()

            if old.has_dotconfig():
                old.read()
                OUT.die('Package ' + old.packagename() + ' is already in'
                        'stalled here.\nUse webapp-config -C to uninstall'
                        ' it first.\nInstall directory already contains a'
                        ' web application!')

            OUT.info('No, there isn\'t.  I can install into there safely.'
                     )

            # check install location
            if (os.path.basename(self.installdir()) == 
                self.maybe_get('my_htdocsbase')):
                OUT.warn('\nYou may be installing into the website\'s root di'
                         'rectory.\nIs this what you meant to do?\n')

            # Now we can install
            self.create_server(self.create_content( self.maybe_get('cat'),
                                                    self.maybe_get('pn'),
                                                    self.maybe_get('pvr')),
                               ws,
                               self.maybe_get('cat'),
                               self.config.get('USER', 'pn'),
                               self.config.get('USER', 'pvr')).install()

        if self.work == 'clean':

            # This function's job is to perform *all* checks necessary to ensure
            # that we can successfully remove an installed application.
            #
            # If any tests fail, this function should abort by calling libsh_die().
            #
            # This function is called from fn_verify(), and runs after the command-
            # line parameters have been parsed.

            # Required: package name and version
            # Category may be required

            self.__r = wrapper.get_root(self)
            wrapper.want_category(self)
            package = self.check_package_set()
            version = self.check_version_set()
            self.set_vars()

            webapp = package + ' ' + version

            # special case
            #
            # if a package has been specified, then chances are that they forgot
            # to add the '-d' switch
            if not self.flag_dir:
                OUT.die('Missing switch -d <dir>')

            if not self.upgrading():
                self.setinstalldir()

            old = self.create_dotconfig()

            if not old.has_dotconfig():
                OUT.die('Cannot clean!\n'
                        'No package installed in ' + self.installdir())
            old.read()

            if not os.path.isdir(self.installdir()):
                OUT.die('Directory "'
                        + self.__destd
                        + '" does not appear to exist')

            if self.verbose():
                OUT.warn('clean() called')

            OUT.info('Removing installed files and directories')

            self.config.set('USER', 'cat',  old['WEB_CATEGORY'])
            self.config.set('USER', 'pn',  old['WEB_PN'])
            self.config.set('USER', 'pvr', old['WEB_PVR'])

            old_webapp = old['WEB_PN'] + ' ' + old['WEB_PVR']

            if not webapp == old_webapp:
                OUT.die(webapp + ' does not match ' +
                        old_webapp + ' found in .webapp file at ' +
                        self.installdir() + '.')

            # we don't want to read the .webapp file if we're upgrading,
            # because we've just written the *new* app's details into the file!!
            #
            # we've already read the *old* app's details before we began the
            # upgrade

            # okay - what do we have?
            msg = 'Removing '
            if old['WEB_CATEGORY']:
                msg += old['WEB_CATEGORY'] + '/'
            msg += old['WEB_PN'] + '-' + old['WEB_PVR'] + ' from '\
                   + self.installdir() + '\n  Installed by '\
                   + old['WEB_INSTALLEDBY'] + ' on ' + old['WEB_INSTALLEDDATE']\
                   + '\n  Config files owned by ' + old['WEB_INSTALLEDFOR']

            OUT.info(msg, 1)

            content = self.create_content(old['WEB_CATEGORY'], old['WEB_PN'], old['WEB_PVR'])
            content.read()

            self.create_server(content,
                               self.create_webapp_source(),
                               old['WEB_CATEGORY'], old['WEB_PN'], old['WEB_PVR']).clean()

        if self.work == 'upgrade':

            # This function's job is to perform *all* the checks necessary before
            # we go off and upgrade an installed application
            #
            # If any of the tests fail, this function should abort the script by
            # calling the libsh_die() function
            #
            # If this function returns to the caller, the caller should assume it's
            # safe to start the upgrade :-)

            # catch errors caused by missing <app-name> or <app-version>
            #
            # see Gentoo bug 98638

            # Required: package name and version
            # Category may be required

            self.__r = wrapper.get_root(self)
            wrapper.want_category(self)
            self.check_package_set()
            self.check_version_set()
            self.set_vars()

            # Check that all configurations are valid
            self.checkconfig()

            # Check package availability and read config/server-owned files
            ws = self.create_webapp_source()
            ws.reportpackageavail()
            ws.read(
                virtual_files = self.config.get('USER', 'vhost_config_virtual_files'),
                default_dirs  = self.config.get('USER', 'vhost_config_default_dirs')
                )

            # Set the installation directory
            self.setinstalldir()

            old = self.create_dotconfig()

            if not old.has_dotconfig():
                OUT.die('Cannot clean!\n'
                        'No package installed in ' + self.installdir())
            old.read()

            # special case
            #
            # are we trying to upgrade to the same package?
            # if so, we don't allow that

            # FIXME: Should we check that we are not upgrading from
            # package x to package y? In principle we are just running
            # -C followed by -I so it does not matter too much what
            # we remove and add. Why did the originial bash version
            # invert removing and adding?

            # I don't really think the above comments still apply -- rl03

            if (self.maybe_get('cat') == old['WEB_CATEGORY'] and
                self.config.get('USER', 'pn') == old['WEB_PN'] and
                self.config.get('USER', 'pvr') == old['WEB_PVR']):
                OUT.warn('Do you really want to upgrade to the same pac'
                         'kage?\nWaiting for 8 seconds...\nPress Ctrl-c i'
                         'f you are uncertain.')
                time.sleep(8)

            if (self.maybe_get('cat') + self.config.get('USER', 'pn') != \
                    old['WEB_CATEGORY'] + old['WEB_PN']):
                OUT.warn('Do you really want to switch the installation'
                         ' from package "' + old['WEB_CATEGORY'] + '/' +
                         old['WEB_PN'] + '" to package "' + \
                         self.maybe_get('cat') + '/' + \
                         self.config.get('USER', 'pn') + '"?\nWai'
                         'ting for 8 seconds... \nPress Ctrl-c if you are'
                         ' uncertain.')
                time.sleep(8)

            # okay, what do we have?

            OUT.info('Upgrading '
                     + old['WEB_CATEGORY'] + '/'
                     + old['WEB_PN'] + '-'
                     + old['WEB_PVR'] + ' to '
                     + self.packagename() + '\n'
                     + '  Installed by '
                     + old['WEB_INSTALLEDBY'] + ' on '
                     + old['WEB_INSTALLEDDATE'] + '\n'
                     +'  Config files owned by '
                     + old['WEB_INSTALLEDFOR'], 1)

            content = self.create_content(old['WEB_CATEGORY'], old['WEB_PN'], old['WEB_PVR'])
            content.read()

            self.create_server(content,
                               ws,
                               old['WEB_CATEGORY'],
                               old['WEB_PN'],
                               old['WEB_PVR']).upgrade(self.maybe_get('cat'),
                                                       self.config.get('USER', 'pn'),
                                                       self.config.get('USER', 'pvr'))


    def create_webapp_db(self, category, package, version):

        from WebappConfig.db import  WebappDB

        return WebappDB(self.__r,
                        self.maybe_get('my_persistroot'),
                        category,
                        package,
                        version,
                        self.maybe_get('wa_installsbase'),
                        #FIXME: I am confused by the bash version here
                        # The bash version uses the following command to
                        # create missing directories in the install db:
                        # mkdir -p "`dirname ${WA_INSTALLS}`" -m ${G_PERMS_DOTCONFIG}
                        # G_PERMS_DOTCONFIG defaults to "0600". I
                        # don't understand why I have only 755 directories
                        # my install db. Choosing 0755 here should be safe.
                        PermissionMap('0755'),
                        self.get_perm('g_perms_dotconfig'),
                        self.verbose(),
                        self.pretend())

    def create_webapp_source(self):

        from WebappConfig.db import  WebappSource

        return WebappSource(self.__r,
                            self.maybe_get('my_approot'),
                            self.maybe_get('cat'),
                            self.maybe_get('pn'),
                            self.maybe_get('pvr'),
                            pm = self.config.get('USER', 'package_manager'))

    def create_dotconfig(self):

        from WebappConfig.dotconfig import  DotConfig

        return DotConfig(self.installdir(),
                         self.maybe_get('my_dotconfig'),
                         self.get_perm('g_perms_dotconfig'),
                         self.pretend())

    def create_ebuild(self):

        from WebappConfig.ebuild import  Ebuild

        return Ebuild(self)


    def create_content(self, category, package, version):

        from WebappConfig.content import  Contents

        return Contents(self.installdir(),
                        category,
                        package,
                        version,
                        self.get_perm('g_perms_dotconfig'),
                        self.maybe_get('my_dotconfig'),
                        self.verbose(),
                        self.pretend(),
                        self.__r)

    def create_server(self, content, webapp_source, category, package, version):

        # handle server type

        allowed_servers = {'apache'   : WebappConfig.server.Apache,
                           'lighttpd' : WebappConfig.server.Lighttpd,
                           'cherokee' : WebappConfig.server.Cherokee,
                           'nginx'    : WebappConfig.server.Nginx,
                           'gatling'  : WebappConfig.server.Gatling,
                           'tracd'    : WebappConfig.server.Tracd,
                           'uwsgi'    : WebappConfig.server.uWSGI,}


        server = self.config.get('USER', 'vhost_server')

        if not server in allowed_servers:
            OUT.die('I don\'t support the "' + server + '" web server.')

        from WebappConfig.protect import Protection

        directories = {'source' :      'htdocs',
                       'destination' : self.installdir(),
                       'hostroot' :    self.maybe_get('my_hostrootbase'),
                       'vhostroot' :   self.maybe_get('vhost_root')}

        handlers = {'source'    : webapp_source,
                    'dotconfig' : self.create_dotconfig(),
                    'ebuild'    : self.create_ebuild(),
                    'db'        : self.create_webapp_db(category, package, version),
                    'protect'   : Protection(category,package,version,
                                    self.config.get('USER','package_manager')),
                    'content'   : content}

        flags = {'linktype' : self.maybe_get('g_link_type'),
                 'host'     : self.maybe_get('vhost_hostname'),
                 'orig'     : self.maybe_get('g_orig_installdir'),
                 'upgrade'  : self.upgrading(),
                 'verbose'  : self.verbose(),
                 'pretend'  : self.pretend()}

        return allowed_servers[server](directories,
                                       self.create_permissions(),
                                       handlers,
                                       flags,
                                       pm = self.config.get('USER', 'package_manager'))

    def create_permissions(self):

        return {'file' : {'virtual' :      [self.get_user('vhost_default_uid'),
                                            self.get_group('vhost_default_gid'),
                                            self.get_perm('vhost_perms_virtualowned_file')],
                          # These will be re-set by the servers
                          'server-owned' : [self.maybe_get_user('vhost_server_uid'),
                                            self.maybe_get_group('vhost_server_gid'),
                                            self.get_perm('vhost_perms_serverowned_file')],
                          'config-owned' : [self.get_user('vhost_config_uid'),
                                            self.get_group('vhost_config_gid'),
                                            self.get_perm('vhost_perms_configowned_file')],
                          'config-server-owned' : [self.get_user('vhost_config_uid'),
                                                   self.maybe_get_group('vhost_server_gid'),
                                                   self.get_perm('vhost_perms_serverowned_file')],},
                'dir' :  {'default-owned' :[self.get_user('vhost_default_uid'),
                                            self.get_group('vhost_default_gid'),
                                            self.get_perm('vhost_perms_defaultowned_dir')],
                          # These will be re-set by the servers
                          'server-owned' : [self.maybe_get_user('vhost_server_uid'),
                                            self.maybe_get_group('vhost_server_gid'),
                                            self.get_perm('vhost_perms_serverowned_dir')],
                          'config-owned' : [self.get_user('vhost_config_uid'),
                                            self.get_group('vhost_config_gid'),
                                            self.get_perm('vhost_perms_configowned_dir')],
                          'config-server-owned' : [self.get_user('vhost_config_uid'),
                                                  self.maybe_get_group('vhost_server_gid'),
                                                  self.get_perm('vhost_perms_serverowned_dir')],
                          'install-owned': [self.get_user('vhost_default_uid'),
                                            self.get_group('vhost_default_gid'),
                                            self.get_perm('vhost_perms_installdir')],},
                }
