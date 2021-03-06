#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright © 2007-2011  Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use, modify,
# copy, or redistribute it subject to the terms and conditions of the GNU
# General Public License v.2.  This program is distributed in the hope that it
# will be useful, but WITHOUT ANY WARRANTY expressed or implied, including the
# implied warranties of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.  You should have
# received a copy of the GNU General Public License along with this program;
# if not, write to the Free Software Foundation, Inc., 51 Franklin Street,
# Fifth Floor, Boston, MA 02110-1301, USA. Any Red Hat trademarks that are
# incorporated in the source code or documentation are not subject to the GNU
# General Public License and may only be used or replicated with the express
# permission of Red Hat, Inc.
#
# Red Hat Author(s): Mike McGrath <mmcgrath@redhat.com>
#                    Toshio Kuratomi <tkuratom@redhat.com>
#                    Ricky Zhou <rzhou@redhat.com>

import os
import pwd
import sys
import codecs
import tempfile
import logging
import syslog
import datetime
import subprocess
import time

from fedora.client import AccountSystem, AuthError, ServerError
from kitchen.text.converters import to_bytes
from urllib2 import URLError

try:
    import selinux
    from shutil import rmtree
    from selinux import copytree, install as move
    have_selinux = (selinux.is_selinux_enabled() == 1)
except ImportError:
    from shutil import move, rmtree, copytree
    have_selinux = False

try:
    import cPickle as pickle
except ImportError:
    import pickle

import ConfigParser
from optparse import OptionParser

import gettext
import locale
try:
    locale.setlocale(locale.LC_ALL, '')
except locale.Error:
    locale.setlocale(locale.LC_ALL, 'C')
if os.path.isdir('po'):
    t = gettext.translation('fas', 'po', fallback=True)
else:
    t = gettext.translation('fas', '/usr/share/locale', fallback=True)

_ = t.gettext


parser = OptionParser()

parser.add_option('-i', '--install',
                     dest = 'install',
                     default = False,
                     action = 'store_true',
                     help = _('Download and sync most recent content'))
parser.add_option('-I', '--info',
                     dest = 'info_username',
                     default = False,
                     metavar = 'info_username',
                     help = _('Get info about a user'))
parser.addd_option('--daemon', help=('Start fasClient as a daemon'))
parser.add_option('-c', '--config',
                     dest = 'CONFIG_FILE',
                     default = '/etc/fas.conf',
                     metavar = 'CONFIG_FILE',
                     help = _('Specify config file (default "%default")'))
parser.add_option('--nogroup',
                     dest = 'no_group',
                     default = False,
                     action = 'store_true',
                     help = _('Do not sync group information'))
parser.add_option('--nopasswd',
                     dest = 'no_passwd',
                     default = False,
                     action = 'store_true',
                     help = _('Do not sync passwd information'))
parser.add_option('--noshadow',
                     dest = 'no_shadow',
                     default = False,
                     action = 'store_true',
                     help = _('Do not sync shadow information'))
parser.add_option('--nohome',
                     dest = 'no_home_dirs',
                     default = False,
                     action = 'store_true',
                     help = _('Do not create home dirs'))
parser.add_option('--nossh',
                     dest = 'no_ssh_keys',
                     default = False,
                     action = 'store_true',
                     help = _('Do not create ssh keys'))
parser.add_option('-s', '--server',
                     dest = 'FAS_URL',
                     default = None,
                     metavar = 'FAS_URL',
                     help = _('Specify URL of fas server.'))
parser.add_option('-p', '--prefix',
                     dest = 'prefix',
                     default = None,
                     metavar = 'prefix',
                     help = _('Specify install prefix.  Useful for testing'))
parser.add_option('-e', '--enable',
                     dest = 'enable',
                     default = False,
                     action = 'store_true',
                     help = _('Enable FAS synced shell accounts'))
parser.add_option('-d', '--disable',
                     dest = 'disable',
                     default = False,
                     action = 'store_true',
                     help = _('Disable FAS synced shell accounts'))
parser.add_option('-a', '--aliases',
                     dest = 'aliases',
                     default = False,
                     action = 'store_true',
                     help = _('Sync mail aliases'))
