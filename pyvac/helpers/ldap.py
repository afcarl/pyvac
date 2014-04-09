from __future__ import absolute_import

import os
import logging
import hashlib
from base64 import b64encode as encode
import random
import string
import yaml

try:
    from yaml import CSafeLoader as YAMLLoader
except ImportError:
    from yaml import SafeLoader as YAMLLoader

import ldap
from ldap import dn, modlist

log = logging.getLogger(__file__)


class UnknownLdapUser(Exception):
    """ When user was not found in a ldap search """


class LdapWrapper(object):
    """ Simple ldap class wrapper"""
    _conn = None
    _base = None
    _filter = None

    def __init__(self, filename):
        with open(filename) as fdesc:
            conf = yaml.load(fdesc, YAMLLoader)

        url = conf['ldap_url']
        self._base = conf['basedn']
        self._filter = conf['search_filter']

        self.mail_attr = conf['mail_attr']
        self.firstname_attr = conf['firstname_attr']
        self.lastname_attr = conf['lastname_attr']
        self.login_attr = conf['login_attr']
        self.manager_attr = conf['manager_attr']
        self.country_attr = conf['country_attr']

        self.admin_dn = conf['admin_dn']

        self.system_DN = conf['system_dn']
        self.system_password = conf['system_pass']

        self._conn = ldap.initialize(url)
        self._bind(self.system_DN, self.system_password)

        log.info('Ldap wrapper initialized')

    def _bind(self, dn, password):
        log.debug('binding with dn: %s' % dn)
        self._conn.simple_bind_s(dn, password)

    def _search(self, what, retrieve):
        # rebind with system dn
        self._bind(self.system_DN, self.system_password)
        log.debug('searching: %s for: %s' % (what, retrieve))
        return self._conn.search_s(self._base, ldap.SCOPE_SUBTREE, what,
                                   retrieve)

    def _search_admin(self, what, retrieve):
        # rebind with system dn
        self._bind(self.system_DN, self.system_password)
        return self._conn.search_s(self.admin_dn, ldap.SCOPE_SUBTREE, what,
                                   retrieve)

    def _search_by_item(self, item):
        required_fields = ['cn', 'mail', 'uid', 'givenName', 'sn', 'manager',
                           'userPassword']
        res = self._search(self._filter % item, required_fields)
        if not res:
            raise UnknownLdapUser

        USER_DN, entry = res[0]
        return self.parse_ldap_entry(USER_DN, entry)

    def search_user_by_login(self, login):
        item = 'cn=*%s*' % login
        return self._search_by_item(item)

    def search_user_by_dn(self, user_dn):
        return self._search_by_item(user_dn)

    def _extract_country(self, user_dn):
        """ Get country from a user dn """
        for rdn in dn.str2dn(user_dn):
            rdn = rdn[0]
            if rdn[0] == self.country_attr:
                return rdn[1]

    def _extract_cn(self, user_dn):
        """ Get cn from a user dn """
        for rdn in dn.str2dn(user_dn):
            rdn = rdn[0]
            if rdn[0] == self.login_attr:
                return rdn[1]

    def parse_ldap_entry(self, user_dn, entry):
        """
        Format ldap entry and parse user_dn to output dict with expected values
        """
        if not user_dn or not entry:
            return

        data = {
            'email': entry[self.mail_attr].pop(),
            'firstname': entry[self.firstname_attr].pop(),
            'lastname': entry[self.lastname_attr].pop(),
            'login': entry[self.login_attr].pop(),
            'manager_dn': entry[self.manager_attr].pop(),
        }
        # save user dn
        data['dn'] = user_dn
        data['country'] = self._extract_country(user_dn)
        data['manager_cn'] = self._extract_country(data['manager_dn'])
        data['userPassword'] = entry['userPassword'].pop()

        return data

    def authenticate(self, login, password):
        """ Authenticate user using given credentials """

        user_data = self.search_user_by_login(login)
        # try to bind with password
        self._bind(user_data['dn'], password)
        return user_data

    def add_user(self, user, unit=None, uid=None):
        """ Add new user into ldap directory """
        # The dn of our new entry/object
        dn = 'cn=%s,c=%s,%s' % (user.login, user.country, self._base)
        # A dict to help build the "body" of the object
        attrs = {}
        attrs['objectClass'] = ['inetOrgPerson', 'top']
        attrs['employeeType'] = ['Employee']
        attrs['cn'] = [user.login]
        attrs['givenName'] = [user.firstname]
        attrs['sn'] = [user.lastname]
        if uid:
            attrs['uid'] = [uid]
        attrs['mail'] = [user.email]
        if not unit:
            unit = 'development'
        attrs['ou'] = [unit]

        # generate a random password for the user, he will change it later
        password = randomstring()
        log.info('temporary password generated for user %s: %s' %
                 (dn, password))
        attrs['userPassword'] = [hashPassword(password)]
        attrs['manager'] = [user.manager_dn]

        # Convert our dict for the add-function using modlist-module
        ldif = modlist.addModlist(attrs)

        # rebind with system dn
        self._bind(self.system_DN, self.system_password)
        # Do the actual synchronous add-operation to the ldapserver
        self._conn.add_s(dn, ldif)

        # return password to display it to the administrator
        return password

    def update_user(self, user, password=None):
        """ Update user params in ldap directory """

        # convert fields to ldap fields
        # retrieve them from model as it was updated before
        fields = {
            'mail': [str(user.email)],
            'givenName': [str(user.firstname)],
            'sn': [str(user.lastname)],
            'manager': [str(user.manager_dn)],
        }
        if password:
            fields['userPassword'] = password

        # dn of object we want to update
        dn = 'cn=%s,c=%s,%s' % (user.login, user.country, self._base)
        # retrieve current user information
        required = ['objectClass', 'employeeType', 'cn', 'givenName', 'sn',
                    'manager', 'mail', 'ou', 'uid', 'userPassword']
        item = 'cn=*%s*' % user.login
        res = self._search(self._filter % item, required)
        USER_DN, entry = res[0]

        old = {}
        new = {}
        # for each field to be updated
        for field in fields:
            # get old value
            old[field] = entry.get(field, '')
            # set new value
            new[field] = fields[field]

        # Convert place-holders for modify-operation using modlist-module
        ldif = modlist.modifyModlist(old, new)
        if ldif:
            # Do the actual modification if needed
            print self._conn.modify_s(dn, ldif)

    def delete_user(self, user_dn):
        """ Delete user from ldap """
        log.info('deleting user %s from ldap' % user_dn)

        # rebind with system dn
        self._bind(self.system_DN, self.system_password)
        # Do the actual synchronous add-operation to the ldapserver
        self._conn.delete_s(user_dn)

    def get_hr_by_country(self, country):
        """ Get hr mail of country for a user_dn"""
        what = '(member=*)'
        results = self._search_admin(what, None)
        for USER_DN, entry in results:
            # item = self._extract_country(entry['member'])
            # XXX: for now return on the first HR found
            # if item == country:
            # found valid hr user for this country
            login = self._extract_cn(entry['member'])
            user_data = self.search_user_by_login(login)
            return user_data


class LdapCache(object):
    """ Ldap cache class singleton """
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            raise RuntimeError('Ldap is not initialized')

        return cls._instance

    @classmethod
    def configure(cls, settings):
        cls._instance = cls.from_config(settings)

    @classmethod
    def from_config(cls, config, **kwargs):
        """
        Return a Ldap client object configured from the given configuration.
        """
        return LdapWrapper(config)


def hashPassword(password):
    """ Generate a password in SSHA format suitable for ldap """
    salt = os.urandom(4)
    return '{SSHA}' + encode(hashlib.sha1(str(password) + salt).digest() + salt)


def randomstring(length=8):
    """ Generates a random ascii string """
    chars = string.letters + string.digits

    # Generate string from population
    data = [random.choice(chars) for _ in xrange(length)]

    return ''.join(data)