parser.add_option('-f', '--force-refresh',
                     dest = 'force_refresh',
                     default = False,
                     action = 'store_true',
                     help = _('Fetch FAS data from the database, not cache'))
parser.add_option('--nosession',
                     dest = 'nosession',
                     default = False,
                     action = 'store_true',
                     help = _('Disable the creation of ~/.fedora_session'))
parser.add_option('--debug',
                     dest = 'debug',
                     default = False,
                     action = 'store_true',
                     help = _('Enable debugging messages'))

(opts, args) = parser.parse_args()

log = logging.getLogger('fas')

try:
    config = ConfigParser.ConfigParser()
    if os.path.exists(opts.CONFIG_FILE):
        config.read(opts.CONFIG_FILE)
    elif os.path.exists('fas.conf'):
        config.read('fas.conf')
        print >> sys.stderr, 'Could not open %s, defaulting to ./fas.conf' % opts.CONFIG_FILE
    else:
        print >> sys.stderr, 'Could not open %s' % opts.CONFIG_FILE
        sys.exit(5)
except ConfigParser.MissingSectionHeaderError, e:
        print >> sys.stderr, 'Config file does not have proper formatting: %s' % e
        sys.exit(6)

FAS_URL = config.get('global', 'url').strip('"')
if opts.prefix:
    prefix = opts.prefix
else:
    prefix = config.get('global', 'prefix').strip('"')

def _chown(arg, dir_name, files):
    os.chown(dir_name, arg[0], arg[1])
    for file in files:
        os.chown(os.path.join(dir_name, file), arg[0], arg[1])

class MakeShellAccounts(AccountSystem):
    _orig_euid = None
    _orig_egid = None
    _orig_groups = None
    _users = None
    _groups = None
    _good_users = None
    _group_types = None
    _temp = None

    def __init__(self, *args, **kwargs):
        self._orig_euid = os.geteuid()
        self._orig_egid = os.getegid()
        self._orig_groups = os.getgroups()

        force_refresh = kwargs.get('force_refresh')
        if force_refresh is None:
            self.force_refresh = False
        else:
            del(kwargs['force_refresh'])
            self.force_refresh = force_refresh
        super(MakeShellAccounts, self).__init__(*args, **kwargs)

    def _make_tempdir(self, force=False):
        '''Return a temporary directory'''
        if not self._temp or force:
            # Remove any existing temp directories
            if self._temp:
                rmtree(self._temp)
            self._temp = tempfile.mkdtemp('-tmp', 'fas-', config.get('global', 'temp').strip('"'))
        return self._temp
    temp = property(_make_tempdir)

    def _refresh_users(self, force=False):
        '''Return a list of users in FAS'''
        # Cached values present, return
        if not self._users or force:
            self._users = self.user_data()
        return self._users

    users = property(_refresh_users)

    def _refresh_groups(self, force=False):
        '''Return a list of groups in FAS'''
        # Cached values present, return
        if not self._groups or force:
            group_data = self.group_data(force_refresh=self.force_refresh)
            # The JSON output from FAS encodes dictionary keys as strings, but leaves
            # array elements as integers (in the case of group member UIDs).  This
            # normalizes them to all strings.
            for group in group_data:
                for role_type in ('administrators', 'sponsors', 'users'):
                    group_data[group][role_type] = [str(uid) for uid in group_data[group][role_type]]
            self._groups = group_data
        return self._groups

    groups = property(_refresh_groups)

    def _refresh_good_users_group_types(self, force=False):
        # Cached values present, return
        if self._good_users and self._group_types and not force:
            return

        cla_group = config.get('global', 'cla_group').strip('"')
        if cla_group not in self.groups:
            print >> sys.stderr, 'No such group: %s' % cla_group
            print >> sys.stderr, 'Aborting.'
            sys.exit(1)

        cla_uids = self.groups[cla_group]['users'] + \
            self.groups[cla_group]['sponsors'] + \
            self.groups[cla_group]['administrators']

        user_groupcount = {}
        group_types = {}
        for uid in cla_uids:
            user_groupcount[uid] = 0

        for group in self.groups:
            group_type = self.groups[group]['type']
            if group.startswith('cla_'):
                continue
            for uid in self.groups[group]['users'] + \
                self.groups[group]['sponsors'] + \
                self.groups[group]['administrators']:
                if group_type not in group_types:
                    group_types[group_type] = set()
                group_types[group_type].add(uid)
                if uid in user_groupcount:
                    user_groupcount[uid] += 1

        good_users = set()
        for uid in user_groupcount:
            # If the user is active, has signed a CLA, and is in at least one
            # other group, add them to good_users.
            if uid in self.users and user_groupcount[uid] > 0:
                good_users.add(uid)

        self._good_users = good_users
        self._group_types = group_types

    def _refresh_good_users(self, force=False):
        '''Return a list of users in who have CLA + 1 group'''
        self._refresh_good_users_group_types(force)
        return self._good_users

    good_users = property(_refresh_good_users)

    def _refresh_group_types(self, force=False):
        '''Return a list of users in group with various types'''
        self._refresh_good_users_group_types(force)
        return self._group_types

    group_types = property(_refresh_group_types)

    def drop_privs(self, pw):
        # initgroups is only in python >= 2.7
        #os.initgroups(pw.pw_name, pw.pw_gid)
        groups = set(os.getgroups())
        groups.add(pw.pw_gid)

        os.setgroups(list(groups))
        os.setegid(pw.pw_gid)
        os.seteuid(pw.pw_uid)

    def restore_privs(self):
        os.seteuid(self._orig_euid)
        os.setegid(self._orig_egid)
        os.setgroups(self._orig_groups)

    def filter_users(self, valid_groups=None, restricted_groups=None):
        '''Return a list of users who get normal and restricted accounts on a machine'''
        if valid_groups is None:
            valid_groups = []
        if restricted_groups is None:
            restricted_groups = []

        all_groups = valid_groups + restricted_groups

        users = {}

        for group in all_groups:
            uids = set()
            restricted = group not in valid_groups

            if group.startswith('@'):
                # Filter by group type
                group_type = group[1:]
                if group_type == 'all':
                    # It's all good as long as a the user is in CLA + one group
                    uids.update(self.good_users)
                else:
                    if group_type not in self.group_types:
                        print >> sys.stderr, 'No such group type: %s' % group_type
                        continue
                    uids.update(self.group_types[group_type])
            else:
                if group not in self.groups:
                    print >> sys.stderr, 'No such group: %s' % group
                    continue
                uids.update(self.groups[group]['users'])
                uids.update(self.groups[group]['sponsors'])
                uids.update(self.groups[group]['administrators'])

            for uid in uids:
                if uid not in self.users:
                    # The user is most likely inactive.
                    continue
                if restricted:
                    # Make sure that the most privileged group wins.
                    if uid not in users:
                        users[uid] = {}
                        users[uid]['shell'] = config.get('users', 'shell').strip('"')
                        users[uid]['ssh_cmd'] = config.get('users', 'ssh_restricted_app').strip('"')
                        users[uid]['ssh_options'] = config.get('users', 'ssh_key_options').strip('"')
                else:
                    users[uid] = {}
                    users[uid]['shell'] = config.get('users', 'ssh_restricted_shell').strip('"')
                    try:
                        users[uid]['ssh_cmd'] = config.get('users', 'ssh_admin_app').strip('"')
                    except ConfigParser.NoOptionError:
                        users[uid]['ssh_cmd'] = ''
                    try:
                        users[uid]['ssh_options'] = config.get('users', 'ssh_admin_options').strip('"')
                    except ConfigParser.NoOptionError:
                        users[uid]['ssh_options'] = ''
        return users

    def passwd_text(self, users):
        '''Create the text password file'''
        i = 0
        home_dir_base = os.path.join(prefix, config.get('users', 'home').strip('"').lstrip('/'))

        # Touch shadow and secure the permissions
        shadow_file = codecs.open(os.path.join(self.temp, 'shadow.txt'), mode='w', encoding='utf-8')
        shadow_file.close()
        os.chmod(os.path.join(self.temp, 'shadow.txt'), 00600)

        passwd_file = codecs.open(os.path.join(self.temp, 'passwd.txt'), mode='w', encoding='utf-8')
        shadow_file = codecs.open(os.path.join(self.temp, 'shadow.txt'), mode='w', encoding='utf-8')

        for uid, user in sorted(users.iteritems()):
            username = self.users[uid]['username']
            human_name = self.users[uid]['human_name']
            password = self.users[uid]['password']
            home_dir = '%s/%s' % (home_dir_base, username)
            shell = user['shell']

            passwd_file.write('=%s %s:x:%s:%s:%s:%s:%s\n' % (uid, username, uid, uid, human_name, home_dir, shell))
            passwd_file.write('0%i %s:x:%s:%s:%s:%s:%s\n' % (i, username, uid, uid, human_name, home_dir, shell))
            passwd_file.write('.%s %s:x:%s:%s:%s:%s:%s\n' % (username, username, uid, uid, human_name, home_dir, shell))

            shadow_file.write('=%s %s:%s::::7:::\n' % (uid, username, password))
            shadow_file.write('0%i %s:%s::::7:::\n' % (i, username, password))
            shadow_file.write('.%s %s:%s::::7:::\n' % (username, username, password))
            i += 1

        passwd_file.close()
        shadow_file.close()

    def groups_text(self, users):
        '''Create the text groups file'''
        i = 0
        group_file = codecs.open(os.path.join(self.temp, 'group.txt'), 'w')

        # First create all of our users/groups combo
        # Only create user groups for users that actually exist on the system
        for uid in sorted(users.iterkeys()):
            username = self.users[uid]['username']
            group_file.write('=%s %s:x:%s:\n' % (uid, username, uid))
            group_file.write('0%i %s:x:%s:\n' % (i, username, uid))
            group_file.write('.%s %s:x:%s:\n' % (username, username, uid))
            i += 1

        for groupname, group in sorted(self.groups.iteritems()):
            gid = group['id']
            members = []
            memberships = ''

            for member_uid in group['administrators'] + \
                group['sponsors'] + \
                group['users']:
                try:
                    members.append(self.users[member_uid]['username'])
                except KeyError:
                    # This means that the user is most likely disabled.
                    pass

            members.sort()
            memberships = ','.join(members)
            group_file.write('=%i %s:x:%i:%s\n' % (gid, groupname, gid, memberships))
            group_file.write('0%i %s:x:%i:%s\n' % (i, groupname, gid, memberships))
            group_file.write('.%s %s:x:%i:%s\n' % (groupname, groupname, gid, memberships))
            i += 1

        group_file.close()

    def make_group_db(self, users):
        '''Compile the groups file'''
        self.groups_text(users)
        subprocess.call(['/usr/bin/makedb', '-o', os.path.join(self.temp, 'group.db'), os.path.join(self.temp, 'group.txt')])

    def make_passwd_db(self, users):
        '''Compile the password and shadow files'''
        self.passwd_text(users)
        subprocess.call(['/usr/bin/makedb', '-o', os.path.join(self.temp, 'passwd.db'), os.path.join(self.temp, 'passwd.txt')])
        subprocess.call(['/usr/bin/makedb', '-o', os.path.join(self.temp, 'shadow.db'), os.path.join(self.temp, 'shadow.txt')])
        os.chmod(os.path.join(self.temp, 'shadow.db'), 0400)
        os.chmod(os.path.join(self.temp, 'shadow.txt'), 0400)

    def make_aliases_text(self):
        '''Create the aliases file'''
        email_file = codecs.open(os.path.join(self.temp, 'aliases'), mode='w', encoding='utf-8')
        recipient_file = codecs.open(os.path.join(self.temp, 'relay_recipient_maps'), mode='w', encoding='utf-8')
        try:
            email_template = codecs.open(config.get('host', 'aliases_template').strip('"'))
        except IOError, e:
            print >> sys.stderr, 'Could not open aliases template %s: %s' % (config.get('host', 'aliases_template').strip('"'), e)
            print >> sys.stderr, 'Aborting.'
            sys.exit(1)

        email_file.write('# Generated by fasClient\n')
        for line in email_template.readlines():
            email_file.write(line)

        email_template.close()

        username_email = []
        for uid in self.good_users:
            if self.users[uid]['alias_enabled']:
                username_email.append( (self.users[uid]['username'], self.users[uid]['email']) )
            else:
                recipient_file.write('%s@fedoraproject.org OK\n' % self.users[uid]['username'])
        #username_email = [(self.users[uid]['username'],
        #    self.users[uid]['email']) for uid in self.good_users]
        username_email.sort()

        for username, email in username_email:
            email_file.write('%s: %s\n' % (username, email))

        for groupname, group in sorted(self.groups.iteritems()):
            administrators = []
            sponsors = []
            members = []

            for uid in group['users']:
                if uid in self.good_users:
                    # The user has an @fedoraproject.org alias
                    username = self.users[uid]['username']
                    members.append(username)
                else:
                    # Add their email if they aren't disabled.
                    if uid in self.users:
                        members.append(self.users[uid]['email'])

            for uid in group['sponsors']:
                if uid in self.good_users:
                    # The user has an @fedoraproject.org alias
                    username = self.users[uid]['username']
                    sponsors.append(username)
                    members.append(username)
                else:
                    # Add their email if they aren't disabled.
                    if uid in self.users:
                        sponsors.append(self.users[uid]['email'])
                        members.append(self.users[uid]['email'])

            for uid in group['administrators']:
                if uid in self.good_users:
                    # The user has an @fedoraproject.org alias
                    username = self.users[uid]['username']
                    administrators.append(username)
                    sponsors.append(username)
                    members.append(username)
                else:
                    # Add their email if they aren't disabled.
                    if uid in self.users:
                        administrators.append(self.users[uid]['email'])
                        sponsors.append(self.users[uid]['email'])
                        members.append(self.users[uid]['email'])

            if administrators:
                administrators.sort()
                email_file.write('%s-administrators: %s\n' % (groupname, ','.join(administrators)))
            if sponsors:
                sponsors.sort()
                email_file.write('%s-sponsors: %s\n' % (groupname, ','.join(sponsors)))
            if members:
                members.sort()
                email_file.write('%s-members: %s\n' % (groupname, ','.join(members)))
        email_file.close()
        recipient_file.close()

    def create_home_dirs(self, users, modes=None):
        ''' Create homedirs and home base dir if they do not exist '''
        if modes is None:
            modes = {}
        home_dir_base = to_bytes(os.path.join(prefix, config.get('users', 'home').strip('"').lstrip('/')))
        if not os.path.exists(home_dir_base):
            os.makedirs(home_dir_base, mode=0755)
            if have_selinux:
                selinux.restorecon(home_dir_base)
        for uid in users:
            username = self.users[uid]['username']
            home_dir = os.path.join(home_dir_base, username)
            if not os.path.exists(home_dir):
                syslog.syslog('Creating homedir for %s' % username)
                copytree('/etc/skel/', home_dir)
                os.path.walk(home_dir, _chown, [int(uid), int(uid)])
            else:
                dir_stat = os.stat(home_dir)
                if dir_stat.st_uid == 0:
                    if username in modes:
                        os.chmod(home_dir, modes[username])
                    else:
                        os.chmod(home_dir, 0755)
                    os.chown(home_dir, int(uid), int(uid))

    def remove_stale_homedirs(self, users):
        ''' Remove homedirs of users that no longer have access '''
        home_dir_base = os.path.join(prefix, config.get('users', 'home').strip('"').lstrip('/'))
        valid_users = [self.users[uid]['username'] for uid in users]
        current_users = os.listdir(home_dir_base)
        modes = {}
        for user in current_users:
            if user not in valid_users:
                home_dir = os.path.join(home_dir_base, user)
                dir_stat = os.stat(home_dir)
                if dir_stat.st_uid != 0:
                    modes[user] = dir_stat.st_mode
                    syslog.syslog('Locking permissions on %s' % home_dir)
                    os.chmod(home_dir, 0700)
                    os.chown(home_dir, 0, 0)
        return modes

    def create_ssh_key_user(self, uid):
        home_dir_base = os.path.join(prefix, config.get('users', 'home').strip('"').lstrip('/'))
        username = self.users[uid]['username']
        ssh_dir = to_bytes(os.path.join(home_dir_base, username, '.ssh'))
        key_file = os.path.join(ssh_dir, 'authorized_keys')
        if self.users[uid]['ssh_key']:
            if users[uid]['ssh_cmd'] or users[uid]['ssh_options']:
               key = []
               for key_tmp in self.users[uid]['ssh_key'].split("\n"):
                   if key_tmp:
                       key.append('command="%s",%s %s' % (users[uid]['ssh_cmd'], users[uid]['ssh_options'], key_tmp))
               key = "\n".join(key)
            else:
               key = self.users[uid]['ssh_key']
            if not os.path.exists(ssh_dir):
                os.makedirs(ssh_dir, mode=0700)
            f = codecs.open(key_file, mode='a+', encoding='utf-8')
            if f.read() != key + '\n':
               f.truncate(0)
               f.write(key + '\n')
            f.close()
            os.chmod(key_file, 0600)
            #os.path.walk(ssh_dir, _chown, [int(uid), int(uid)])
            if have_selinux:
                selinux.restorecon(ssh_dir, recursive=True)
        else:
            # If the user does not have an SSH key listed, ensure
            # that their authorized_key file does not exist.
            try:
                os.remove(key_file)
            except OSError:
                pass

    def create_ssh_keys(self, users):
        ''' Create SSH keys '''
        home_dir_base = os.path.join(prefix, config.get('users', 'home').strip('"').lstrip('/'))
        for uid in users:
            pw = pwd.getpwuid(int(uid))
            lock_dir = False

            self.drop_privs(pw)

            try:
                self.create_ssh_key_user(uid)
            except IOError, e:
                print >> sys.stderr, 'Error when creating SSH key for %s!' % uid
                print >> sys.stderr, 'Locking their home directory, please investigate.'
                lock_dir = True

            self.restore_privs()

            if lock_dir:
                os.chmod(pw.pw_dir, 0700)
                os.chown(pw.pw_dir, 0, 0)

    def install_passwd_db(self):
        '''Install the password database'''
        try:
            move(os.path.join(self.temp, 'passwd.db'), os.path.join(prefix, 'var/db/passwd.db'))
        except IOError, e:
            print >> sys.stderr, 'ERROR: Could not install passwd db: %s' % e

    def install_shadow_db(self):
        '''Install the shadow database'''
        try:
            move(os.path.join(self.temp, 'shadow.db'), os.path.join(prefix, 'var/db/shadow.db'))
        except IOError, e:
            print >> sys.stderr, 'ERROR: Could not install shadow db: %s' % e

    def install_group_db(self):
        '''Install the group database'''
        try:
            move(os.path.join(self.temp, 'group.db'), os.path.join(prefix, 'var/db/group.db'))
        except IOError, e:
            print >> sys.stderr, 'ERROR: Could not install group db: %s' % e

    def install_aliases(self):
        '''Install the aliases file'''
        move(os.path.join(self.temp, 'aliases'), os.path.join(prefix, 'etc/aliases'))
        move(os.path.join(self.temp, 'relay_recipient_maps'), os.path.join(prefix, 'etc/postfix/relay_recipient_maps'))
        subprocess.call(['/usr/bin/newaliases'])
        if have_selinux:
            selinux.restorecon('/etc/postfix/relay_recipient_maps')
        subprocess.call(['/usr/sbin/postmap', '/etc/postfix/relay_recipient_maps'])

    def user_info(self, username):
        '''Print information on a user'''
        person = self.person_by_username(username)
        if not person:
            print 'No such person: %s' % username
            return
        print 'User: %s' % person['username']
        print ' Name: %s' % person['human_name']
        print ' Created: %s' % person['creation'].split(' ')[0]
        print ' Timezone: %s' % person['timezone']
        print ' IRC Nick: %s' % person['ircnick']
        print ' Locale: %s' % person['locale']
        print ' Status: %s' % person['status']
        print ' Approved Groups: '
        if person['approved_memberships']:
            for group in person['approved_memberships']:
                print '   %s' % group['name']
        else:
            print '    None'
        print ' Unapproved Groups: '
        if person['unapproved_memberships']:
            for group in person['unapproved_memberships']:
                print '   %s' % group['name']
        else:
            print '    None'

    def cleanup(self):
        '''Perform any necessary cleanup tasks'''
        if self.temp:
            rmtree(self.temp)

def enable():
    '''Enable FAS authentication'''
    temp = tempfile.mkdtemp('-tmp', 'fas-', config.get('global', 'temp').strip('"'))

    old = open('/etc/sysconfig/authconfig', 'r')
    new = open(temp + '/authconfig', 'w')
    for line in old:
        if line.startswith('USEDB'):
            new.write('USEDB=yes\n')
        else:
            new.write(line)
    new.close()
    old.close()
    try:
        move(os.path.join(temp, 'authconfig'), '/etc/sysconfig/authconfig')
    except IOError, e:
        print >> sys.stderr, 'ERROR: Could not write /etc/sysconfig/authconfig: %s' % e
        sys.exit(5)
    subprocess.call(['/usr/sbin/authconfig', '--updateall'])
    rmtree(temp)

def disable():
    '''Disable FAS authentication'''
    temp = tempfile.mkdtemp('-tmp', 'fas-', config.get('global', 'temp').strip('"'))
    old = open('/etc/sysconfig/authconfig', 'r')
    new = open(os.path.join(temp, 'authconfig'), 'w')
    for line in old:
        if line.startswith('USEDB'):
            new.write('USEDB=no\n')
        else:
            new.write(line)
    old.close()
    new.close()
    try:
        move(os.path.join(temp, 'authconfig'), '/etc/sysconfig/authconfig')
    except IOError, e:
        print >> sys.stderr, 'ERROR: Could not write /etc/sysconfig/authconfig: %s' % e
        sys.exit(5)
    subprocess.call(['/usr/sbin/authconfig', '--updateall'])
    rmtree(temp)

if __name__ == '__main__':

    if not (opts.install or opts.enable or opts.disable or opts.aliases or opts.info_username):
        parser.print_help()
        sys.exit(0)

    if opts.enable:
        enable()
    if opts.disable:
        disable()

    try:
        fas = MakeShellAccounts(FAS_URL,
                username=config.get('global', 'login').strip('"'),
                password=config.get('global', 'password').strip('"'),
                force_refresh=opts.force_refresh,
                debug=opts.debug)
    except AuthError, e:
        print >> sys.stderr, str(e)
        sys.exit(1)
    except URLError, e:
        print >> sys.stderr, 'Could not connect to %s: %s\n' % (FAS_URL, e.reason[1])
        sys.exit(9)

    valid_groups = []
    restricted_groups = []

    valid_grouplist = config.get('host', 'groups').strip('"')
    restricted_grouplist = config.get('host', 'ssh_restricted_groups').strip('"')

    if valid_grouplist:
        valid_groups = valid_grouplist.split(',')
    if restricted_grouplist:
        restricted_groups = restricted_grouplist.split(',')

    if opts.info_username:
        fas.user_info(opts.info_username)

    if opts.install:
        users = fas.filter_users(valid_groups=valid_groups, restricted_groups=restricted_groups)
        fas.make_group_db(users)
        fas.make_passwd_db(users)
        if not opts.no_group:
            fas.install_group_db()
        if not opts.no_passwd:
            fas.install_passwd_db()
        if not opts.no_shadow:
            fas.install_shadow_db()
        if not opts.no_home_dirs:
            try:
                modefile = open(config.get('global', 'modefile'), 'r')
                modes = pickle.load(modefile)
            except IOError:
                modes = {}
            else:
                modefile.close()
            fas.create_home_dirs(users, modes=modes)
            new_modes = fas.remove_stale_homedirs(users)
            modes.update(new_modes)
            try:
                modefile = open(config.get('global', 'modefile'), 'w')
                pickle.dump(modes, modefile)
            except IOError:
                pass
            else:
                modefile.close()
        if not opts.no_ssh_keys:
            fas.create_ssh_keys(users)

    if opts.aliases:
        fas.make_aliases_text()
        fas.install_aliases()

    fas.cleanup()

